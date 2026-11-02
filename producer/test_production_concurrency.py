"""Offline concurrency checks for reservation, Stop, and receipt persistence."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from production import Ledger


def plan(ident="reference"):
    return {
        "id": ident,
        "kind": "reference",
        "text": "Producer: https://example.com/producer",
        "request_key": "reviewed:" + ident,
        "request_fingerprint": "fixture-fingerprint-" + ident,
    }


class LedgerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "production.json"
        self.ledger = Ledger(self.path)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.threads = []
        self.addCleanup(self.finish_threads)
        self.adapter = Mock()
        self.adapter.execute.side_effect = self.blocked_execute

    def finish_threads(self):
        self.release.set()
        for thread in self.threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "A fixture worker did not finish.")

    def blocked_execute(self, selected):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("The test did not release its offline adapter.")
        return {"state": "done", "remote_id": "mock-" + selected["id"]}

    def start(self, function):
        done = threading.Event()
        result = {}

        def run():
            try:
                result["value"] = function()
            except BaseException as error:
                result["error"] = error
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        self.threads.append(worker)
        worker.start()
        return done, result

    def result(self, job):
        done, result = job
        self.assertTrue(done.wait(2), "The operation waited for unrelated adapter I/O.")
        if "error" in result:
            raise result["error"]
        return result["value"]

    def dispatch(self, selected=None, active=lambda: True):
        return self.ledger.execute(selected or plan(), "fixture-channel", 12.5,
                                   self.adapter, active=active)

    def test_snapshot_reads_durable_reservation_while_adapter_is_blocked(self):
        sending = self.start(self.dispatch)
        self.assertTrue(self.entered.wait(2))

        snapshot = self.result(self.start(self.ledger.snapshot))
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["state"], "working")
        self.assertEqual(snapshot[0]["request_key"], "reviewed:reference")
        self.assertEqual(json.loads(self.path.read_text())["fixture-channel:reference"],
                         snapshot[0])
        snapshot[0]["text"] = "Mutating a snapshot cannot change the reserved payload."
        self.assertEqual(self.result(self.start(self.ledger.snapshot))[0]["text"],
                         plan()["text"])

        self.release.set()
        self.assertEqual(self.result(sending)["state"], "done")
        self.assertEqual(Ledger(self.path).snapshot()[0]["state"], "done")

    def test_concurrent_duplicates_return_reservation_and_send_only_once(self):
        sending = self.start(self.dispatch)
        self.assertTrue(self.entered.wait(2))
        duplicates = [self.start(self.dispatch) for _ in range(3)]
        for duplicate in duplicates:
            self.assertEqual(self.result(duplicate)["state"], "working")
        self.adapter.execute.assert_called_once_with(plan())
        self.assertEqual(len(self.result(self.start(self.ledger.snapshot))), 1)

        self.release.set()
        delivered = self.result(sending)
        self.assertEqual(self.dispatch(), delivered)
        self.assertEqual(self.ledger.snapshot(), [delivered])
        self.adapter.execute.assert_called_once()

    def test_another_action_can_finish_without_losing_the_inflight_receipt(self):
        first = self.start(self.dispatch)
        self.assertTrue(self.entered.wait(2))
        fast = Mock()
        fast.execute.return_value = {"state": "done", "remote_id": "mock-second"}
        second = self.start(lambda: self.ledger.execute(
            plan("second"), "fixture-channel", 13, fast))
        self.assertEqual(self.result(second)["state"], "done")
        self.assertEqual([row["state"] for row in self.ledger.snapshot()],
                         ["working", "done"])

        self.release.set()
        self.assertEqual(self.result(first)["state"], "done")
        stored = json.loads(self.path.read_text())
        self.assertEqual(set(stored), {"fixture-channel:reference", "fixture-channel:second"})
        self.assertTrue(all(row["state"] == "done" for row in stored.values()))
        fast.execute.assert_called_once_with(plan("second"))

    def test_stop_after_durable_reservation_prevents_dispatch_and_retry(self):
        stopped = threading.Event()
        save = self.ledger.save

        def save_then_stop():
            save()
            # The reservation is durable; Stop wins before the dispatch recheck.
            stopped.set()

        with patch.object(self.ledger, "save", side_effect=save_then_stop):
            receipt = self.dispatch(active=lambda: not stopped.is_set())
        self.assertEqual(receipt["state"], "needs_attention")
        self.assertIn("nothing sent", receipt["detail"])
        self.assertEqual(self.ledger.snapshot(), [receipt])
        self.adapter.execute.assert_not_called()
        self.assertEqual(self.dispatch(), receipt)
        self.adapter.execute.assert_not_called()

    def test_lifecycle_callback_does_not_invert_show_and_ledger_locks(self):
        show_lock = threading.Lock()
        rechecking = threading.Event()
        calls = []

        def active():
            calls.append(None)
            if len(calls) == 1:
                return True
            rechecking.set()
            with show_lock:
                return False

        with show_lock:
            sending = self.start(lambda: self.dispatch(active=active))
            self.assertTrue(rechecking.wait(2))
            # Runtime snapshots own the show lock before reading the ledger.
            # The dispatch lifecycle recheck must not own the ledger lock.
            snapshot = self.result(self.start(self.ledger.snapshot))
            self.assertEqual(snapshot[0]["state"], "working")
        self.assertEqual(self.result(sending)["state"], "needs_attention")
        self.adapter.execute.assert_not_called()

    def test_stop_before_reservation_leaves_no_receipt_or_dispatch(self):
        self.assertIsNone(self.dispatch(active=lambda: False))
        self.assertEqual(self.ledger.snapshot(), [])
        self.assertFalse(self.path.exists())
        self.adapter.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()

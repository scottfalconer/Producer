"""Optional read-only private advice, with mocked inference and controlled workers."""

import math
import threading
import unittest
from unittest.mock import Mock, patch

from editor_consult import EditorConsult


THREAD = threading.Thread


class EditorConsultTests(unittest.TestCase):
    def setUp(self):
        self.running = True
        self.context = Mock(return_value={"notes": "Private author notes", "transcript": {"through": 12}})
        self.api = object()
        self.editor = EditorConsult("unused-fixture-key", self.context, lambda: self.running, api=self.api)
        self.workers = []
        self.release = threading.Event()

    def tearDown(self):
        self.editor.close()
        self.release.set()
        for worker in self.workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def launch(self, args=None, before=12):
        def worker(**options):
            thread = THREAD(**options)
            self.workers.append(thread)
            return thread
        with patch("editor_consult.threading.Thread", side_effect=worker):
            return self.editor.request(args or {"question": "How can we clarify this example?"}, before)

    def finish(self):
        self.workers[-1].join(timeout=2)
        self.assertFalse(self.workers[-1].is_alive())

    def test_one_readonly_request_returns_advice_with_source_cutoff(self):
        with patch("editor_consult.structured", return_value={"advice": "Name the expected outcome before comparing results."}) as structured:
            ack = self.launch()
            self.assertTrue(ack["queued"])
            self.finish()
        self.context.assert_called_once_with(12.0)
        self.assertIs(structured.call_args.args[0], self.api)
        packet = structured.call_args.args[2]
        self.assertEqual(packet["source_before"], 12.0)
        self.assertEqual(packet["show"]["notes"], "Private author notes")
        self.assertEqual(structured.call_count, 1)
        state = self.editor.snapshot()
        self.assertFalse(state["busy"])
        self.assertEqual(state["results"][0]["source_before"], 12)
        self.assertEqual(state["results"][0]["status"], "ready")
        self.assertIn("completed_at", state["results"][0])

    def test_busy_worker_does_not_queue_more_inference_or_block_live_director(self):
        entered = threading.Event()
        def advice(*args):
            entered.set()
            self.assertTrue(self.release.wait(2))
            return {"advice": "A useful comparison."}
        with patch("editor_consult.structured", side_effect=advice) as structured:
            first = self.launch()
            self.assertTrue(entered.wait(1))
            second = self.launch({"question": "Another question"})
            self.assertTrue(second["busy"])
            self.assertFalse(second["queued"])
            self.assertEqual(first["id"], second["id"])
            self.release.set()
            self.finish()
        self.assertEqual(structured.call_count, 1)

    def test_stop_during_request_suppresses_late_advice_and_new_calls(self):
        entered = threading.Event()
        def advice(*args):
            entered.set()
            self.assertTrue(self.release.wait(2))
            return {"advice": "LATE_SENTINEL"}
        with patch("editor_consult.structured", side_effect=advice):
            self.launch()
            self.assertTrue(entered.wait(1))
            self.running = False
            self.editor.close()
            self.release.set()
            self.finish()
        self.assertEqual(self.editor.snapshot()["results"], [])
        self.assertFalse(self.editor.request({"question": "Must not run"}, 12)["queued"])

    def test_context_callback_does_not_hold_worker_lock_or_block_stop(self):
        owner_lock = threading.Lock()
        entered = threading.Event()
        stopped = threading.Event()
        def context(before):
            entered.set()
            with owner_lock:
                return {"source_before": before}
        self.editor.context = context
        with patch("editor_consult.structured", side_effect=AssertionError("Stopped before inference")) as structured:
            with owner_lock:
                self.launch()
                self.assertTrue(entered.wait(1))
                closer = THREAD(target=lambda: (self.editor.close(), stopped.set()))
                closer.start()
                self.assertTrue(stopped.wait(1), "Stop waited on the owner's context callback")
            closer.join(timeout=1)
            self.finish()
        structured.assert_not_called()

    def test_provider_failure_is_retained_without_retry_and_a_later_request_can_work(self):
        with patch("editor_consult.structured", side_effect=RuntimeError("PRIVATE_ERROR_BODY_SENTINEL")) as structured:
            self.launch()
            self.finish()
        self.assertEqual(structured.call_count, 1)
        first = self.editor.snapshot()["results"][0]
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["error"], "RuntimeError")
        self.assertNotIn("PRIVATE_ERROR_BODY_SENTINEL", str(self.editor.snapshot()))
        with patch("editor_consult.structured", return_value={"advice": "Review the result on screen."}):
            self.launch()
            self.finish()
        self.assertEqual(self.editor.snapshot()["results"][-1]["status"], "ready")

    def test_history_is_bounded_and_snapshots_do_not_mutate_retained_advice(self):
        with patch("editor_consult.structured", return_value={"advice": "A useful focused suggestion."}):
            for index in range(5):
                self.launch({"question": f"Review example {index}"})
                self.finish()
        state = self.editor.snapshot()
        self.assertEqual(len(state["results"]), 3)
        self.assertEqual([row["id"] for row in state["results"]], ["consult-3", "consult-4", "consult-5"])
        state["results"][0]["advice"] = "Changed copy"
        self.assertNotEqual(self.editor.snapshot()["results"][0]["advice"], "Changed copy")

    def test_invalid_requests_and_oversized_result_fail_without_live_side_effects(self):
        for args, before in (({}, 12), ({"question": ""}, 12), ({"question": "x" * 1201}, 12),
                             ({"question": "Review", "url": "https://example.com"}, 12),
                             ({"question": "Review"}, True), ({"question": "Review"}, math.nan)):
            with self.subTest(args=args, before=before), self.assertRaises(ValueError):
                self.editor.request(args, before)
        with patch("editor_consult.structured", return_value={"advice": "x" * 4001}):
            self.launch()
            self.finish()
        self.assertEqual(self.editor.snapshot()["results"][-1]["status"], "failed")

    def test_api_client_is_constructed_lazily_only_for_an_accepted_worker(self):
        editor = EditorConsult("unused-lazy-key", self.context, lambda: False)
        with patch("editor_consult.OpenAIProducer", side_effect=AssertionError("No active show")) as client:
            self.assertFalse(editor.request({"question": "Review"}, 12)["queued"])
        client.assert_not_called()
        editor.close()

    def test_broken_context_or_active_callback_cannot_leave_worker_busy(self):
        self.editor.context = Mock(side_effect=RuntimeError("Fixture unavailable"))
        with patch("editor_consult.structured") as structured:
            self.launch()
            self.finish()
        self.assertFalse(self.editor.snapshot()["busy"])
        self.assertEqual(self.editor.snapshot()["results"][-1]["status"], "failed")
        structured.assert_not_called()
        self.editor.active = Mock(side_effect=RuntimeError("Owner state gone"))
        self.assertFalse(self.editor.request({"question": "Review"}, 12)["queued"])


if __name__ == "__main__":
    unittest.main()

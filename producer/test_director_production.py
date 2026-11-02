"""Production dispatch and attribution boundaries; no network or model calls."""

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from director_production import DirectorProduction, public_outputs
from production import Ledger, Production
from rehearsal import MockTwitch


def prepared(kind, ident, text, **changes):
    return {"id": ident, "kind": kind, "text": text, "when": "When useful for the show",
            "choices": [], "duration": 0, "category_id": "", "intention_id": "",
            "poll_mode": "native", **changes}


class DirectorProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.json"
        self.now = 10.0
        self.running = True
        self.public_enabled = True
        self.public = {
            "graphic": {"id": "generated-id", "kind": "reference",
                        "line": "Producer source: https://example.com/producer",
                        "attribution": "Project reference"},
            "context": {"subject": "Applied AI", "purpose": "Building a live producer"},
        }
        self.plans = [
            prepared("reference", "repo", "Producer: https://example.com/producer"),
            prepared("marker", "section", "Demonstration begins"),
            prepared("title", "title", "Applied AI Office Hours"),
            prepared("category", "category", "Science & Technology", category_id="509670"),
            prepared("poll", "vote", "Which example next?", choices=["Simple", "Complex"], duration=30),
        ]
        self.adapter = MockTwitch(clock=lambda: self.now)
        self.production = Production(
            {"production": {"preferences": "Keep it useful", "actions": self.plans}},
            Ledger(self.path), self.adapter, "synthetic-office-hours",
        )
        self.bridge = self.build()

    def build(self, **changes):
        return DirectorProduction(self.production, **{
            "offline": True, "active": lambda: self.running,
            "public_enabled": lambda: self.public_enabled,
            "public_state": lambda: copy.deepcopy(self.public),
            "clock": lambda: self.now, **changes,
        })

    def state(self):
        return {"public": copy.deepcopy(self.public), "revision": 0,
                "confidence": {"line": "PRIVATE_CUE_MUST_NEVER_BE_POSTED"},
                "working_notes": "PRIVATE_WORKING_NOTES"}

    def share(self, slot="card"):
        output = next(row for row in self.bridge.snapshot()["shareable_outputs"] if row["slot"] == slot)
        return self.bridge.execute({"action": "share_on_air", "output_key": output["output_key"]}, self.state())

    def test_share_uses_exact_public_copy_and_separate_simulated_producer_identity(self):
        result = self.share()
        self.assertTrue(result["ok"])
        self.assertTrue(result["simulated"])
        receipt = result["receipt"]
        self.assertEqual(receipt["text"], self.public["graphic"]["line"] + " — Project reference")
        self.assertEqual(receipt["message"], receipt["text"])
        self.assertEqual(receipt["display_name"], "Producer")
        self.assertEqual(receipt["sender_id"], "synthetic-producer")
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertNotIn("PRIVATE_", json.dumps(self.bridge.snapshot()))

    def test_context_can_be_shared_but_private_text_and_extra_arguments_cannot(self):
        self.assertEqual(self.share("context")["receipt"]["text"], "Applied AI — Building a live producer")
        key = self.bridge.snapshot()["shareable_outputs"][0]["output_key"]
        for args in ({"action": "share_on_air", "output_key": key, "text": "PRIVATE_CUE"},
                     {"action": "share_on_air", "output_key": "confidence"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.bridge.execute(args, self.state())
        self.assertEqual(len(self.adapter.receipts), 1)

    def test_public_identity_is_stable_across_renderer_ids_and_deduplicates(self):
        first = self.share()
        self.public["graphic"]["id"] = "different-renderer-uuid"
        second = self.share()
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual(len(self.adapter.receipts), 1)
        # A recreated director after Pause/Resume retains persisted idempotency.
        self.production.ledger = Ledger(self.path)
        self.bridge = self.build()
        self.assertTrue(self.share()["duplicate"])
        self.assertEqual(len(self.adapter.receipts), 1)

    def test_expired_replaced_or_unexamined_public_output_is_rejected(self):
        state = self.state()
        output = self.bridge.snapshot()["shareable_outputs"][0]
        self.public["graphic"] = None
        with self.assertRaisesRegex(ValueError, "no longer visible"):
            self.bridge.execute({"action": "share_on_air", "output_key": output["output_key"]}, state)
        self.public["graphic"] = {"line": "A newer item", "kind": "remark", "attribution": "Producer"}
        new = self.bridge.snapshot()["shareable_outputs"][0]
        with self.assertRaisesRegex(ValueError, "no longer visible"):
            self.bridge.execute({"action": "share_on_air", "output_key": new["output_key"]}, state)
        self.assertEqual(self.adapter.receipts, [])

    def test_queued_share_revalidates_public_output_after_waiting_for_reservation(self):
        blocked, release, validated = (threading.Event() for _ in range(3))
        results, failures = {}, {}
        original_execute, original_share = self.adapter.execute, self.bridge._share_plan
        expected = self.state()
        output_key = self.bridge.snapshot()["shareable_outputs"][0]["output_key"]

        def execute(selected):
            if selected["id"] == "section":
                blocked.set()
                if not release.wait(5):
                    raise AssertionError("The test did not release its offline adapter.")
            return original_execute(selected)

        def share_plan(args, state):
            selected = original_share(args, state)
            validated.set()
            return selected

        def run(name, args):
            try:
                results[name] = self.bridge.execute(args, expected)
            except BaseException as error:
                failures[name] = error

        first = threading.Thread(target=run, args=("first", {
            "action": "execute_plan", "plan_id": "section"}), daemon=True)
        queued = threading.Thread(target=run, args=("queued", {
            "action": "share_on_air", "output_key": output_key}), daemon=True)
        with patch.object(self.adapter, "execute", side_effect=execute), \
                patch.object(self.bridge, "_share_plan", side_effect=share_plan):
            try:
                first.start()
                self.assertTrue(blocked.wait(2))
                queued.start()
                self.assertTrue(validated.wait(2))
                self.public["graphic"] = {"kind": "remark", "line": "A newer public item"}
            finally:
                release.set()
                first.join(2)
                if queued.ident is not None:
                    queued.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(queued.is_alive())
        self.assertNotIn("first", failures)
        self.assertTrue(results["first"]["ok"])
        self.assertIsInstance(failures.get("queued"), ValueError)
        self.assertIn("no longer visible", str(failures["queued"]))
        self.assertEqual([row["kind"] for row in self.production.ledger.snapshot()], ["marker"])
        self.assertEqual([row["kind"] for row in self.adapter.receipts], ["marker"])

    def test_share_replaced_after_durable_reservation_is_not_dispatched(self):
        save = self.production.ledger.save

        def save_then_replace():
            save()
            self.public["graphic"] = None

        with patch.object(self.production.ledger, "save", side_effect=save_then_replace):
            result = self.share()
        self.assertFalse(result["ok"])
        self.assertEqual(result["receipt"]["state"], "needs_attention")
        self.assertIn("public output changed", result["receipt"]["detail"])
        self.assertEqual(self.adapter.receipts, [])
        self.assertEqual(self.production.ledger.snapshot()[0]["state"], "needs_attention")

    def test_share_releases_public_state_lock_before_adapter_dispatch(self):
        show_lock = threading.Lock()

        def public():
            with show_lock:
                return copy.deepcopy(self.public)

        execute = self.adapter.execute

        def dispatch(selected):
            self.assertTrue(show_lock.acquire(blocking=False))
            show_lock.release()
            return execute(selected)

        self.bridge = self.build(public_state=public)
        with patch.object(self.adapter, "execute", side_effect=dispatch):
            self.assertTrue(self.share()["ok"])

    def test_every_existing_reviewed_action_reaches_the_offline_adapter(self):
        for item in self.plans:
            with self.subTest(kind=item["kind"]):
                result = self.bridge.execute({"action": "execute_plan", "plan_id": item["id"]}, self.state())
                self.assertTrue(result["ok"])
                self.assertEqual(result["receipt"]["text"], item["text"])
                self.assertEqual(result["receipt"]["kind"], item["kind"])
        self.assertEqual(len(self.adapter.receipts), 5)
        with self.assertRaisesRegex(ValueError, "reviewed"):
            self.bridge.execute({"action": "execute_plan", "plan_id": "not-a-plan"}, self.state())

    def test_dynamic_offline_actions_are_validated_and_preserve_payloads(self):
        args = [
            {"action": "create_marker", "request_id": "moment", "text": "A useful explanation"},
            {"action": "set_title", "request_id": "title", "text": "Building Producer"},
            {"action": "set_category", "request_id": "category", "text": "Science & Technology", "category_id": "509670"},
            {"action": "create_poll", "request_id": "poll", "text": "What next?", "choices": ["Code", "Demo"], "duration": 15},
        ]
        for arg in args:
            with self.subTest(action=arg["action"]):
                result = self.bridge.execute(arg, self.state())
                self.assertTrue(result["ok"])
                self.assertEqual(result["receipt"]["text"], arg["text"])
                self.assertEqual(result["receipt"]["evidence_at"], self.now)
        self.assertEqual(len(self.adapter.receipts), 4)

    def test_request_id_cannot_change_payload_or_repeat_after_resume(self):
        args = {"action": "create_poll", "request_id": "one-poll", "text": "What next?",
                "choices": ["Code", "Demo"], "duration": 15}
        self.bridge.execute(args, self.state())
        self.assertTrue(self.bridge.execute(args, self.state())["duplicate"])
        self.production.ledger = Ledger(self.path)
        self.bridge = self.build()
        self.assertTrue(self.bridge.execute(args, self.state())["duplicate"])
        for changed in ({**args, "duration": 30}, {**args, "choices": ["One", "Two"]},
                        {"action": "create_marker", "request_id": "one-poll", "text": "Different"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "different production action"):
                self.bridge.execute(changed, self.state())
        self.assertEqual(len(self.adapter.receipts), 1)

    def test_invalid_dynamic_payloads_do_not_reserve_or_send(self):
        bad = [
            {"action": "create_poll", "request_id": "bad", "text": "What next?", "choices": ["One"], "duration": 15},
            {"action": "create_poll", "request_id": "bad", "text": "What next?", "choices": ["One", "Two"], "duration": 2},
            {"action": "set_category", "request_id": "bad", "text": "Unverified name", "category_id": "name-not-id"},
            {"action": "create_marker", "request_id": "bad", "text": ""},
            {"action": "create_marker", "request_id": "../bad", "text": "Marker"},
            {"action": "send_arbitrary_chat", "text": "Something private"},
        ]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.bridge.execute(args, self.state())
        self.assertEqual(self.production.ledger.snapshot(), [])
        self.assertEqual(self.adapter.receipts, [])

    def test_only_this_shows_successful_poll_can_be_read_and_it_completes_on_source_clock(self):
        made = self.bridge.execute({"action": "create_poll", "request_id": "poll", "text": "What next?",
                                   "choices": ["Code", "Demo"], "duration": 15}, self.state())
        ident = made["receipt"]["remote_id"]
        pending = self.bridge.execute({"action": "get_poll_result", "poll_id": ident}, self.state())
        self.assertEqual(pending["poll"]["status"], "active")
        self.assertEqual(pending["poll"]["choices"], ["Code", "Demo"])
        self.now += 16
        done = self.bridge.execute({"action": "get_poll_result", "poll_id": ident}, self.state())
        self.assertTrue(done["simulated"])
        self.assertEqual(done["poll"]["status"], "completed")
        self.assertEqual(len(done["poll"]["counts"]), 2)
        with self.assertRaisesRegex(ValueError, "created by this show"):
            self.bridge.execute({"action": "get_poll_result", "poll_id": "mock-another-show"}, self.state())

    def test_poll_response_is_whitelisted_and_invalid_counts_rejected(self):
        made = self.bridge.execute({"action": "execute_plan", "plan_id": "vote"}, self.state())
        ident = made["receipt"]["remote_id"]
        self.adapter.poll_result = Mock(return_value={"counts": [3, 2], "SECRET": "must not reach model",
                                                     "line": "Untrusted free-form prose"})
        result = self.bridge.execute({"action": "get_poll_result", "poll_id": ident}, self.state())
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("Untrusted", json.dumps(result))
        for counts in ([1], [True, 2], [-1, 2], [1_000_001, 2], "wrong"):
            self.adapter.poll_result.return_value = {"counts": counts}
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                self.bridge.execute({"action": "get_poll_result", "poll_id": ident}, self.state())

    def test_stopped_disabled_live_and_mismatched_offline_transport_fail_closed(self):
        args = {"action": "execute_plan", "plan_id": "repo"}
        self.running = False
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.bridge.execute(args, self.state())
        self.running = True
        self.public_enabled = False
        self.assertFalse(self.bridge.snapshot()["enabled"])
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.bridge.execute(args, self.state())
        self.public_enabled = True
        self.adapter.platform = "twitch"
        with self.assertRaisesRegex(ValueError, "simulated Twitch"):
            self.bridge.execute(args, self.state())
        live = self.build(offline=False)
        self.assertFalse(live.snapshot()["enabled"])
        with self.assertRaisesRegex(ValueError, "not enabled"):
            live.execute(args, self.state())
        self.assertEqual(self.adapter.receipts, [])

    def test_even_explicit_live_permission_cannot_run_unreviewed_dynamic_payloads_or_read_poll_data(self):
        self.adapter.platform = "twitch"
        live = self.build(offline=False, live_enabled=True)
        with self.assertRaisesRegex(ValueError, "reviewed plan_id"):
            live.execute({"action": "set_title", "request_id": "new", "text": "New title"}, self.state())
        with self.assertRaisesRegex(ValueError, "offline transport"):
            live.execute({"action": "get_poll_result", "poll_id": "anything"}, self.state())
        self.assertEqual(self.adapter.receipts, [])

    def test_uncertain_action_is_never_retried_and_snapshot_does_not_read_platform(self):
        self.adapter.execute = Mock(return_value={"state": "uncertain", "detail": "Unknown result"})
        args = {"action": "execute_plan", "plan_id": "section"}
        first = self.bridge.execute(args, self.state())
        second = self.bridge.execute(args, self.state())
        self.assertFalse(first["ok"])
        self.assertTrue(second["duplicate"])
        self.adapter.execute.assert_called_once()
        self.adapter.poll_result = Mock(side_effect=AssertionError("No platform reads in state"))
        before = self.path.read_bytes()
        state = self.bridge.snapshot()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(state["receipts"][0]["state"], "uncertain")
        self.adapter.poll_result.assert_not_called()

    def test_redacted_oversized_and_nontext_visuals_are_never_shareable(self):
        self.assertEqual(public_outputs({"graphic": {"redacted": True, "line": "private"}}), [])
        self.assertEqual(public_outputs({"graphic": {"line": "x" * 500, "attribution": "credit"}}), [])
        self.assertEqual(public_outputs({"visual": {"asset_id": "private-file-name"}}), [])


if __name__ == "__main__":
    unittest.main()

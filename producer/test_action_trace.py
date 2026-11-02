"""Action-boundary retention and local-only translation replay."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from action_trace import ActionTrace, load_trace, normalized_scene, replay_actions, visible_state
import test_realtime_director as protocol_fixtures


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.jsonl"
        self.clock = 0
        self.trace = ActionTrace(self.path, clock=lambda: self.clock, source_clock=lambda: 2)

    def tearDown(self):
        self.trace.close()
        self.temp.cleanup()

    def test_retains_exact_visible_output_without_private_model_material(self):
        state = {"revision": 3, "confidence": {"line": "Give a concrete example"},
                 "public": {"graphic": {"line": "Applied AI", "id": "graphic1", "until": 100}},
                 "working_notes": "PRIVATE_THINKING", "deeper_advice": ["PRIVATE_ADVICE"],
                 "csrf": "PRIVATE_TOKEN", "transcript": "PRIVATE_TRANSCRIPT"}
        self.trace.emit("screen_state", {"state": state, "api_key": "PRIVATE_KEY"})
        self.trace.emit("function_call", {"name": "update_screens", "arguments": {
            "confidence": {"action": "show", "line": "Give a concrete example"},
            "working_notes": "PRIVATE_THINKING",
            "research_packet": "PRIVATE_PACKET"}})
        content = self.path.read_text()
        self.assertNotIn("PRIVATE_", content)
        result = load_trace(self.path)[1]["payload"]["state"]
        self.assertEqual(result["public"], state["public"])
        self.assertEqual(result["confidence"], state["confidence"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_hash_chain_rejects_alteration_and_reordering(self):
        self.trace.emit("session_ready", {"model": "authored"})
        original = self.path.read_text()
        self.path.write_text(original.replace('"authored"', '"modified"'))
        with self.assertRaisesRegex(ValueError, "integrity"):
            load_trace(self.path)
        self.path.write_text("\n".join(reversed(original.splitlines())) + "\n")
        with self.assertRaisesRegex(ValueError, "integrity"):
            load_trace(self.path)

    def test_input_frame_retains_only_identity_time_and_digest_without_media_or_fixture_text(self):
        metadata = {"frame_id": "frame-selected-123", "source_time": 2, "sha256": "a" * 64}
        self.trace.emit("input_frame", {**metadata,
            "jpeg": "PRIVATE_JPEG_BYTES", "image_url": "PRIVATE_IMAGE_URL", "audio": "PRIVATE_AUDIO",
            "text": "PRIVATE_FIXTURE_MESSAGE", "author": "PRIVATE_FIXTURE_AUTHOR",
            "api_key": "PRIVATE_KEY", "source_path": "PRIVATE_OWNER_PATH"})
        event = load_trace(self.path)[-1]
        self.assertEqual(event["kind"], "input_frame")
        self.assertEqual(event["payload"], metadata)
        self.assertEqual(event["source_s"], 2)
        self.assertNotIn("PRIVATE_", self.path.read_text())

    def test_concurrent_writers_keep_ordered_clocks_and_hashes(self):
        def add():
            for _ in range(30):
                self.trace.emit("session_ready", {"model": "authored"})
        threads = [threading.Thread(target=add) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        events = load_trace(self.path)
        self.assertEqual(len(events), 121)
        self.assertEqual([e["sequence"] for e in events], list(range(121)))

    def test_existing_journal_is_never_overwritten(self):
        with self.assertRaises(FileExistsError):
            ActionTrace(self.path)

    def test_replay_preserves_original_expected_revision_and_detects_translation_bug(self):
        expected = {"revision": 2, "confidence": None, "public": {"graphic": None}}
        args = {"confidence": {"action": "show", "line": "Try an everyday task"}}
        result = {"ok": True, "state": {"revision": 3,
                  "confidence": {"line": "Try an everyday task"}, "public": {"graphic": None}}}
        self.trace.emit("function_call", {"name": "update_screens", "call_id": "one",
                        "arguments": args, "expected_state": expected})
        self.trace.emit("tool_receipt", {"call_id": "one", "receipt": result})
        examined = []
        def apply(chosen, prior):
            examined.append(prior)
            self.assertEqual(chosen, args)
            return copy.deepcopy(result)
        replayed = replay_actions(load_trace(self.path), apply)
        self.assertTrue(replayed[0]["matches_recording"])
        self.assertEqual(examined, [expected])
        broken = copy.deepcopy(result)
        broken["state"]["confidence"] = None
        replayed = replay_actions(load_trace(self.path), lambda *_: broken)
        self.assertFalse(replayed[0]["matches_recording"])

    def test_replay_local_rejection_compares_state_and_skips_non_screen_tools(self):
        state = {"revision": 0, "confidence": None, "public": {"graphic": None}}
        self.trace.emit("function_call", {"name": "read_transcript", "call_id": "read", "arguments": {}})
        self.trace.emit("function_call", {"name": "update_screens", "call_id": "bad",
                        "arguments": {"visual": {"action": "show", "asset_id": "missing"}},
                        "expected_state": state})
        self.trace.emit("tool_receipt", {"call_id": "bad", "receipt": {
            "ok": False, "error": "tool_arguments_or_local_state_rejected", "state": state}})
        def reject(*_):
            raise ValueError("Unavailable asset")
        results = replay_actions(load_trace(self.path), reject, state=lambda: state)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["matches_recording"])

    def test_normalized_scene_only_ignores_generated_identity_and_wall_clock_fields(self):
        first = {"confidence": {"line": "A"}, "public": {"graphic": {
            "id": "a", "line": "B", "until": 100, "duration": 10, "message_id": "q1"}}}
        second = copy.deepcopy(first)
        second["public"]["graphic"].update(id="b", until=200)
        self.assertEqual(normalized_scene(first), normalized_scene(second))
        second["public"]["graphic"]["duration"] = 20
        self.assertNotEqual(normalized_scene(first), normalized_scene(second))

    def test_production_trace_keeps_public_outcomes_and_omits_private_adapter_context(self):
        row = {"plan_id": "share-1", "kind": "chat", "message": "Reference: https://example.org",
               "state": "done", "simulated": True, "display_name": "Producer",
               "sender_id": "synthetic-producer", "raw_response": "PRIVATE_ADAPTER",
               "credentials": {"client_secret": "PRIVATE_CREDENTIAL"}}
        production = {"policy": "realtime-production-v1", "offline": True, "enabled": True,
                      "capabilities": ["share_on_air", "execute_plan"], "receipts": [row] * 25,
                      "reviewed_plans": [{"id": "reference", "text": "Public link", "when": "PRIVATE_TRIGGER"}],
                      "shareable_outputs": [{"output_key": "public-1", "slot": "card", "text": "Public link"}],
                      "adapter": "PRIVATE_ADAPTER", "source_context": "PRIVATE_SOURCE"}
        state = {"revision": 1, "production": production}
        self.trace.emit("screen_state", {"state": state})
        self.trace.emit("tool_receipt", {"name": "production_action", "call_id": "share",
                        "receipt": {"ok": True, "simulated": True, "duplicate": False,
                                    "receipt": row, "state": state, "raw": "PRIVATE_RAW"}})
        self.trace.emit("production_chat", {"event": {"type": "channel.chat.message", "id": "p1",
                        "at": 2, "user_id": "synthetic-producer", "author": "Producer", "text": row["message"],
                        "source_path": "PRIVATE_PATH"}})
        self.assertNotIn("PRIVATE_", self.path.read_text())
        saved = load_trace(self.path)
        self.assertEqual(len(saved[1]["payload"]["state"]["production"]["receipts"]), 20)
        receipt = saved[2]["payload"]["receipt"]
        self.assertEqual(receipt["receipt"]["message"], row["message"])
        self.assertFalse(receipt["duplicate"])
        self.assertEqual(receipt["state"]["production"]["capabilities"], ["share_on_air", "execute_plan"])
        self.assertNotIn("production", visible_state({"revision": 0}))

    def test_production_call_is_never_silently_counted_as_validated_without_a_router(self):
        self.trace.emit("function_call", {"name": "production_action", "call_id": "one",
                        "arguments": {"action": "create_marker", "request_id": "marker", "text": "A new topic"}})
        expected = {"ok": True, "simulated": True, "duplicate": False,
                    "receipt": {"state": "done", "kind": "marker", "plan_id": "m1"}}
        self.trace.emit("tool_receipt", {"call_id": "one", "receipt": expected})
        results = replay_actions(load_trace(self.path), lambda *_: self.fail("Not a screen call"))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["matches_recording"])
        self.assertEqual(results[0]["actual"]["error"], "production_replay_unavailable")
        results = replay_actions(load_trace(self.path), lambda *_: None, production_apply=lambda *_: expected)
        self.assertTrue(results[0]["matches_recording"])
        wrong_identity = copy.deepcopy(expected)
        wrong_identity["receipt"]["sender_id"] = "host-account"
        results = replay_actions(load_trace(self.path), lambda *_: None, production_apply=lambda *_: wrong_identity)
        self.assertFalse(results[0]["matches_recording"])


class CoreTraceTests(unittest.TestCase):
    def test_core_records_complete_events_deduplicated_action_and_receipt_without_thoughts(self):
        fixture = protocol_fixtures.DirectorTests()
        fixture.setUp()
        try:
            recorded = []
            director = fixture.make(event_sink=lambda k, p: recorded.append((k, p)))
            fixture.begin(director)
            args = {"confidence": {"action": "show", "line": "Explain the result"},
                    "working_notes": "PRIVATE_MODEL_THINKING"}
            fixture.call(director, args=args)
            fixture.call(director, args=args)
            fixture.finish(director)
            kinds = [k for k, _ in recorded]
            self.assertEqual(kinds.count("function_event"), 2)
            self.assertEqual(kinds.count("function_call"), 1)
            self.assertEqual(kinds.count("tool_receipt"), 1)
            self.assertIn("response_requested", kinds)
            self.assertNotIn("PRIVATE_MODEL_THINKING", json.dumps(recorded))
            call = next(p for k, p in recorded if k == "function_call")
            self.assertEqual(call["response_id"], "response-one")
            self.assertEqual(call["response_sequence"], 1)
            self.assertEqual(call["expected_state"]["revision"], 0)
            receipt = next(p for k, p in recorded if k == "tool_receipt")
            self.assertEqual(receipt["receipt"]["state"]["revision"], 1)
        finally:
            fixture.tearDown()

    def test_invalid_json_retains_hash_and_receipt_without_arbitrary_private_prose(self):
        fixture = protocol_fixtures.DirectorTests()
        fixture.setUp()
        try:
            recorded = []
            director = fixture.make(event_sink=lambda k, p: recorded.append((k, p)))
            fixture.begin(director)
            director.accept({"type": "response.function_call_arguments.done", "response_id": "response-one",
                             "call_id": "bad", "name": "update_screens", "arguments": "PRIVATE_INVALID_PROSE"})
            self.assertNotIn("PRIVATE_INVALID_PROSE", json.dumps(recorded))
            call = next(p for k, p in recorded if k == "function_call")
            self.assertEqual(call["arguments_error"], "invalid_json_object")
            self.assertEqual(len(call["arguments_sha256"]), 64)
            receipt = next(p for k, p in recorded if k == "tool_receipt")
            self.assertFalse(receipt["receipt"]["ok"])
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()

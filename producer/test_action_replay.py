"""The saved model actions exercise real local routing and timed rendering."""

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from action_replay import RoutingReplay, replay_bundle
from action_trace import ActionTrace, capture_scene, load_trace
from director_runtime import ShowDirector


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.temp.name)
        self.manifest = {
            "wall_epoch_s": 1700000000,
            "asset_catalog": [],
            "initial_state": {"revision": 0, "source_time": 0, "confidence": None,
                              "public": {"graphic": None, "context": None, "visual": None},
                              "public_output_enabled": True, "audience": []},
        }
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.temp.cleanup()

    def record_confidence_timing(self):
        self.manifest["initial_state"]["confidence_timing_policy"] = "source-clock-v1"
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        with RoutingReplay(self.manifest) as router:
            trace = ActionTrace(self.bundle / "actions.jsonl", clock=lambda: router.wall,
                                source_clock=lambda: router.director.audio_end)
            router.session["action_trace"] = trace
            trace.emit("screen_state", {"state": router.state()})
            instructions = [
                (5, {"action": "show", "line": "One useful cue"}),
                (17, {"action": "keep"}),
                (29, {"action": "show", "line": "One useful cue"}),
                (50, {"action": "show", "line": "Another useful cue"}),
                (60, {"action": "clear"}),
                (70, {"action": "show", "line": "Another useful cue"}),
            ]
            for index, (source, confidence) in enumerate(instructions):
                router.wall = source + 100
                router.director.audio_end = source
                expected = router.state()
                args = {"confidence": confidence}
                trace.emit("function_call", {"name": "update_screens", "call_id": str(index),
                           "arguments": args, "expected_state": expected})
                result = router.apply(args, expected)
                trace.emit("tool_receipt", {"name": "update_screens", "call_id": str(index), "receipt": result})
                trace.emit("screen_state", {"state": router.state()})
            router.director.audio_end = 80
            router.wall = 1000
            trace.emit("screen_state", {"state": router.state()})
            trace.close()

    def test_source_clock_cue_metadata_replays_without_model_calls(self):
        self.record_confidence_timing()
        report, scenes = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["api_calls"], 0)
        self.assertEqual(report["confidence_timing_policy"], "source-clock-v1")
        self.assertEqual(report["matched_actions"], 6)
        self.assertEqual(report["matched_scenes"], 8)
        self.assertEqual(scenes[3]["confidence"]["shown_at_source_s"], 5)
        self.assertEqual(scenes[3]["confidence"]["visible_for_s"], 24)
        self.assertIsNone(scenes[5]["confidence"])
        self.assertEqual(scenes[-1]["confidence"]["shown_at_source_s"], 70)
        self.assertEqual(scenes[-1]["confidence"]["visible_for_s"], 10)

    def test_replay_detects_broken_identical_cue_timestamp_reset(self):
        self.record_confidence_timing()
        original = ShowDirector._set_confidence

        def wrongly_reset(director, cue):
            original(director, cue)
            if cue:
                director.session["confidence_shown_at_source_s"] = director.audio_end

        with patch.object(ShowDirector, "_set_confidence", wrongly_reset):
            report, _ = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "mismatched")
        self.assertLess(report["matched_scenes"], report["scene_checks"])
        self.assertEqual(report["api_calls"], 0)

    def test_legacy_confidence_does_not_acquire_new_timing_metadata(self):
        with RoutingReplay(self.manifest) as router:
            router.director.audio_end = 12
            result = router.apply({"confidence": {"action": "show", "line": "Original cue"}}, router.state())
            self.assertEqual(result["state"]["confidence"], {"line": "Original cue", "kind": "editorial"})
            self.assertNotIn("confidence_timing_policy", result["state"])

    def test_initial_cue_restores_recorded_source_timestamp(self):
        initial = self.manifest["initial_state"]
        initial.update(source_time=30, confidence_timing_policy="source-clock-v1",
                       confidence={"line": "Existing cue", "kind": "editorial",
                                   "shown_at_source_s": 20, "visible_for_s": 10})
        with RoutingReplay(self.manifest) as router:
            router.director.audio_end = 42
            self.assertEqual(router.state()["confidence"]["visible_for_s"], 22)
            self.assertNotIn("visible_for_s", router.session["confidence"])

    def test_unknown_confidence_timing_policy_fails_explicitly(self):
        for policy in ("unknown", None, True):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "confidence timing"):
                RoutingReplay({**self.manifest, "confidence_timing_policy": policy})

    def record(self, *, bad_asset=False, question=False, source_attribution=False):
        with RoutingReplay(self.manifest) as router:
            trace = ActionTrace(self.bundle / "actions.jsonl", clock=lambda: router.wall,
                                source_clock=lambda: router.director.audio_end)
            def snapshot():
                trace.emit("screen_state", {"state": router.state()})
            initial = router.state()
            snapshot()
            trace.emit("response_requested", {"response_sequence": 1, "state": initial})
            router.wall = 0.4
            args = {"confidence": {"action": "show", "line": "Explain the concrete task"},
                    "card": {"action": "show", "kind": "title", "line": "Applied AI", "hold_seconds": 2},
                    "context": {"action": "set", "subject": "Working with AI", "purpose": "An everyday example"}}
            if source_attribution:
                args["card"] = {"action": "show", "kind": "remark", "line": "Colored light changes how paint looks.",
                                "attribution": "Exploratorium", "hold_seconds": 2}
            if bad_asset:
                args["visual"] = {"action": "show", "asset_id": "missing"}
            if question:
                router.inputs({"audience": [{"id": "q1", "author": "Fixture viewer", "text": "Can noncoders use this?"}]})
                args["card"] = {"action": "show", "kind": "question", "line": "Can noncoders use this?",
                                "question_id": "q1", "hold_seconds": 2}
                if self.manifest.get("audience_text_policy") == "verbatim":
                    args["confidence"]["question_id"] = "q1"
            trace.emit("function_call", {"name": "update_screens", "call_id": "c1", "response_sequence": 1,
                        "arguments": args, "expected_state": initial})
            result = router.apply(args, initial)
            trace.emit("tool_receipt", {"name": "update_screens", "call_id": "c1", "receipt": result})
            snapshot()
            router.wall = 2
            snapshot()
            router.wall = 2.5
            snapshot()
            # The next call really examined the old state. A replay must not
            # replace its revision with the current revision to force success.
            trace.emit("response_requested", {"response_sequence": 2, "state": initial})
            stale = {"confidence": {"action": "clear"}}
            trace.emit("function_call", {"name": "update_screens", "call_id": "c2", "response_sequence": 2,
                        "arguments": stale, "expected_state": initial})
            trace.emit("tool_receipt", {"name": "update_screens", "call_id": "c2", "receipt": router.apply(stale, initial)})
            trace.close()

    def test_real_router_replays_actions_expiry_and_stale_revision(self):
        self.record()
        report, scenes = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["api_calls"], 0)
        self.assertEqual(report["matched_actions"], 2)
        self.assertEqual(report["matched_scenes"], 4)
        self.assertEqual(scenes[1]["graphic"]["line"], "Applied AI")
        self.assertIsNone(scenes[-1]["graphic"])
        self.assertEqual(scenes[-1]["confidence"]["line"], "Explain the concrete task")
        self.assertFalse(report["calls"][1]["actual"]["ok"])
        self.assertEqual(report["calls"][1]["expected_revision"], 0)

    def record_interleaved_scene(self, *, commit_trace=False, scene_after_apply=False):
        """A sampler runs while a model tool is between invocation and receipt."""
        with RoutingReplay(self.manifest) as router:
            trace = ActionTrace(self.bundle / "actions.jsonl", clock=lambda: router.wall,
                                source_clock=lambda: router.director.audio_end)
            if commit_trace:
                router.session["action_trace"] = trace
            initial = router.state()
            trace.emit("response_requested", {"response_sequence": 1, "state": initial})
            router.wall = 0.4
            args = {"confidence": {"action": "show", "line": "Give a concrete example"},
                    "card": {"action": "show", "kind": "remark", "line": "One working loop", "hold_seconds": 2}}
            trace.emit("function_call", {"name": "update_screens", "call_id": "interleaved",
                        "response_sequence": 1, "arguments": args, "expected_state": initial})
            if not scene_after_apply:
                router.wall = 0.5
                trace.emit("screen_state", {"state": router.state()})
            router.wall = 0.6
            result = router.apply(args, initial)
            if scene_after_apply:
                router.wall = 0.7
                trace.emit("screen_state", {"state": router.state()})
            router.wall = 0.8
            trace.emit("tool_receipt", {"name": "update_screens", "call_id": "interleaved", "receipt": result})
            trace.emit("screen_state", {"state": router.state()})
            trace.close()

    def test_legacy_invocation_does_not_publish_a_card_before_its_receipt(self):
        self.record_interleaved_scene()
        report, scenes = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed")
        self.assertIsNone(scenes[0]["graphic"])
        self.assertEqual(scenes[0]["revision"], 0)
        self.assertEqual(scenes[1]["graphic"]["line"], "One working loop")

    def test_atomic_commit_orders_scenes_on_both_sides_of_mutation(self):
        for after_apply in (False, True):
            with self.subTest(scene_after_apply=after_apply):
                (self.bundle / "actions.jsonl").unlink(missing_ok=True)
                self.record_interleaved_scene(commit_trace=True, scene_after_apply=after_apply)
                report, scenes = replay_bundle(self.bundle)
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["calls"][0]["timing_basis"], "screen_commit")
                self.assertEqual(report["calls"][0]["applied_wall_s"], 0.6)
                self.assertEqual(scenes[0]["revision"], 1 if after_apply else 0)
                self.assertEqual(bool(scenes[0]["graphic"]), after_apply)

    def test_legacy_post_mutation_scene_remains_a_reported_timing_limit(self):
        self.record_interleaved_scene(scene_after_apply=True)
        report, _ = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "mismatched")
        self.assertEqual(report["matched_actions"], 1)
        self.assertEqual(len(report["scene_mismatches"]), 1)
        self.assertTrue(any("approximation" in limit for limit in report["limits"]))

    def test_atomic_replay_still_detects_a_broken_screen_translation(self):
        self.record_interleaved_scene(commit_trace=True, scene_after_apply=True)
        original = ShowDirector.apply
        def broken(router, args, expected):
            result = original(router, args, expected)
            router.session["confidence"] = None
            result["state"] = router.state()
            return result
        with patch.object(ShowDirector, "apply", broken):
            report, _ = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "mismatched")
        self.assertEqual(report["matched_actions"], 0)
        self.assertEqual(report["matched_scenes"], 0)
        self.assertEqual(report["api_calls"], 0)

    def test_scene_and_commit_journals_hold_the_state_lock(self):
        with RoutingReplay(self.manifest) as router:
            trace = ActionTrace(self.bundle / "actions.jsonl", clock=lambda: router.wall,
                                source_clock=lambda: router.director.audio_end)
            router.session["action_trace"] = trace
            emit = trace.emit
            observed = []
            def checked_emit(kind, payload):
                # Another thread must not acquire the state lock while either
                # the scene snapshot or completed mutation is being journaled.
                acquired = []
                def attempt_lock():
                    available = router.director.lock.acquire(blocking=False)
                    acquired.append(available)
                    if available:
                        router.director.lock.release()
                thread = threading.Thread(target=attempt_lock)
                thread.start()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(acquired, [False])
                observed.append(kind)
                router.wall += 0.001  # The journal time, not a stale pre-write clock.
                return emit(kind, payload)
            with patch.object(trace, "emit", checked_emit):
                initial = router.state()
                row = capture_scene(router.director, trace)
                self.assertEqual(row["wall_s"], 0.001)
                router.apply({"confidence": {"action": "show", "line": "One useful cue"}}, initial)
                row = capture_scene(router.director, trace)
                self.assertEqual(row["revision"], 1)
                self.assertEqual(row["wall_s"], 0.003)
            trace.close()
            self.assertEqual(observed, ["screen_state", "screen_commit", "screen_state"])
            self.assertEqual(load_trace(trace.path)[2]["payload"], {"revision": 1})

    def test_capture_scene_does_not_reenter_the_production_plain_lock(self):
        # The actual server uses Lock, whereas RoutingReplay uses RLock. Run a
        # separate bounded process so a nested-lock regression cannot hang the
        # suite or its router cleanup.
        import subprocess
        import sys
        script = """
import json, sys, threading
from pathlib import Path
from action_replay import RoutingReplay
from action_trace import ActionTrace, capture_scene
bundle = Path(sys.argv[1])
manifest = json.loads((bundle / 'manifest.json').read_text())
with RoutingReplay(manifest) as router:
    router.director.lock = threading.Lock()
    trace = ActionTrace(bundle / 'plain-lock-actions.jsonl', clock=lambda: router.wall)
    try:
        row = capture_scene(router.director, trace)
        assert row['revision'] == 0
        assert row['confidence'] is None
    finally:
        trace.close()
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.bundle)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_translation_regression_changes_report_without_a_new_model_call(self):
        self.record()
        original = ShowDirector.apply
        def broken(router, args, expected):
            result = original(router, args, expected)
            if result.get("ok"):
                router.session["confidence"] = None
                result["state"] = router.state()
            return result
        with patch.object(ShowDirector, "apply", broken):
            report, _ = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "mismatched")
        self.assertEqual(report["api_calls"], 0)
        self.assertFalse(report["calls"][0]["matches_recording"])
        self.assertGreater(len(report["scene_mismatches"]), 0)

    def test_missing_asset_rejection_does_not_partially_show_confidence(self):
        self.record(bad_asset=True)
        report, scenes = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["calls"][0]["actual"]["ok"])
        self.assertTrue(all(scene["confidence"] is None for scene in scenes))

    def test_question_uses_saved_availability_and_actual_selection_expiry(self):
        self.record(question=True)
        report, scenes = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(scenes[1]["graphic"]["kind"], "question")
        self.assertEqual(scenes[1]["graphic"]["message_id"], "q1")
        self.assertIsNone(scenes[-1]["graphic"])

    def test_fixture_moderation_withdraws_question_between_model_calls(self):
        for kind in ("channel.chat.message_delete", "channel.chat.clear_user_messages", "channel.chat.clear"):
            with self.subTest(kind=kind):
                fixture = {"events": [
                    {"id": "q1", "at": 0, "type": "channel.chat.message", "user_id": "fixture-person"},
                    {"id": "moderated", "at": 1.5, "type": kind,
                     "message_id": "q1", "user_id": "fixture-person"},
                ]}
                (self.bundle / "mock-events.json").write_text(json.dumps(fixture))
                (self.bundle / "actions.jsonl").unlink(missing_ok=True)
                with RoutingReplay(self.manifest) as router:
                    trace = ActionTrace(self.bundle / "actions.jsonl", clock=lambda: router.wall,
                                        source_clock=lambda: router.director.audio_end)
                    router.inputs({"audience": [{"id": "q1", "author": "Fixture viewer", "text": "Which light?"}]})
                    router.audience.questions[0]["user_id"] = "fixture-person"
                    initial = router.state()
                    trace.emit("response_requested", {"response_sequence": 1, "state": initial})
                    router.wall = router.director.audio_end = 0.4
                    args = {"confidence": {"action": "show", "line": "Viewer: which light?"},
                            "card": {"action": "show", "kind": "question", "line": "Which light?",
                                     "question_id": "q1", "hold_seconds": 30}}
                    trace.emit("function_call", {"name": "update_screens", "call_id": "c1",
                               "response_sequence": 1, "arguments": args, "expected_state": initial})
                    trace.emit("tool_receipt", {"name": "update_screens", "call_id": "c1",
                               "receipt": router.apply(args, initial)})
                    trace.emit("screen_state", {"state": router.state()})
                    router.wall, router.director.audio_end = 2, 1
                    trace.emit("screen_state", {"state": router.state()})
                    router.wall, router.director.audio_end = 2.5, 1.5
                    router.audience.accept({
                        "metadata": {"message_id": "actual-moderation", "message_timestamp":
                                     datetime.fromtimestamp(router.epoch + router.wall, timezone.utc).isoformat()},
                        "payload": {"subscription": {"type": kind, "version": "1"}, "event": {
                            "broadcaster_user_id": router.audience.channel,
                            "message_id": "q1", "target_user_id": "fixture-person"}},
                    })
                    state = router.state()
                    self.assertIsNone(state["public"]["graphic"])
                    self.assertEqual(state["confidence"]["line"], "Viewer: which light?")
                    trace.emit("screen_state", {"state": state})
                    trace.close()
                report, scenes = replay_bundle(self.bundle)
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["screen_actions"], 1)
                self.assertEqual(report["matched_scenes"], 3)
                self.assertEqual(scenes[1]["graphic"]["message_id"], "q1")
                self.assertIsNone(scenes[-1]["graphic"])
                self.assertEqual(scenes[-1]["confidence"]["line"], "Viewer: which light?")

    def test_missing_expired_inbox_message_does_not_invalidate_selected_question(self):
        with RoutingReplay(self.manifest) as router:
            router.inputs({"audience": [{"id": "q1", "author": "Fixture viewer", "text": "Which light?"}]})
            router.wall = router.director.audio_end = 110
            initial = router.state()
            result = router.apply({"card": {"action": "show", "kind": "question", "line": "Which light?",
                                           "question_id": "q1", "hold_seconds": 120}}, initial)
            self.assertTrue(result["ok"])
            router.wall = router.director.audio_end = 121
            self.assertEqual(router.audience.poll(router.sid)["questions"], [])
            router.inputs({"audience": []})
            self.assertEqual(router.state()["public"]["graphic"]["message_id"], "q1")
            router.wall = 231
            self.assertIsNone(router.state()["public"]["graphic"])

    def test_network_guard_fails_closed_before_any_request(self):
        import requests
        with RoutingReplay(copy.deepcopy(self.manifest)) as router:
            with self.assertRaisesRegex(AssertionError, "forbidden"):
                requests.get("https://example.invalid/never-contacted")
            self.assertEqual(router.network_attempts, 1)

    def test_explicit_topic_policy_preserves_new_and_old_run_semantics(self):
        topic = {"subject": "AI for everyday work", "purpose": "One practical demonstration"}
        for policy, survives in ((None, False), ("model-managed", True)):
            manifest = {**self.manifest}
            if policy:
                manifest["context_policy"] = policy
            with self.subTest(policy=policy), RoutingReplay(manifest) as router:
                router.session["on_air"].update_context(topic)
                router.wall = 600
                self.assertEqual(router.state()["public"]["context"] is not None, survives)

    def test_unknown_topic_policy_cannot_silently_change_replay(self):
        with self.assertRaisesRegex(ValueError, "Unknown recorded"):
            RoutingReplay({**self.manifest, "context_policy": "unknown"})

    def test_audience_text_policy_preserves_legacy_and_verbatim_semantics(self):
        original = "Can I use this without writing code?"
        paraphrase = "Can noncoders use it?"
        cue = "Maya: no coding?"
        for policy in (None, "legacy-summary", "verbatim"):
            manifest = copy.deepcopy(self.manifest)
            if policy is not None:
                manifest["audience_text_policy"] = policy
            with self.subTest(policy=policy), RoutingReplay(manifest) as router:
                router.inputs({"audience": [{"id": "q1", "author": "Maya", "text": original}]})
                private = {"action": "show", "line": cue}
                if policy == "verbatim":
                    private["question_id"] = "q1"
                result = router.apply({
                    "confidence": private,
                    "card": {"action": "show", "kind": "question", "line": paraphrase,
                             "question_id": "q1", "hold_seconds": 30},
                }, router.state())
                self.assertTrue(result["ok"])
                state = result["state"]
                self.assertEqual(state["public"]["graphic"]["line"],
                                 original if policy == "verbatim" else "Summary: " + paraphrase)
                self.assertEqual(state["confidence"]["line"], original if policy == "verbatim" else cue)
                self.assertEqual(state["public"]["graphic"]["message_id"], "q1")
                self.assertEqual(router.network_attempts, 0)

    def test_explicit_verbatim_policy_survives_saved_action_replay(self):
        self.manifest["audience_text_policy"] = "verbatim"
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        self.record(question=True)
        report, scenes = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["audience_text_policy"], "verbatim")
        self.assertEqual(report["api_calls"], 0)
        self.assertEqual(scenes[1]["graphic"]["line"], "Can noncoders use this?")
        self.assertEqual(scenes[1]["confidence"]["line"], "Can noncoders use this?")

    def test_unknown_audience_text_policy_cannot_silently_change_replay(self):
        for policy in ("unknown", None, True):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "Unknown recorded audience-text"):
                RoutingReplay({**self.manifest, "audience_text_policy": policy})

    def test_unknown_audience_input_cannot_silently_use_the_legacy_fixture_path(self):
        for mode in ("unknown", None, True, {}, []):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Unknown recorded audience-input"):
                RoutingReplay({**self.manifest, "audience_input": mode})

    def test_saved_screen_observation_replays_under_a_different_owner_from_recorded_frames(self):
        self.manifest.update(audience_input="screen", audience_text_policy="verbatim")
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        author = "Maya · simulated viewer"
        body = "  What’s that material? 🎃  "
        original_owner = "original-selected-api-session"
        recorded, frames = [], []
        router = RoutingReplay(self.manifest)
        router.sid = router.audience.owner = original_owner
        with router:
            trace = ActionTrace(self.bundle / "actions.jsonl", clock=lambda: router.wall,
                                source_clock=lambda: router.director.audio_end)
            trace.emit("screen_state", {"state": router.state()})
            for index, source_time in enumerate((4, 8), 1):
                router.wall = router.director.audio_end = source_time
                jpeg = b"selected-programme-frame-" + str(index).encode()
                store = router.session["screen_chat"]
                frame_id = store.frame_id(jpeg, source_time)
                frame = store.register_frame(frame_id, source_time, hashlib.sha256(jpeg).hexdigest())
                frames.append(frame)
                trace.emit("input_frame", frame)
                examined = router.state()
                trace.emit("response_requested", {"response_sequence": index, "state": examined})
                router.wall += 0.4
                args = {"screen_comment": {"frame_id": frame_id, "author": author, "text": body},
                        "card": {"action": "show", "kind": "question", "hold_seconds": 30},
                        "confidence": {"action": "show"}}
                trace.emit("function_call", {"name": "update_screens", "call_id": "screen-" + str(index),
                           "response_sequence": index, "arguments": args, "expected_state": examined})
                result = router.apply(args, examined)
                self.assertTrue(result["ok"])
                recorded.append(result["state"])
                trace.emit("tool_receipt", {"name": "update_screens", "call_id": "screen-" + str(index), "receipt": result})
                trace.emit("screen_state", {"state": router.state()})
            trace.close()
        self.assertNotEqual(frames[0]["frame_id"], frames[1]["frame_id"])
        self.assertEqual(recorded[0]["public"]["graphic"]["message_id"], recorded[1]["public"]["graphic"]["message_id"])
        actual_owners = set()
        original_apply = ShowDirector.apply

        def record_owner(director, args, examined):
            actual_owners.add(director.sid)
            return original_apply(director, args, examined)

        with patch.object(ShowDirector, "apply", record_owner):
            report, scenes = replay_bundle(self.bundle)
        self.assertEqual(actual_owners, {"local-action-replay"})
        self.assertNotIn(original_owner, actual_owners)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["api_calls"], 0)
        self.assertEqual(report["matched_actions"], 2)
        self.assertEqual(report["matched_scenes"], 3)
        for index, call in enumerate(report["calls"]):
            actual, expected = call["actual"]["state"], recorded[index]
            self.assertEqual(actual["audience_source"], "screen")
            self.assertEqual(actual["confidence"], expected["confidence"])
            self.assertEqual(actual["public"]["graphic"]["message_id"], expected["public"]["graphic"]["message_id"])
            self.assertEqual(actual["public"]["graphic"]["line"], body)
            self.assertEqual(actual["public"]["graphic"]["attribution"], author + " · from screen")
            self.assertEqual(actual["audience"], expected["audience"])
            self.assertEqual(actual["screen_frames"], expected["screen_frames"])
            self.assertEqual(actual["audience"][0]["source"], "screen")
            self.assertEqual(actual["audience"][0]["frame_id"], frames[index]["frame_id"])
            self.assertEqual(scenes[index + 1]["confidence"]["line"], body)

    def test_card_attribution_policy_preserves_legacy_and_model_source_credit(self):
        for policy in (None, "legacy-fixed", "model"):
            manifest = copy.deepcopy(self.manifest)
            if policy is not None:
                manifest["card_attribution_policy"] = policy
            with self.subTest(policy=policy), RoutingReplay(manifest) as router:
                result = router.apply({"card": {"action": "show", "kind": "remark",
                                     "line": "Colored light changes how paint looks.",
                                     "attribution": "Exploratorium"}}, router.state())
                self.assertTrue(result["ok"])
                self.assertEqual(result["state"]["public"]["graphic"]["attribution"],
                                 "Exploratorium" if policy == "model" else "Producer")
                self.assertEqual(router.network_attempts, 0)

    def test_model_source_credit_survives_saved_action_replay(self):
        self.manifest["card_attribution_policy"] = "model"
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        self.record(source_attribution=True)
        report, scenes = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["card_attribution_policy"], "model")
        self.assertEqual(report["api_calls"], 0)
        self.assertEqual(scenes[1]["graphic"]["attribution"], "Exploratorium")

    def test_unknown_card_attribution_policy_cannot_silently_change_replay(self):
        for policy in ("unknown", None, True):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "Unknown recorded card-attribution"):
                RoutingReplay({**self.manifest, "card_attribution_policy": policy})

    def production_manifest(self):
        self.manifest.update(production_policy="realtime-production-v1", production_replay={
            "platform": "fixture", "channel_id": "synthetic-test-show",
            "brief": {"production": {"preferences": "Useful public references", "actions": [
                {"id": "prepared-link", "kind": "reference", "when": "Resource discussed",
                 "text": "Reference https://example.org/guide"}]}}})
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))

    def record_production(self):
        self.production_manifest()
        with RoutingReplay(self.manifest) as router:
            trace = ActionTrace(self.bundle / "actions.jsonl", clock=lambda: router.wall,
                                source_clock=lambda: router.director.audio_end)
            router.session["action_trace"] = trace
            sequence = 0

            def invoke(name, args, source_time=None):
                nonlocal sequence
                sequence += 1
                router.wall += 0.2
                if source_time is not None:
                    router.director.audio_end = source_time
                examined = router.state()
                trace.emit("response_requested", {"response_sequence": sequence, "state": examined})
                trace.emit("function_call", {"name": name, "call_id": str(sequence),
                           "response_sequence": sequence, "arguments": args, "expected_state": examined})
                result = (router.apply if name == "update_screens" else router.production_apply)(args, examined)
                trace.emit("tool_receipt", {"name": name, "call_id": str(sequence), "receipt": result})
                capture_scene(router.director, trace)
                return result

            trace.emit("screen_state", {"state": router.state()})
            invoke("update_screens", {"confidence": {"action": "show", "line": "PRIVATE PRESENTER CUE"},
                "card": {"action": "show", "kind": "reference", "line": "https://example.org/guide", "hold_seconds": 45}}, 1)
            key = router.state()["production"]["shareable_outputs"][0]["output_key"]
            first = invoke("production_action", {"action": "share_on_air", "output_key": key}, 2)
            self.assertEqual(first["receipt"]["display_name"], "Producer")
            self.assertNotIn("PRIVATE PRESENTER", first["receipt"]["message"])
            duplicate = invoke("production_action", {"action": "share_on_air", "output_key": key}, 3)
            self.assertTrue(duplicate["duplicate"])
            invoke("production_action", {"action": "execute_plan", "plan_id": "prepared-link"}, 4)
            poll = invoke("production_action", {"action": "create_poll", "request_id": "p1",
                "text": "Which example?", "choices": ["Code", "Research"], "duration": 15}, 5)
            poll_id = poll["receipt"]["remote_id"]
            pending = invoke("production_action", {"action": "get_poll_result", "poll_id": poll_id}, 6)
            self.assertEqual(pending["poll"]["status"], "active")
            invoke("production_action", {"action": "create_marker", "request_id": "m1", "text": "Example begins"}, 7)
            invoke("production_action", {"action": "set_title", "request_id": "t1", "text": "Applied AI examples"}, 8)
            invoke("production_action", {"action": "set_category", "request_id": "c1", "text": "Science & Technology", "category_id": "509670"}, 9)
            completed = invoke("production_action", {"action": "get_poll_result", "poll_id": poll_id}, 21)
            self.assertEqual(completed["poll"]["counts"], [3, 2])
            trace.close()

    def test_offline_production_replays_actual_actions_identity_duplicates_and_polls(self):
        self.record_production()
        report, scenes = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(report["api_calls"], 0)
        self.assertEqual(report["screen_actions"], 1)
        self.assertEqual(report["production_actions"], 9)
        self.assertEqual(report["matched_actions"], 10)
        self.assertEqual(report["matched_scenes"], 11)
        self.assertTrue(report["production_chat"]["matches_recording"])
        self.assertEqual(len(report["production_chat"]["actual"]), 2)
        self.assertTrue(all(row["author"] == "Producer" for row in report["production_chat"]["actual"]))
        self.assertEqual(len(scenes), 11)

    def test_production_translation_failure_is_reported_without_an_api_call(self):
        self.record_production()
        from rehearsal import MockTwitch
        execute = MockTwitch.execute
        def wrong_sender(adapter, plan):
            row = execute(adapter, plan)
            if row.get("message"):
                row["sender_id"] = "incorrect-host-identity"
            return row
        with patch.object(MockTwitch, "execute", wrong_sender):
            report, _ = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "mismatched")
        self.assertLess(report["matched_actions"], 10)
        self.assertEqual(report["api_calls"], 0)

    def test_missing_production_replay_inputs_cannot_pass_by_skipping_calls(self):
        self.record_production()
        del self.manifest["production_replay"]
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        report, _ = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["unvalidated_production_actions"], 9)
        self.assertEqual(report["api_calls"], 0)

    def test_production_commits_replay_failed_dispatch_before_its_receipt(self):
        self.production_manifest()
        with RoutingReplay(self.manifest) as router:
            trace = ActionTrace(self.bundle / "actions.jsonl", clock=lambda: router.wall,
                                source_clock=lambda: router.director.audio_end)
            router.session["action_trace"] = trace
            for index in (1, 2):
                router.wall = router.director.audio_end = index
                examined = router.state()
                trace.emit("response_requested", {"response_sequence": index, "state": examined})
                args = {"action": "create_poll", "request_id": "poll-" + str(index), "text": "Next?",
                        "choices": ["Code", "Research"], "duration": 15}
                trace.emit("function_call", {"name": "production_action", "call_id": str(index),
                           "response_sequence": index, "arguments": args, "expected_state": examined})
                capture_scene(router.director, trace)
                router.wall += 0.1
                result = router.production_apply(args, examined)
                # The second native poll is denied while the first is active;
                # its durable failure still mutates production state atomically.
                self.assertEqual(result["ok"], index == 1)
                router.wall += 0.1
                capture_scene(router.director, trace)
                router.wall += 0.1
                trace.emit("tool_receipt", {"name": "production_action", "call_id": str(index), "receipt": result})
            trace.close()
        report, _ = replay_bundle(self.bundle)
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(report["matched_actions"], 2)
        self.assertEqual(report["matched_scenes"], 4)
        self.assertEqual([call["timing_basis"] for call in report["calls"]], ["production_commit"] * 2)
        self.assertEqual(report["api_calls"], 0)

    def test_historical_replay_has_no_production_state_or_implicit_enablement(self):
        with RoutingReplay(self.manifest) as router:
            self.assertNotIn("production", router.state())
            self.assertNotIn("production_policy", router.session)
        with self.assertRaisesRegex(ValueError, "Unknown recorded production"):
            RoutingReplay({**self.manifest, "production_policy": "future-production"})


if __name__ == "__main__":
    unittest.main()

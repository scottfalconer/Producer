"""Unified local routing with a fake Realtime connection; no model or hardware."""

import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import server
from audience import Audience
from director_runtime import ShowDirector, note_brief
from engine import fingerprint
from on_air import OnAir
from show_materials import ShowMaterials
from transcript import ShowTranscript


class FakeDirector:
    def __init__(self, key, **callbacks):
        self.callbacks = callbacks
        self.closed = False
        self.audio = []
        self.images = []

    def snapshot(self):
        return {"connected": not self.closed, "closed": self.closed, "error": None,
                "status": "Fake Realtime", "model": "fake-realtime"}

    def append(self, sequence, encoded):
        if self.closed:
            raise ValueError("Fake stopped")
        self.audio.append((sequence, encoded))

    def image(self, jpeg, source_end, frame_id=None):
        if source_end < 0:
            raise ValueError("Invalid image time")
        self.images.append((jpeg, source_end, frame_id) if frame_id else (jpeg, source_end))

    def close(self):
        self.closed = True


class DirectorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.materials_folder = self.folder / "materials"
        self.materials_folder.mkdir()
        Image.new("RGB", (40, 30), "blue").save(self.materials_folder / "diagram.png")
        self.materials = ShowMaterials(self.materials_folder)
        self.audience = Audience()
        self.sid = "fixture-director-session"
        self.audience.owner = self.sid
        self.audience.channel = "fixture"
        self.audience.connected = True
        self.audience.status = "Simulated viewer fixture"
        self.audience_patch = patch("server.audience", self.audience)
        self.audience_patch.start()
        self.lock = threading.Lock()
        self.running = True
        brief = note_brief("Private show notes\nExplain the request comparison.")
        self.session = {
            "offset": 0, "created": time.monotonic() - 2,
            "seen": time.monotonic(), "notes": "PRIVATE_NOTES_SENTINEL",
            "brief": brief, "materials": self.materials, "memory": {},
            "on_air": OnAir(True), "visual": None, "offline": True,
            "test_question": None,
            "transcript": ShowTranscript(self.folder / "transcripts", "runtime-test-show-0001", {"input_type": "authored"}, fingerprint(brief)),
        }
        self.director = self.build()
        self.session["director"] = self.director

    def tearDown(self):
        self.director.close()
        self.audience.stop()
        self.audience_patch.stop()
        self.temp.cleanup()

    def build(self, *, allow_audience=True):
        return ShowDirector(
            self.sid, self.session, key="unused", lock=self.lock,
            active=lambda: self.running,
            public_state=lambda: server.public_state(self.sid, self.session),
            audience=self.audience, allow_audience=allow_audience,
            director_class=FakeDirector,
        )

    def question(self, ident="q1", text="How does this request work?", author="Fictional viewer"):
        self.audience.questions.append({"id": ident, "user_id": "viewer-" + ident,
                                        "text": text, "author": author, "received": time.time()})

    def test_simulated_timeline_and_eval_labels_are_not_model_context(self):
        self.session["simulated_chat"] = {
            "events": [{"text": "HIDDEN_FIXTURE_SENTINEL"}],
            "eval_labels": {"m1": "HIDDEN_LABEL_SENTINEL"},
            "simulation": {"seed": "HIDDEN_SEED_SENTINEL"},
        }
        state = json.dumps(self.director.state())
        self.assertNotIn("HIDDEN_", state)

    def test_confidence_age_uses_source_time_and_survives_keep_and_identical_reissue(self):
        cue = {"action": "show", "line": "Explain the example"}
        self.director.audio_end = 12.5
        first = self.director.apply({"confidence": cue}, self.director.state())["state"]
        self.assertEqual(first["confidence_timing_policy"], "source-clock-v1")
        self.assertEqual(first["confidence"]["shown_at_source_s"], 12.5)
        self.assertEqual(first["confidence"]["visible_for_s"], 0)
        with patch("time.time", return_value=9999999999):
            self.director.audio_end = 43.125
            kept = self.director.apply({"confidence": {"action": "keep"}}, first)["state"]
        self.assertEqual(kept["confidence"]["shown_at_source_s"], 12.5)
        self.assertEqual(kept["confidence"]["visible_for_s"], 30.625)
        self.director.audio_end = 88
        repeated = self.director.apply({"confidence": cue}, kept)["state"]
        self.assertEqual(repeated["confidence"]["shown_at_source_s"], 12.5)
        self.assertEqual(repeated["confidence"]["visible_for_s"], 75.5)
        self.assertEqual(self.director.snapshot()["confidence"],
                         {"line": "Explain the example", "kind": "editorial"})
        self.director.audio_end = 3600
        self.assertEqual(self.director.state()["confidence"]["visible_for_s"], 3587.5)
        self.assertEqual(self.director.state()["confidence"]["line"], "Explain the example")

    def test_changed_or_cleared_confidence_resets_its_visibility_start(self):
        self.director.audio_end = 5
        self.director.apply({"confidence": {"action": "show", "line": "First cue"}}, self.director.state())
        self.director.audio_end = 8
        changed = self.director.apply({"confidence": {"action": "show", "line": "Next cue"}}, self.director.state())["state"]
        self.assertEqual(changed["confidence"]["shown_at_source_s"], 8)
        self.assertEqual(changed["confidence"]["visible_for_s"], 0)
        self.director.audio_end = 10
        cleared = self.director.apply({"confidence": {"action": "clear"}}, changed)["state"]
        self.assertIsNone(cleared["confidence"])
        self.assertNotIn("confidence_shown_at_source_s", self.session)
        self.director.audio_end = 13
        returned = self.director.apply({"confidence": {"action": "show", "line": "Next cue"}}, cleared)["state"]
        self.assertEqual(returned["confidence"]["shown_at_source_s"], 13)

    def test_new_audience_question_identity_restarts_private_visibility_clock(self):
        self.question("q1", text="Same words?", author="Maya")
        self.question("q2", text="Same words?", author="Maya")
        self.director.audio_end = 2
        first = self.director.apply({"confidence": {"action": "show", "question_id": "q1"}}, self.director.state())["state"]
        self.director.audio_end = 9
        second = self.director.apply({"confidence": {"action": "show", "question_id": "q2"}}, first)["state"]
        self.assertEqual(second["confidence"]["shown_at_source_s"], 9)
        self.assertEqual(second["confidence"]["message_id"], "q2")
        self.assertEqual(second["confidence"]["line"], "Same words?")

    def test_audience_history_reports_actual_presentations_without_blocking_revisits(self):
        self.question()
        initial = self.director.state()
        self.assertEqual(initial["audience"][0]["times_featured"], 0)
        selection = {"card": {"action": "show", "kind": "question", "question_id": "q1"}}
        first = self.director.apply(selection, initial)
        self.assertTrue(first["ok"])
        self.assertEqual(first["state"]["audience"][0]["times_featured"], 1)
        renewed = self.director.apply(selection, first["state"])
        self.assertEqual(renewed["state"]["audience"][0]["times_featured"], 1)
        background = self.director.apply({"card": {"action": "show", "kind": "remark",
            "line": "An explanation can replace a question.", "attribution": "Prepared source"}}, renewed["state"])
        self.assertTrue(background["ok"])
        revisited = self.director.apply(selection, background["state"])
        self.assertTrue(revisited["ok"])
        self.assertEqual(revisited["state"]["audience"][0]["times_featured"], 2)

    def test_source_credit_reaches_factual_card_and_original_question_author_wins(self):
        result = self.director.apply({"card": {"action": "show", "kind": "remark",
            "line": "A useful sourced explanation.", "attribution": "Source: supplied museum reference"}}, self.director.state())
        self.assertEqual(result["state"]["public"]["graphic"]["attribution"], "Source: supplied museum reference")
        self.question(author="Original viewer")
        question = self.director.apply({"card": {"action": "show", "kind": "question",
            "question_id": "q1", "attribution": "Invented author"}}, self.director.state())
        self.assertEqual(question["state"]["public"]["graphic"]["attribution"], "Original viewer · from chat")

    def test_one_instruction_updates_both_screens_and_keeps_private_context_private(self):
        initial = self.director.state()
        asset = self.materials.catalog[0]["id"]
        result = self.director.apply({
            "confidence": {"action": "show", "line": "Explain the concrete result"},
            "card": {"action": "show", "kind": "takeaway", "line": "A clear visible result", "hold_seconds": 10},
            "context": {"action": "set", "subject": "Request comparison", "purpose": ""},
            "visual": {"action": "show", "asset_id": asset},
            "working_notes": "PRIVATE_WORKING_SENTINEL",
        }, initial)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["confidence"]["line"], "Explain the concrete result")
        public = server.public_state(self.sid, self.session)
        self.assertEqual(public["graphic"]["line"], "A clear visible result")
        self.assertEqual(public["visual"]["asset_id"], asset)
        self.assertNotIn("PRIVATE_", json.dumps(public))
        self.assertEqual(self.director.core.callbacks["brief"]["notes"], "PRIVATE_NOTES_SENTINEL")

    def test_context_subtitle_is_exact_audience_copy_and_legacy_purpose_still_routes(self):
        result = self.director.apply({"context": {"action": "set", "subject": "Making Halloween decorations",
            "subtitle": "A skeleton, a cauldron and colored lights", "purpose": "PRIVATE_PLAN_SHOULD_NOT_WIN"}}, self.director.state())
        self.assertEqual(result["state"]["public"]["context"]["purpose"], "A skeleton, a cauldron and colored lights")
        self.assertNotIn("PRIVATE_PLAN_SHOULD_NOT_WIN", json.dumps(result["state"]["public"]))
        legacy = self.director.apply({"context": {"action": "set", "subject": "Earlier replay",
            "purpose": "The original displayed second line"}}, result["state"])
        self.assertEqual(legacy["state"]["public"]["context"]["purpose"], "The original displayed second line")
        clear_subtitle = self.director.apply({"context": {"action": "set", "subject": "Headline only",
            "subtitle": "", "purpose": "A legacy fallback must not override explicit removal"}}, legacy["state"])
        self.assertEqual(clear_subtitle["state"]["public"]["context"]["purpose"], "")

    def test_supplied_research_is_private_director_and_editor_reference(self):
        packet = "# REF-001\nSource: https://example.test/source\nEarlier episode at 02:31."
        self.director.close()
        self.session["research_packet"] = packet
        self.director = self.build()
        self.assertEqual(self.director.core.callbacks["brief"]["research_packet"], packet)
        self.assertEqual(self.director.review_context(0)["research_packet"], packet)
        self.assertNotIn("research_packet", self.director.state())
        self.assertNotIn("REF-001", json.dumps(server.public_state(self.sid, self.session)))

    def test_invalid_asset_or_question_cannot_partially_change_private_screen(self):
        initial = self.director.state()
        for extra in (
            {"visual": {"action": "show", "asset_id": "img-not-in-this-show"}},
            {"card": {"action": "show", "kind": "question", "line": "Invented", "question_id": "absent"}},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.director.apply({"confidence": {"action": "show", "line": "Must remain unapplied"}, **extra}, initial)
            self.assertIsNone(self.session["confidence"])
            self.assertIsNone(self.session["on_air"].snapshot()["graphic"])
            self.assertEqual(self.session["director_revision"], initial["revision"])

    def test_question_removed_during_selection_does_not_partially_update_screens(self):
        self.question()
        initial = self.director.state()
        with patch.object(self.audience, "pin_question", return_value=False), self.assertRaises(ValueError):
            self.director.apply({
                "confidence": {"action": "show", "line": "Take this question"},
                "card": {"action": "show", "kind": "question", "line": "How does this request work?", "question_id": "q1"},
            }, initial)
        self.assertIsNone(self.session["confidence"])
        self.assertIsNone(self.session["on_air"].snapshot()["graphic"])
        self.assertEqual(self.session["director_revision"], initial["revision"])

    def test_featured_comment_uses_source_verbatim_on_both_screens(self):
        original = "  First time here — excited! 🎃\nWhat’s that material?  "
        self.question(text=original, author="Maya · simulated viewer")
        result = self.director.apply({
            "confidence": {"action": "show", "question_id": "q1", "line": "Maya: introduce the show?"},
            "card": {"action": "show", "kind": "question", "question_id": "q1",
                     "line": "Summary: What are we making first?"},
        }, self.director.state())
        self.assertTrue(result["ok"])
        private = result["state"]["confidence"]
        public = result["state"]["public"]["graphic"]
        self.assertEqual(private, {"line": original, "kind": "question", "message_id": "q1",
                                   "attribution": "Maya · simulated viewer",
                                   "shown_at_source_s": 0, "visible_for_s": 0.0})
        self.assertEqual(public["line"], original)
        self.assertEqual(public["message_id"], "q1")
        self.assertEqual(public["attribution"], "Maya · simulated viewer · from chat")
        self.assertNotIn("Summary:", public["line"])

    def test_public_comment_does_not_replace_private_editorial_guidance(self):
        original = "Can this attachment be reused?"
        self.question(text=original)
        result = self.director.apply({
            "confidence": {"action": "show", "line": "Switch to the demo scene"},
            "card": {"action": "show", "kind": "question", "question_id": "q1"},
        }, self.director.state())
        self.assertEqual(result["state"]["confidence"],
                         {"line": "Switch to the demo scene", "kind": "editorial",
                          "shown_at_source_s": 0, "visible_for_s": 0.0})
        self.assertEqual(result["state"]["public"]["graphic"]["line"], original)

    def test_comment_selection_needs_identity_not_model_authored_text(self):
        self.question(text="Can I reuse the same attachment next year?")
        for private in ({"action": "show"}, {"action": "show", "line": ""},
                        {"action": "show", "line": "   "}):
            with self.subTest(private=private):
                result = self.director.apply({
                    "confidence": private,
                    "card": {"action": "show", "kind": "question", "question_id": "q1"},
                }, self.director.state())
                self.assertEqual(result["state"]["confidence"]["line"], "Can I reuse the same attachment next year?")
                self.assertEqual(result["state"]["public"]["graphic"]["line"], "Can I reuse the same attachment next year?")

    def test_private_only_comment_is_exact_and_does_not_pin_or_publish_it(self):
        original = "Would a warmer light change the effect?"
        self.question(text=original, author="Theo")
        result = self.director.apply({
            "confidence": {"action": "show", "question_id": "q1", "line": "Theo: warmer?"},
        }, self.director.state())
        self.assertEqual(result["state"]["confidence"],
                         {"line": original, "kind": "question", "message_id": "q1", "attribution": "Theo",
                          "shown_at_source_s": 0, "visible_for_s": 0.0})
        self.assertIsNone(result["state"]["public"]["graphic"])
        self.assertIsNone(self.audience.selection)

    def test_private_comment_can_be_selected_while_public_question_is_kept(self):
        self.question(text="Original public question?")
        self.question(ident="q2", text="A different complete private question?", author="Maya")
        result = self.director.apply({
            "card": {"action": "show", "kind": "question", "question_id": "q1"},
        }, self.director.state())
        result = self.director.apply({
            "card": {"action": "keep"},
            "confidence": {"action": "show", "question_id": "q2"},
        }, result["state"])
        self.assertEqual(result["state"]["confidence"]["line"], "A different complete private question?")
        self.assertEqual(result["state"]["public"]["graphic"]["line"], "Original public question?")

    def test_oversize_comment_is_omitted_without_partial_changes(self):
        self.question(text="A" * 181)
        initial = self.director.state()
        asset = self.materials.catalog[0]["id"]
        selections = (
            {"confidence": {"action": "show", "line": "Short summary"},
             "card": {"action": "show", "kind": "question", "question_id": "q1", "line": "Short summary"}},
            {"confidence": {"action": "show", "question_id": "q1", "line": "Short summary"}},
        )
        for selection in selections:
            with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, "complete audience comment does not fit"):
                self.director.apply({
                    **selection,
                    "context": {"action": "set", "subject": "Must not appear"},
                    "visual": {"action": "show", "asset_id": asset},
                    "working_notes": "Must not be saved",
                }, initial)
            self.assertEqual(self.director.state(), initial)
            self.assertIsNone(self.audience.selection)

    def test_exact_180_character_comment_is_not_shortened(self):
        original = "A" * 180
        self.question(text=original)
        result = self.director.apply({
            "confidence": {"action": "show", "question_id": "q1"},
            "card": {"action": "show", "kind": "question", "question_id": "q1"},
        }, self.director.state())
        self.assertEqual(result["state"]["confidence"]["line"], original)
        self.assertEqual(result["state"]["public"]["graphic"]["line"], original)

    def test_selected_comment_uses_renderers_utf16_bound_without_shortening(self):
        for index, original in enumerate(("🎃" * 91, "\ud800" + "🎃" * 90)):
            ident = "too-wide-" + str(index)
            self.question(ident=ident, text=original)
            initial = self.director.state()
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "complete audience comment does not fit"):
                self.director.apply({
                    "confidence": {"action": "show", "question_id": ident},
                    "card": {"action": "show", "kind": "question", "question_id": ident},
                    "context": {"action": "set", "subject": "Must not appear"},
                }, initial)
            self.assertEqual(self.director.state(), initial)
            self.assertIsNone(self.audience.selection)
        original = "🎃" * 90
        self.question(ident="fits", text=original)
        result = self.director.apply({
            "confidence": {"action": "show", "question_id": "fits"},
            "card": {"action": "show", "kind": "question", "question_id": "fits"},
        }, self.director.state())
        self.assertEqual(result["state"]["confidence"]["line"], original)
        self.assertEqual(result["state"]["public"]["graphic"]["line"], original)

    def test_missing_or_deleted_private_comment_cannot_change_any_screen(self):
        self.question()
        initial = self.director.state()
        self.audience.dismiss(self.sid, "q1")
        for ident in ("q1", "absent", []):
            with self.subTest(ident=ident), self.assertRaises(ValueError):
                self.director.apply({
                    "confidence": {"action": "show", "question_id": ident, "line": "Must not appear"},
                    "card": {"action": "show", "kind": "remark", "line": "Must not appear"},
                }, initial)
            self.assertIsNone(self.session["confidence"])
            self.assertIsNone(self.session["on_air"].snapshot()["graphic"])
            self.assertEqual(self.session["director_revision"], initial["revision"])

    def test_conflicting_simultaneous_comment_selection_is_rejected_atomically(self):
        self.question()
        self.question(ident="q2", text="Another source comment?")
        initial = self.director.state()
        with self.assertRaisesRegex(ValueError, "same audience comment"):
            self.director.apply({
                "confidence": {"action": "show", "question_id": "q2"},
                "card": {"action": "show", "kind": "question", "question_id": "q1"},
            }, initial)
        self.assertEqual(self.director.state(), initial)
        self.assertIsNone(self.audience.selection)

    def test_historical_summary_policy_preserves_original_output(self):
        self.session["audience_text_policy"] = "legacy-summary"
        self.question(text="Did you make that cauldron or buy it?", author="Rowan")
        result = self.director.apply({
            "confidence": {"action": "show", "line": "Rowan: made or bought?"},
            "card": {"action": "show", "kind": "question", "question_id": "q1", "line": "Made or bought?"},
        }, self.director.state())
        self.assertEqual(result["state"]["confidence"], {"line": "Rowan: made or bought?", "kind": "editorial",
                                                        "shown_at_source_s": 0, "visible_for_s": 0.0})
        self.assertEqual(result["state"]["public"]["graphic"]["line"], "Summary: Made or bought?")

    def test_stale_revision_reconsiders_without_altering_newer_state(self):
        old = self.director.state()
        self.director.apply({"confidence": {"action": "show", "line": "Newer cue"}}, old)
        result = self.director.apply({"confidence": {"action": "clear"}}, old)
        self.assertFalse(result["ok"])
        self.assertEqual(self.session["confidence"]["line"], "Newer cue")

    def test_past_recall_is_capped_by_accepted_audio_even_when_model_asks_for_future(self):
        self.session["transcript"].record(0, 2, "Earlier accepted explanation")
        self.session["transcript"].record(2, 4, "FUTURE_SENTINEL")
        self.director.audio_end = 2
        result = self.director.recall({"start": 0, "end": 100}, before=100)
        self.assertEqual(result["before"], 2)
        self.assertNotIn("FUTURE_SENTINEL", json.dumps(result))

    def test_stop_rejects_late_tool_and_transcript_callbacks(self):
        initial = self.director.state()
        previous = self.session["transcript"].path.read_bytes()
        self.running = False
        self.director.close()
        for call in (
            lambda: self.director.apply({"confidence": {"action": "show", "line": "Too late"}}, initial),
            lambda: self.director.transcribe(0, 2, "Too late"),
            lambda: self.director.recall({}, 2),
        ):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.session["transcript"].path.read_bytes(), previous)
        self.assertIsNone(self.session["confidence"])

    def test_audio_sequence_failure_does_not_accept_a_second_packet(self):
        audio = base64.b64encode(b"\0" * 24000).decode()
        self.assertTrue(self.director.append(0, audio)["accepted"])
        with self.assertRaises(ValueError):
            self.director.append(0, audio)
        self.assertEqual(len(self.director.core.audio), 1)
        self.assertEqual(self.director.audio_end, 0.5)

    def test_keep_does_not_resurrect_an_expired_image(self):
        asset = self.materials.catalog[0]["id"]
        self.session["visual"] = {"kind": "asset", "asset_id": asset, "until": time.time() - 1}
        initial = self.director.state()
        self.assertIsNone(initial["public"]["visual"])
        result = self.director.apply({"visual": {"action": "keep"}}, initial)
        self.assertIsNone(result["state"]["public"]["visual"])

    def test_unapproved_viewer_data_is_redacted_from_director_public_context(self):
        self.question(text="REAL_VIEWER_PRIVATE_SENTINEL", author="REAL_AUTHOR_SENTINEL")
        self.audience.pin_question(self.sid, "q1")
        self.session["on_air"].publish({"kind": "question", "line": "REAL_VIEWER_PRIVATE_SENTINEL", "attribution": "REAL_AUTHOR_SENTINEL", "message_id": "q1"})
        self.director.close()
        self.director = self.build(allow_audience=False)
        self.session["director"] = self.director
        state = self.director.state()
        self.assertEqual(state["audience"], [])
        self.assertNotIn("REAL_VIEWER_PRIVATE_SENTINEL", json.dumps(state))
        self.assertNotIn("REAL_AUTHOR_SENTINEL", json.dumps(state))

    def test_screen_mode_never_reads_direct_fixture_inbox(self):
        self.question(text="DIRECT_FIXTURE_SENTINEL", author="Fixture author")
        self.session["audience_input"] = "screen"
        self.director.close()
        self.director = self.build(allow_audience=True)
        self.session["director"] = self.director
        state = self.director.state()
        self.assertNotIn("DIRECT_FIXTURE_SENTINEL", json.dumps(state))
        self.assertEqual(state["audience_source"], "screen")
        self.assertEqual(state["audience"], [])
        self.assertEqual(state["screen_frames"], [])

    def screen_mode(self):
        self.director.close()
        self.session["audience_input"] = "screen"
        self.director = self.build(allow_audience=False)
        self.session["director"] = self.director

    def screen_frame(self):
        jpeg = b"\xff\xd8visible-simulated-chat-panel\xff\xd9"
        self.director.append(0, base64.b64encode(b"\0" * 24000).decode(),
                             base64.b64encode(jpeg).decode(), source_end=0.5)
        state = self.director.state()
        return state, {"frame_id": state["screen_frames"][0]["frame_id"],
                       "author": "Maya · simulated viewer", "text": "  What’s that material? 🎃  "}

    def test_screen_comment_atomically_updates_both_screens_without_an_inbox_roundtrip(self):
        self.screen_mode()
        self.session["audience_text_policy"] = "legacy-summary"  # A pixel-read source never inherits fixture rewriting.
        state, comment = self.screen_frame()
        self.assertEqual(state["audience"], [])
        result = self.director.apply({"screen_comment": comment,
            "card": {"action": "show", "kind": "question"},
            "confidence": {"action": "show"}}, state)
        public, private = result["state"]["public"]["graphic"], result["state"]["confidence"]
        self.assertEqual(public["line"], comment["text"])
        self.assertEqual(private["line"], comment["text"])
        self.assertEqual(public["message_id"], private["message_id"])
        self.assertEqual(public["attribution"], comment["author"] + " · from screen")
        self.assertEqual(result["state"]["audience"][0]["times_featured"], 1)
        self.assertEqual(self.audience.questions, [])
        self.assertIsNone(self.audience.selection)

    def test_screen_comment_private_only_and_independent_editorial_cue_remain_possible(self):
        self.screen_mode()
        state, comment = self.screen_frame()
        result = self.director.apply({"screen_comment": comment,
            "confidence": {"action": "show"}}, state)
        ident = result["state"]["confidence"]["message_id"]
        self.assertEqual(result["state"]["confidence"]["line"], comment["text"])
        self.assertIsNone(result["state"]["public"]["graphic"])
        self.assertIsNone(self.director.screen_chat.selection)
        public = self.director.apply({"card": {"action": "show", "kind": "question", "question_id": ident},
            "confidence": {"action": "show", "line": "Explain how the material bends"}}, result["state"])
        self.assertEqual(public["state"]["confidence"]["line"], "Explain how the material bends")
        self.assertEqual(public["state"]["public"]["graphic"]["line"], comment["text"])

    def test_new_public_screen_comment_does_not_replace_an_explicit_private_cue(self):
        self.screen_mode()
        state, comment = self.screen_frame()
        result = self.director.apply({"screen_comment": comment,
            "card": {"action": "show", "kind": "question"},
            "confidence": {"action": "show", "line": "Return to the material comparison"}}, state)
        self.assertEqual(result["state"]["public"]["graphic"]["line"], comment["text"])
        self.assertEqual(result["state"]["confidence"], {"line": "Return to the material comparison", "kind": "editorial",
                                                        "shown_at_source_s": 0.5, "visible_for_s": 0.0})

    def test_screen_comment_cannot_use_fixture_ids_or_unexamined_stale_or_wrong_frames(self):
        self.question(ident="fixture-hidden", text="A hidden fixture question")
        self.screen_mode()
        before_frame = self.director.state()
        state, comment = self.screen_frame()
        candidates = [
            ({"card": {"action": "show", "kind": "question", "question_id": "fixture-hidden"}}, state),
            ({"screen_comment": comment, "card": {"action": "show", "kind": "question"}}, before_frame),
            ({"screen_comment": {**comment, "frame_id": "another-show-frame"}, "card": {"action": "show", "kind": "question"}}, state),
            ({"screen_comment": comment, "card": {"action": "show", "kind": "question", "question_id": "fixture-hidden"}}, state),
        ]
        for args, examined in candidates:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.director.apply(args, examined)
            self.assertEqual(self.director.screen_chat.questions, [])
            self.assertIsNone(self.session["confidence"])
            self.assertIsNone(self.session["on_air"].snapshot()["graphic"])
        self.director.audio_end = 20.501
        with self.assertRaisesRegex(ValueError, "unavailable|stale"):
            self.director.apply({"screen_comment": comment,
                "card": {"action": "show", "kind": "question"}}, state)

    def test_invalid_screen_action_does_not_commit_observed_text(self):
        self.screen_mode()
        state, comment = self.screen_frame()
        with self.assertRaises(ValueError):
            self.director.apply({"screen_comment": comment,
                "card": {"action": "show", "kind": "question"},
                "visual": {"action": "show", "asset_id": "not-selected"}}, state)
        self.assertEqual(self.director.screen_chat.questions, [])
        self.assertIsNone(self.director.screen_chat.selection)
        changed = self.director.apply({"confidence": {"action": "show", "line": "A newer cue"}}, state)
        stale = self.director.apply({"screen_comment": comment,
            "card": {"action": "show", "kind": "question"}}, state)
        self.assertFalse(stale["ok"])
        self.assertEqual(self.director.screen_chat.questions, [])
        self.assertEqual(stale["state"]["confidence"], changed["state"]["confidence"])

    def test_screen_frames_register_only_after_successful_selected_native_image_send(self):
        self.screen_mode()
        self.session["events"] = Mock()
        self.session["action_trace"] = Mock()
        state, _ = self.screen_frame()
        image = self.director.core.images[0]
        self.assertEqual(image[2], state["screen_frames"][0]["frame_id"])
        self.assertNotIn("visible-simulated-chat-panel", json.dumps(state))
        self.session["events"].advance.assert_not_called()
        self.session["action_trace"].emit.assert_called_once_with("input_frame", state["screen_frames"][0])
        jpeg = base64.b64encode(b"\xff\xd8another-frame\xff\xd9").decode()
        with patch.object(self.director.core, "image", side_effect=ValueError("Send failed")), self.assertRaisesRegex(ValueError, "Send failed"):
            self.director.append(1, base64.b64encode(b"\0" * 24000).decode(), jpeg, source_end=1)
        self.assertEqual(len(self.director.screen_chat.frames(1)), 1)
        self.director.close()
        self.assertEqual(self.session["screen_chat"].questions, [])
        self.assertEqual(self.session["screen_chat"]._frames, [])
        self.assertIsNone(self.session["screen_chat"].selection)

    def test_screen_mode_rejects_future_frame_before_accepting_audio(self):
        self.screen_mode()
        with self.assertRaisesRegex(ValueError, "future"):
            self.director.append(0, base64.b64encode(b"\0" * 24000).decode(),
                                 base64.b64encode(b"\xff\xd8future-frame\xff\xd9").decode(), source_end=0.501)
        self.assertEqual(self.director.core.audio, [])
        self.assertEqual(self.director.state()["screen_frames"], [])

    def test_disabled_public_output_rejects_display_request_before_private_mutation(self):
        self.session["on_air"].enabled = False
        initial = self.director.state()
        with self.assertRaises(ValueError):
            self.director.apply({
                "confidence": {"action": "show", "line": "Must remain unapplied"},
                "card": {"action": "show", "kind": "title", "line": "Public mode is off"},
            }, initial)
        self.assertIsNone(self.session["confidence"])

    def test_invalid_image_time_is_rejected_before_native_audio_is_accepted(self):
        audio = base64.b64encode(b"\0" * 24000).decode()
        frame = base64.b64encode(b"\xff\xd8image\xff\xd9").decode()
        with self.assertRaises(ValueError):
            self.director.append(0, audio, frame, source_end=-1)
        self.assertEqual(self.director.core.audio, [])
        self.assertEqual(self.director.sequence, 0)


if __name__ == "__main__":
    unittest.main()

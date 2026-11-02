"""Speech-ready public work runs beside private continuity, using no real provider."""

import base64
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

import server
from engine import produce
from test_continuity import BRIEF
from test_engine import PCM, Stub


class SpeechReadyTests(unittest.TestCase):
    def test_speech_hook_precedes_continuity_and_times_each_stage(self):
        clock = [0.0]
        calls = []

        class API(Stub):
            def transcribe(self, pcm):
                calls.append("transcription")
                clock[0] += 2
                return super().transcribe(pcm)

            def decide(self, *args):
                calls.append("private")
                clock[0] += 4
                return super().decide(*args)

        with patch("engine.time.monotonic", side_effect=lambda: clock[0]):
            row = produce(
                BRIEF, 0, 8, PCM, b"", [], API(),
                on_speech=lambda speech: calls.append("public"),
            )
        self.assertEqual(calls, ["transcription", "public", "private"])
        self.assertEqual(row["processing_timing_s"], {"transcription_s": 2, "continuity_s": 4})
        self.assertEqual(row["processing_s"], 6)

    def test_stop_after_transcription_or_hook_prevents_further_work(self):
        api = Stub()
        hook = Mock()
        checks = iter([True, False])
        with self.assertRaisesRegex(ValueError, "ended before editorial"):
            produce(BRIEF, 0, 8, PCM, b"", [], api, lambda: next(checks), on_speech=hook)
        hook.assert_not_called()
        running = [True]
        with self.assertRaisesRegex(ValueError, "ended before editorial"):
            produce(
                BRIEF, 0, 8, PCM, b"", [], api, lambda: running[0],
                on_speech=lambda speech: running.__setitem__(0, False),
            )
        self.assertFalse(hasattr(api, "past"))


class PublicCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.private = patch("server.PRIVATE", self.folder / "private")
        self.private.start()
        Image.new("RGB", (32, 32), "blue").save(self.folder / "diagram.png")
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.key_file = Path("/unused-key")
        self.http.audience_openai = False
        self.http.show_folder = self.folder
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.headers = {"Origin": self.base, "X-Desk": server.TOKEN}
        response = self.post("/api/start", {"brief": BRIEF, "on_air": True, "input_type": "microphone"})
        self.assertEqual(response.status_code, 200, response.text)
        self.sid = response.json()["id"]
        self.session = server.LIVE[self.sid]
        self.session["created"] -= 8

    def tearDown(self):
        for sid in list(server.LIVE):
            self.post("/api/stop", {"id": sid})
        server.audience.stop()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.private.stop()
        self.temp.cleanup()

    def post(self, path, data):
        return requests.post(self.base + path, json=data, headers=self.headers, timeout=3)

    def direction(self):
        return {"context": None, "graphic": None, "question_status": "keep", "question_id": "",
                "visual_action": "keep", "visual": None}

    def dispatch(self, end=8, **kwargs):
        return server.dispatch_public(self.sid, self.session, Path("/unused"), "Current speech", end - 8, end, **kwargs)

    def test_public_finishes_while_private_continuity_is_still_running(self):
        public_done = threading.Event()
        private_entered = threading.Event()
        calls = []

        def public_model(*args, **kwargs):
            calls.append("public_started")
            self.assertTrue(private_entered.wait(1), "Private work did not run alongside public work")
            public_done.set()
            return self.direction()

        def continuity(*args, **kwargs):
            private_entered.set()
            self.assertTrue(public_done.wait(1), "Public work waited for private continuity")
            calls.append("private_finished")
            return {"updates": [], "commitments": [], "boundary": False, "cue": {},
                    "closing": False, "closing_quote": "", "activity": {}}

        api = Mock()
        api.transcribe.return_value = "Here is a useful explanation for the audience."
        with (
            patch("server.extract", return_value=PCM), patch("server._read_key", return_value="unused"),
            patch("server.OpenAIProducer", return_value=api),
            patch("server.propose_graphic", side_effect=public_model) as propose,
            patch("continuity.structured", side_effect=continuity),
            patch("server.judge_audience"),
        ):
            response = self.post("/api/window", {
                "id": self.sid, "start": 0, "end": 8,
                "audio": base64.b64encode(b"mock-webm").decode(),
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(propose.call_count, 1)
        self.assertEqual(calls, ["public_started", "private_finished"])
        row = response.json()
        self.assertNotIn("speech", row)
        for name in ("transcription_s", "continuity_s", "decode_s", "arrival_lag_s", "server_total_s", "source_to_private_s"):
            self.assertGreaterEqual(row["processing_timing_s"][name], 0)
        self.assertEqual(self.session["last"], 8)

    def test_dispatch_stays_single_fresh_and_latched_to_source_end(self):
        with patch("server.threading.Thread") as thread:
            self.assertTrue(self.dispatch())
            self.assertFalse(self.dispatch())  # Busy and same source.
            self.session["on_air"].busy = False
            self.assertFalse(self.dispatch(boundary=True))  # Completed early worker cannot repeat.
            self.assertFalse(self.dispatch(12))  # Minimum interval remains bounded.
            self.assertTrue(self.dispatch(16))
            self.session["on_air"].busy = False
            self.session["created"] -= 100
            self.assertFalse(self.dispatch(24))  # No fresh output from a queued old cut.
            self.assertEqual(thread.call_count, 2)

    def test_existing_context_does_not_gate_model_judgment_and_stop_blocks_dispatch(self):
        self.session["materials"] = None
        self.session["on_air"].update_context({"subject": "Existing subject", "purpose": ""})
        with patch("server.threading.Thread") as thread:
            self.assertTrue(self.dispatch())
            self.session["on_air"].busy = False
            self.assertFalse(self.dispatch(boundary=True))
            self.session["on_air"].busy = False
            server.LIVE.pop(self.sid)
            self.session["on_air"].close()
            self.assertFalse(self.dispatch(40, boundary=True))
            self.assertEqual(thread.call_count, 1)

    def test_public_timing_is_content_free_and_quiet_is_not_a_rendered_change(self):
        with (
            patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer"),
            patch("server.propose_graphic", return_value=self.direction()),
        ):
            server.make_public_graphic(self.sid, self.session, Path("/unused"), "Private speech", 8, 0)
        result = requests.get(self.base + "/api/on-air", timeout=3).json()
        timing = result["timing"]["public"]
        self.assertTrue(timing["result_accepted"])
        for name in ("context_changed", "graphic_queued", "question_resolved", "visual_changed"):
            self.assertFalse(timing[name])
        self.assertNotIn("Private speech", str(timing))
        self.assertGreaterEqual(timing["source_to_public_result_s"], 0)
        self.assertGreaterEqual(timing["processing_s"], 0)

    def test_optional_public_failure_releases_busy_without_retrying_same_cut(self):
        self.session["on_air"].busy = True
        self.session["on_air"].last_attempt = 8
        with (
            patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer"),
            patch("server.propose_graphic", side_effect=RuntimeError("provider unavailable")),
        ):
            server.make_public_graphic(self.sid, self.session, Path("/unused"), "Current speech", 8, 0)
        self.assertFalse(self.session["on_air"].busy)
        self.assertFalse(self.session["public_timing"]["result_accepted"])
        with patch("server.threading.Thread") as thread:
            self.assertFalse(self.dispatch(boundary=True))
            thread.assert_not_called()

    def test_public_parallelism_does_not_allow_overlapping_private_windows(self):
        self.session["busy"] = True
        with patch("server.OpenAIProducer") as provider:
            response = self.post("/api/window", {
                "id": self.sid, "start": 0, "end": 8, "audio": "AA==",
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("out of order", response.json()["error"])
        provider.assert_not_called()
        self.assertEqual(self.session["last"], 0)

    def seed_question(self, ident, text):
        server.audience.questions.append({
            "id": ident, "text": text, "author": "Simulated viewer",
            "user_id": "fictional-viewer", "received": time.time(),
        })

    def test_model_can_clear_an_unanswered_question_and_unpin_its_moderation_identity(self):
        self.seed_question("q1", "Earlier question?")
        self.session["on_air"].publish({"kind": "question", "line": "Earlier question?", "message_id": "q1"})
        server.audience.pin_question(self.sid, "q1")
        question = self.session["on_air"].snapshot()["graphic"]
        direction = {**self.direction(), "card_action": "clear"}
        with patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer"), patch("server.propose_graphic", return_value=direction) as model:
            server.make_public_graphic(self.sid, self.session, Path("/unused"), "Current show has moved on.")
        self.assertEqual(model.call_args.kwargs["current_card"]["id"], question["id"])
        self.assertIsNone(self.session["on_air"].snapshot()["graphic"])
        self.assertIsNone(server.audience.selection)

    def test_audience_model_sees_current_question_and_can_revisit_an_earlier_candidate(self):
        self.seed_question("q1", "Earlier question?")
        self.seed_question("q2", "Useful again now?")
        self.session["shown_questions"].update({"q1", "q2"})
        self.session["on_air"].publish({"kind": "question", "line": "Earlier question?", "message_id": "q1"})
        server.audience.pin_question(self.sid, "q1")
        decision = {"message_id": "q2", "decision": "show_now", "surface": "on_air", "reason": "More useful now", "summary": ""}
        with patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer"), patch("server.select_questions", return_value=[decision]) as model:
            server.judge_audience(self.sid, self.session, Path("/unused"))
        self.assertEqual(model.call_args.args[2]["current_question"]["line"], "Earlier question?")
        self.assertIn("q2", {q["id"] for q in model.call_args.args[1]})
        self.assertEqual(self.session["on_air"].snapshot()["graphic"]["message_id"], "q2")
        self.assertEqual(server.audience.selection["message_id"], "q2")

    def test_model_timed_question_expiry_releases_pin_without_claiming_an_answer(self):
        self.seed_question("q1", "Still unanswered?")
        at = time.time()
        public = self.session["on_air"]
        public.publish({"kind": "question", "line": "Still unanswered?", "message_id": "q1"}, at)
        server.audience.pin_question(self.sid, "q1")
        question = public.snapshot(at)["graphic"]
        public.apply_card("keep", hold_seconds=2, expected_id=question["id"], now=at)
        with patch("server.time.time", return_value=at + 3):
            state = server.public_state(self.sid, self.session)
        self.assertIsNone(state["graphic"])
        self.assertIsNone(server.audience.selection)
        self.assertEqual(self.session["direction_revision"], 0, "Expiry is not an answered decision")
        with patch("server.time.time", return_value=at + 121):
            self.assertNotIn("q1", {q["id"] for q in server.audience.poll(self.sid)["questions"]})

    def test_pending_question_keeps_pin_until_it_can_be_displayed(self):
        self.seed_question("q1", "Queued question?")
        at = time.time()
        public = self.session["on_air"]
        public.publish({"kind": "title", "line": "Current topic"}, at)
        public.publish({"kind": "question", "line": "Queued question?", "message_id": "q1"}, at + 1)
        server.audience.pin_question(self.sid, "q1")
        with patch("server.time.time", return_value=at + 2):
            state = server.public_state(self.sid, self.session)
        self.assertEqual(state["graphic"]["kind"], "title")
        self.assertEqual(server.audience.selection["message_id"], "q1")
        with patch("server.time.time", return_value=at + 6):
            state = server.public_state(self.sid, self.session)
        self.assertEqual(state["graphic"]["message_id"], "q1")
        self.assertEqual(server.audience.selection["message_id"], "q1")


if __name__ == "__main__":
    unittest.main()

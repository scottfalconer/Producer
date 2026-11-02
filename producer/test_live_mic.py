"""Live-microphone routing and the authored voice-control fixture; no paid calls."""

import base64
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import server
from test_continuity import BRIEF


class LiveMicrophoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = patch("server.PRIVATE", Path(self.temp.name) / "private")
        self.private.start()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.key_file = Path("/unused-test-key")
        self.http.audience_openai = False
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.headers = {"Origin": self.base, "X-Desk": server.TOKEN}

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
        return requests.post(
            self.base + path, json=data, headers=self.headers, timeout=3
        )

    def get(self, path):
        response = requests.get(self.base + path, timeout=3)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def start(self, **changes):
        response = self.post("/api/start", {"brief": BRIEF, **changes})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_fixed_question_is_pinned_without_recording_or_real_twitch(self):
        with (
            patch("server.rehearsal.Rehearsal", side_effect=AssertionError("No clip")),
            patch("server.TwitchAdapter", side_effect=AssertionError("No Twitch")),
            patch("audience.authorization", side_effect=AssertionError("No OAuth")),
        ):
            started = self.start(
                input_type="microphone", test_question="voice-controls", on_air=True
            )
            sid = started["id"]
            session = server.LIVE[sid]
            self.assertIsNone(session["rehearsal"])
            self.assertEqual(session["production"].adapter.platform, "fixture")
            self.assertEqual(started["test_label"], server.VOICE_TEST_LABEL)
            self.assertTrue(started["audience_required"])
            self.assertFalse(self.http.audience_openai)
            connected = self.post("/api/audience/start", {"id": sid})
            self.assertEqual(connected.status_code, 200, connected.text)
            self.assertIn("Simulated audience", connected.json()["status"])
            questions = self.post("/api/audience/poll", {"id": sid}).json()
            self.assertEqual(len(questions["questions"]), 1)
            public = self.get("/api/on-air")
            self.assertEqual(public["test_label"], server.VOICE_TEST_LABEL)
            self.assertNotIn("test_events", started)
            self.assertNotIn("test_events", questions)
            self.assertEqual(len(public["test_events"]), 1)
            self.assertEqual(public["test_events"][0]["event"], "start")
            self.assertTrue(public["test_events"][0]["has_question"])
            self.assertFalse(public["rehearsal"])
            self.assertIsNone(public["caption"])
            self.assertEqual(public["graphic"]["line"], server.VOICE_QUESTION["text"])
            self.assertEqual(
                public["graphic"]["attribution"], "Mic test · fictional viewer"
            )
            self.assertEqual(questions["active_question"], public["graphic"])
            self.assertTrue(public["graphic"]["awaiting_response"])
            self.assertNotIn("until", public["graphic"])
            # Inbox text expiry must not erase the selected programme question.
            server.audience.questions[0]["received"] -= 121
            self.assertEqual(self.get("/api/on-air")["graphic"], public["graphic"])
            caption = Mock()
            session["captions"] = caption
            session["visual"] = {"kind": "map", "asset_id": "canada"}
            response = self.post("/api/stop", {"id": sid})
            self.assertEqual(response.status_code, 200, response.text)
            caption.close.assert_called_once_with()
            self.assertNotIn(sid, server.LIVE)
            self.assertIsNone(server.audience.owner)
            self.assertEqual(self.get("/api/on-air"), {"graphic": None})

    def test_invalid_fixture_sources_and_ids_have_no_side_effects(self):
        for changes in (
            {"input_type": "desktop"},
            {"input_type": "microphone", "test_question": "arbitrary"},
            {"test_question": "voice-controls", "on_air": True},
            {"input_type": "microphone", "test_question": "voice-controls"},
            {"input_type": "microphone", "rehearsal": "office-hours"},
            {
                "input_type": "microphone",
                "test_question": "voice-controls",
                "on_air": True,
                "rehearsal": "office-hours",
            },
        ):
            with self.subTest(changes=changes):
                response = self.post("/api/start", {"brief": BRIEF, **changes})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertFalse(server.LIVE)
                self.assertIsNone(server.audience.owner)

    def test_ordinary_start_does_not_add_a_question_or_change_source(self):
        started = self.start(offline=False)
        session = server.LIVE[started["id"]]
        self.assertEqual(session["input_type"], "selected-feed")
        self.assertIsNone(session["test_question"])
        self.assertIsNone(started["test_label"])
        self.assertIsNone(session["production"].adapter)
        self.assertIsNone(server.audience.owner)
        self.assertNotIn("test_events", self.get("/api/on-air"))
        server.record_mic_event(session, "direction")
        self.assertNotIn("test_events", session)

    def test_audio_only_windows_require_microphone_and_report_no_frame(self):
        def row(brief, start, end, pcm, frame, *args):
            return {
                "start": start,
                "end": end,
                "available": end,
                "speech": "",
                "cue": None,
                "frame_sampled": bool(frame),
            }

        audio = base64.b64encode(b"test-browser-audio").decode()
        with (
            patch("server._read_key", return_value="test-key"),
            patch("server.OpenAIProducer"),
            patch("server.extract", return_value=b"test-pcm") as extract,
            patch("server.produce", side_effect=row) as produce,
        ):
            started = self.start(input_type="microphone")
            for n, frame in enumerate(({}, {"frame": ""})):
                response = self.post(
                    "/api/window",
                    {
                        "id": started["id"],
                        "start": n / 10,
                        "end": (n + 1) / 10,
                        "audio": audio,
                        **frame,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["frame_sampled"])
                self.assertEqual(produce.call_args.args[4], b"")
            self.assertEqual(extract.call_count, 2)
            self.post("/api/stop", {"id": started["id"]})
            produce.reset_mock()
            extract.reset_mock()
            started = self.start()
            response = self.post(
                "/api/window",
                {"id": started["id"], "start": 0, "end": 0.1, "audio": audio},
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"], "Invalid sampled frame.")
            produce.assert_not_called()
            extract.assert_not_called()

    def test_bad_nonempty_frames_still_fail_for_microphone(self):
        started = self.start(input_type="microphone")
        with patch("server.produce") as produce:
            response = self.post(
                "/api/window",
                {
                    "id": started["id"],
                    "start": 0,
                    "end": 0.1,
                    "audio": "AA==",
                    "frame": base64.b64encode(b"not-a-jpeg").decode(),
                },
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"], "Invalid sampled frame.")
            produce.assert_not_called()

    def test_local_map_then_skip_share_the_same_public_question(self):
        started = self.start(
            input_type="microphone", test_question="voice-controls", on_air=True
        )
        sid = started["id"]
        session = server.LIVE[sid]
        question = self.get("/api/on-air")["graphic"]
        with server.LOCK:
            server.apply_direction(
                sid, session, {"question_status": "keep", "command": "show_map"}
            )
        mapped = self.get("/api/on-air")
        self.assertEqual(mapped["graphic"], question)
        self.assertEqual(mapped["visual"]["asset_id"], "canada")
        map_event = mapped["test_events"][-1]
        self.assertEqual(map_event["command"], "show_map")
        self.assertTrue(map_event["has_question"])
        self.assertTrue(map_event["has_visual"])
        self.assertFalse(map_event["resolution_applied"])
        self.assertEqual(
            self.post("/api/audience/poll", {"id": sid}).json()["active_question"],
            question,
        )
        with server.LOCK:
            server.apply_direction(
                sid,
                session,
                {
                    "question_status": "skipped",
                    "question_id": question["id"],
                    "command": "none",
                },
            )
        skipped = self.get("/api/on-air")
        self.assertIsNone(skipped["graphic"])
        self.assertEqual(skipped["visual"], mapped["visual"])
        skip_event = skipped["test_events"][-1]
        self.assertEqual(skip_event["question_status"], "skipped")
        self.assertTrue(skip_event["resolution_applied"])
        self.assertEqual(skip_event["reason"], "semantic_resolution")
        self.assertFalse(skip_event["has_question"])
        self.assertIsNone(
            self.post("/api/audience/poll", {"id": sid}).json()["active_question"]
        )

    def test_audience_disconnect_withdraws_the_pinned_question(self):
        started = self.start(
            input_type="microphone", test_question="voice-controls", on_air=True
        )
        self.assertIsNotNone(self.get("/api/on-air")["graphic"])
        response = self.post("/api/audience/stop", {"id": started["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        ended = self.get("/api/on-air")
        self.assertIsNone(ended["graphic"])
        self.assertEqual(ended["test_events"][-2]["event"], "audience_stop")
        self.assertEqual(ended["test_events"][-1]["event"], "question_withdrawn")
        self.assertEqual(
            ended["test_events"][-1]["reason"], "audience_disconnected"
        )

    def test_selection_invalidation_and_clear_all_have_distinct_reasons(self):
        started = self.start(
            input_type="microphone", test_question="voice-controls", on_air=True
        )
        sid = started["id"]
        server.audience.selection["valid"] = False
        state = self.get("/api/on-air")
        self.assertEqual(state["test_events"][-1]["reason"], "selection_invalid")
        self.assertIsNone(state["graphic"])
        self.post("/api/stop", {"id": sid})
        started = self.start(
            input_type="microphone", test_question="voice-controls", on_air=True
        )
        sid = started["id"]
        with server.LOCK:
            server.apply_direction(
                sid,
                server.LIVE[sid],
                {"question_status": "keep", "command": "clear_all"},
            )
        state = self.get("/api/on-air")
        self.assertIsNone(state["graphic"])
        self.assertEqual(state["test_events"][-1]["reason"], "clear_all")
        self.assertFalse(state["test_events"][-1]["resolution_applied"])

    def test_diagnostics_are_bounded_content_free_and_capture_failed_resolution(self):
        started = self.start(
            input_type="microphone", test_question="voice-controls", on_air=True
        )
        sid = started["id"]
        secret_content = "Untrusted source words or names must never enter diagnostics"
        with server.LOCK:
            for _ in range(35):
                server.apply_direction(
                    sid,
                    server.LIVE[sid],
                    {
                        "question_status": "answered",
                        "question_id": "not-the-current-question",
                        "command": "none",
                        "command_quote": secret_content,
                        "question_quote": secret_content,
                        "reason": secret_content,
                        "transcript": secret_content,
                    },
                )
        state = self.get("/api/on-air")
        events = state["test_events"]
        self.assertEqual(len(events), 30)
        self.assertNotIn(secret_content, json.dumps(events))
        self.assertNotIn(server.VOICE_QUESTION["text"], json.dumps(events))
        self.assertNotIn(server.VOICE_QUESTION["author"], json.dumps(events))
        allowed = {
            "event", "elapsed_s", "command", "question_status", "resolution_applied",
            "changed", "revision", "has_question", "has_visual", "reason",
        }
        for event in events:
            self.assertLessEqual(set(event), allowed)
            self.assertEqual(event["question_status"], "answered")
            self.assertFalse(event["resolution_applied"])
            self.assertFalse(event["changed"])
            self.assertTrue(event["has_question"])
            self.assertNotIn("reason", event)
            self.assertGreaterEqual(event["elapsed_s"], 0)

    def test_poll_detects_unattributed_question_loss_without_fabricating_a_reason(self):
        started = self.start(
            input_type="microphone", test_question="voice-controls", on_air=True
        )
        self.get("/api/on-air")
        server.LIVE[started["id"]]["on_air"].clear_output()
        state = self.get("/api/on-air")
        self.assertIsNone(state["graphic"])
        self.assertEqual(
            state["test_events"][-1]["reason"], "unattributed_state_change"
        )
        count = len(state["test_events"])
        self.assertEqual(len(self.get("/api/on-air")["test_events"]), count)


if __name__ == "__main__":
    unittest.main()

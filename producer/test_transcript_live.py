"""Transcript lifecycle through actual local HTTP, with all inference mocked."""

import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests

import server
from test_continuity import BRIEF
import test_cycle_latency
from test_engine import PCM


class TranscriptLiveTests(unittest.TestCase):
    setUp = test_cycle_latency.PublicCycleTests.setUp
    tearDown = test_cycle_latency.PublicCycleTests.tearDown
    post = test_cycle_latency.PublicCycleTests.post

    def window(self):
        return self.post("/api/window", {"id": self.sid, "start": 0, "end": 8, "audio": "AA=="})

    def rows(self):
        return [json.loads(line) for line in self.session["transcript"].path.read_text().splitlines()]

    def test_asr_is_saved_before_later_continuity_failure(self):
        api = Mock()
        api.transcribe.return_value = "We raised an unresolved question about costs."
        with (
            patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer", return_value=api),
            patch("server.extract", return_value=PCM),
            patch("server.dispatch_public") as public,
            patch("continuity.structured", side_effect=RuntimeError("Private inference failed")),
        ):
            response = self.window()
        self.assertEqual(response.status_code, 400)
        segments = [r for r in self.rows() if r["kind"] == "segment"]
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], api.transcribe.return_value)
        self.assertEqual(segments[0]["status"], "transcribed")
        self.assertTrue(any(r.get("status") == "continuity_failed_after_transcription" for r in self.rows()))
        self.assertEqual(public.call_args.args[3], api.transcribe.return_value)
        self.assertEqual(len(public.call_args.args), 6)  # Current speech only, no stored transcript.

    def test_stop_before_asr_returns_discards_late_speech_and_retains_gap(self):
        started, release = threading.Event(), threading.Event()

        def transcribe(_):
            started.set()
            self.assertTrue(release.wait(2))
            return "This late result must not be saved or published."

        api = Mock()
        api.transcribe.side_effect = transcribe
        response = []
        with (
            patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer", return_value=api),
            patch("server.extract", return_value=PCM), patch("server.dispatch_public") as public,
            patch("continuity.structured") as private,
        ):
            worker = threading.Thread(target=lambda: response.append(self.window()))
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                stopped = self.post("/api/stop", {"id": self.sid, "reason": "ended", "through": 8})
                self.assertEqual(stopped.status_code, 200, stopped.text)
            finally:
                release.set()
                worker.join(timeout=2)
        self.assertEqual(response[0].status_code, 400)
        self.assertNotIn("late result", self.session["transcript"].path.read_text())
        self.assertTrue(any(r.get("kind") == "gap" and r["end"] == 8 for r in self.rows()))
        public.assert_not_called()
        private.assert_not_called()

    def test_pause_resume_reuses_file_then_end_while_paused_finalizes(self):
        self.session["transcript"].record(0, 8, "Accepted first speech.")
        show_id = self.session["show_id"]
        stopped = self.post("/api/stop", {"id": self.sid, "reason": "paused", "through": 10})
        self.assertEqual(stopped.status_code, 200, stopped.text)
        resumed = self.post("/api/start", {
            "brief": BRIEF, "on_air": True, "input_type": "microphone", "show_id": show_id, "offset": stopped.json()["transcript"]["through"],
        })
        self.assertEqual(resumed.status_code, 200, resumed.text)
        new_sid = resumed.json()["id"]
        self.assertEqual(server.LIVE[new_sid]["transcript"].path, self.session["transcript"].path)
        self.assertEqual(server.LIVE[new_sid]["transcript"].context(10, "first")["excerpts"][0]["text"], "Accepted first speech.")
        active_end = self.post("/api/stop", {"show_id": show_id, "reason": "ended", "through": 10})
        self.assertEqual(active_end.status_code, 400)
        self.assertIn(new_sid, server.LIVE)
        paused = self.post("/api/stop", {"id": new_sid, "reason": "paused", "through": 10})
        self.assertEqual(paused.status_code, 200, paused.text)
        ended = self.post("/api/stop", {"show_id": show_id, "reason": "ended", "through": 10})
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(ended.json()["transcript"]["last_stop_reason"], "ended")
        refused = self.post("/api/start", {
            "brief": BRIEF, "input_type": "microphone", "show_id": show_id, "offset": 10,
        })
        self.assertEqual(refused.status_code, 400)
        self.assertTrue(self.session["transcript"].path.exists())

    def test_transcript_save_failure_blocks_public_and_private_inference(self):
        api = Mock()
        api.transcribe.return_value = "Speech that cannot be saved."
        with (
            patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer", return_value=api),
            patch("server.extract", return_value=PCM),
            patch.object(self.session["transcript"], "record", side_effect=ValueError("The local show transcript could not be saved. Monitoring must pause.")),
            patch("server.dispatch_public") as public, patch("continuity.structured") as private,
        ):
            response = self.window()
        self.assertEqual(response.status_code, 400)
        self.assertIn("transcript could not be saved", response.json()["error"])
        self.assertFalse(self.session["busy"])
        public.assert_not_called()
        private.assert_not_called()

    def test_stop_storage_failure_still_releases_audience_and_captions(self):
        caption = Mock()
        self.session["captions"] = caption
        with (
            patch.object(self.session["transcript"], "stop", side_effect=ValueError("Transcript save failed")),
            patch.object(server.audience, "stop", wraps=server.audience.stop) as audience_stop,
        ):
            response = self.post("/api/stop", {"id": self.sid, "through": 8})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(self.sid, server.LIVE)
        self.assertTrue(self.session["on_air"].closed)
        caption.close.assert_called_once()
        audience_stop.assert_called_once()
        self.assertIsNone(server.audience.owner)

    def test_no_text_quiet_decode_failure_and_private_transcript_summary(self):
        with patch("server.extract", side_effect=ValueError("Decode failed")):
            response = self.window()
        self.assertEqual(response.status_code, 400)
        failed = [r for r in self.rows() if r.get("kind") == "segment"]
        self.assertEqual(failed[0]["status"], "failed")
        self.assertEqual(failed[0]["text"], "")
        on_air = requests.get(self.base + "/api/on-air", timeout=3).json()
        self.assertNotIn("transcript", on_air)
        self.assertNotIn(str(self.session["transcript"].path), json.dumps(on_air))

    def test_quiet_and_empty_asr_have_distinct_saved_window_statuses(self):
        api = Mock()
        api.transcribe.return_value = ""
        with (
            patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer", return_value=api),
            patch("server.extract", return_value=b"\x00\x00" * 16000 * 8),
        ):
            quiet = self.window()
        self.assertEqual(quiet.status_code, 200, quiet.text)
        api.transcribe.assert_not_called()
        self.session["created"] -= 8
        with (
            patch("server._read_key", return_value="unused"), patch("server.OpenAIProducer", return_value=api),
            patch("server.extract", return_value=PCM),
        ):
            empty = self.post("/api/window", {"id": self.sid, "start": 8, "end": 16, "audio": "AA=="})
        self.assertEqual(empty.status_code, 200, empty.text)
        api.transcribe.assert_called_once()
        self.assertEqual([r["status"] for r in self.rows() if r["kind"] == "segment"], ["quiet_not_transcribed", "no_text"])

    def test_stale_source_storage_failure_cannot_leave_audience_running(self):
        caption = Mock()
        self.session["captions"] = caption
        self.session["seen"] -= 130
        with (
            patch.object(self.session["transcript"], "stop", side_effect=ValueError("Transcript finalization failed")),
            patch.object(server.audience, "stop", wraps=server.audience.stop) as audience_stop,
        ):
            response = self.post("/api/start", {"brief": BRIEF, "input_type": "microphone"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(self.sid, server.LIVE)
        self.assertTrue(self.session["on_air"].closed)
        caption.close.assert_called_once()
        audience_stop.assert_called_once()
        self.assertIsNone(server.audience.owner)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import threading
import unittest
import io
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import requests
from PIL import Image

from audience import Audience
from rehearsal import Rehearsal, MockTwitch, CLIP
from test_continuity import BRIEF


class RehearsalTests(unittest.TestCase):
    def test_screen_chat_stays_in_pixels_without_connecting_structured_inbox(self):
        events = [{"type": "channel.chat.message", "at": .5, "id": "m1",
                   "author": "Maya · simulated viewer", "user_id": "m1", "text": "What is the prop?"}]
        inbox = Audience()
        with patch("rehearsal.CLIP"):
            demo = Rehearsal(inbox, "screen-fixture", chat_events=events)
        demo.connect()
        demo.advance(90)
        self.assertIsNone(inbox.owner)
        self.assertFalse(inbox.connected)
        self.assertEqual(inbox.questions, [])
        output = io.BytesIO()
        Image.new("RGB", (852, 480), "blue").save(output, "JPEG")
        with patch("rehearsal.extract", return_value=output.getvalue()):
            frame = demo.frame_at(1, available_through=1)
        with Image.open(io.BytesIO(frame)) as image:
            self.assertEqual(image.size, (1280, 720))
        with patch("rehearsal.extract", side_effect=AssertionError("No future frame")):
            with self.assertRaisesRegex(ValueError, "not yet available"):
                demo.frame_at(1, available_through=.9)

    @unittest.skipUnless(CLIP.exists(), "Local owner-selected media is not installed")
    def test_installed_fixture_decodes_audio_frame_and_captions(self):
        demo = Rehearsal(Audience(), "fixture")
        pcm, frame = demo.cut(0, 15)
        self.assertEqual(len(pcm), 15 * 16000 * 2)
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        self.assertEqual(len(demo.caption_audio(0, 0, 1)), 48000)

    def test_fictional_event_shapes_deduplication_and_moderation(self):
        inbox = Audience()
        with (
            patch("rehearsal.CLIP"),
            patch(
                "audience.authorization",
                side_effect=AssertionError("Real Twitch auth forbidden"),
            ),
        ):
            demo = Rehearsal(inbox, "fixture")
            demo.connect()
            demo.advance(90)
            before = inbox.poll("fixture")["questions"]
            self.assertTrue(before)
            self.assertTrue(all("simulated viewer" in q["author"] for q in before))
            demo.advance(90)
            self.assertEqual(before, inbox.poll("fixture")["questions"])
            demo.advance(95)
            self.assertNotIn(
                "c08", [q["id"] for q in inbox.poll("fixture")["questions"]]
            )
            demo.advance(156)
            self.assertNotIn(
                "c12", [q["id"] for q in inbox.poll("fixture")["questions"]]
            )
            demo.advance(180)
            self.assertEqual(inbox.poll("fixture")["questions"], [])
            inbox.stop()
            demo.advance(180)
            self.assertFalse(inbox.connected)

    def test_pinned_source_and_no_future_cut(self):
        with patch("rehearsal.CLIP"):
            demo = Rehearsal(Audience(), "fixture")
        with (
            patch("rehearsal.bounded_audio", return_value=b"pcm") as audio,
            patch("rehearsal.extract", return_value=b"jpg"),
        ):
            self.assertEqual(demo.cut(15, 30), (b"pcm", b"jpg"))
            audio.assert_called_once_with(CLIP, 15, 30, 30)
            for start, end in ((0, 181), (-1, 1), (0, 45), (10, 5)):
                with self.assertRaises(ValueError):
                    demo.cut(start, end)

    def test_mock_actions_have_no_network_transport(self):
        with patch("requests.request", side_effect=AssertionError("No network")):
            mock = MockTwitch()
            result = mock.execute(
                {"id": "ref", "kind": "reference", "text": "https://example.com"}
            )
            self.assertEqual(result["state"], "done")
            self.assertIn("SIMULATED", result["detail"])
            self.assertEqual(len(mock.receipts), 1)

    def test_caption_input_is_pinned_and_cannot_read_future_audio(self):
        with patch("rehearsal.CLIP"):
            demo = Rehearsal(Audience(), "fixture")
        with patch("rehearsal.extract", return_value=b"pcm") as extract:
            self.assertEqual(demo.caption_audio(2, 15, 18), b"pcm")
            self.assertIn(str(CLIP), extract.call_args.args[0])
            for seq, offset, elapsed in (
                (-1, 0, 10),
                (0, 0, 0.9),
                (180, 0, 181),
                (True, 0, 10),
            ):
                with self.assertRaises(ValueError):
                    demo.caption_audio(seq, offset, elapsed)

    def test_http_demo_start_cannot_construct_real_twitch_adapter(self):
        import server

        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        http.key_file = Path("/unused-test-key")
        http.audience_openai = False
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        headers = {"Origin": base, "X-Desk": server.TOKEN}
        sid = None
        try:
            with (
                tempfile.TemporaryDirectory() as private,
                patch("server.PRIVATE", Path(private)),
                patch("rehearsal.CLIP"),
                patch(
                    "server.TwitchAdapter",
                    side_effect=AssertionError("Real Twitch forbidden"),
                ),
                patch(
                    "audience.authorization",
                    side_effect=AssertionError("Real OAuth forbidden"),
                ),
            ):
                r = requests.post(
                    base + "/api/start",
                    json={"brief": BRIEF, "rehearsal": "office-hours"},
                    headers=headers,
                    timeout=3,
                )
                self.assertEqual(r.status_code, 200, r.text)
                sid = r.json()["id"]
                self.assertEqual(
                    server.LIVE[sid]["production"].adapter.platform, "fixture"
                )
                self.assertTrue(
                    requests.get(base + "/api/rehearsal", timeout=3).json()["running"]
                )
                self.assertTrue(
                    requests.post(
                        base + "/api/stop", json={"id": sid}, headers=headers, timeout=3
                    ).json()["stopped"]
                )
                self.assertFalse(
                    requests.get(base + "/api/rehearsal", timeout=3).json()["running"]
                )
                self.assertEqual(
                    json.loads(requests.get(base + "/api/on-air", timeout=3).text),
                    {"graphic": None},
                )
        finally:
            if sid:
                server.LIVE.pop(sid, None)
            server.audience.stop()
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)

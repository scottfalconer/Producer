"""Passive editorial output and pinned owner materials; no inference or Twitch calls."""

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
from captions import CaptionSession
from test_continuity import BRIEF


class PassiveShowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.private = patch("server.PRIVATE", self.folder / "private")
        self.private.start()
        (self.folder / "notes.md").write_text("Private show notes: explain the architecture.")
        Image.new("RGB", (120, 80), "blue").save(self.folder / "Architecture.png")
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.key_file = Path("/unused-test-key")
        self.http.audience_openai = False
        self.http.show_folder = self.folder
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
        return requests.post(self.base + path, json=data, headers=self.headers, timeout=3)

    def get(self, path):
        return requests.get(self.base + path, timeout=3)

    def start(self, **changes):
        response = self.post(
            "/api/start", {"brief": BRIEF, "on_air": True, "input_type": "microphone", **changes}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def direction(self, **changes):
        return {
            "context": None, "graphic": None, "question_status": "keep", "question_id": "",
            "visual_action": "keep", "visual": None, **changes,
        }

    def run_editorial(self, sid, direction):
        with (
            patch("server._read_key", return_value="test-key"),
            patch("server.OpenAIProducer"),
            patch("server.propose_graphic", side_effect=direction if callable(direction) else None,
                  return_value=None if callable(direction) else direction) as propose,
        ):
            server.make_public_graphic(sid, server.LIVE[sid], Path("/unused"), "Current programme speech")
            return propose

    def test_materials_summary_is_private_and_images_are_pinned_to_active_show(self):
        response = self.get("/api/show-materials")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["image_count"], 1)
        self.assertIn("Private show notes", response.json()["notes"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        started = self.start(show_folder="/ignored/request/path")
        sid = started["id"]
        materials = server.LIVE[sid]["materials"]
        item = materials.catalog[0]
        asset_path = "/show-assets/" + item["id"]
        self.assertEqual(started["show_materials"]["folder"], str(self.folder))
        self.assertNotIn("notes", started["show_materials"])
        before = self.get(asset_path)
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.headers["Content-Type"], "image/jpeg")
        self.assertEqual(before.headers["Cache-Control"], "no-store")
        Image.new("RGB", (120, 80), "red").save(self.folder / "Architecture.png")
        (self.folder / "notes.md").write_text("Changed after Start")
        self.assertEqual(self.get(asset_path).content, before.content)
        self.assertIn("Private show notes", materials.notes)
        for path in ("/show-assets/notes.md", "/show-assets/../notes.md", "/show-assets/unknown"):
            self.assertEqual(self.get(path).status_code, 404)
        self.post("/api/stop", {"id": sid})
        self.assertEqual(self.get(asset_path).status_code, 404)

    def test_default_missing_folder_is_optional_and_no_request_path_is_read(self):
        missing = self.folder / "missing-default"
        with patch("server.DEFAULT_SHOW_FOLDER", missing):
            self.http.show_folder = missing
            response = self.get("/api/show-materials")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["image_count"], 0)
            self.assertEqual(response.json()["notes"], "")
            self.assertEqual(self.start()["show_materials"]["image_count"], 0)

    def test_subtitle_audio_has_no_request_detection_callback(self):
        started = self.start(captions=True)
        caption = CaptionSession.__new__(CaptionSession)
        caption.lock = threading.RLock()
        caption.closed = False
        caption.items, caption.retired = {}, set()
        caption.order, caption.current = 0, None
        caption.close = Mock()

        def connect(key, on_text=None):
            caption.on_text = on_text
            return caption

        def append(sequence, audio):
            caption.accept({
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "subtitle-turn", "delta": "Producer, bring up a map of Canada.",
            })

        caption.append = Mock(side_effect=append)
        with (
            patch("server._read_key", return_value="test-key"),
            patch("server.CaptionSession", side_effect=connect) as construct,
            patch("server.offer_speech", side_effect=AssertionError("Request detection is shelved")) as offer,
            patch("server.direct_speech", side_effect=AssertionError("Request detection is shelved")) as direct,
        ):
            response = self.post("/api/captions/start", {"id": started["id"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(construct.call_args.kwargs["on_text"])
            response = self.post("/api/captions/audio", {
                "id": started["id"], "sequence": 0,
                "audio": base64.b64encode(b"\x00\x00" * 20).decode(),
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Producer", caption.current["text"])
            offer.assert_not_called()
            direct.assert_not_called()
            self.assertIsNone(server.LIVE[started["id"]]["visual"])

    def test_asset_selection_uses_catalogue_not_model_urls_and_never_private_notes(self):
        started = self.start()
        sid = started["id"]
        item = server.LIVE[sid]["materials"].catalog[0]
        proposal = self.direction(visual_action="show", visual={
            "kind": "asset", "asset_id": item["id"], "src": "https://untrusted.example/image",
            "title": "Untrusted model title",
        })
        called = self.run_editorial(sid, proposal)
        self.assertEqual(called.call_args.kwargs["catalog"], [item])
        self.assertTrue(called.call_args.kwargs["contact_sheet"].startswith(b"\xff\xd8"))
        self.assertNotIn("notes", called.call_args.kwargs)
        state = self.get("/api/on-air").json()
        visual = state["visual"]
        self.assertEqual(visual["src"], "/show-assets/" + item["id"])
        self.assertEqual(visual["title"], item["title"])
        self.assertEqual(visual["alt"], item["title"])
        self.assertGreater(visual["until"], time.time() + 40)
        until = visual["until"]
        self.run_editorial(sid, self.direction())
        self.assertEqual(self.get("/api/on-air").json()["visual"]["until"], until)
        self.run_editorial(sid, self.direction(visual_action="show", visual={
            "kind": "asset", "asset_id": "not-in-catalog",
        }))
        self.assertEqual(self.get("/api/on-air").json()["visual"]["until"], until)
        self.run_editorial(sid, self.direction(visual_action="clear"))
        self.assertIsNone(self.get("/api/on-air").json()["visual"])

    def test_stopped_or_late_editorial_result_cannot_change_public_output(self):
        started = self.start()
        sid = started["id"]
        session = server.LIVE[sid]
        item = session["materials"].catalog[0]

        def stopped(*args, **kwargs):
            server.LIVE.pop(sid)
            session["on_air"].close()
            return self.direction(visual_action="show", visual={"kind": "asset", "asset_id": item["id"]})

        self.run_editorial(sid, stopped)
        self.assertIsNone(session["visual"])
        started = self.start()
        sid = started["id"]
        with (
            patch("server._read_key", return_value="test-key"), patch("server.OpenAIProducer"),
            patch("server.propose_graphic", return_value=self.direction(visual_action="show", visual={
                "kind": "asset", "asset_id": server.LIVE[sid]["materials"].catalog[0]["id"],
            })),
        ):
            server.LIVE[sid]["created"] -= 100
            server.make_public_graphic(sid, server.LIVE[sid], Path("/unused"), "Earlier speech", 0)
        self.assertIsNone(server.LIVE[sid]["visual"])

    def test_unapproved_real_viewer_question_and_recent_text_never_enter_editorial_model(self):
        started = self.start(offline=False)
        sid = started["id"]
        session = server.LIVE[sid]
        server.audience.owner, server.audience.connected = sid, True
        server.audience.questions = [{
            "id": "real-viewer-question", "user_id": "private-viewer-id", "author": "Viewer",
            "text": "Unapproved viewer message", "received": time.time(),
        }]
        session["on_air"].publish({
            "kind": "question", "line": "Unapproved viewer message", "message_id": "real-viewer-question",
        })
        server.audience.pin_question(sid, "real-viewer-question")
        question = session["on_air"].snapshot()["graphic"]
        proposed = self.direction(question_status="answered", question_id=question["id"])
        called = self.run_editorial(sid, proposed)
        self.assertIsNone(called.call_args.kwargs["question"])
        self.assertEqual(called.call_args.args[2], [])
        self.assertEqual(session["on_air"].snapshot()["graphic"], question)

    def test_passive_answer_only_resolves_the_examined_synthetic_question(self):
        started = self.start(test_question="voice-controls")
        sid = started["id"]
        session = server.LIVE[sid]
        question = session["on_air"].snapshot()["graphic"]
        called = self.run_editorial(sid, self.direction(question_status="answered", question_id="wrong-id"))
        self.assertEqual(called.call_args.kwargs["question"], question)
        self.assertEqual(session["on_air"].snapshot()["graphic"], question)
        self.run_editorial(sid, self.direction(question_status="answered", question_id=question["id"]))
        self.assertIsNone(session["on_air"].snapshot()["graphic"])
        self.assertIsNone(server.audience.selection)

    def test_window_spanning_question_display_cannot_resolve_it(self):
        started = self.start(test_question="voice-controls")
        sid = started["id"]
        session = server.LIVE[sid]
        question = session["on_air"].snapshot()["graphic"]
        session["wall_created"] = question["displayed_at"] - 5
        proposal = self.direction(question_status="answered", question_id=question["id"])
        with (
            patch("server._read_key", return_value="test-key"), patch("server.OpenAIProducer"),
            patch("server.propose_graphic", return_value=proposal) as propose,
        ):
            server.make_public_graphic(sid, session, Path("/unused"), "Earlier speech", 1, 0)
            self.assertIsNone(propose.call_args.kwargs["question"])
            self.assertEqual(session["on_air"].snapshot()["graphic"], question)
            server.make_public_graphic(sid, session, Path("/unused"), "A later answer", 7, 6)
            self.assertEqual(propose.call_args.kwargs["question"], question)
            self.assertIsNone(session["on_air"].snapshot()["graphic"])


if __name__ == "__main__":
    unittest.main()

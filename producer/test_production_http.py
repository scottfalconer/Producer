"""Active-show production receipts reach chat pixels through real local HTTP routes."""

import copy
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

import server
from audience import Audience
from director_runtime import ShowDirector, note_brief
from screen_chat_overlay import _visible, overlay_png
from test_director_runtime import FakeDirector


class ProductionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.audience = Audience()
        self.addCleanup(self.audience.stop)
        self.patches = [
            patch("server.PRIVATE", self.folder / "private"),
            patch("server.LIVE", {}),
            patch("server.TRANSCRIPTS", {}),
            patch("server.audience", self.audience),
            patch("server._read_key", return_value="unused-fixture-key"),
            patch("server.ShowDirector", side_effect=lambda *args, **kwargs:
                  ShowDirector(*args, director_class=FakeDirector, **kwargs)),
            patch("server.TwitchAdapter", side_effect=AssertionError("No real Twitch transport")),
            patch("server.OpenAIProducer", side_effect=AssertionError("No paid inference")),
            patch("server.rehearsal.EventFixture", side_effect=AssertionError("No hidden fixture inbox")),
            patch.object(self.audience, "start", side_effect=AssertionError("No Twitch inbox")),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.key_file = Path("/unused-key")
        self.http.show_folder = self.folder
        self.http.audience_openai = False
        self.http.chat_fixture_override = {
            "id": "no-fixture-comments", "source": "Authored empty test timeline",
            "synthetic": True, "duration": 600, "events": [],
        }
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.headers = {"Origin": self.base, "X-Desk": server.TOKEN}
        original_request = requests.sessions.Session.request

        def only_loopback(session, method, url, *args, **kwargs):
            if not url.startswith(self.base + "/"):
                raise AssertionError("Tests may only contact their isolated loopback helper")
            return original_request(session, method, url, *args, **kwargs)

        self.network = patch("requests.sessions.Session.request", new=only_loopback)
        self.network.start()
        self.addCleanup(self.network.stop)

    def tearDown(self):
        try:
            for sid in list(server.LIVE):
                self.post("/api/stop", {"id": sid})
        finally:
            self.http.shutdown()
            self.http.server_close()
            self.thread.join(timeout=2)

    def post(self, path, body):
        response = requests.post(self.base + path, json=body, headers=self.headers, timeout=3)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def png(self, query=""):
        response = requests.get(self.base + "/api/simulated-chat.png" + query, timeout=3)
        self.assertEqual(response.status_code, 200, response.text[:100])
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.content

    def start(self, offset=20):
        notes = "PRIVATE_SHOW_NOTES. Explain the useful project example."
        started = self.post("/api/start", {
            "brief": note_brief(notes), "notes": notes, "runtime": "realtime",
            "input_type": "microphone", "on_air": True, "offset": offset,
        })
        self.assertTrue(started["offline"])
        self.assertFalse(started["audience_required"])
        self.assertEqual(started["audience_input"], "screen")
        self.session = server.LIVE[started["id"]]
        self.assertEqual(self.session["production_policy"], "realtime-production-v1")
        self.assertEqual(self.session["producer_posts"], [])
        self.assertIsNone(self.session["events"])
        self.assertEqual(self.session["production"].adapter.platform, "fixture")
        self.post("/api/director/start", {"id": started["id"]})
        self.director = self.session["director"]
        self.assertIn("production", self.director.core.callbacks)
        self.assertEqual(self.director.state()["audience"], [])
        return started

    def display(self):
        return self.director.core.callbacks["apply"]({
            "confidence": {"action": "show", "line": "PRIVATE_PRESENTER_CUE"},
            "card": {"action": "show", "kind": "reference",
                     "line": "Project: https://github.com/scottfalconer/Producer",
                     "attribution": "Producer project"},
        }, self.director.state())["state"]

    def share(self, state):
        return self.director.core.callbacks["production"]({
            "action": "share_on_air",
            "output_key": state["production"]["shareable_outputs"][0]["output_key"],
        }, state)

    def test_only_confirmed_actual_post_enters_active_http_overlay(self):
        started = self.start()
        state = self.display()
        empty_png = self.png()
        self.assertEqual(empty_png, overlay_png([], 20))
        self.assertEqual(self.session["producer_posts"], [], "Displaying a card is not a chat delivery")
        adapter = self.session["production"].adapter
        execute = adapter.execute
        pending, release = threading.Event(), threading.Event()
        results, errors = [], []

        def delayed(plan):
            pending.set()
            if not release.wait(2):
                raise AssertionError("The delivery test did not release its pending action")
            return execute(plan)

        def send():
            try:
                results.append(self.share(state))
            except Exception as exc:
                errors.append(exc)

        adapter.execute = delayed
        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        try:
            self.assertTrue(pending.wait(1))
            self.assertEqual(self.png(), empty_png, "An in-flight write has no confirmed pixels")
            self.assertEqual(self.session["producer_posts"], [])
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        result = results[0]
        self.assertTrue(result["ok"])
        receipt = result["receipt"]
        self.assertEqual(receipt["state"], "done")
        self.assertTrue(receipt["simulated"])
        self.assertEqual(receipt["sender_login"], "producer")
        posts = self.session["producer_posts"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "Producer")
        self.assertEqual(posts[0]["text"], receipt["message"])
        self.assertNotIn("PRIVATE_", posts[0]["text"])
        self.session["created"] = time.monotonic() - 1
        self.assertEqual(self.png(), overlay_png(posts, 21))
        self.assertNotEqual(self.png(), empty_png)
        self.assertEqual(self.session["simulated_chat"]["events"], [], "The authored input fixture is unchanged")
        self.assertEqual(self.audience.questions, [])
        self.assertEqual(self.director.screen_chat.questions, [])
        self.assertEqual(self.director.state()["audience"], [], "Outgoing pixels are not inserted as an audience question")
        self.post("/api/stop", {"id": started["id"]})
        self.assertEqual(self.png(), overlay_png([], 12), "An ended session cannot leak its delivery to the page")
        self.start()
        self.assertEqual(self.png(), empty_png, "The next show starts without the previous show's Producer posts")

    def test_uncertain_delivery_never_appears_in_http_overlay_or_retries(self):
        self.start()
        state = self.display()
        empty_png = self.png()
        adapter = self.session["production"].adapter
        adapter.execute = Mock(return_value={
            "state": "uncertain", "detail": "No delivery confirmation",
            "message": state["production"]["shareable_outputs"][0]["text"],
            "sender_id": "synthetic-producer", "sender_login": "producer", "display_name": "Producer",
        })
        result = self.share(state)
        self.assertFalse(result["ok"])
        self.assertEqual(result["receipt"]["state"], "uncertain")
        self.assertEqual(self.session["producer_posts"], [])
        self.assertEqual(self.png(), empty_png)
        repeated = self.share(result["state"])
        self.assertTrue(repeated["duplicate"])
        self.assertFalse(repeated["ok"])
        adapter.execute.assert_called_once()
        self.assertEqual(self.png(), empty_png)
        self.assertEqual(self.director.state()["audience"], [])

    def test_actual_post_after_fixture_end_is_visible_and_old_rows_expire(self):
        old = {"at": 520, "type": "channel.chat.message", "id": "old-fixture-message",
               "author": "Maya · simulated viewer", "user_id": "synthetic-maya", "text": "An old fixture comment"}
        self.http.chat_fixture_override["events"] = [old]
        self.start(offset=620)
        self.assertEqual(self.session["simulated_chat"]["duration"], 600)
        self.assertEqual(self.png(), overlay_png([], 620), "Fixture messages must keep aging beyond source600")
        result = self.share(self.display())
        self.assertTrue(result["ok"])
        posts = copy.deepcopy(self.session["producer_posts"])
        self.assertEqual(posts[0]["at"], 620.001)
        self.session["created"] = time.monotonic() - 1
        with patch("screen_chat_overlay.overlay_png", wraps=overlay_png) as painter:
            actual_png = self.png()
        timeline, elapsed = painter.call_args.args
        self.assertGreater(elapsed, 620)
        self.assertEqual(_visible(timeline["events"], elapsed), posts)
        self.assertIn(old, timeline["events"], "Old input stays available to the painter but must expire naturally")
        self.assertEqual(actual_png, overlay_png(posts, elapsed))
        self.assertNotEqual(actual_png, overlay_png([], elapsed))
        self.assertEqual(self.director.state()["audience"], [])
        invalid_preview = requests.get(self.base + "/api/simulated-chat.png?preview=1&at=620", timeout=3)
        self.assertEqual(invalid_preview.status_code, 400, "Explicit fixture preview keeps its source bound")


if __name__ == "__main__":
    unittest.main()

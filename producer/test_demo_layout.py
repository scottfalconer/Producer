"""Public demo projection and private-mirror projection, without model I/O."""

from http.server import ThreadingHTTPServer
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

import server
from demo_layout import presenter_mirror_snapshot
from on_air import OnAir


def show():
    core = Mock()
    core.snapshot.return_value = {"connected": True, "closed": False, "error": None,
                                  "private_debug": "must not escape"}
    director = SimpleNamespace(core=core, audio_end=7.5,
                               snapshot=Mock(side_effect=AssertionError("Passive reads must not renew the heartbeat")))
    on_air = OnAir(True, context_persistent=True)
    on_air.current = {"kind": "remark", "id": "public-card", "line": "Saved actions can be replayed.", "attribution": "Producer"}
    return {"input_type": "obs", "native_capture": object(), "native_through": 7.5,
            "seen": 123.0, "offset": 0.0, "director": director, "on_air": on_air,
            "confidence": {"kind": "editorial", "line": "Show the example", "secret": "private extra"},
            "brief": "private brief", "research_packet": "private research", "memory": "private memory",
            "transcript": "private transcript", "simulated_chat": {"events": ["fixture never exported"]}}


class PresenterProjectionTests(unittest.TestCase):
    def test_only_cue_presence_and_time_without_touching_heartbeat(self):
        session = show()
        result = presenter_mirror_snapshot(session, source_available=server.source_available,
                                           source_time=server.show_source_time)
        self.assertEqual(result, {"confidence": {"kind": "editorial", "line": "Show the example"},
                                  "presence": "active", "elapsed": 7.5})
        self.assertEqual(session["seen"], 123.0)
        session["director"].snapshot.assert_not_called()
        result["confidence"]["line"] = "different"
        self.assertEqual(session["confidence"]["line"], "Show the example")

    def test_source_missing_closed_error_or_disconnected_clears_private_projection(self):
        for change in ({"native_error": "private failure detail"}, {"director": None}):
            session = {**show(), **change}
            result = presenter_mirror_snapshot(session, source_available=server.source_available,
                                               source_time=server.show_source_time)
            self.assertEqual(result, {"confidence": None, "presence": "off", "elapsed": 0.0})
        for state in ({"closed": True, "connected": True}, {"connected": False}, {"connected": True, "error": "private error"}):
            session = show()
            session["director"].core.snapshot.return_value = state
            self.assertIsNone(presenter_mirror_snapshot(session, source_available=lambda _: True,
                                                      source_time=lambda _: 7)["confidence"])

    def test_invalid_cue_cannot_leak_arbitrary_session_data(self):
        for cue in (None, {"line": ["unexpected"]}, {"line": "  "}):
            session = {**show(), "confidence": cue}
            result = presenter_mirror_snapshot(session, source_available=lambda _: True,
                                               source_time=lambda _: 0)
            self.assertIsNone(result["confidence"])

    def test_mirror_does_not_shorten_an_already_validated_cue(self):
        line = "A source question preserved without a separate mirror text cap. " * 5
        session = {**show(), "confidence": {"kind": "question", "line": line}}
        result = presenter_mirror_snapshot(session, source_available=lambda _: True,
                                           source_time=lambda _: 0)
        self.assertEqual(result["confidence"]["line"], line)


class DemoLayoutHTTPTests(unittest.TestCase):
    def setUp(self):
        self.sessions = patch.dict(server.LIVE, {}, clear=True)
        self.sessions.start()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.sessions.stop()

    def get(self, path):
        response = requests.get(self.base + path, timeout=3)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_demo_api_is_public_only_and_does_not_refresh_source(self):
        session = show()
        server.LIVE["private-session-id"] = session
        result = self.get("/api/demo-layout").json()
        self.assertEqual(set(result), {"active", "elapsed", "simulated_chat", "public"})
        self.assertTrue(result["active"])
        self.assertEqual(result["public"]["graphic"]["line"], "Saved actions can be replayed.")
        for private in ("private-session-id", "Show the example", "private", "fixture never exported", "confidence"):
            self.assertNotIn(private, str(result))
        self.assertEqual(session["seen"], 123.0)
        session["director"].snapshot.assert_not_called()
        normal = self.get("/api/on-air").json()
        self.assertNotIn("confidence", normal)
        with patch("screen_chat_overlay.overlay_png", return_value=b"fixture PNG") as painter:
            image = self.get("/api/simulated-chat.png")
            self.assertEqual(image.headers["Content-Type"], "image/png")
            painter.assert_called_once()
        self.assertEqual(session["seen"], 123.0)
        session["director"].snapshot.assert_not_called()

    def test_missing_source_and_stop_clear_all_demo_output(self):
        session = show()
        server.LIVE["private-session-id"] = session
        for state in ({"connected": False}, {"connected": True, "closed": True}, {"connected": True, "error": "private error"}):
            session["director"].core.snapshot.return_value = state
            result = self.get("/api/demo-layout").json()
            self.assertFalse(result["active"])
            self.assertEqual(result["public"], {"graphic": None})
        server.LIVE.clear()
        self.assertEqual(self.get("/api/demo-layout").json(),
                         {"active": False, "elapsed": 0, "simulated_chat": False, "public": {"graphic": None}})

    def test_demo_assets_allow_pixel_blobs_but_no_frames_or_inline_scripts(self):
        response = self.get("/demo-layout")
        policy = response.headers["Content-Security-Policy"]
        self.assertIn("img-src 'self' data: blob:", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("presenter", response.text.lower())
        for asset in ("/demo-layout.js", "/demo-layout.css"):
            self.get(asset)


if __name__ == "__main__":
    unittest.main()

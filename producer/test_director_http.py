"""Realtime lifecycle HTTP routes using a fake director and isolated local state."""

import copy
import hashlib
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

import server
from director_runtime import note_brief


class FakeShowDirector:
    instances = []

    def __init__(self, sid, session, **options):
        self.sid, self.session, self.options = sid, session, options
        self.closed = False
        self.core = self
        self.packets = []
        self.instances.append(self)

    def snapshot(self):
        return {"connected": not self.closed, "closed": self.closed,
                "status": "listening" if not self.closed else "error", "model": "fake-realtime",
                "confidence": copy.deepcopy(self.session.get("confidence")),
                "memory": copy.deepcopy(self.session.get("memory")),
                "through": self.session["offset"]}

    def append(self, *packet):
        if self.closed:
            raise ValueError("Stopped fake director")
        self.packets.append(packet)
        return {"accepted": True, "through": self.session["offset"] + 0.5}

    def caption_snapshot(self):
        return None

    def close(self):
        self.closed = True


class DirectorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.private_patch = patch("server.PRIVATE", self.folder / "private")
        self.private_patch.start()
        FakeShowDirector.instances = []
        self.director_patch = patch("server.ShowDirector", FakeShowDirector)
        self.director_patch.start()
        self.key_patch = patch("server._read_key", return_value="unused-fixture-key")
        self.key_patch.start()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.key_file = Path("/unused-key")
        self.http.show_folder = self.folder
        self.http.audience_openai = False
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.headers = {"Origin": self.base, "X-Desk": server.TOKEN}
        self.notes = "Original private show notes\nExplain one practical example and follow useful questions."
        self.brief = note_brief(self.notes)

    def tearDown(self):
        for sid in list(server.LIVE):
            self.post("/api/stop", {"id": sid})
        server.audience.stop()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.key_patch.stop()
        self.director_patch.stop()
        self.private_patch.stop()
        self.temp.cleanup()

    def post(self, path, data):
        return requests.post(self.base + path, json=data, headers=self.headers, timeout=3)

    def start(self, **extra):
        response = self.post("/api/start", {
            "brief": self.brief, "notes": self.notes, "runtime": "realtime",
            "input_type": "microphone", "on_air": True, **extra,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def connect(self, sid):
        response = self.post("/api/director/start", {"id": sid})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_chat_changes_on_new_start_but_resume_pins_the_exact_timeline(self):
        first = self.start()
        first_chat = copy.deepcopy(server.LIVE[first["id"]]["simulated_chat"])
        self.assertTrue(first_chat["synthetic"])
        saved = self.folder / "private" / "simulated-chat" / (first["show_id"] + ".json")
        self.assertTrue(saved.is_file())
        self.post("/api/stop", {"id": first["id"], "reason": "paused", "through": 10})
        resumed = self.start(show_id=first["show_id"], offset=10)
        self.assertEqual(server.LIVE[resumed["id"]]["simulated_chat"], first_chat)
        self.post("/api/stop", {"id": resumed["id"], "reason": "ended", "through": 10})
        second = self.start()
        self.assertNotEqual(server.LIVE[second["id"]]["simulated_chat"]["events"], first_chat["events"])
        self.assertNotIn("eval_labels", str(second))

    def test_browser_overlay_uses_this_show_timeline(self):
        started = self.start()
        events = server.LIVE[started["id"]]["simulated_chat"]
        with patch("screen_chat_overlay.overlay_png", return_value=b"test-png") as painter:
            response = requests.get(self.base + "/api/simulated-chat.png?preview=1&at=20", timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(painter.call_args.args, (events, 20))

    def test_direct_notes_prepare_uses_no_responses_or_editorial_call(self):
        with (
            patch("server.prepare", side_effect=AssertionError("No preparation model")) as prepare,
            patch("server.OpenAIProducer.post", side_effect=AssertionError("No Responses request")) as post,
        ):
            response = self.post("/api/prepare", {"runtime": "realtime", "notes": self.notes})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["question"], "")
        self.assertEqual(result["brief"]["title"], "Original private show notes")
        prepare.assert_not_called()
        post.assert_not_called()

    def test_folder_research_packet_is_separate_and_pinned_until_next_start(self):
        packet = "# REF-001\nSource: https://example.test/source\nA sourced reference.\n"
        path = self.folder / "research-packet.md"
        path.write_text(packet)
        (self.folder / "notes.md").write_text("Folder notes")
        result = requests.get(self.base + "/api/show-materials", timeout=3).json()
        self.assertEqual(result["notes"], "Folder notes")
        self.assertEqual(result["research_packet"], packet)
        self.assertTrue(result["research_available"])
        started = self.start()
        session = server.LIVE[started["id"]]
        self.assertEqual(session["research_packet"], packet)
        self.assertEqual(started["research_packet"]["sha256"], hashlib.sha256(packet.encode()).hexdigest())
        self.assertNotIn("REF-001", str(started))
        self.assertNotIn("REF-001", session["transcript"].path.read_text())
        path.write_text("Changed after Start")
        self.connect(started["id"])
        self.assertEqual(FakeShowDirector.instances[-1].session["research_packet"], packet)
        self.assertNotIn("REF-001", requests.get(self.base + "/api/on-air", timeout=3).text)
        self.assertNotIn("REF-001", self.post("/api/director/status", {"id": started["id"]}).text)
        self.post("/api/stop", {"id": started["id"]})
        second = self.start()
        self.assertEqual(server.LIVE[second["id"]]["research_packet"], "Changed after Start")

    def test_explicit_packet_overrides_folder_and_empty_disables_it(self):
        (self.folder / "research-packet.md").write_text("Folder packet")
        for packet in ("Uploaded complete reference\n", ""):
            with self.subTest(packet=packet):
                started = self.start(research_packet=packet)
                self.assertEqual(server.LIVE[started["id"]]["research_packet"], packet)
                self.assertEqual(started["research_packet"]["available"], bool(packet))
                self.post("/api/stop", {"id": started["id"]})

    def test_large_complete_packet_reaches_session_without_truncation(self):
        packet = "# Supplied preparation\n" + "Reference detail. " * 1500 + "\nFINAL_REFERENCE_SENTINEL"
        self.assertGreater(len(packet), 20000)
        started = self.start(research_packet=packet)
        self.assertEqual(server.LIVE[started["id"]]["research_packet"], packet)
        self.assertEqual(started["research_packet"]["characters"], len(packet))
        self.assertEqual(started["research_packet"]["sha256"], hashlib.sha256(packet.encode()).hexdigest())

    def test_invalid_explicit_packet_is_rejected_before_creating_a_show(self):
        for packet in (None, {}, [], 12, "a" * 32001):
            response = self.post("/api/start", {
                "brief": self.brief, "notes": self.notes, "runtime": "realtime",
                "research_packet": packet,
            })
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("research packet", response.json()["error"])
            self.assertEqual(server.LIVE, {})
            self.assertEqual(FakeShowDirector.instances, [])

    def test_invalid_folder_packet_fails_setup_and_start_without_silent_omission(self):
        (self.folder / "research-packet.md").write_bytes(b"\xff")
        result = requests.get(self.base + "/api/show-materials", timeout=3)
        self.assertEqual(result.status_code, 400, result.text)
        self.assertIn("research packet", result.json()["error"])
        started = self.post("/api/start", {"brief": self.brief, "runtime": "realtime"})
        self.assertEqual(started.status_code, 400, started.text)
        self.assertEqual(server.LIVE, {})
        self.assertEqual(FakeShowDirector.instances, [])
        for packet in ("", "A replacement packet"):
            with self.subTest(packet=packet):
                started = self.start(research_packet=packet)
                self.assertEqual(server.LIVE[started["id"]]["research_packet"], packet)
                self.post("/api/stop", {"id": started["id"]})

    def test_pause_resume_reuses_exact_packet_and_changed_packet_cannot_join_show(self):
        packet = "# REF-001\nThe original preparation packet."
        started = self.start(research_packet=packet)
        sid, show_id = started["id"], started["show_id"]
        original = server.LIVE[sid]["transcript"].path
        paused = self.post("/api/stop", {"id": sid, "reason": "paused", "through": 0})
        self.assertEqual(paused.status_code, 200, paused.text)
        changed = self.post("/api/start", {
            "brief": self.brief, "notes": self.notes, "runtime": "realtime",
            "show_id": show_id, "offset": 0, "research_packet": "Different packet",
            "input_type": "microphone",
        })
        self.assertEqual(changed.status_code, 400, changed.text)
        self.assertEqual(server.LIVE, {})
        resumed = self.start(research_packet=packet, show_id=show_id, offset=0)
        self.assertEqual(server.LIVE[resumed["id"]]["research_packet"], packet)
        self.assertEqual(server.LIVE[resumed["id"]]["transcript"].path, original)

    def test_one_explicit_director_connection_has_notes_source_and_offline_scope(self):
        with patch("server.TwitchAdapter", side_effect=AssertionError("No Twitch transport")):
            started = self.start()
            self.assertTrue(started["offline"])
            self.assertEqual(FakeShowDirector.instances, [])
            self.connect(started["id"])
            for action in ("start", "poll"):
                response = self.post("/api/audience/" + action, {"id": started["id"]})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("no chat API", response.json()["error"])
        self.assertEqual(len(FakeShowDirector.instances), 1)
        director = FakeShowDirector.instances[0]
        self.assertEqual(director.session["notes"], self.notes)
        self.assertEqual(director.session["input_type"], "microphone")
        self.assertFalse(director.options["allow_audience"])
        self.assertEqual(director.session["audience_input"], "screen")
        self.assertIsNone(director.session["events"])
        self.assertEqual(self.post("/api/director/start", {"id": started["id"]}).status_code, 400)
        self.assertEqual(self.post("/api/director/audio", {"id": started["id"], "sequence": 0, "audio": "fixture-packet"}).status_code, 200)
        self.assertEqual(director.packets, [(0, "fixture-packet", None, None)])
        status = self.post("/api/director/status", {"id": started["id"]})
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "listening")

    def test_screen_default_does_not_open_fixture_or_twitch_chat(self):
        clip = self.folder / "fixture.mp4"
        clip.touch()
        with (
            patch("server.rehearsal.EventFixture", side_effect=AssertionError("No direct fixture inbox")),
            patch("server.rehearsal.CLIP", clip),
            patch.object(server.audience, "poll", side_effect=AssertionError("No Twitch inbox")),
        ):
            started = self.start(input_type="rehearsal", rehearsal="office-hours")
            self.assertEqual(started["audience_input"], "screen")
            self.assertFalse(started["audience_required"])
            session = server.LIVE[started["id"]]
            self.assertIsNone(session["events"])
            self.assertIsNotNone(session["rehearsal"].chat_events)
            self.connect(started["id"])
            public = requests.get(self.base + "/api/on-air", timeout=3)
            self.assertEqual(public.status_code, 200, public.text)
            self.assertFalse(FakeShowDirector.instances[-1].options["allow_audience"])

    def test_legacy_fixture_is_explicit_offline_only_for_realtime(self):
        started = self.start(audience_input="legacy-fixture")
        self.assertIsNotNone(server.LIVE[started["id"]]["events"])
        self.connect(started["id"])
        self.assertTrue(FakeShowDirector.instances[-1].options["allow_audience"])
        self.post("/api/stop", {"id": started["id"]})
        for value in ("legacy-fixture", "twitch", None):
            response = self.post("/api/start", {"brief": self.brief, "runtime": "realtime",
                                               "offline": False, "audience_input": value})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(server.LIVE, {})

    def test_simulated_chat_is_a_png_source_without_hidden_message_text(self):
        from PIL import Image
        import io
        response = requests.get(self.base + "/api/simulated-chat.png?preview=1&at=40", timeout=3)
        self.assertEqual(response.status_code, 200, response.text[:100])
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(Image.open(io.BytesIO(response.content)).size, (1280, 720))
        self.assertIn("no-store", response.headers["Cache-Control"])
        page = requests.get(self.base + "/simulated-chat", timeout=3)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn("mock-events", page.text)

    def test_realtime_cannot_silently_enter_windowed_editorial_path(self):
        sid = self.start()["id"]
        with patch("server.produce", side_effect=AssertionError("No windowed inference")) as produce:
            response = self.post("/api/window", {"id": sid, "start": 0, "end": 1, "audio": "not-media"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Realtime", response.json()["error"])
        produce.assert_not_called()

    def test_realtime_captions_cannot_open_another_transcription_connection(self):
        sid = self.start()["id"]
        with patch("server.CaptionSession", side_effect=AssertionError("No second speech session")) as captions:
            response = self.post("/api/captions/start", {"id": sid})
        self.assertEqual(response.status_code, 400, response.text)
        captions.assert_not_called()

    def test_live_mode_does_not_enable_unapproved_audience_forwarding(self):
        with patch("server.TwitchAdapter", side_effect=AssertionError("No planned Twitch actions")):
            sid = self.start(offline=False)["id"]
            self.connect(sid)
        self.assertFalse(FakeShowDirector.instances[-1].options["allow_audience"])

    def test_realtime_only_uses_model_managed_topic_context(self):
        started = self.start()
        output = server.LIVE[started["id"]]["on_air"]
        self.assertTrue(output.context_persistent)
        topic = {"subject": "A practical example", "purpose": "From idea to result"}
        output.update_context(topic, now=0)
        self.assertEqual(output.snapshot(600)["context"], topic)
        stopped = self.post("/api/stop", {"id": started["id"], "reason": "ended", "through": 0})
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertIsNone(output.snapshot(601)["context"])

        legacy = self.start(runtime="legacy")
        output = server.LIVE[legacy["id"]]["on_air"]
        self.assertFalse(output.context_persistent)
        output.update_context(topic, now=0)
        self.assertIsNone(output.snapshot(91)["context"])

    def test_stop_closes_director_and_rejects_late_input(self):
        sid = self.start()["id"]
        self.connect(sid)
        instance = FakeShowDirector.instances[-1]
        stopped = self.post("/api/stop", {"id": sid, "reason": "ended", "through": 0})
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertTrue(instance.closed)
        self.assertNotIn(sid, server.LIVE)
        self.assertEqual(self.post("/api/director/audio", {"id": sid, "sequence": 0, "audio": "late"}).status_code, 400)
        self.assertEqual(requests.get(self.base + "/api/on-air", timeout=3).json(), {"graphic": None})

    def test_pause_resume_uses_same_transcript_and_fresh_director(self):
        started = self.start()
        self.connect(started["id"])
        first = FakeShowDirector.instances[-1]
        first.session["memory"]["director_notes"] = "Return to the unfinished signed-request example."
        stopped = self.post("/api/stop", {"id": started["id"], "reason": "paused", "through": 1}).json()
        resumed = self.start(show_id=started["show_id"], offset=1, memory=stopped["memory"])
        self.connect(resumed["id"])
        second = FakeShowDirector.instances[-1]
        self.assertTrue(first.closed)
        self.assertIsNot(first, second)
        self.assertEqual(started["show_id"], resumed["show_id"])
        self.assertEqual(second.session["transcript"].path, first.session["transcript"].path)
        self.assertEqual(second.session["memory"]["director_notes"], "Return to the unfinished signed-request example.")

    def test_explicit_start_releases_a_disconnected_show_without_waiting_for_the_lease(self):
        first = self.start()
        self.connect(first["id"])
        old = FakeShowDirector.instances[-1]
        old.closed = True  # Source/provider ended while the original page was closed.
        second = self.start()
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotIn(first["id"], server.LIVE)
        self.assertIn(second["id"], server.LIVE)
        self.assertEqual(old.session["transcript"].summary()["last_stop_reason"], "source_lost")

    def test_failed_director_start_is_explicit_and_does_not_fall_back(self):
        sid = self.start()["id"]
        with patch("server.ShowDirector", side_effect=ValueError("Fixture connection failed")):
            response = self.post("/api/director/start", {"id": sid})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIsNone(server.LIVE[sid]["director"])
        self.assertFalse(server.LIVE[sid]["director_starting"])
        self.assertEqual(server.LIVE[sid]["runtime"], "realtime")
        self.assertEqual(self.post("/api/director/status", {"id": sid}).status_code, 400)

    def test_stop_during_connection_closes_candidate_without_installing_it(self):
        sid = self.start()["id"]
        connecting, release = threading.Event(), threading.Event()
        response = []

        class SlowDirector(FakeShowDirector):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                connecting.set()
                if not release.wait(2):
                    raise RuntimeError("Fixture connection did not resume")

        with patch("server.ShowDirector", SlowDirector):
            worker = threading.Thread(target=lambda: response.append(self.post("/api/director/start", {"id": sid})))
            worker.start()
            self.assertTrue(connecting.wait(1))
            self.assertEqual(self.post("/api/stop", {"id": sid, "through": 0}).status_code, 200)
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(response[0].status_code, 400, response[0].text)
        self.assertTrue(FakeShowDirector.instances[-1].closed)
        self.assertNotIn(sid, server.LIVE)

    def test_director_routes_require_local_origin_and_csrf(self):
        sid = self.start()["id"]
        response = requests.post(self.base + "/api/director/start", json={"id": sid}, timeout=3)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(FakeShowDirector.instances, [])


if __name__ == "__main__":
    unittest.main()

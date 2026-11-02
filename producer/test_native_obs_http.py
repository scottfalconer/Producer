"""Host-pinned OBS input lifecycle; all media/model/Twitch work is simulated."""

import base64
import copy
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image

import server
from action_trace import load_trace
from director_runtime import ShowDirector
from obs_capture import OBSConfig
from platform_twitch import TwitchAdapter
from screen_chat_overlay import overlay_png
from test_chat_fixture_setup import fixture
import test_director_http as http_fixture
from test_director_runtime import FakeDirector
from realtime_director import RealtimeDirector
from test_realtime_director import ACK, HALF_SECOND, Socket

FakeShowDirector = http_fixture.FakeShowDirector


class FakeOBSFeed:
    instances = []

    def __init__(self, config, on_packet, on_error, *, offset=0):
        self.config, self.on_packet, self.on_error = config, on_packet, on_error
        self.offset, self.closed, self.started = offset, False, False
        self.instances.append(self)

    def start(self):
        self.started = True

    def snapshot(self):
        return {"status": "stopped" if self.closed else "waiting",
                "endpoint": self.config.endpoint, "source_end": self.offset}

    def close(self):
        self.closed = True

    def emit(self, sequence=0):
        self.on_packet(sequence, "fixture-audio", "fixture-jpeg" if not sequence else None,
                       self.offset + (sequence + 1) * 0.5)


class NativeOBSHTTPTests(unittest.TestCase):
    # Reuse fixture setup, not its unrelated test methods.
    post = http_fixture.DirectorHTTPTests.post
    connect = http_fixture.DirectorHTTPTests.connect
    start = http_fixture.DirectorHTTPTests.start

    def setUp(self):
        http_fixture.DirectorHTTPTests.setUp(self)
        self.http.obs_config = OBSConfig(port=8790)
        FakeOBSFeed.instances = []
        self.feed_patch = patch("server.OBSFeed", FakeOBSFeed)
        self.feed_patch.start()

    def tearDown(self):
        try:
            http_fixture.DirectorHTTPTests.tearDown(self)
        finally:
            self.feed_patch.stop()

    def native(self, **extra):
        return self.start(input_type="obs", **extra)

    def test_room_exposes_host_endpoint_without_recording_directory(self):
        self.http.obs_config = OBSConfig(port=8793, recording_dir=self.folder / "private-media")
        room = requests.get(self.base + "/api/room", timeout=3).json()
        self.assertEqual(room["nativeOBS"], {"configured": True, "endpoint": "tcp://127.0.0.1:8793", "recording": True})
        self.assertNotIn("private-media", str(room))
        self.http.obs_config = None
        self.assertEqual(requests.get(self.base + "/api/room", timeout=3).json()["nativeOBS"],
                         {"configured": False, "endpoint": None, "recording": False})

    def test_requires_host_configuration_realtime_and_selected_programme(self):
        for extra in ({"runtime": "legacy"}, {"rehearsal": "office-hours"},
                      {"audience_input": "legacy-fixture"}):
            response = self.post("/api/start", {"brief": self.brief, "runtime": "realtime", "input_type": "obs", **extra})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(server.LIVE, {})
        self.http.obs_config = None
        response = self.post("/api/start", {"brief": self.brief, "runtime": "realtime", "input_type": "obs"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(FakeOBSFeed.instances, [])
        self.assertEqual(FakeShowDirector.instances, [])

    def test_native_offline_is_default_even_when_host_permits_live_actions(self):
        self.http.enable_live_production = True
        self.http.simulate_chat = True
        with patch("server.TwitchAdapter", side_effect=AssertionError("Offline cannot connect Twitch")):
            started = self.native()
        session = server.LIVE[started["id"]]
        self.assertTrue(started["offline"])
        self.assertFalse(started["live_production_enabled"])
        self.assertEqual(session["production"].adapter.platform, "fixture")
        self.assertTrue(session["simulated_chat"]["synthetic"])

    def test_native_live_mode_alone_cannot_enable_twitch_actions(self):
        with patch("server.TwitchAdapter", side_effect=AssertionError("Host opt-in is required")):
            started = self.native(offline=False)
        self.assertFalse(started["offline"])
        self.assertFalse(started["live_production_enabled"])
        self.assertIsNone(server.LIVE[started["id"]]["production"].adapter)
        self.assertIsNone(server.LIVE[started["id"]]["simulated_chat"])

    def test_native_live_capability_failure_does_not_create_mock_success(self):
        self.http.enable_live_production = True
        self.http.simulate_chat = True
        with patch("server.TwitchAdapter", side_effect=ValueError("Unavailable connection")):
            started = self.native(offline=False)
        session = server.LIVE[started["id"]]
        self.assertFalse(started["live_production_enabled"])
        self.assertTrue(started["production_issues"])
        self.assertIsNone(session["production"].adapter)
        self.assertTrue(session["simulated_chat"]["synthetic"])

    def test_native_live_director_routes_one_confirmed_post_to_simulated_pixels_and_stops(self):
        # Real HTTP setup, native callback, ShowDirector and TwitchAdapter. Only
        # the OBS device, Realtime connection and external Twitch responses are fake.
        self.http.enable_live_production = True
        self.http.simulate_chat = True
        self.http.producer_chat_sender = "broadcaster_labelled"
        self.http.chat_fixture_override = fixture()
        calls = []

        def transport(method, url, **kwargs):
            route = url.removeprefix("https://api.twitch.tv/helix/")
            calls.append((method, route, kwargs))
            if (method, route) == ("GET", "users"):
                data = [{"id": "host-1", "broadcaster_type": ""}]
            elif method == "GET" and route in ("streams", "shared_chat/session"):
                data = []
            elif (method, route) == ("POST", "chat/messages"):
                data = [{"message_id": "confirmed-native-delivery", "is_sent": True}]
            else:
                raise AssertionError(f"Unexpected Twitch call: {method} {route}")
            response = Mock(status_code=200, headers={})
            response.json.return_value = {"data": data}
            return response

        adapter = TwitchAdapter(sender_mode="broadcaster_labelled", transport=transport,
            auth=lambda: (None, {"user_id": "host-1", "login": "scottfalconer", "scopes": ["user:write:chat"]}, {}))
        with patch("server.TwitchAdapter", return_value=adapter) as factory:
            started = self.native(offline=False)
        factory.assert_called_once_with(sender_mode="broadcaster_labelled")
        self.assertTrue(started["live_production_enabled"])
        sid = started["id"]
        session = server.LIVE[sid]
        self.assertEqual(session["simulated_chat"], self.http.chat_fixture_override)
        self.assertFalse(started["audience_required"])
        self.assertIsNone(session["events"])
        self.assertEqual([call for call in calls if call[0] != "GET"], [])
        self.connect(sid)
        self.assertIsNone(session["director"], "Waiting for OBS must not start inference or posts")

        def director(*args, **kwargs):
            return ShowDirector(*args, **kwargs, director_class=FakeDirector)

        jpg = io.BytesIO()
        Image.new("RGB", (80, 45), "#191b20").save(jpg, "JPEG")
        pcm = base64.b64encode(b"\0" * 24000).decode()
        frame = base64.b64encode(jpg.getvalue()).decode()
        feed = FakeOBSFeed.instances[0]
        with patch("server.ShowDirector", side_effect=director):
            feed.on_packet(0, pcm, frame, 0.5)
        live_director = session["director"]
        state = live_director.state()
        self.assertEqual(state["production"]["capabilities"], ["share_on_air", "execute_plan"])
        self.assertEqual(state["audience"], [])
        self.assertNotIn(self.http.chat_fixture_override["events"][0]["text"], json.dumps(state))
        self.assertNotIn("simulated_chat", live_director.core.callbacks["brief"])
        state = live_director.apply({"card": {"action": "show", "kind": "remark",
            "line": "Native OBS chat delivery test", "attribution": "Producer"}}, state)["state"]
        args = {"action": "share_on_air", "output_key": state["production"]["shareable_outputs"][0]["output_key"]}
        result = live_director.production_action(args, state)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["simulated"])
        receipt = result["receipt"]
        self.assertEqual(receipt["sender_login"], "scottfalconer")
        self.assertEqual(receipt["message"], "(From Producer) Native OBS chat delivery test — Producer")
        writes = [call for call in calls if call[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:2], ("POST", "chat/messages"))
        self.assertEqual(writes[0][2]["json"], {"broadcaster_id": "host-1", "sender_id": "host-1", "message": receipt["message"]})
        repeated = live_director.production_action(args, result["state"])
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(len([call for call in calls if call[0] != "GET"]), 1)
        self.assertEqual(session["producer_posts"][0]["text"], receipt["message"])
        self.assertEqual(live_director.state()["audience"], [])
        feed.on_packet(1, pcm, None, 1.0)
        painted = requests.get(self.base + "/api/simulated-chat.png", timeout=3)
        self.assertEqual(painted.status_code, 200)
        self.assertNotEqual(painted.content, overlay_png(session["simulated_chat"], 1.0))
        with self.assertRaisesRegex(ValueError, "reviewed"):
            live_director.production_action({"action": "set_title", "request_id": "not-reviewed", "text": "No change"}, live_director.state())
        feed.on_error("OBS source ended")
        self.assertTrue(feed.closed)
        self.assertTrue(live_director.closed)
        with self.assertRaisesRegex(ValueError, "stopped"):
            live_director.production_action(args, repeated["state"])
        self.assertEqual(len([call for call in calls if call[0] != "GET"]), 1)
        self.assertEqual(self.post("/api/stop", {"id": sid}).status_code, 200)

    def test_waiting_uses_no_key_model_or_browser_media(self):
        started = self.native()
        sid = started["id"]
        with patch("server._read_key", side_effect=AssertionError("No paid connection before OBS")):
            waiting = self.connect(sid)
            self.assertEqual(waiting["status"], "waiting")
            self.assertEqual(waiting["through"], 0)
            self.assertFalse(waiting["closed"])
            self.assertFalse(waiting["connected"])
            self.assertEqual(self.post("/api/director/status", {"id": sid}).json()["status"], "waiting")
        self.assertEqual(FakeShowDirector.instances, [])
        feed = FakeOBSFeed.instances[0]
        self.assertTrue(feed.started)
        self.assertIs(feed.config, self.http.obs_config)
        self.assertEqual(self.post("/api/director/start", {"id": sid}).status_code, 400)
        self.assertEqual(self.post("/api/director/audio", {"id": sid, "sequence": 0, "audio": "untrusted-browser"}).status_code, 400)
        response = self.post("/api/stop", {"id": sid, "through": 999})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["transcript"]["through"], 0)
        self.assertTrue(feed.closed)

    def test_action_trace_is_host_opt_in_and_only_native(self):
        sid = self.native()["id"]
        self.connect(sid)
        self.assertNotIn("action_trace", server.LIVE[sid])
        self.post("/api/stop", {"id": sid})
        self.http.obs_action_trace_dir = self.folder / "native-traces"
        sid = self.start()["id"]
        self.connect(sid)
        self.assertNotIn("action_trace", server.LIVE[sid])
        self.assertFalse(self.http.obs_action_trace_dir.exists())

    def test_native_trace_records_real_router_tools_receipts_without_private_reference(self):
        self.http.obs_action_trace_dir = self.folder / "native-traces"
        started = self.native()
        sid = started["id"]
        self.connect(sid)
        session = server.LIVE[sid]
        trace = session["action_trace"]
        socket = Socket()

        def core(key, **callbacks):
            result = RealtimeDirector(key, **callbacks, connection=lambda *a, **k: socket,
                                      start_thread=False)
            result.accept(copy.deepcopy(ACK))
            return result

        def director(*args, **kwargs):
            return ShowDirector(*args, **kwargs, director_class=core)

        with patch("server.ShowDirector", side_effect=director):
            FakeOBSFeed.instances[0].on_packet(0, HALF_SECOND, None, 0.5)
        director = session["director"]
        engine = director.core
        self.assertTrue(engine.consider())
        engine.accept({"type": "response.created", "response": {"id": "trace-response"}})

        def call(name, args, ident):
            engine.accept({"type": "response.function_call_arguments.done", "response_id": "trace-response",
                           "call_id": ident, "name": name, "arguments": json.dumps(args)})

        call("update_screens", {"card": {"action": "show", "kind": "reference",
             "line": "Project: https://example.test/producer", "attribution": "Producer"}}, "screen-call")
        state = director.state()
        call("production_action", {"action": "share_on_air",
             "output_key": state["production"]["shareable_outputs"][0]["output_key"]}, "post-call")
        engine.accept({"type": "response.done", "response": {"id": "trace-response", "status": "completed", "output": []}})
        self.assertEqual(self.post("/api/stop", {"id": sid}).status_code, 200)
        rows = load_trace(trace.path)
        kinds = {row["kind"] for row in rows}
        self.assertTrue({"function_call", "tool_receipt", "screen_commit", "production_commit", "production_chat", "session_closed"} <= kinds)
        receipt = next(row["payload"]["receipt"] for row in rows
                       if row["kind"] == "tool_receipt" and row["payload"]["name"] == "production_action")
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["simulated"])
        self.assertEqual(receipt["receipt"]["state"], "done")
        self.assertEqual(rows[0]["payload"]["source_id"], started["show_id"])
        self.assertEqual(rows[-1]["source_s"], 0.5)
        self.assertEqual(rows[-1]["payload"]["reason"], "native_show_ended")
        self.assertTrue(trace.closed)
        self.assertEqual(trace.path.stat().st_mode & 0o777, 0o600)
        saved = trace.path.read_text()
        self.assertNotIn(self.notes, saved)
        self.assertNotIn("unused-fixture-key", saved)
        self.assertNotIn(HALF_SECOND, saved)

    def test_native_trace_preserves_pause_attempt_and_closes_on_source_failure(self):
        self.http.obs_action_trace_dir = self.folder / "native-traces"
        started = self.native()
        sid = started["id"]
        self.connect(sid)
        first = server.LIVE[sid]["action_trace"]
        self.post("/api/stop", {"id": sid, "reason": "paused"})
        original = first.path.read_bytes()
        resumed = self.native(show_id=started["show_id"], offset=0)
        self.connect(resumed["id"])
        second = server.LIVE[resumed["id"]]["action_trace"]
        self.assertNotEqual(first.path, second.path)
        FakeOBSFeed.instances[-1].on_error("Local OBS fixture disconnected")
        self.assertTrue(FakeOBSFeed.instances[-1].closed)
        self.post("/api/stop", {"id": resumed["id"]})
        self.assertEqual(first.path.read_bytes(), original)
        rows = load_trace(second.path)
        self.assertTrue(any(row["kind"] == "local_error" and row["payload"]["phase"] == "native_input" for row in rows))
        self.assertTrue(second.closed)

    def test_native_trace_stop_does_not_wait_for_late_production_receipt(self):
        self.http.obs_action_trace_dir = self.folder / "native-traces"
        sid = self.native()["id"]
        self.connect(sid)
        session = server.LIVE[sid]

        def core(key, **callbacks):
            result = RealtimeDirector(key, **callbacks, connection=lambda *a, **k: Socket(), start_thread=False)
            result.accept(copy.deepcopy(ACK))
            return result

        with patch("server.ShowDirector", side_effect=lambda *a, **k: ShowDirector(*a, **k, director_class=core)):
            FakeOBSFeed.instances[0].on_packet(0, HALF_SECOND, None, 0.5)
        director = session["director"]
        state = director.apply({"card": {"action": "show", "kind": "remark", "line": "Useful public card"}}, director.state())["state"]
        engine = director.core
        engine.consider()
        engine.accept({"type": "response.created", "response": {"id": "late-response"}})
        entered, release = threading.Event(), threading.Event()
        errors = []
        adapter = session["production"].adapter
        original = adapter.execute

        def slow_execute(plan):
            entered.set()
            release.wait(3)
            return original(plan)

        def deliver():
            try:
                engine.accept({"type": "response.function_call_arguments.done", "response_id": "late-response",
                               "call_id": "late-call", "name": "production_action", "arguments": json.dumps({
                                   "action": "share_on_air", "output_key": state["production"]["shareable_outputs"][0]["output_key"]})})
            except ValueError as exc:
                errors.append(str(exc))

        with patch.object(adapter, "execute", side_effect=slow_execute):
            worker = threading.Thread(target=deliver)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.post("/api/stop", {"id": sid}).status_code, 200)
            self.assertTrue(worker.is_alive(), "Stop must not wait for the platform request")
            self.assertEqual(session["production"].ledger.snapshot()[0]["state"], "working")
            trace = session["action_trace"]
            saved = trace.path.read_bytes()
            self.assertTrue(trace.closed)
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(session["production"].ledger.snapshot()[0]["state"], "done")
        self.assertEqual(trace.path.read_bytes(), saved)
        self.assertTrue(all(error == "Realtime Producer is stopped." for error in errors), errors)

    def test_native_packet_connects_once_and_routes_existing_director(self):
        sid = self.native()["id"]
        self.connect(sid)
        feed = FakeOBSFeed.instances[0]
        feed.emit()
        director = FakeShowDirector.instances[0]
        self.assertEqual(director.packets, [(0, "fixture-audio", "fixture-jpeg", 0.5)])
        self.assertFalse(director.options["allow_audience"])
        self.assertEqual(director.session["audience_input"], "screen")
        self.assertEqual(server.LIVE[sid]["native_through"], 0.5)
        listening = self.post("/api/director/status", {"id": sid}).json()
        self.assertEqual(listening["status"], "listening")
        self.assertEqual(listening["through"], 0.5)
        feed.emit(1)
        self.assertEqual(len(FakeShowDirector.instances), 1)
        self.assertEqual(len(director.packets), 2)
        self.post("/api/stop", {"id": sid, "reason": "paused", "through": 500})
        self.assertTrue(feed.closed)
        self.assertTrue(director.closed)
        with self.assertRaisesRegex(ValueError, "stopped"):
            feed.emit(2)

    def test_browser_inactivity_cannot_replace_native_source_and_chat_uses_source_clock(self):
        sid = self.native()["id"]
        self.connect(sid)
        session = server.LIVE[sid]
        session["created"] -= 300
        session["seen"] -= 300
        with patch("screen_chat_overlay.overlay_png", return_value=b"png") as painter:
            self.assertEqual(requests.get(self.base + "/api/simulated-chat.png", timeout=3).status_code, 200)
        self.assertEqual(painter.call_args.args[1], 0)
        response = self.post("/api/start", {"brief": self.brief, "runtime": "realtime", "input_type": "microphone"})
        self.assertEqual(response.status_code, 400)
        self.assertIs(server.LIVE[sid], session)
        self.assertFalse(FakeOBSFeed.instances[0].closed)

    def test_abandoned_setup_before_receiver_start_can_expire_without_fake_observation(self):
        started = self.native()
        session = server.LIVE[started["id"]]
        session["created"] -= 300
        session["seen"] -= 300
        newer = self.native()
        self.assertNotEqual(newer["id"], started["id"])
        self.assertEqual(session["transcript"].summary()["through"], 0)
        self.assertEqual(FakeOBSFeed.instances, [])
        self.assertEqual(FakeShowDirector.instances, [])

    def test_source_error_clears_both_screens_and_closes_capture_and_director(self):
        sid = self.native()["id"]
        self.connect(sid)
        feed = FakeOBSFeed.instances[0]
        feed.emit()
        session = server.LIVE[sid]
        session["confidence"] = {"line": "private cue"}
        session["on_air"].publish({"kind": "remark", "line": "public card", "attribution": "Producer"})
        feed.on_error("The local OBS feed ended.")
        self.assertTrue(feed.closed)
        self.assertTrue(FakeShowDirector.instances[0].closed)
        self.assertIsNone(session["confidence"])
        self.assertIsNone(session["on_air"].snapshot()["graphic"])
        result = self.post("/api/director/status", {"id": sid}).json()
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["closed"])
        self.assertEqual(result["through"], 0.5)
        self.assertEqual(requests.get(self.base + "/api/on-air", timeout=3).json(), {"graphic": None})
        with self.assertRaisesRegex(ValueError, "stopped"):
            feed.emit(1)

    def test_stop_while_model_connects_closes_late_candidate(self):
        sid = self.native()["id"]
        self.connect(sid)
        feed = FakeOBSFeed.instances[0]
        entered, release = threading.Event(), threading.Event()
        failures = []

        class SlowDirector(FakeShowDirector):
            def __init__(self, *args, **kwargs):
                entered.set()
                release.wait(2)
                super().__init__(*args, **kwargs)

        def emit():
            try:
                feed.emit()
            except ValueError as exc:
                failures.append(str(exc))

        with patch("server.ShowDirector", SlowDirector):
            worker = threading.Thread(target=emit)
            worker.start()
            self.assertTrue(entered.wait(1))
            waiting = self.post("/api/director/status", {"id": sid}).json()
            self.assertEqual(waiting["status"], "connecting")
            response = self.post("/api/stop", {"id": sid})
            self.assertEqual(response.status_code, 200, response.text)
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(failures)
        self.assertTrue(feed.closed)
        self.assertTrue(FakeShowDirector.instances[0].closed)
        self.assertEqual(server.LIVE, {})

    def test_resume_pins_same_endpoint_notes_and_timeline(self):
        started = self.native()
        sid, show_id = started["id"], started["show_id"]
        timeline = copy.deepcopy(server.LIVE[sid]["simulated_chat"])
        self.connect(sid)
        FakeOBSFeed.instances[0].emit()
        self.post("/api/stop", {"id": sid, "reason": "paused"})
        self.http.obs_config = OBSConfig(port=8791)
        changed = self.post("/api/start", {"brief": self.brief, "notes": self.notes, "runtime": "realtime", "input_type": "obs", "show_id": show_id, "offset": 0.5})
        self.assertEqual(changed.status_code, 400, changed.text)
        self.http.obs_config = OBSConfig(port=8790)
        resumed = self.native(show_id=show_id, offset=0.5)
        self.assertEqual(server.LIVE[resumed["id"]]["simulated_chat"], timeline)
        self.assertEqual(self.connect(resumed["id"])["through"], 0.5)

    def test_cli_validation_precedes_server_creation(self):
        for args in (("--obs-input-port", "0"), ("--obs-input-port", "8786"),
                     ("--obs-recording-dir", str(self.folder)),
                     ("--obs-action-trace-dir", str(self.folder))):
            with self.subTest(args=args), patch("sys.argv", ["producer/server.py", *args]), \
                    patch("server.ThreadingHTTPServer") as factory, patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    server.main()
                self.assertEqual(error.exception.code, 2)
                factory.assert_not_called()
        helper = Mock()
        with patch("sys.argv", ["producer/server.py", "--obs-input-port", "8790"]), \
                patch("server.ThreadingHTTPServer", return_value=helper), patch("builtins.print"):
            server.main()
        self.assertEqual(helper.obs_config.endpoint, "tcp://127.0.0.1:8790")
        self.assertEqual(helper.obs_config.startup_timeout, 120)
        self.assertFalse(helper.enable_live_production)
        self.assertIsNone(helper.obs_action_trace_dir)


if __name__ == "__main__":
    unittest.main()

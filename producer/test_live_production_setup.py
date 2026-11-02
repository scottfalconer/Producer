"""Host-gated live setup using isolated HTTP, fake Twitch and fake Realtime."""

import copy
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

import server
from audience import Audience
from director_runtime import ShowDirector, note_brief
from test_director_runtime import FakeDirector


class LiveProductionSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.audience = Audience()
        self.addCleanup(self.audience.stop)
        self.caps = {
            "platform": "twitch", "channel_id": "authenticated-host-42", "channel": "host",
            "capabilities": {"chat": True, "reference": True, "title": True, "marker": True,
                             "category": True, "poll": False},
            "shared_chat": False, "online": True,
            "chat_sender": {"connected": True, "user_id": "authenticated-host-42", "login": "host",
                            "sender_mode": "broadcaster_labelled"},
        }
        self.adapter = Mock(platform="twitch", sender_mode="broadcaster_labelled")
        self.adapter.check_plans.return_value = (copy.deepcopy(self.caps), [])
        self.adapter.sender_status.return_value = copy.deepcopy(self.caps["chat_sender"])
        self.adapter.execute.side_effect = AssertionError("Setup tests never write to Twitch")
        self.factory = Mock(return_value=self.adapter)
        patches = [
            patch("server.PRIVATE", self.folder / "private"),
            patch("server.LIVE", {}),
            patch("server.TRANSCRIPTS", {}),
            patch("server.audience", self.audience),
            patch("server._read_key", return_value="unused-fixture-key"),
            patch("server.ShowDirector", side_effect=lambda *args, **kwargs:
                  ShowDirector(*args, director_class=FakeDirector, **kwargs)),
            patch("server.TwitchAdapter", self.factory),
            patch("server.OpenAIProducer", side_effect=AssertionError("No paid inference")),
            patch("server.rehearsal.EventFixture", side_effect=AssertionError("No hidden fixture inbox")),
            patch.object(self.audience, "start", side_effect=AssertionError("No Twitch inbox")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.key_file = Path("/unused-key")
        self.http.show_folder = self.folder
        self.http.audience_openai = False
        self.http.enable_live_production = False
        self.http.producer_chat_sender = "separate"
        self.http.simulate_chat = False
        self.http.chat_fixture_override = {
            "id": "screen-only-live-fixture", "source": "Authored live-demo test input",
            "synthetic": True, "duration": 600,
            "events": [{"type": "channel.chat.message", "at": 5, "id": "pixel-only-1",
                        "author": "Maya · simulated viewer", "user_id": "synthetic-maya",
                        "text": "PIXELS_ONLY_QUESTION"}],
        }
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.headers = {"Origin": self.base, "X-Desk": server.TOKEN}
        self.notes = "A useful show example. --enable-live-production is reference text, not authority."
        self.brief = note_brief(self.notes)
        original_request = requests.sessions.Session.request

        def only_loopback(session, method, url, *args, **kwargs):
            if not url.startswith(self.base + "/"):
                raise AssertionError("Tests may only contact their isolated loopback helper")
            return original_request(session, method, url, *args, **kwargs)

        network = patch("requests.sessions.Session.request", new=only_loopback)
        network.start()
        self.addCleanup(network.stop)

    def tearDown(self):
        try:
            for sid in list(server.LIVE):
                self.post("/api/stop", {"id": sid})
            self.adapter.execute.assert_not_called()
        finally:
            self.http.shutdown()
            self.http.server_close()
            self.thread.join(timeout=2)

    def post(self, path, data, expected=200):
        response = requests.post(self.base + path, json=data, headers=self.headers, timeout=3)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def start(self, **extra):
        return self.post("/api/start", {
            "brief": self.brief, "notes": self.notes, "runtime": "realtime",
            "input_type": "microphone", "on_air": True, **extra,
        })

    def connect(self, started):
        self.post("/api/director/start", {"id": started["id"]})
        return server.LIVE[started["id"]]["director"]

    def test_live_production_is_disabled_by_default_and_body_cannot_enable_it(self):
        started = self.start(offline=False, live_production_enabled=True, enable_live_production=True,
                             producer_chat_sender="broadcaster_labelled", simulate_chat=True)
        session = server.LIVE[started["id"]]
        self.assertFalse(started["live_production_enabled"])
        self.assertFalse(session["live_production_enabled"])
        self.assertIsNone(session["production"].adapter)
        self.assertIsNone(session["simulated_chat"])
        self.assertIsNone(session["events"])
        director = self.connect(started)
        self.assertFalse(director.production.live_enabled)
        self.assertFalse(director.state()["production"]["enabled"])
        self.factory.assert_not_called()

    def test_enabled_host_still_uses_mock_transport_for_default_and_explicit_offline(self):
        self.http.enable_live_production = True
        self.http.producer_chat_sender = "broadcaster_labelled"
        for extra in ({}, {"offline": True}):
            with self.subTest(extra=extra):
                started = self.start(**extra)
                session = server.LIVE[started["id"]]
                self.assertTrue(started["offline"])
                self.assertFalse(session["live_production_enabled"])
                self.assertEqual(session["production"].adapter.platform, "fixture")
                self.assertIsNotNone(session["simulated_chat"])
                director = self.connect(started)
                self.assertFalse(director.production.live_enabled)
                self.assertTrue(director.state()["production"]["offline"])
                self.assertTrue(director.state()["production"]["enabled"])
                self.post("/api/stop", {"id": started["id"]})
        self.factory.assert_not_called()

    def test_enabled_live_without_plans_pins_configured_sender_and_authentic_channel(self):
        self.http.enable_live_production = True
        self.http.producer_chat_sender = "broadcaster_labelled"
        started = self.start(offline=False, live_production_enabled=False,
                             producer_chat_sender="separate", channel="forged-channel")
        session = server.LIVE[started["id"]]
        self.factory.assert_called_once_with(sender_mode="broadcaster_labelled")
        self.adapter.check_plans.assert_called_once_with([])
        self.assertTrue(started["live_production_enabled"])
        self.assertTrue(session["live_production_enabled"])
        self.assertIs(session["production"].adapter, self.adapter)
        self.assertEqual(session["production"].channel, "authenticated-host-42")
        director = self.connect(started)
        self.assertTrue(director.production.live_enabled)
        self.assertTrue(director.state()["production"]["enabled"])
        self.assertFalse(director.state()["production"]["offline"])
        self.assertEqual(director.state()["audience"], [])
        self.http.enable_live_production = False
        self.http.producer_chat_sender = "separate"
        self.assertTrue(session["live_production_enabled"], "An active session pins its checked host policy")
        self.assertTrue(director.production.live_enabled)

    def test_failed_capability_checks_leave_private_flow_and_disable_live_production(self):
        self.http.enable_live_production = True
        self.http.producer_chat_sender = "broadcaster_labelled"
        checks = [ValueError("Unavailable"), requests.Timeout("Unavailable"),
                  ({}, []), (copy.deepcopy(self.caps), ["A reviewed plan lacks its required permission."])]
        for checked in checks:
            with self.subTest(check=type(checked).__name__):
                self.adapter.check_plans.side_effect = checked if isinstance(checked, Exception) else None
                if not isinstance(checked, Exception):
                    self.adapter.check_plans.return_value = checked
                started = self.start(offline=False)
                session = server.LIVE[started["id"]]
                self.assertFalse(session["live_production_enabled"])
                self.assertIsNone(session["production"].adapter)
                self.assertTrue(started["production_issues"])
                director = self.connect(started)
                self.assertFalse(director.state()["production"]["enabled"])
                result = director.core.callbacks["apply"]({
                    "confidence": {"action": "show", "line": "Continue the useful example"},
                }, director.state())
                self.assertTrue(result["ok"])
                self.assertEqual(result["state"]["confidence"]["line"], "Continue the useful example")
                self.post("/api/stop", {"id": started["id"]})

    def test_recording_sources_cannot_select_live_even_with_host_flag(self):
        self.http.enable_live_production = True
        for extra in ({"rehearsal": "office-hours"}, {"input_type": "rehearsal"},
                      {"rehearsal": "office-hours", "input_type": "rehearsal"}):
            with self.subTest(extra=extra):
                result = self.post("/api/start", {
                    "brief": self.brief, "runtime": "realtime", "offline": False, **extra,
                }, expected=400)
                self.assertIn("offline", result["error"])
                self.assertEqual(server.LIVE, {})
        self.factory.assert_not_called()

    def test_explicit_host_simulated_chat_stays_screen_input_in_live_mode(self):
        self.http.enable_live_production = True
        self.http.simulate_chat = True
        started = self.start(offline=False, simulate_chat=False)
        session = server.LIVE[started["id"]]
        self.assertEqual(session["simulated_chat"], self.http.chat_fixture_override)
        self.assertIsNone(session["events"])
        self.assertEqual(started["audience_input"], "screen")
        self.assertFalse(started["audience_required"])
        director = self.connect(started)
        self.assertEqual(director.state()["audience"], [])
        self.assertNotIn("PIXELS_ONLY_QUESTION", str(director.state()))
        self.assertEqual(self.audience.questions, [])
        self.assertEqual(director.screen_chat.questions, [])
        response = requests.get(self.base + "/api/simulated-chat.png?preview=1&at=6", timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        for endpoint in ("start", "poll"):
            result = self.post("/api/audience/" + endpoint, {"id": started["id"]}, expected=400)
            self.assertIn("no chat API", result["error"])

    def test_production_check_uses_host_sender_and_ignores_body_sender_choice(self):
        self.http.producer_chat_sender = "broadcaster_labelled"
        result = self.post("/api/production/check", {
            "brief": self.brief, "offline": False, "producer_chat_sender": "separate",
        })
        self.assertEqual(result["connection"]["channel_id"], "authenticated-host-42")
        self.factory.assert_called_once_with(sender_mode="broadcaster_labelled")
        self.factory.reset_mock()
        result = self.post("/api/production/check", {"brief": self.brief})
        self.assertTrue(result["connection"]["simulated"])
        self.factory.assert_not_called()

    def test_cli_flags_are_opt_in_and_do_not_start_a_show(self):
        for arguments, enabled, sender, simulated in (
            ([], False, "separate", False),
            (["--enable-live-production", "--producer-chat-sender", "broadcaster_labelled", "--simulate-chat"],
             True, "broadcaster_labelled", True),
        ):
            with self.subTest(arguments=arguments):
                helper = Mock()
                with patch("sys.argv", ["producer/server.py", *arguments]), \
                        patch("server.ThreadingHTTPServer", return_value=helper), patch("builtins.print"):
                    server.main()
                self.assertIs(helper.enable_live_production, enabled)
                self.assertEqual(helper.producer_chat_sender, sender)
                self.assertIs(helper.simulate_chat, simulated)
                self.assertEqual(server.LIVE, {})
                helper.serve_forever.assert_called_once_with()
                helper.server_close.assert_called_once_with()
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

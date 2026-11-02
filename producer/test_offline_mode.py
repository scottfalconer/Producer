"""Offline transport is independent of the selected programme source."""

import base64
import copy
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import server
from rehearsal import EventFixture
from test_continuity import BRIEF
from test_production import brief, plan


def reviewed_brief():
    return brief([
        plan("reference", "repo"),
        {**plan("marker", "section"), "text": "Main demonstration", "intention_id": ""},
        {**plan("title", "title"), "text": "Building Producer", "intention_id": ""},
    ])


class OfflineModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.private = patch("server.PRIVATE", self.folder / "private")
        self.private.start()
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

    def start(self, **changes):
        response = self.post("/api/start", {
            "brief": reviewed_brief(), "input_type": "microphone", "on_air": True,
            **changes,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_default_and_explicit_offline_never_check_real_twitch_even_with_plans(self):
        with (
            patch("server.TwitchAdapter", side_effect=AssertionError("No real capabilities")) as adapter,
            patch("server.channel_status", side_effect=AssertionError("No real status")) as status,
            patch("audience.authorization", side_effect=AssertionError("No OAuth")) as auth,
            patch("server.audience.start", side_effect=AssertionError("No real inbox")) as inbox,
            patch("server.rehearsal.Rehearsal", side_effect=AssertionError("No recording")),
        ):
            for setting in ({}, {"offline": True}):
                self.assertTrue(self.post("/api/twitch", {}).json()["offline"])
                caps = self.post("/api/production/check", {"brief": reviewed_brief()})
                self.assertEqual(caps.status_code, 200, caps.text)
                self.assertTrue(caps.json()["connection"]["simulated"])
                started = self.start(**setting)
                sid = started["id"]
                session = server.LIVE[sid]
                self.assertTrue(started["offline"])
                self.assertTrue(started["audience_required"])
                self.assertEqual(started["production_issues"], [])
                self.assertEqual(session["production"].adapter.platform, "fixture")
                self.assertIsInstance(session["events"], EventFixture)
                self.assertIsNone(session["rehearsal"])
                self.assertIsNone(session["test_question"])
                self.assertEqual(server.audience.questions, [])
                self.assertEqual(self.post("/api/audience/start", {"id": sid}).status_code, 200)
                state = requests.get(self.base + "/api/on-air", timeout=3).json()
                self.assertTrue(state["offline"])
                self.assertEqual(state["test_label"], server.OFFLINE_LABEL)
                self.assertIsNone(state["graphic"])
                # Active mode is pinned; a setup request cannot switch its transport.
                self.assertTrue(self.post("/api/twitch", {"offline": False}).json()["offline"])
                self.assertTrue(self.post("/api/production/check", {
                    "brief": reviewed_brief(), "offline": False,
                }).json()["connection"]["simulated"])
                conflicting = self.post("/api/start", {
                    "brief": reviewed_brief(), "offline": False,
                })
                self.assertEqual(conflicting.status_code, 400, conflicting.text)
                self.post("/api/stop", {"id": sid})
            adapter.assert_not_called()
            status.assert_not_called()
            auth.assert_not_called()
            inbox.assert_not_called()

    def test_non_boolean_modes_and_recording_live_mode_are_rejected(self):
        with patch("server.TwitchAdapter") as adapter:
            for value in (None, 0, 1, "true", {}, []):
                for endpoint, data in (
                    ("/api/start", {"brief": BRIEF}),
                    ("/api/twitch", {}),
                    ("/api/production/check", {"brief": BRIEF}),
                ):
                    response = self.post(endpoint, {**data, "offline": value})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertFalse(server.LIVE)
            response = self.post("/api/start", {
                "brief": BRIEF, "rehearsal": "office-hours", "offline": False,
            })
            self.assertEqual(response.status_code, 400, response.text)
            adapter.assert_not_called()

    def test_explicit_false_keeps_real_route_behind_existing_mocked_adapters(self):
        real = Mock()
        real.check_plans.return_value = ({"channel_id": "own-channel"}, [])
        with (
            patch("server.TwitchAdapter", return_value=real) as adapter,
            patch("server.audience.start", return_value={"status": "Mock real connection"}) as inbox,
            patch("server.channel_status", return_value={"checked": time.time(), "channel": "own"}) as status,
            patch("server.last_twitch", None),
        ):
            started = self.start(offline=False)
            session = server.LIVE[started["id"]]
            self.assertFalse(started["offline"])
            self.assertIs(session["production"].adapter, real)
            self.assertIsNone(session["events"])
            self.assertIsNone(session["rehearsal"])
            adapter.assert_called_once_with()
            self.assertEqual(self.post("/api/audience/start", {"id": started["id"]}).status_code, 200)
            inbox.assert_called_once_with(started["id"])
            self.assertTrue(self.post("/api/twitch", {}).json()["offline"])
            self.assertTrue(self.post("/api/twitch", {"offline": True}).json()["offline"])
            status.assert_not_called()
            self.assertEqual(self.post("/api/twitch", {"offline": False}).status_code, 200)
            status.assert_called_once_with()

    def test_offline_microphone_uses_supplied_audio_and_clocked_fictional_events(self):
        supplied = b"selected-microphone-webm"

        def observed(notes, start, end, pcm, frame, *rest):
            self.assertEqual(pcm, b"decoded-mic-pcm")
            self.assertEqual(frame, b"")
            return {"start": start, "end": end, "available": end, "speech": "",
                    "frame_sampled": False, "cue": None}

        with (
            patch("server.rehearsal.Rehearsal", side_effect=AssertionError("No recording")),
            patch("server._read_key", return_value="test-key"), patch("server.OpenAIProducer"),
            patch("server.extract", return_value=b"decoded-mic-pcm") as extract,
            patch("server.produce", side_effect=observed),
            patch("server.judge_audience"),
            patch("server.offer_speech", side_effect=AssertionError("Voice requests remain shelved")) as offer,
        ):
            started = self.start(brief=BRIEF)
            sid = started["id"]
            server.LIVE[sid]["created"] -= 15
            response = self.post("/api/window", {
                "id": sid, "start": 0, "end": 15,
                "audio": base64.b64encode(supplied).decode(),
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["frame_sampled"])
            self.assertEqual(extract.call_args.args[1], supplied)
            polled = self.post("/api/audience/poll", {"id": sid}).json()
            self.assertEqual([q["id"] for q in polled["questions"]], ["c01", "c02"])
            self.assertTrue(all("simulated viewer" in q["author"] for q in polled["questions"]))
            self.assertEqual(
                polled["questions"], self.post("/api/audience/poll", {"id": sid}).json()["questions"]
            )
            self.assertIsNone(server.LIVE[sid]["rehearsal"])
            offer.assert_not_called()

    def test_offline_receipts_and_sharing_memory_are_honestly_simulated(self):
        speech = "Here is the repository and the main demonstration."
        answer = {
            "actions": [
                {"plan_id": "repo", "trigger": "resource_discussed", "quote": speech},
                {"plan_id": "section", "trigger": "section_begins", "quote": speech},
            ]
        }

        def observed(notes, start, end, pcm, frame, history, api, active, memory, context, on_speech):
            return {"start": start, "end": end, "available": end, "speech": speech,
                    "frame_sampled": False, "cue": None, "memory": copy.deepcopy(memory),
                    "production_answer": answer}

        with (
            patch("server._read_key", return_value="test-key"), patch("server.OpenAIProducer"),
            patch("server.extract", return_value=b"pcm"), patch("server.produce", side_effect=observed),
            patch("server.judge_audience"), patch("server.make_public_graphic"),
            patch("server.TwitchAdapter", side_effect=AssertionError("No live action")),
        ):
            started = self.start()
            sid = started["id"]
            response = self.post("/api/window", {"id": sid, "start": 0, "end": 0.1, "audio": "AA=="})
            self.assertEqual(response.status_code, 200, response.text)
            actions = response.json()["actions"]
            self.assertEqual(len(actions), 2)
            self.assertTrue(all("SIMULATED" in a["detail"] for a in actions))
            shared = next(i for i in response.json()["memory"]["items"] if i["id"] == "repo")
            self.assertIn("Simulated", shared["evidence"])
            self.assertNotIn("Twitch confirmed", shared["evidence"])
            self.assertIn("offline", server.LIVE[sid]["production"].ledger.path.parts)

    def test_offline_ledgers_do_not_reuse_real_action_receipts(self):
        real = Mock()
        real.check_plans.return_value = ({"channel_id": "own-channel"}, [])
        show_id = "one-reviewed-show-identity"
        with patch("server.TwitchAdapter", return_value=real):
            live = self.start(offline=False, show_id=show_id)
            live_path = server.LIVE[live["id"]]["production"].ledger.path
            self.post("/api/stop", {"id": live["id"]})
            reused = self.post("/api/start", {"brief": reviewed_brief(), "input_type": "microphone", "show_id": show_id})
            self.assertEqual(reused.status_code, 400)  # Transcript scope also rejects cross-mode reuse.
            offline = self.start(show_id="separate-offline-show-identity")
            offline_path = server.LIVE[offline["id"]]["production"].ledger.path
            self.assertNotEqual(live_path, offline_path)
            self.assertEqual(live_path, self.folder / "private/actions" / (show_id + ".json"))


if __name__ == "__main__":
    unittest.main()

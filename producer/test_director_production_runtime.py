"""Actual director routing, outgoing chat pixels and lifecycle; no model/network."""

import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import server
from audience import Audience
from director_runtime import ShowDirector, note_brief
from on_air import OnAir
from production import Ledger, Production
from rehearsal import MockTwitch
from screen_chat_overlay import _visible, composite, overlay_png, with_producer_posts
from show_materials import ShowMaterials
from test_director_runtime import FakeDirector


class ProductionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.audience = Audience()
        self.audience_patch = patch("server.audience", self.audience)
        self.audience_patch.start()
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network in offline routing"))
        self.network.start()
        self.running, self.sid = True, "offline-production-runtime"
        brief = note_brief("Office Hours: a useful demonstration.")
        self.adapter = MockTwitch()
        self.session = {
            "offset": 0, "created": time.monotonic() - 30, "seen": time.monotonic(),
            "notes": "PRIVATE_SHOW_NOTES", "brief": brief, "memory": {},
            "materials": ShowMaterials(self.folder), "on_air": OnAir(True),
            "visual": None, "offline": True, "audience_input": "screen",
            "production_policy": "realtime-production-v1", "producer_posts": [],
            "production": Production(brief, Ledger(self.folder / "actions.json"), self.adapter, "synthetic-office-hours"),
        }
        self.lock = threading.Lock()
        self.director = ShowDirector(self.sid, self.session, key="unused", lock=self.lock,
            active=lambda: self.running, public_state=lambda: server.public_state(self.sid, self.session),
            audience=self.audience, director_class=FakeDirector)
        self.session["director"] = self.director

    def tearDown(self):
        self.director.close()
        self.network.stop()
        self.audience_patch.stop()
        self.audience.stop()
        self.temp.cleanup()

    def show(self):
        return self.director.core.callbacks["apply"]({
            "confidence": {"action": "show", "line": "PRIVATE_PRESENTER_CUE"},
            "card": {"action": "show", "kind": "reference", "line": "Project: https://github.com/scottfalconer/Producer",
                     "attribution": "Producer project"},
        }, self.director.state())["state"]

    def share(self, state=None):
        state = state or self.show()
        return self.director.core.callbacks["production"]({"action": "share_on_air",
            "output_key": state["production"]["shareable_outputs"][0]["output_key"]}, state)

    def test_confirmed_actual_message_is_exact_future_pixel_input_not_an_audience_inbox(self):
        state = self.show()
        expected_text = state["public"]["graphic"]["line"] + " — Producer project"
        result = self.share(state)
        self.assertTrue(result["ok"])
        receipt = result["receipt"]
        self.assertEqual(receipt["message"], expected_text)
        self.assertEqual(receipt["sender_login"], "producer")
        posts = self.director.producer_post_snapshot()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["text"], receipt["message"])
        self.assertEqual(posts[0]["author"], receipt["display_name"])
        self.assertEqual(posts[0]["user_id"], receipt["sender_id"])
        self.assertNotIn("PRIVATE_", json.dumps(posts))
        self.assertEqual(result["state"]["audience"], [])
        self.assertEqual(self.audience.questions, [])
        events = with_producer_posts([], posts)
        self.assertEqual(_visible(events, 0), [])
        self.assertEqual(_visible(events, 0.5)[0]["text"], expected_text)
        self.assertEqual(overlay_png(events, 0), overlay_png([], 0))
        self.assertNotEqual(overlay_png(events, 0.5), overlay_png([], 0.5))
        # The real compositor includes the post in the next selected image.
        jpg = io.BytesIO()
        Image.new("RGB", (1280, 720), "#191b20").save(jpg, "JPEG")
        self.assertNotEqual(composite(jpg.getvalue(), events, 0.5), composite(jpg.getvalue(), [], 0.5))
        # Merging a recorded delivery twice cannot add a second chat row.
        self.assertEqual(with_producer_posts(events, posts), events)

    def test_duplicate_share_has_one_delivery_and_one_visible_echo(self):
        result = self.share()
        repeated = self.share(result["state"])
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(len(self.session["production"].ledger.snapshot()), 1)
        self.assertEqual(len(self.director.producer_post_snapshot()), 1)

    def test_stale_screen_or_disabled_output_cannot_send(self):
        old = self.show()
        state = self.director.apply({"card": {"action": "clear"}}, old)["state"]
        self.assertFalse(self.share(old)["ok"])
        self.assertEqual(self.director.producer_post_snapshot(), [])
        self.session["on_air"].enabled = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.director.production_action({"action": "set_title", "request_id": "t1", "text": "A title"}, state)

    def test_failed_or_uncertain_delivery_never_appears_in_chat(self):
        self.adapter.execute = lambda plan: {"state": "uncertain", "detail": "No delivery confirmation", "message": plan["text"]}
        result = self.share()
        self.assertFalse(result["ok"])
        self.assertEqual(self.director.producer_post_snapshot(), [])
        repeated = self.share(result["state"])
        self.assertTrue(repeated["duplicate"])
        self.assertFalse(repeated["ok"])

    def test_stop_during_dispatch_keeps_receipt_without_publishing_echo(self):
        execute = self.adapter.execute
        def stopped(plan):
            result = execute(plan)
            self.running = False
            return result
        self.adapter.execute = stopped
        result = self.share()
        self.assertTrue(result["show_stopped"])
        self.assertEqual(result["receipt"]["state"], "done")
        self.assertEqual(self.director.producer_post_snapshot(), [])

    def test_own_screen_echo_rejected_but_viewer_response_remains_eligible(self):
        receipt = self.share()["receipt"]
        posts = self.director.producer_post_snapshot()
        jpg = io.BytesIO()
        Image.new("RGB", (1280, 720), "#191b20").save(jpg, "JPEG")
        frame = composite(jpg.getvalue(), with_producer_posts([], posts), 0.5)
        self.director.append(0, base64.b64encode(b"\0" * 24000).decode(), base64.b64encode(frame).decode(), source_end=0.5)
        state = self.director.state()
        comment = {"frame_id": state["screen_frames"][0]["frame_id"], "author": "Producer", "text": receipt["message"]}
        with self.assertRaisesRegex(ValueError, "own delivery echo"):
            self.director.apply({"screen_comment": comment, "card": {"action": "show", "kind": "question"}}, state)
        self.assertEqual(self.director.screen_chat.questions, [])
        viewer = {**comment, "author": "Maya", "text": "Can that project work with OBS on Linux?"}
        result = self.director.apply({"screen_comment": viewer, "card": {"action": "show", "kind": "question"}}, state)
        self.assertEqual(result["state"]["public"]["graphic"]["line"], viewer["text"])

    def test_viewer_zoom_feedback_can_stay_private_without_changing_or_sharing_public_output(self):
        self.show()
        self.director.apply({"context": {"action": "set", "subject": "Applied AI Office Hours",
                                        "subtitle": "Building a useful agent"}}, self.director.state())
        text = "Can we zoom in on the text? It's too small to read."
        jpg = io.BytesIO()
        Image.new("RGB", (1280, 720), "#191b20").save(jpg, "JPEG")
        events = [{"type": "channel.chat.message", "id": "viewer-zoom", "at": 0.1,
                   "user_id": "synthetic-viewer", "author": "Maya", "text": text}]
        frame = composite(jpg.getvalue(), events, 0.5)
        self.director.append(0, base64.b64encode(b"\0" * 24000).decode(),
                             base64.b64encode(frame).decode(), source_end=0.5)
        examined = self.director.state()
        comment = {"frame_id": examined["screen_frames"][0]["frame_id"], "author": "Maya", "text": text}
        # This exercises the model's chosen destination, not a local classifier:
        # the full observed feedback is a private cue, with public output kept.
        result = self.director.core.callbacks["apply"]({
            "screen_comment": comment, "confidence": {"action": "show"},
            "card": {"action": "keep"}, "context": {"action": "keep"},
        }, examined)
        self.assertTrue(result["ok"])
        state = result["state"]
        self.assertEqual(state["confidence"]["line"], text)
        self.assertEqual(state["confidence"]["attribution"], "Maya")
        for slot in ("graphic", "context", "visual"):
            # Keep may renew a display deadline; content and output identity
            # must remain unchanged when this feedback is directed backstage.
            before, after = examined["public"].get(slot), state["public"].get(slot)
            self.assertEqual({k: v for k, v in (after or {}).items() if k != "until"},
                             {k: v for k, v in (before or {}).items() if k != "until"})
        self.assertEqual(state["production"]["shareable_outputs"], examined["production"]["shareable_outputs"])
        self.assertNotIn(text, json.dumps(state["production"]["shareable_outputs"]))
        self.assertNotIn("PRIVATE_PRESENTER_CUE", json.dumps(state["production"]["shareable_outputs"]))
        self.assertEqual(self.director.producer_post_snapshot(), [])
        self.assertEqual(self.adapter.receipts, [])
        self.assertEqual(self.session["production"].ledger.snapshot(), [])
        self.assertIsNone(self.director.screen_chat.selection)

    def test_poll_and_other_production_tools_do_not_create_fake_chat_posts(self):
        for args in (
            {"action": "create_poll", "request_id": "poll1", "text": "Next demo?", "choices": ["Agents", "Hardware"], "duration": 15},
            {"action": "create_marker", "request_id": "marker1", "text": "Working demonstration"},
            {"action": "set_title", "request_id": "title1", "text": "Applied AI Office Hours"},
            {"action": "set_category", "request_id": "category1", "text": "Science & Technology", "category_id": "509670"},
        ):
            result = self.director.production_action(args, self.director.state())
            self.assertTrue(result["ok"], result)
        self.assertEqual(self.director.producer_post_snapshot(), [])
        poll_id = self.session["production"].ledger.snapshot()[0]["remote_id"]
        args = {"action": "get_poll_result", "poll_id": poll_id}
        self.assertEqual(self.director.production_action(args, self.director.state())["poll"]["status"], "active")
        self.director.audio_end = 16
        result = self.director.production_action(args, self.director.state())
        self.assertEqual(result["poll"]["counts"], [3, 2])
        self.assertEqual(result["poll"]["status"], "completed")

    def live_setup(self, *, enabled, chat_layer=True):
        self.director.close()
        self.adapter = Mock(platform="twitch")
        self.adapter.execute.side_effect = lambda plan: {
            "state": "done", "remote_id": "confirmed-delivery",
            "sender_id": "host-1", "sender_login": "scottfalconer",
            "sender_mode": "broadcaster_labelled",
            "message": "(From Producer) " + plan["text"],
        }
        self.session["production"].adapter = self.adapter
        self.session.update(offline=False, live_production_enabled=enabled,
            simulated_chat={"events": [], "duration": 600} if chat_layer else None)
        self.director = ShowDirector(self.sid, self.session, key="unused", lock=self.lock,
            active=lambda: self.running, public_state=lambda: server.public_state(self.sid, self.session),
            audience=self.audience, director_class=FakeDirector)
        self.session["director"] = self.director

    def test_live_director_requires_pinned_opt_in_and_keeps_dynamic_actions_off(self):
        self.live_setup(enabled=False)
        self.assertFalse(self.director.state()["production"]["enabled"])
        with self.assertRaisesRegex(ValueError, "not enabled"):
            self.director.production_action({"action": "share_on_air", "output_key": "public-unavailable"}, self.show())
        self.adapter.execute.assert_not_called()
        self.live_setup(enabled=True)
        self.assertEqual(self.director.state()["production"]["capabilities"], ["share_on_air", "execute_plan"])
        with self.assertRaisesRegex(ValueError, "reviewed"):
            self.director.production_action({"action": "set_title", "request_id": "unreviewed", "text": "Not approved"}, self.director.state())
        with self.assertRaisesRegex(ValueError, "offline"):
            self.director.production_action({"action": "get_poll_result", "poll_id": "unknown"}, self.director.state())
        self.adapter.execute.assert_not_called()

    def test_explicit_live_delivery_echo_uses_actual_sender_and_marked_text(self):
        self.live_setup(enabled=True)
        result = self.share()
        self.assertTrue(result["ok"])
        self.assertFalse(result["simulated"])
        receipt = result["receipt"]
        self.assertEqual(receipt["sender_mode"], "broadcaster_labelled")
        self.assertEqual(receipt["sender_login"], "scottfalconer")
        post = self.director.producer_post_snapshot()[0]
        self.assertEqual(post["author"], "scottfalconer")
        self.assertEqual(post["user_id"], "host-1")
        self.assertEqual(post["text"], receipt["message"])
        self.assertTrue(post["text"].startswith("(From Producer) "))
        self.assertTrue(self.director.own_chat_echo({"author": "ScottFalconer", "text": post["text"]}))
        self.assertFalse(self.director.own_chat_echo({"author": "ScottFalconer", "text": "An actual different comment from the host"}))

    def test_live_receipt_prevents_self_echo_even_without_simulated_chat_layer(self):
        self.live_setup(enabled=True, chat_layer=False)
        result = self.share()
        self.assertEqual(self.director.producer_post_snapshot(), [])
        self.assertTrue(self.director.own_chat_echo({"author": "scottfalconer", "text": result["receipt"]["message"]}))


if __name__ == "__main__":
    unittest.main()

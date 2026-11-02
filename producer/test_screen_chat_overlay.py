"""Visible-only mock chat, deterministic geometry and source-clock gates."""

import io
import math
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

from screen_chat_overlay import MAX_AGE, MAX_MESSAGES, _font, _wrap, composite, overlay_png, render_overlay, snapshot_events
from PIL import ImageDraw


def message(ident="m1", at=10, user="v1", text="Can you show that again?"):
    return {"type": "channel.chat.message", "at": at, "id": ident,
            "author": "Maya · simulated viewer", "user_id": user, "text": text}


class ScreenChatOverlayTests(unittest.TestCase):
    def test_future_messages_never_change_visible_pixels(self):
        event = message()
        baseline = render_overlay([], 9).tobytes()
        self.assertEqual(render_overlay([event], 9).tobytes(), baseline)
        self.assertNotEqual(render_overlay([event], 10).tobytes(), baseline)
        expired = 10 + MAX_AGE + .001
        self.assertEqual(render_overlay([event], expired).tobytes(), render_overlay([], expired).tobytes())

    def test_message_delete_user_clear_and_room_clear_remove_pixels(self):
        event = message()
        baseline = render_overlay([], 11).tobytes()
        removals = [
            {"type": "channel.chat.message_delete", "message_id": "m1"},
            {"type": "channel.chat.clear_user_messages", "user_id": "v1"},
            {"type": "channel.chat.clear"},
        ]
        for removal in removals:
            with self.subTest(removal=removal):
                events = [event, {**removal, "id": "remove", "at": 11}]
                self.assertNotEqual(render_overlay(events, 10.9).tobytes(), baseline)
                self.assertEqual(render_overlay(events, 11).tobytes(), baseline)

    def test_newest_messages_and_whole_overflow_are_used_without_shortening(self):
        early = message("older", at=1, text="An older message")
        recent = [message(str(i), at=i + 2, text="Hi " + str(i)) for i in range(MAX_MESSAGES)]
        self.assertEqual(render_overlay([early, *recent], 10).tobytes(), render_overlay(recent, 10).tobytes())
        long = message(text="A very long original message. " * 100)
        self.assertEqual(render_overlay([long], 11).tobytes(), render_overlay([], 11).tobytes())

    def test_chat_scrolls_chronologically_instead_of_backfilling_with_older_short_rows(self):
        older = message("older", at=0, text="An old reaction")
        long = message("long", at=1, text="A longer question about how this was put together. " * 5)
        recent = [message(str(i), at=2 + i, text="Hi " + str(i)) for i in range(5)]
        self.assertEqual(render_overlay([older, long, *recent], 20).tobytes(),
                         render_overlay(recent, 20).tobytes())

    def test_snapshot_and_image_are_deterministic_without_text_metadata(self):
        original = {"events": [message(text="  What's that?\nCan it come off? 🎃  ")]}
        pinned = snapshot_events(original)
        before = overlay_png(pinned, 12)
        original["events"][0]["text"] = "Changed after selection"
        self.assertEqual(before, overlay_png(pinned, 12))
        self.assertNotEqual(before, overlay_png(original, 12))
        with Image.open(io.BytesIO(before)) as image:
            self.assertEqual((image.mode, image.size), ("RGBA", (1280, 720)))
            self.assertEqual(image.getpixel((100, 100))[3], 0)
            self.assertEqual(image.getpixel((1000, 300))[3], 0)
            self.assertGreater(image.getpixel((1000, 680))[3], 200)
            self.assertFalse(image.info)

    def test_composite_is_readable_geometry_and_native_image_bound(self):
        source = io.BytesIO()
        Image.new("RGB", (852, 480), "blue").save(source, "JPEG")
        result = composite(source.getvalue(), [message()], 12)
        self.assertLessEqual(len(result), 350000)
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual((image.format, image.size), ("JPEG", (1280, 720)))
            self.assertGreater(image.getpixel((100, 100))[2], 240)
            self.assertGreater(image.getpixel((1000, 300))[2], 240)
            self.assertLess(image.getpixel((1000, 680))[2], 100)

    def test_old_style_preserves_original_pixels_and_new_cards_leave_gaps(self):
        from screen_chat_overlay_v1 import render_overlay as legacy
        from screen_chat_overlay_v2 import render_overlay as compact
        events = [message(text="What is that?"), message("m2", at=11, text="A second comment")]
        self.assertEqual(render_overlay(events, 12, style="panel-v1").tobytes(), legacy(events, 12).tobytes())
        for at in (0, 11, 12, 50, 103):
            for width, height in ((1280, 720), (640, 360)):
                self.assertEqual(render_overlay(events, at, width, height, style="compact-v2").tobytes(),
                                 compact(events, at, width, height).tobytes())
        self.assertNotEqual(render_overlay(events, 12).tobytes(), legacy(events, 12).tobytes())
        self.assertNotEqual(render_overlay(events, 12).tobytes(), compact(events, 12).tobytes())
        image = render_overlay(events, 12)
        self.assertEqual(image.getpixel((1000, 300))[3], 0)
        # Transparent space between individually backed bottom-anchored cards.
        alphas = [image.getpixel((1100, y))[3] for y in range(400, 700)]
        self.assertTrue(any(a == 0 for a in alphas))
        self.assertTrue(any(a > 200 for a in alphas))
        with self.assertRaisesRegex(ValueError, "Unknown"):
            render_overlay(events, 12, style="unregistered")

    def test_opening_disclosure_is_plain_bottom_text_and_expires_without_messages(self):
        opening = render_overlay([], 0)
        self.assertIsNotNone(opening.getbbox())
        left, top, right, bottom = opening.getbbox()
        self.assertGreater(top, 640)
        self.assertLess(bottom, 710)
        self.assertAlmostEqual((left + right) / 2, 640, delta=3)
        self.assertEqual(opening.getpixel((640, 640))[3], 0, "No background panel")
        self.assertIn((255, 255, 255, 255), set(opening.getdata()))
        self.assertEqual(opening.tobytes(), render_overlay([], 11.999).tobytes())
        self.assertIsNone(render_overlay([], 12).getbbox())
        self.assertIsNone(render_overlay([], 60).getbbox())
        message_only = render_overlay([message(at=0)], 1)
        self.assertEqual(message_only.crop((0, 640, 1280, 720)).tobytes(),
                         opening.crop((0, 640, 1280, 720)).tobytes(), "Chat must not cover disclosure")
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.json"
            events.write_text('{"events": []}')
            output = Path(directory) / "frames"
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("screen_chat_overlay.py")),
                                     "--events", str(events), "--output", str(output), "--duration", "20"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            index = json.loads((output / "index.json").read_text())
            self.assertEqual([row["at"] for row in index], [0, 12])
            with Image.open(output / index[1]["file"]) as image:
                self.assertIsNone(image.getbbox())

    def test_bundled_fonts_render_fixture_punctuation_and_emoji_without_shortening(self):
        for char, emoji in (("—", False), ("’", False), ("🎃", True), ("👋", True)):
            font = _font(21, emoji=emoji)
            self.assertNotEqual(bytes(font.getmask(char)), bytes(font.getmask("\U0010ffff")))
        draw = ImageDraw.Draw(Image.new("RGBA", (400, 300)))
        text = "  First time here — Halloween 🎃  "
        self.assertEqual("".join(_wrap(draw, text, 21, 240)), text)

    def test_invalid_clock_and_dimensions_fail_closed(self):
        for at in (True, -1, math.nan, math.inf, None):
            with self.subTest(at=at), self.assertRaises(ValueError):
                render_overlay([], at)
        with self.assertRaises(ValueError):
            render_overlay([], 0, width=10000)


if __name__ == "__main__":
    unittest.main()

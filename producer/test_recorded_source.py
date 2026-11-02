"""Causal recorded playback, media identity and local preparation; no API."""

import io
import math
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from recorded_source import RecordingSource


class RecordingSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.original = self.folder / "selected.mp4"
        self.original.write_bytes(b"explicitly selected source")
        self.source = RecordingSource(self.original, 2, cache_dir=self.folder / "cache")
        self.source.cache_dir.mkdir()
        self.source.clip_path.write_bytes(b"pinned video")
        samples = struct.pack("<48000h", *(range(-24000, 24000)))
        self.source.pcm_path.write_bytes(samples)
        self.source.report = {"duration_s": 2}
        self.source._pin()

    def tearDown(self):
        self.temp.cleanup()

    def test_audio_is_exact_requested_past_interval(self):
        actual = self.source.audio24(0.5, 1, available_through=1)
        self.assertEqual(len(actual), 24000)
        self.assertEqual(actual, self.source.pcm_path.read_bytes()[24000:48000])

    def test_audio_rejects_future_invalid_and_unbounded_intervals_without_reading(self):
        for start, end, clock in ((0, 0.5, 0.49), (-1, 0.5, 1), (0, 2.1, 3),
                                  (0, math.nan, 1), (0, True, 1), (0, 0.5, None),
                                  (0, 0.5, math.inf), (1, 1, 1)):
            with self.subTest(start=start, end=end, clock=clock), self.assertRaises(ValueError):
                self.source.audio24(start, end, available_through=clock)

    def test_pcm_change_is_rejected(self):
        self.source.pcm_path.write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.source.audio24(0, 0.5, available_through=0.5)

    def test_frame_uses_pre_cutoff_sample_and_no_future_access(self):
        jpeg = b"\xff\xd8sample\xff\xd9"
        with patch("recorded_source._run", return_value=jpeg) as run:
            self.assertEqual(self.source.frame_at(1, available_through=1), jpeg)
        command = run.call_args.args[0]
        seek = float(command[command.index("-ss") + 1])
        length = float(command[command.index("-t") + 1])
        self.assertLess(seek, 1)
        self.assertLessEqual(seek + length, 1)
        with patch("recorded_source._run", side_effect=AssertionError("Must not decode")):
            for end, clock in ((1, 0.9), (0, 1), (3, 3), (True, 1), (1, None)):
                with self.subTest(end=end, clock=clock), self.assertRaises(ValueError):
                    self.source.frame_at(end, available_through=clock)

    def test_bad_jpeg_and_changed_clip_are_rejected(self):
        for value in (b"notjpeg", b"\xff\xd8" + b"x" * 350000 + b"\xff\xd9"):
            with patch("recorded_source._run", return_value=value), self.assertRaises(ValueError):
                self.source.frame_at(1, available_through=1)
        self.source.clip_path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.source.frame_at(1, available_through=1)

    def test_screen_chat_is_pinned_visible_in_native_frame_and_never_future(self):
        events = [{"type": "channel.chat.message", "id": "fictional", "at": .5,
                   "author": "Maya · simulated viewer", "user_id": "m1", "text": "What is that prop?"}]
        selected = RecordingSource(self.original, 2, cache_dir=self.source.cache_dir, chat_events=events)
        selected.report = self.source.report
        selected._pin()
        events[0]["text"] = "Later mutation"
        self.assertEqual(selected.chat_events[0]["text"], "What is that prop?")
        output = io.BytesIO()
        Image.new("RGB", (852, 480), "blue").save(output, "JPEG")
        with patch("recorded_source._run", return_value=output.getvalue()):
            actual = selected.frame_at(1, available_through=1)
        with Image.open(io.BytesIO(actual)) as image:
            self.assertEqual(image.size, (1280, 720))
        self.assertLessEqual(len(actual), 350000)
        with patch("recorded_source._run", side_effect=AssertionError("Future source must not decode")):
            with self.assertRaisesRegex(ValueError, "not yet available"):
                selected.frame_at(1, available_through=.9)

    def test_invalid_duration_and_incomplete_cache_are_rejected(self):
        for duration in (0, -1, 601, True, math.inf):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                RecordingSource(self.original, duration, cache_dir=self.folder / "other")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.source.prepare()

    def test_generated_media_prepares_reuses_and_gates_last_packet(self):
        original = self.folder / "generated.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=320x180:r=30:d=2", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=2", "-t", "2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(original),
        ], check=True, capture_output=True)
        cache = self.folder / "generated-cache"
        source = RecordingSource(original, 2, cache_dir=cache)
        report = source.prepare()
        self.assertEqual(report["pcm"]["bytes"], 96000)
        self.assertEqual(len(source.audio24(1.5, 2, available_through=2)), 24000)
        with Image.open(io.BytesIO(source.frame_at(1, available_through=1))) as image:
            self.assertEqual(image.width, 640)
        with self.assertRaisesRegex(ValueError, "not yet available"):
            source.audio24(1.5, 2, available_through=1.9)
        self.assertEqual(source.prepare(), report)
        source.pcm_path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "cache has changed"):
            source.prepare()

    def test_delayed_audio_preserves_video_clock_and_exact_packet_length(self):
        original = self.folder / "delayed-audio.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=320x180:r=30:d=3", "-itsoffset", "0.125",
            "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=44100:duration=3", "-t", "3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(original),
        ], check=True, capture_output=True)
        source = RecordingSource(original, 2, cache_dir=self.folder / "delayed-cache")
        report = source.prepare()
        self.assertEqual(report["pcm"]["bytes"], 96000)
        pcm = source.audio24(0, 0.5, available_through=0.5)
        samples = struct.unpack("<12000h", pcm)
        self.assertTrue(all(value == 0 for value in samples[:2400]))
        self.assertGreater(max(abs(value) for value in samples[4800:]), 500)
        self.assertEqual(len(source.audio24(1.5, 2, available_through=2)), 24000)

    def test_missing_audio_tail_is_not_filled_to_match_video(self):
        original = self.folder / "short-audio.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=320x180:r=30:d=3", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=44100:duration=1", "-t", "3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(original),
        ], check=True, capture_output=True)
        source = RecordingSource(original, 2, cache_dir=self.folder / "short-cache")
        with self.assertRaisesRegex(ValueError, "audio does not cover"):
            source.prepare()


if __name__ == "__main__":
    unittest.main()

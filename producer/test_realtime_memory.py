"""On-demand past recall and pinned native-audio rehearsal packets; no API calls."""

import io
import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from audience import Audience
from rehearsal import CLIP, Rehearsal
from transcript import MAX_READ_CHARS, MAX_READ_ROWS, ShowTranscript


class RealtimeRecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = ShowTranscript(self.folder, "realtime-memory-test-0001", {"input_type": "authored"}, "notes-hash")

    def tearDown(self):
        self.temp.cleanup()

    def test_model_can_query_earlier_topic_and_short_ai_term_without_future_text(self):
        self.store.record(0, 2, "The first AI example used an unsigned request.")
        self.store.record(2, 4, "We then discussed a different topic.")
        self.store.record(4, 6, "FUTURE_SENTINEL AI solution.")
        read = self.store.read(before=4, query="AI")
        self.assertEqual([row["start"] for row in read["excerpts"]], [0])
        self.assertNotIn("FUTURE_SENTINEL", json.dumps(read))
        self.assertEqual(read["through"], 4)
        self.assertEqual(read["show_id"], self.store.show_id)

    def test_explicit_range_returns_only_complete_windows_within_cutoff(self):
        for n in range(4):
            self.store.record(n * 2, n * 2 + 2, f"Section {n}")
        read = self.store.read(before=6, start=2, end=100)
        self.assertEqual([(row["start"], row["end"]) for row in read["excerpts"]], [(2, 4), (4, 6)])
        self.assertEqual(read["range"], {"start": 2, "end": 6})
        self.assertEqual(self.store.read(before=6, start=3, end=5)["excerpts"], [])

    def test_read_is_bounded_but_does_not_modify_or_truncate_stored_recognition(self):
        words = "Recognized source text " * 1000
        self.store.record(0, 2, words)
        before = self.store.path.read_bytes()
        read = self.store.read(before=2, query="source", limit=999)
        self.assertEqual(sum(len(row["text"]) for row in read["excerpts"]), MAX_READ_CHARS)
        self.assertTrue(read["truncated"])
        self.assertTrue(read["excerpts"][0]["excerpted"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_default_read_is_recent_and_other_show_cannot_enter_recall(self):
        for n in range(15):
            self.store.record(n * 2, n * 2 + 2, f"This show section {n}")
        other = ShowTranscript(self.folder, "realtime-memory-test-0002", {"input_type": "authored"}, "notes-hash")
        other.record(0, 2, "OTHER_SHOW_SENTINEL")
        read = self.store.read(before=30)
        self.assertEqual(len(read["excerpts"]), MAX_READ_ROWS)
        self.assertEqual(read["excerpts"][0]["end"], 30)
        self.assertTrue(read["truncated"])
        self.assertNotIn("OTHER_SHOW_SENTINEL", json.dumps(read))

    def test_failed_or_untranscribed_intervals_do_not_become_speech(self):
        self.store.record(0, 2, "Accepted words.")
        self.store.record(2, 4, "FAILED_SENTINEL", status="failed")
        self.store.record(6, 8, status="quiet_not_transcribed")
        read = self.store.read(before=8, start=0, end=8)
        self.assertEqual([row["text"] for row in read["excerpts"]], ["Accepted words."])
        self.assertEqual(read["gaps"], [{"start": 4, "end": 6, "status": "not_observed"}])

    def test_invalid_read_inputs_are_rejected_and_zero_cutoff_is_empty(self):
        for args in ({"before": True}, {"before": math.nan}, {"before": 8, "start": 9},
                     {"before": 8, "query": "x" * 2001}, {"before": 8, "query": []},
                     {"before": 8, "limit": False}, {"before": 8, "limit": 0}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.read(**args)
        self.assertEqual(self.store.read(before=0)["excerpts"], [])


class NativeRehearsalTests(unittest.TestCase):
    def demo(self):
        with patch("rehearsal.CLIP"):
            return Rehearsal(Audience(), "fixture")

    def test_audio_packet_uses_pinned_source_and_trims_before_resampling(self):
        demo = self.demo()
        with patch("rehearsal.extract", return_value=b"x" * 50000) as extract:
            packet = demo.audio24(4, 4.5, available_through=4.5)
        self.assertEqual(len(packet), 24000)
        args = extract.call_args.args[0]
        self.assertIn(str(CLIP), args)
        self.assertEqual(args[args.index("-af") + 1], "atrim=end=0.500000,aresample=24000")
        self.assertEqual(args[args.index("-ar") + 1], "24000")
        with patch("rehearsal.extract", return_value=b"short"), self.assertRaisesRegex(ValueError, "does not cover"):
            demo.audio24(4, 4.5, available_through=4.5)

    def test_audio_and_frame_cannot_read_ahead_or_accept_invalid_source_time(self):
        demo = self.demo()
        with patch("rehearsal.extract", side_effect=AssertionError("Invalid input must not decode")):
            for start, end, available in ((-1, 0.5, 1), (0, 0.5, 0.49), (0, 3, 3),
                                          (179.5, 180.5, 181), (0, math.nan, 1), (0, True, 1)):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    demo.audio24(start, end, available_through=available)
            for end, available in ((0, 1), (1, 0.9), (181, 182), (True, 1), (math.inf, 2)):
                with self.subTest(end=end), self.assertRaises(ValueError):
                    demo.frame_at(end, available_through=available)

    def test_frame_sampling_is_bounded_before_cutoff_and_rejects_invalid_output(self):
        demo = self.demo()
        jpeg = b"\xff\xd8sample\xff\xd9"
        with patch("rehearsal.extract", return_value=jpeg) as extract:
            self.assertEqual(demo.frame_at(10, available_through=10), jpeg)
        args = extract.call_args.args[0]
        seek, duration = float(args[args.index("-ss") + 1]), float(args[args.index("-t") + 1])
        self.assertLess(seek, 10)
        self.assertLessEqual(seek + duration, 10)
        self.assertEqual(args[args.index("-vf") + 1], "scale=640:-2")
        for invalid in (b"not-jpeg", b"\xff\xd8" + b"x" * 350000 + b"\xff\xd9"):
            with patch("rehearsal.extract", return_value=invalid), self.assertRaises(ValueError):
                demo.frame_at(10)

    def test_audio_prefix_is_independent_of_future_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = [int(12000 * math.sin(2 * math.pi * 440 * n / 16000)) for n in range(8000)]
            packets = []
            for index, suffix in enumerate(([0] * 8000, [12000] * 8000)):
                path = Path(temporary) / f"fixture-{index}.wav"
                with wave.open(str(path), "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(16000)
                    stream.writeframes(struct.pack("<16000h", *(first + suffix)))
                with patch("rehearsal.CLIP", path):
                    demo = Rehearsal(Audience(), "fixture")
                    packets.append(demo.audio24(0, 0.5, available_through=0.5))
            self.assertEqual(len(packets[0]), 24000)
            self.assertEqual(packets[0], packets[1])

    @unittest.skipUnless(CLIP.exists(), "Local owner-selected media is not installed")
    def test_installed_recording_supplies_native_packet_and_bounded_frame(self):
        demo = Rehearsal(Audience(), "fixture")
        self.assertEqual(len(demo.audio24(0, 0.5, available_through=0.5)), 24000)
        frame = demo.frame_at(4, available_through=4)
        self.assertLessEqual(len(frame), 350000)
        with Image.open(io.BytesIO(frame)) as image:
            self.assertEqual(image.width, 640)


if __name__ == "__main__":
    unittest.main()

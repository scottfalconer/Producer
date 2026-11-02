import json
import tempfile
import unittest
from pathlib import Path
from engine import (
    DEFAULT_BRIEF,
    editorial_cue,
    measured_cue,
    meter,
    prior_only,
    produce,
    valid_brief,
    window_media,
)
from server import as_of, events
import numpy as np
import subprocess

BRIEF = valid_brief(json.loads(DEFAULT_BRIEF.read_text()))


class Stub:
    def transcribe(self, _):
        return "We need one concrete example for our audience."

    def decide(self, brief, start, end, text, frame, past):
        self.past = past
        return {
            "action": "cue",
            "cue": "Give one example.",
            "quote": "one concrete example",
        }


PCM = (np.ones(32000, dtype="<i2") * 6000).tobytes()


class CutTests(unittest.TestCase):
    def test_late_previous_cue_suppresses_without_entering_model_context(self):
        api = Stub()
        prior = {
            "start": 0,
            "end": 45,
            "available": 50,
            "cue": {"kind": "editorial", "line": "Clarify the test."},
        }
        future = {
            "start": 90,
            "end": 135,
            "available": 140,
            "cue": {"kind": "editorial", "line": "Future event."},
        }
        row = produce(BRIEF, 45, 90, PCM, b"image", [prior, future], api)
        self.assertEqual(api.past, [])
        self.assertIsNone(row["cue"])
        self.assertEqual(row["outcome"], "suppressed")
        self.assertEqual(prior_only([prior, future], 90), [prior])
        self.assertIsNotNone(
            produce(BRIEF, 45, 90, PCM, b"image", [future], Stub())["cue"]
        )

    def test_cancel_after_transcription_prevents_editorial_request(self):
        api = Stub()
        checks = iter([True, False])
        with self.assertRaisesRegex(ValueError, "ended before editorial"):
            produce(BRIEF, 0, 20, PCM, b"", [], api, lambda: next(checks))
        self.assertFalse(hasattr(api, "past"))

    def test_uncertain_or_unquoted_candidate_is_quiet(self):
        for answer in (
            None,
            {},
            {"action": "quiet"},
            {"action": "cue", "quote": "absent evidence", "cue": "Interrupt."},
        ):
            self.assertIsNone(editorial_cue(answer, "Actual speech"))
        self.assertIsNone(measured_cue(meter(PCM)))
        self.assertIsNone(measured_cue(meter(b"")))
        low = measured_cue(meter(b"\x00\x00" * 16000 * 16))
        self.assertEqual(low["kind"], "signal")
        self.assertIn("intentional pause", low["evidence"])
        self.assertIsNone(measured_cue(meter(b"\x00\x00" * 16000 * 2)))

    def test_prefix_response_is_invariant_to_future_speech_cues_and_measurements(self):
        initial = dict(
            start=0,
            end=45,
            available=50,
            cue={
                "kind": "editorial",
                "line": "Give one example.",
                "evidence": "one example",
            },
            meter=meter(PCM),
            outcome="candidate",
        )
        future = dict(
            initial,
            start=90,
            end=135,
            available=140,
            cue={"kind": "signal", "line": "Future sentinel", "evidence": "Future"},
        )
        a = {"brief": BRIEF, "duration": 180, "rows": [initial]}
        b = {**a, "rows": [initial, future]}
        for t in (0, 44, 49, 50, 90, 139):
            self.assertEqual(as_of(a, t), as_of(b, t))
        self.assertNotIn("Future", json.dumps(as_of(b, 50)))
        self.assertNotIn("Give one example", json.dumps(as_of(b, 49)))
        self.assertEqual(as_of(b, 50), as_of(b, 50))
        self.assertEqual(as_of(b, 10)["eligible"], [])

    def test_freshness_and_adjacent_cooldown_in_final_projection(self):
        a = dict(
            start=0,
            end=45,
            available=50,
            cue={"kind": "editorial", "line": "Give one example."},
        )
        b = dict(
            a,
            start=45,
            end=90,
            available=95,
            cue={"kind": "editorial", "line": "Explain this term."},
        )
        doc = {"brief": BRIEF, "duration": 240, "rows": [a, b]}
        self.assertEqual(len(events(doc)), 1)
        self.assertEqual(events(doc)[0]["until"], 68)

    def test_decoding_excludes_a_changed_future_audio_and_frame(self):
        with tempfile.TemporaryDirectory() as raw:

            def make(name, color, freq):
                path = Path(raw) / name
                args = [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=red:s=96x64:r=10:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=220:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color={color}:s=96x64:r=10:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={freq}:duration=1",
                    "-filter_complex",
                    "[0:v][1:a][2:v][3:a]concat=n=2:v=1:a=1[v][a]",
                    "-map",
                    "[v]",
                    "-map",
                    "[a]",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s16le",
                    str(path),
                ]
                subprocess.run(args, check=True)
                return path

            a, b = make("a.mkv", "blue", 330), make("b.mkv", "yellow", 660)
            aa, af = window_media(a, 0, 1)
            ba, bf = window_media(b, 0, 1)
            # Resampler taps can straddle the boundary. Identical entire inputs
            # including the final samples are required by this product.
            self.assertEqual(af, bf)
            self.assertTrue(aa == ba, "Future audio changed a prefix sample")
            self.assertNotEqual(window_media(a, 1, 2), window_media(b, 1, 2))


class ProjectionTests(unittest.TestCase):
    def test_fully_masked_cue_absent_from_retrospective_index(self):
        from server import review_index

        row = {
            "start": 240,
            "end": 270,
            "available": 275,
            "meter": meter(PCM),
            "outcome": "candidate",
            "cue": {
                "kind": "editorial",
                "line": "Give an example.",
                "evidence": "example",
            },
        }
        doc = {"brief": BRIEF, "duration": 3600, "rows": [row]}
        self.assertNotIn("window-0", [c["id"] for c in review_index(doc)])
        self.assertIn("window-0", [c["id"] for c in as_of(doc, 280)["eligible"]])

    def test_audio_cutoff_is_exposed_without_future_cues(self):
        row = {
            "start": 0,
            "end": 45,
            "cutoff": 45.25,
            "available": 49,
            "cue": None,
            "outcome": "quiet",
            "meter": meter(PCM),
        }
        doc = {"brief": BRIEF, "duration": 60, "rows": [row]}
        self.assertIsNone(as_of(doc, 48)["observation"])
        self.assertEqual(as_of(doc, 49)["observation"]["cutoff"], 45.25)


if __name__ == "__main__":
    unittest.main()

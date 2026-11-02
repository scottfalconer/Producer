"""Durable speech and private recall, with no external model or Twitch calls."""

import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from continuity import new_memory
from engine import fingerprint, produce
from test_continuity import BRIEF
from test_engine import PCM
from transcript import MAX_CONTEXT_CHARS, MAX_CONTEXT_ROWS, PART_CHARS, ShowTranscript

SOURCE = {"input_type": "microphone", "rehearsal": None, "offline": True, "transcriber": "test-asr"}
SHOW_ID = "local-test-show-123456"


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "transcripts"
        self.store = ShowTranscript(self.folder, SHOW_ID, SOURCE, fingerprint(BRIEF))

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        return [json.loads(line) for line in self.store.path.read_text().splitlines()]

    def test_full_speech_is_split_without_loss_and_private_file_permissions(self):
        speech = "Earlier question: what changes with local control? " * 500
        self.store.record(0, 8, speech)
        parts = [r for r in self.rows() if r["kind"] == "segment"]
        self.assertEqual("".join(r["text"] for r in parts), speech)
        self.assertTrue(all(len(r["text"]) <= PART_CHARS for r in parts))
        self.assertTrue(all(r["start"] == 0 and r["end"] == 8 and r["speaker"] == "unknown" for r in parts))
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(self.store.summary()["segments"], 1)
        self.assertTrue(all("audio" not in row and "frame" not in row for row in self.rows()))

    def test_pause_resume_preserves_same_show_and_rejects_overlap_or_finished_id(self):
        self.store.record(0, 8, "A complete first explanation.")
        self.store.stop(10, "paused")
        resumed = ShowTranscript(self.folder, SHOW_ID, SOURCE, fingerprint(BRIEF), 10)
        resumed.record(10, 18, "The follow-up explanation.")
        with self.assertRaisesRegex(ValueError, "overlaps"):
            resumed.record(10, 18, "Must not duplicate speech.")
        resumed.stop(18, "ended")
        self.assertEqual(resumed.summary()["segments"], 2)
        self.assertEqual(resumed.summary()["gaps"], 1)
        with self.assertRaisesRegex(ValueError, "has ended"):
            ShowTranscript(self.folder, SHOW_ID, SOURCE, fingerprint(BRIEF), 18)
        self.assertTrue(resumed.path.exists())

    def test_reused_id_cannot_join_different_source_or_brief(self):
        self.store.stop(0, "paused")
        before = self.store.path.read_bytes()
        for source, digest in (({**SOURCE, "input_type": "tab"}, fingerprint(BRIEF)), (SOURCE, "changed-brief")):
            with self.subTest(source=source, digest=digest), self.assertRaisesRegex(ValueError, "different notes or input"):
                ShowTranscript(self.folder, SHOW_ID, source, digest)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unknown_id_paths_links_and_hardlinks_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            ShowTranscript(self.folder, "../outside-transcript", SOURCE, "hash")
        victim = Path(self.temp.name) / "private.txt"
        victim.write_text("Do not touch this file")
        link_id = "unsafe-local-test-link"
        (self.folder / (link_id + ".jsonl")).symlink_to(victim)
        with self.assertRaises(ValueError):
            ShowTranscript(self.folder, link_id, SOURCE, "hash")
        hard_id = "unsafe-local-hard-link"
        os.link(victim, self.folder / (hard_id + ".jsonl"))
        with self.assertRaises(ValueError):
            ShowTranscript(self.folder, hard_id, SOURCE, "hash")
        linked_folder = Path(self.temp.name) / "linked-directory"
        linked_folder.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "not a link"):
            ShowTranscript(linked_folder, SHOW_ID, SOURCE, "hash")
        self.assertEqual(victim.read_text(), "Do not touch this file")

    def test_quiet_failed_and_unobserved_intervals_are_not_invented_transcript(self):
        self.store.record(0, 8, status="quiet_not_transcribed")
        self.store.record(8, 16, status="no_text")
        self.store.record(20, 28, status="failed")
        self.store.stop(30)
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows if r["kind"] == "segment"], ["quiet_not_transcribed", "no_text", "failed"])
        self.assertEqual([(r["start"], r["end"]) for r in rows if r["kind"] == "gap"], [(16, 20), (28, 30)])
        self.assertEqual(self.store.context(30, "What did I say?")["excerpts"], [])
        self.assertEqual(self.store.summary()["segments"], 0)

    def test_past_only_retrieval_finds_earlier_topic_and_stays_bounded(self):
        self.store.record(0, 8, "We opened by asking about local control and the hidden costs.")
        for n in range(1, 22):
            self.store.record(n * 8, (n + 1) * 8, ("Unrelated weather illustration. " * 80))
        self.store.record(176, 184, "FUTURE SENTINEL must never enter an earlier recall.")
        context = self.store.context(176, "Let's wrap up the local control tradeoff.", new_memory(BRIEF), BRIEF["goal"])
        self.assertNotIn("FUTURE SENTINEL", json.dumps(context))
        self.assertIn("hidden costs", json.dumps(context))
        self.assertLessEqual(len(context["excerpts"]), MAX_CONTEXT_ROWS)
        self.assertLessEqual(sum(len(r["text"]) for r in context["excerpts"]), MAX_CONTEXT_CHARS)
        self.assertTrue(all(r["end"] <= 176 for r in context["excerpts"]))
        self.assertEqual(self.store.context(0, "local control")["excerpts"], [])

    def test_reopened_store_retrieves_only_accepted_prior_segments(self):
        self.store.record(0, 8, "The first accepted thought.")
        self.store.record(8, 16, "A future thought relative to the requested cut.")
        self.store.stop(16, "paused")
        reopened = ShowTranscript(self.folder, SHOW_ID, SOURCE, fingerprint(BRIEF), 16)
        context = reopened.context(8, "thought")
        self.assertEqual([r["text"] for r in context["excerpts"]], ["The first accepted thought."])
        self.assertEqual(context["through"], 8)

    def test_history_does_not_satisfy_current_quote_validators(self):
        past_speech = "Local control means all of the processing stays on your computer."
        current = "Now I would like to explore another angle."
        history = {"through": 8, "excerpts": [{"id": "old", "start": 0, "end": 8, "text": past_speech}]}
        answer = {
            "updates": [{"id": "why", "status": "covered", "quote": past_speech, "next_action": "", "next_action_quote": ""}],
            "commitments": [], "closing": False, "closing_quote": "", "boundary": True,
            "cue": {"line": "Return to local control", "quote": past_speech, "intention_id": "why"},
            "activity": {"phase": "opening"},
        }
        api = Mock()
        api.transcribe.return_value = current
        with patch("continuity.structured", return_value=copy.deepcopy(answer)) as structured:
            row = produce(BRIEF, 8, 16, PCM, b"", [], api, memory=new_memory(BRIEF), on_speech=lambda speech: history)
        self.assertEqual(structured.call_args.args[2]["earlier_transcript"], history)
        self.assertEqual(structured.call_args.args[2]["speech"], current)
        self.assertIsNone(row["cue"])
        self.assertEqual(row["memory"]["items"][0]["status"], "open")

    def test_write_failure_is_explicit_and_partial_record_is_not_retrieved(self):
        with patch("transcript.os.write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "could not be saved"):
                self.store.record(0, 8, "This cannot be saved")
        self.store.record(0, 8, "A" * (PART_CHARS + 10))
        lines = self.store.path.read_text().splitlines(keepends=True)
        self.store.path.write_text("".join(lines[:-1]))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.store.context(8, "A")

    def test_actual_authored_return_answer_can_remind_the_next_unfinished_action(self):
        # Model answer captured in transcript-recall-api-be392e2beb27.json;
        # replayed locally here, without depending on a private artifact or API.
        speech = "That was a useful detour. Back to the request comparison we left unfinished."
        brief = copy.deepcopy(BRIEF)
        brief["intentions"] = [{"id": "compare", "text": "Compare signed and unsigned requests",
            "kind": "demonstrate", "priority": "essential", "next_action": "Complete the request comparison"}]
        memory = new_memory(brief)
        memory["items"][0].update(status="started", at=8, evidence="Earlier work started")
        answer = {
            "activity": {"label": "Returning to the unfinished request comparison", "quote": speech, "phase": "returning"},
            "actions": [], "research_cue": {"research_id": "", "line": "", "quote": ""},
            "updates": [{"id": "compare", "status": "started", "quote": speech, "next_action": "", "next_action_quote": ""}],
            "commitments": [], "closing": False, "closing_quote": "", "boundary": True,
            "cue": {"line": "Next: send signed request", "quote": speech, "intention_id": "compare"},
        }
        api = Mock()
        api.transcribe.return_value = speech
        with patch("continuity.structured", return_value=answer):
            row = produce(brief, 100, 108, PCM, b"", [], api, memory=memory)
        self.assertEqual(row["cue"]["line"], "Next: send signed request")
        self.assertEqual(row["cue"]["kind"], "editorial")  # Older answer had no explicit cue kind.
        self.assertEqual(row["cue"]["evidence"], speech)
        self.assertEqual(row["memory"]["items"][0]["status"], "started")
        for phase in ("developing", "opening"):
            changed = copy.deepcopy(answer)
            changed["activity"]["phase"] = phase
            with patch("continuity.structured", return_value=changed):
                quiet = produce(brief, 100, 108, PCM, b"", [], api, memory=memory)
            self.assertEqual(quiet["cue"]["line"], "Next: send signed request")


if __name__ == "__main__":
    unittest.main()

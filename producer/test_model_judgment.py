"""The model chooses private editorial timing and a sourced stable reference."""

import copy
import unittest
from unittest.mock import Mock, patch

from continuity import OBSERVATION, apply_observation, new_memory, restore_memory
from engine import produce
from test_continuity import BRIEF
from test_engine import PCM

SPEECH = "There are several useful angles we could explore from here."


class ModelJudgmentTests(unittest.TestCase):
    def answer(self, **changes):
        return {"updates": [], "commitments": [], "closing": False, "closing_quote": "",
                "boundary": False, "activity": {"phase": "developing"},
                "cue": {"line": "Try a concrete example", "quote": SPEECH, "intention_id": "", "kind": "editorial"},
                "presenter_reference": {"action": "replace", "line": "When ready, share the repository",
                    "quote": "Share the repository", "source": "brief", "intention_id": "repo"}, **changes}

    def observe(self, answer, memory=None, history=None, earlier=None):
        api = Mock()
        api.transcribe.return_value = SPEECH
        with patch("continuity.structured", return_value=answer) as structured:
            row = produce(BRIEF, 100, 108, PCM, b"", history or [], api,
                          memory=memory or new_memory(BRIEF), on_speech=lambda _: earlier or {})
        return row, structured

    def test_model_can_select_later_note_without_fixed_order_or_boundary(self):
        row, _ = self.observe(self.answer())
        self.assertEqual(row["cue"]["line"], "Try a concrete example")
        self.assertTrue(row["memory"]["reference_decided"])
        self.assertEqual(row["memory"]["presenter_reference"]["intention_id"], "repo")
        self.assertEqual(row["memory"]["presenter_reference"]["source"], "brief")
        self.assertEqual(row["memory"]["items"][0]["status"], "open")

    def test_model_reference_keeps_exact_wording_or_clears_deliberately(self):
        first, _ = self.observe(self.answer())
        original = first["memory"]["presenter_reference"]
        keep = self.answer(presenter_reference={"action": "keep"})
        kept = apply_observation(first["memory"], keep, SPEECH, 116, brief=BRIEF)
        self.assertEqual(kept["presenter_reference"], original)
        self.assertEqual(kept["reference_at"], 116)
        cleared = apply_observation(kept, {"presenter_reference": {"action": "clear"}}, SPEECH, 124, brief=BRIEF)
        self.assertTrue(cleared["reference_decided"])
        self.assertIsNone(cleared["presenter_reference"])
        self.assertFalse(new_memory(BRIEF)["reference_decided"])

    def test_reference_can_use_known_past_detail_without_pretending_it_is_current(self):
        earlier = {"excerpts": [{"id": "old-question", "start": 0, "end": 8,
                                 "text": "Which tradeoff matters most for independent builders?"}]}
        answer = self.answer(presenter_reference={"action": "replace", "line": "Return to the builders' tradeoff",
            "quote": "Which tradeoff matters most", "source": "transcript", "intention_id": ""})
        row, sent = self.observe(answer, earlier=earlier)
        self.assertEqual(row["memory"]["presenter_reference"]["source"], "transcript")
        self.assertEqual(sent.call_args.args[2]["speech"], SPEECH)
        self.assertEqual(sent.call_args.args[2]["earlier_transcript"], earlier)
        self.assertEqual(row["memory"]["items"][0]["status"], "open")

    def test_reference_requires_actual_named_source_and_known_optional_id(self):
        for changes in (
            {"quote": "Never supplied in any source"},
            {"source": "current"},  # This quote is in the brief, not current speech.
            {"intention_id": "invented"}, {"line": "X" * 181},
        ):
            answer = self.answer()
            answer["presenter_reference"].update(changes)
            row, _ = self.observe(answer)
            self.assertTrue(row["memory"]["reference_decided"])
            self.assertIsNone(row["memory"]["presenter_reference"])

    def test_covered_intention_can_be_a_wrapup_reference_without_reopening_state(self):
        memory = new_memory(BRIEF)
        memory["items"][0]["status"] = "covered"
        answer = self.answer(presenter_reference={"action": "replace", "line": "Land the local-control takeaway",
            "quote": "Explain local control", "source": "brief", "intention_id": "why"})
        row, _ = self.observe(answer, memory)
        self.assertEqual(row["memory"]["presenter_reference"]["intention_id"], "why")
        self.assertEqual(row["memory"]["items"][0]["status"], "covered")

    def test_recent_cue_does_not_trigger_hidden_timer_or_prefix_veto(self):
        answer = self.answer(cue={"line": "Next: make the unfinished step visible", "quote": SPEECH,
                                  "kind": "memory", "intention_id": "test"})
        memory = new_memory(BRIEF)
        memory["items"][1]["status"] = "started"
        history = [{"start": 80, "end": 88, "available": 99,
                    "cue": {"kind": "editorial", "line": answer["cue"]["line"]}}]
        row, _ = self.observe(answer, memory, history)
        self.assertEqual(row["cue"]["line"], answer["cue"]["line"])
        self.assertEqual(row["cue"]["kind"], "memory")
        self.assertEqual(row["outcome"], "candidate")

    def test_model_quiet_and_source_invalid_cue_remain_quiet(self):
        for line, quote in (("", ""), ("Show the result", "This was never said")):
            answer = self.answer(cue={"line": line, "quote": quote, "intention_id": "", "kind": "editorial"})
            row, _ = self.observe(answer)
            self.assertIsNone(row["cue"])

    def test_reference_and_deliberate_quiet_survive_pause_snapshot(self):
        row, _ = self.observe(self.answer())
        restored = restore_memory(BRIEF, row["memory"])
        self.assertEqual(restored["presenter_reference"], row["memory"]["presenter_reference"])
        self.assertTrue(restored["reference_decided"])
        quiet = copy.deepcopy(restored)
        quiet["presenter_reference"] = None
        self.assertTrue(restore_memory(BRIEF, quiet)["reference_decided"])
        self.assertIsNone(restore_memory(BRIEF, quiet)["presenter_reference"])

    def test_reference_is_required_in_same_observation_schema_without_an_extra_call(self):
        self.assertIn("presenter_reference", OBSERVATION["required"])
        _, sent = self.observe(self.answer())
        self.assertEqual(sent.call_count, 1)

    def test_model_can_clear_or_keep_current_cue_without_inventing_source_evidence(self):
        for action in ("keep", "clear"):
            answer = self.answer(cue={"action": action, "line": "", "quote": "",
                                      "intention_id": "", "kind": "editorial"})
            row, _ = self.observe(answer)
            self.assertIsNone(row["cue"])
            self.assertEqual(row["cue_action"], action)
            self.assertTrue(row["cue_decided"])
        answer = self.answer(cue={"action": "show", "line": "Invented cue", "quote": "Not in current speech",
                                  "intention_id": "", "kind": "editorial"})
        row, _ = self.observe(answer)
        self.assertIsNone(row["cue"])
        self.assertEqual(row["cue_action"], "keep")
        self.assertFalse(row["cue_decided"])

    def test_only_an_explicit_valid_model_action_counts_as_a_cue_decision(self):
        implicit, _ = self.observe(self.answer())
        self.assertEqual(implicit["cue_action"], "show")
        self.assertFalse(implicit["cue_decided"])
        answer = self.answer()
        answer["cue"]["action"] = "show"
        explicit, _ = self.observe(answer)
        self.assertTrue(explicit["cue_decided"])
        silent = produce(BRIEF, 0, 8, b"\x00\x00" * 16000 * 8, b"", [], Mock(), memory=new_memory(BRIEF))
        self.assertEqual(silent["cue_action"], "keep")
        self.assertFalse(silent["cue_decided"])


if __name__ == "__main__":
    unittest.main()

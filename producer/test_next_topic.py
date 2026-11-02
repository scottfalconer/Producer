"""Current model judgment can override editorial cadence; evidence bounds remain."""

import copy
import unittest
from unittest.mock import patch

import numpy as np

from continuity import new_memory
from engine import produce, suppress, valid_brief


BRIEF = valid_brief(
    {
        "title": "A practical example",
        "goal": "Explain the problem, discuss rollout choices, then show an example.",
        "audience": "New builders",
        "context": "Authored fixture",
        "tip": "Gentle topic cues at real openings",
        "recovery": "Return to the example",
        "rundown": [],
        "timing": {"mode": "flexible", "minutes": 30},
        "intentions": [
            {
                "id": "problem",
                "text": "Explain the problem",
                "kind": "explain",
                "priority": "essential",
                "next_action": "Explain the problem",
            },
            {
                "id": "rollout",
                "text": "Discuss rollout choices",
                "kind": "explain",
                "priority": "essential",
                "next_action": "Discuss rollout choices",
            },
            {
                "id": "example",
                "text": "Show a working example",
                "kind": "demonstrate",
                "priority": "essential",
                "next_action": "Run the example",
            },
        ],
        "research": [],
    }
)
SPEECH = "That covers the starting point. We have a moment before we move on."


class TranscriptOnly:
    def transcribe(self, *_):
        return SPEECH

    def decide(self, *_):
        raise AssertionError("The continuity path must handle this fixture")


class NextTopicTests(unittest.TestCase):
    def memory(self):
        memory = new_memory(BRIEF)
        memory["items"][0].update(
            status="covered", at=0, evidence="Earlier explanation"
        )
        return memory

    def observation(self, **changes):
        return {
            "updates": [],
            "commitments": [],
            "closing": False,
            "closing_quote": "",
            "boundary": True,
            "activity": {
                "phase": "opening",
                "label": "A completed thought",
                "quote": SPEECH,
            },
            "cue": {
                "line": "Next: rollout choices.",
                "kind": "transition",
                "quote": SPEECH,
                "intention_id": "rollout",
            },
            **changes,
        }

    def run_window(self, observation, memory=None, history=None, start=0):
        with patch("continuity.structured", return_value=observation):
            return produce(
                BRIEF,
                start,
                start + 15,
                np.full(16000 * 15, 6000, dtype="<i2").tobytes(),
                b"",
                history or [],
                TranscriptOnly(),
                memory=self.memory() if memory is None else memory,
            )

    def test_finished_topic_and_open_planned_topic_can_produce_a_gentle_next_cue(self):
        row = self.run_window(self.observation())
        self.assertEqual(row["cue"]["kind"], "transition")
        self.assertEqual(row["cue"]["intention_id"], "rollout")
        self.assertEqual(row["cue"]["line"], "Next: rollout choices.")
        self.assertEqual(row["memory"]["items"][0]["status"], "covered")
        self.assertEqual(row["memory"]["items"][1]["status"], "open")

    def test_model_topic_choice_does_not_need_boundary_flag(self):
        row = self.run_window(self.observation(boundary=False))
        self.assertEqual(row["cue"]["kind"], "transition")

    def test_model_can_choose_a_topic_cue_during_a_developing_thought(self):
        row = self.run_window(
            self.observation(
                activity={
                    "phase": "developing",
                    "label": "Still explaining",
                    "quote": SPEECH,
                }
            )
        )
        self.assertEqual(row["cue"]["kind"], "transition")

    def test_a_covered_topic_can_be_revisited_but_unknown_id_is_rejected(self):
        for ident in ("rollout", "invented-topic"):
            with self.subTest(ident=ident):
                memory = self.memory()
                memory["items"][1]["status"] = "covered"
                observation = self.observation()
                observation["cue"]["intention_id"] = ident
                cue = self.run_window(observation, memory)["cue"]
                self.assertEqual(cue is not None, ident == "rollout")

    def test_model_can_replace_a_recent_transition_without_timer_veto(self):
        history = [
            {
                "start": 0,
                "end": 10,
                "available": 10,
                "cue": {"kind": "transition", "line": "Next: the problem to solve."},
            }
        ]
        row = self.run_window(self.observation(), history=history, start=30)
        self.assertEqual(row["cue"]["kind"], "transition")
        self.assertEqual(row["outcome"], "candidate")
        row = self.run_window(self.observation(), history=history, start=50)
        self.assertEqual(row["cue"]["kind"], "transition")

    def test_model_can_return_to_a_recent_topic(self):
        history = [
            {
                "end": 10,
                "available": 10,
                "cue": {"kind": "transition", "line": "Next: rollout choices."},
            }
        ]
        row = self.run_window(self.observation(), history=history, start=60)
        self.assertEqual(row["cue"]["kind"], "transition")
        self.assertEqual(row["outcome"], "candidate")

    def test_legacy_cooldown_remains_only_for_historical_replays(self):
        candidate = {"kind": "editorial", "line": "Explain the actual tradeoff"}
        history = [
            {
                "available": 20,
                "cue": {"kind": "transition", "line": "Next: another topic."},
            }
        ]
        self.assertEqual(suppress(candidate, history, 100), candidate)
        self.assertIsNone(suppress(candidate, history, 100, legacy=True))
        self.assertEqual(suppress(candidate, history, 171), candidate)

    def test_silence_is_not_a_topic_request_or_a_reason_to_call_the_provider(self):
        class NoCalls:
            def transcribe(self, *_):
                raise AssertionError("Silence is not a request for the next topic")

            def decide(self, *_):
                raise AssertionError("Silence is not a request for the next topic")

        memory = self.memory()
        before = copy.deepcopy(memory)
        row = produce(
            BRIEF, 0, 16, b"\x00\x00" * 16000 * 16, b"", [], NoCalls(), memory=memory
        )
        self.assertIsNone(row["cue"])
        self.assertEqual(row["memory"], before)


if __name__ == "__main__":
    unittest.main()

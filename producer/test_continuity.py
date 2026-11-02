import copy
import json
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from continuity import new_memory, apply_observation, host_update, restore_memory
from engine import valid_brief, produce
from audience import Audience
from preparation import prepare, research

BRIEF = valid_brief(
    {
        "title": "Demo",
        "goal": "Explain the choice and show the result",
        "audience": "New builders",
        "context": "Fictional test",
        "tip": "Sparse cues",
        "recovery": "Return to the test",
        "rundown": [{"name": "Demo", "minutes": 30}],
        "timing": {"mode": "flexible", "minutes": 30},
        "intentions": [
            {
                "id": "why",
                "text": "Explain local control",
                "kind": "explain",
                "priority": "essential",
                "next_action": "Explain local control",
            },
            {
                "id": "test",
                "text": "Run the test",
                "kind": "demonstrate",
                "priority": "essential",
                "next_action": "Send the unsigned request",
            },
            {
                "id": "repo",
                "text": "Share the repository",
                "kind": "share",
                "priority": "essential",
                "next_action": "Share the repository",
            },
        ],
        "research": [],
    }
)


def change(ident, state, quote, next_action=""):
    return {
        "updates": [
            {
                "id": ident,
                "status": state,
                "quote": quote,
                "next_action": next_action,
                "next_action_quote": quote,
            }
        ],
        "commitments": [],
        "closing": False,
    }


class MemoryTests(unittest.TestCase):
    def test_future_promise_and_claimed_demo_are_not_completion(self):
        for ident, quote in [
            ("test", "I will run the test after this question."),
            ("repo", "I have shared the repository link now."),
            ("why", "I will explain local control after we finish the test."),
        ]:
            out = apply_observation(
                new_memory(BRIEF), change(ident, "covered", quote), quote, 15
            )
            self.assertEqual(
                next(i for i in out["items"] if i["id"] == ident)["status"], "uncertain"
            )

    def test_out_of_order_explanation_preserves_unfinished_demo(self):
        m = new_memory(BRIEF)
        m = apply_observation(
            m,
            change(
                "test",
                "started",
                "Next I will send the unsigned request.",
                "Send the unsigned request",
            ),
            "Next I will send the unsigned request.",
            15,
        )
        quote = "Local control matters because the presenter can choose where the data goes and stop processing at any time."
        m = apply_observation(m, change("why", "covered", quote), quote, 30)
        self.assertEqual(m["items"][0]["status"], "covered")
        self.assertEqual(m["items"][1]["status"], "started")
        self.assertEqual(m["items"][1]["next_action"], "Send the unsigned request")

    def test_exact_evidence_and_known_id_required(self):
        m = new_memory(BRIEF)
        for answer in [
            change("why", "covered", "invented proof"),
            change("absent", "covered", "some current evidence"),
        ]:
            self.assertEqual(
                apply_observation(m, answer, "some current evidence", 15)["items"],
                m["items"],
            )

    def test_manual_choice_wins_until_explicit_reopen(self):
        m = host_update(new_memory(BRIEF), "test", "skipped")
        q = "I plan to show the test later."
        self.assertEqual(
            apply_observation(m, change("test", "started", q), q, 15)["items"][1][
                "status"
            ],
            "skipped",
        )
        host_update(m, "test", "open")
        self.assertFalse(m["items"][1]["locked"])

    def test_pause_resume_preserves_dynamic_promises(self):
        m = new_memory(BRIEF)
        q = "I will show the failure case before we finish."
        answer = {
            "updates": [],
            "commitments": [
                {
                    "text": "Show failure case",
                    "quote": q,
                    "next_action": "Show failure case",
                }
            ],
        }
        m = apply_observation(m, answer, q, 15)
        restored = restore_memory(BRIEF, m)
        self.assertEqual(restored["items"][-1]["text"], "Show failure case")
        self.assertEqual(len(restored["items"]), 4)
        self.assertEqual(len(apply_observation(m, answer, q, 30)["items"]), 4)

    def test_repeated_commitment_is_not_duplicated(self):
        m = new_memory(BRIEF)
        q = "I still need to share the repository link before we close."
        out = apply_observation(
            m,
            {
                "commitments": [
                    {"text": q, "quote": q, "next_action": "Share the repository"}
                ]
            },
            q,
            45,
        )
        self.assertEqual(len(out["items"]), 3)

    def test_invented_next_action_does_not_replace_prepared_action(self):
        q = "I will run the test later."
        out = apply_observation(
            new_memory(BRIEF),
            change("test", "started", q, "Prepare a new test server"),
            q,
            15,
        )
        self.assertEqual(out["items"][1]["next_action"], "Send the unsigned request")

    def test_closing_needs_current_evidence(self):
        m = new_memory(BRIEF)
        self.assertFalse(
            apply_observation(
                m,
                {"closing": True, "closing_quote": "invented"},
                "still explaining",
                15,
            )["closing"]
        )

    def test_silence_does_not_generate_rescue_or_provider_call(self):
        class NoCalls:
            def transcribe(self, *_):
                raise AssertionError("Must not infer a stall from silence")

        row = produce(
            BRIEF,
            0,
            16,
            b"\x00\x00" * 16000 * 16,
            b"",
            [],
            NoCalls(),
            memory=new_memory(BRIEF),
        )
        self.assertIsNone(row["cue"])
        self.assertEqual(row["memory"]["items"], new_memory(BRIEF)["items"])

    def test_model_cue_does_not_require_transition_and_memory_can_update(self):
        import numpy as np

        q = "Local control matters because the presenter can choose where the data goes and stop processing at any time."

        class Api:
            def transcribe(self, *_):
                return q

        answer = {
            **change("why", "covered", q),
            "boundary": False,
            "cue": {"line": "Run the test", "quote": q, "intention_id": "test"},
        }
        with patch("continuity.structured", return_value=answer):
            row = produce(
                BRIEF,
                0,
                15,
                (np.ones(32000, dtype="<i2") * 6000).tobytes(),
                b"",
                [],
                Api(),
                memory=new_memory(BRIEF),
            )
        self.assertEqual(row["cue"]["line"], "Run the test")
        self.assertEqual(row["memory"]["items"][0]["status"], "covered")

    def test_v2_rundown_can_be_empty(self):
        brief = copy.deepcopy(BRIEF)
        brief["rundown"] = []
        self.assertEqual(valid_brief(brief)["rundown"], [])

    def test_model_judges_when_an_interrupted_demo_reminder_is_useful(self):
        import numpy as np
        from unittest.mock import Mock

        speech = (
            "That question opens an interesting discussion about the implementation."
        )
        memory = new_memory(BRIEF)
        memory["items"][1]["status"] = "started"
        api = Mock()
        api.transcribe.return_value = speech
        for phase, expected in [
            ("opening", True),
            ("developing", True),
            ("returning", True),
        ]:
            answer = {
                "updates": [],
                "commitments": [],
                "boundary": True,
                "activity": {"phase": phase},
                "cue": {
                    "line": "Send the unsigned request",
                    "quote": speech,
                    "intention_id": "test",
                },
            }
            with patch("continuity.structured", return_value=answer):
                row = produce(
                    BRIEF,
                    0,
                    15,
                    (np.ones(32000, dtype="<i2") * 6000).tobytes(),
                    b"",
                    [],
                    api,
                    memory=memory,
                )
            self.assertEqual(bool(row["cue"]), expected)
            self.assertEqual(
                row["memory"]["items"][1]["next_action"], "Send the unsigned request"
            )

    def test_source_grounded_model_wording_is_not_vetoed_by_regex(self):
        import numpy as np

        for line, q in [
            ("Let me run that test", "Let me run that test now."),
            ("Run the test now", "Run the test now, he said."),
        ]:

            class Api:
                def transcribe(self, *_):
                    return q

            answer = {
                "updates": [],
                "boundary": True,
                "cue": {"line": line, "quote": q, "intention_id": "test"},
            }
            with patch("continuity.structured", return_value=answer):
                row = produce(
                    BRIEF,
                    0,
                    15,
                    (np.ones(32000, dtype="<i2") * 6000).tobytes(),
                    b"",
                    [],
                    Api(),
                    memory=new_memory(BRIEF),
                )
            self.assertEqual(row["cue"]["line"], line)

    def test_brief_preserves_type_priority_and_flexible_clock(self):
        self.assertEqual(valid_brief(BRIEF), BRIEF)
        broken = copy.deepcopy(BRIEF)
        broken["intentions"][1]["id"] = "why"
        with self.assertRaises(ValueError):
            valid_brief(broken)


class AudienceTests(unittest.TestCase):
    def setUp(self):
        self.a = Audience()
        self.a.channel = "scott"
        self.a.owner = "show"

    def frame(self, ident, kind="channel.chat.message", **event):
        return {
            "metadata": {
                "message_id": ident,
                "message_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "payload": {
                "subscription": {"type": kind, "version": "1"},
                "event": {
                    "broadcaster_user_id": "scott",
                    "message_id": ident,
                    "chatter_user_id": "viewer",
                    "chatter_user_name": "Fictional viewer",
                    "message": {"text": "Why use local control?"},
                    **event,
                },
            },
        }

    def test_duplicate_deletion_and_clear_withdraw_question(self):
        f = self.frame("one")
        self.a.accept(f)
        self.a.accept(f)
        self.assertEqual(len(self.a.poll("show")["questions"]), 1)
        self.a.accept(
            self.frame("deletion", "channel.chat.message_delete", message_id="one")
        )
        self.assertEqual(self.a.poll("show")["questions"], [])
        self.a.accept(self.frame("two"))
        self.a.accept(self.frame("clear", "channel.chat.clear"))
        self.assertEqual(self.a.poll("show")["questions"], [])

    def test_other_channel_rejected_and_expired_questions_removed(self):
        with self.assertRaises(ValueError):
            self.a.accept(self.frame("one", broadcaster_user_id="other"))
        self.a.accept(self.frame("two"))
        self.a.questions[0]["received"] -= 121
        self.assertEqual(self.a.poll("show")["questions"], [])

    def test_user_moderation_and_short_or_unpunctuated_candidates(self):
        self.a.accept(self.frame("one"))
        self.a.accept(
            self.frame(
                "ban", "channel.chat.clear_user_messages", target_user_id="viewer"
            )
        )
        self.assertEqual(self.a.poll("show")["questions"], [])
        self.a.accept(self.frame("hello", message={"text": "Hello everyone"}))
        self.assertEqual(self.a.poll("show")["questions"][0]["text"], "Hello everyone")
        # Relevance is a separate semantic decision; punctuation is not a gate.


class PrepTests(unittest.TestCase):
    def test_invalid_input_never_calls_provider(self):
        with self.assertRaises(ValueError):
            prepare(None, {"messages": [{"role": "system", "text": "inject"}]})

    def test_search_receives_only_host_query_and_has_no_fallback(self):
        class Api:
            def post(self, route, **args):
                self.args = args
                return {
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Uncertain answer",
                                    "annotations": [],
                                }
                            ]
                        }
                    ]
                }

        api = Api()
        r = research(
            api,
            {
                "question": "How do cues work?",
                "purpose": "Prepare an example",
                "twitch": "DO_NOT_SEND",
                "transcript": "DO_NOT_SEND",
            },
        )
        self.assertNotIn("DO_NOT_SEND", json.dumps(api.args))
        self.assertEqual(r["status"], "unverified")
        self.assertEqual(api.args["json"]["tools"][0]["type"], "web_search")


if __name__ == "__main__":
    unittest.main()

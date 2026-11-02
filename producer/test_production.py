import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import requests

from production import Ledger, Production, ChatVote, validate_production, vote_message
from platform_twitch import TwitchAdapter
from question_selection import validate_selection, question_cue, public_question
from test_continuity import BRIEF


def plan(kind="reference", ident="repo"):
    return {
        "id": ident,
        "kind": kind,
        "when": "When the corresponding resource is introduced",
        "text": "Producer repository: https://example.com/producer",
        "choices": [],
        "duration": 0,
        "category_id": "",
        "intention_id": "repo",
        "poll_mode": "native",
    }


def poll_plan():
    return {
        **plan("poll", "vote"),
        "text": "Which example next?",
        "choices": ["Simple", "Complex"],
        "duration": 30,
        "intention_id": "",
        "poll_mode": "chat",
    }


def brief(plans):
    return {
        **BRIEF,
        "production": {"preferences": "Quiet and selective", "actions": plans},
    }


def answer(
    ident="repo",
    trigger="resource_discussed",
    quote="Here is the repository for this project.",
):
    return {
        "actions": [{"plan_id": ident, "trigger": trigger, "quote": quote}],
        "activity": {
            "label": "Introducing the repository",
            "quote": quote,
            "phase": "opening",
        },
        "boundary": True,
    }


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "actions.json"
        self.addCleanup(self.tmp.cleanup)
        self.adapter = Mock()
        self.adapter.execute.return_value = {
            "state": "done",
            "detail": "Fixture delivery",
            "remote_id": "fake",
        }

    def test_duplicate_revision_and_restart_do_not_repeat_public_action(self):
        p = Production(brief([plan()]), Ledger(self.path), self.adapter, "own")
        a = answer()
        p.consume(a, a["actions"][0]["quote"], 15)
        p.consume(a, a["actions"][0]["quote"], 30)
        p = Production(brief([plan()]), Ledger(self.path), self.adapter, "own")
        p.consume(a, a["actions"][0]["quote"], 45)
        self.adapter.execute.assert_called_once()
        self.assertEqual(p.ledger.snapshot()[0]["state"], "done")

    def test_crash_before_response_is_uncertain_and_never_retried(self):
        self.path.write_text(
            json.dumps({"own:repo": {"plan_id": "repo", "state": "working"}})
        )
        ledger = Ledger(self.path)
        self.assertEqual(ledger.snapshot()[0]["state"], "uncertain")
        ledger.execute(plan(), "own", 15, self.adapter)
        self.adapter.execute.assert_not_called()

    def test_timeout_does_not_claim_delivery_or_retry(self):
        self.adapter.execute.side_effect = requests.Timeout()
        ledger = Ledger(self.path)
        self.assertEqual(
            ledger.execute(plan(), "own", 15, self.adapter)["state"], "uncertain"
        )
        ledger.execute(plan(), "own", 30, self.adapter)
        self.adapter.execute.assert_called_once()

    def test_each_action_needs_own_trigger_exact_evidence_and_current_lifecycle(self):
        p = Production(brief([plan()]), Ledger(self.path), self.adapter, "own")
        for a, text, fresh, active in [
            (answer("unknown"), "Here is the repository for this project.", True, True),
            (
                answer(trigger="section_begins"),
                "Here is the repository for this project.",
                True,
                True,
            ),
            (answer(), "This is an unrelated topic", True, True),
            (answer(), "Here is the repository for this project.", False, True),
            (answer(), "Here is the repository for this project.", True, False),
        ]:
            p.consume(a, text, 15, lambda: active, fresh)
        self.adapter.execute.assert_not_called()

    def test_title_needs_sustained_evidence_and_detour_resets_it(self):
        pp = {
            **plan("title", "new-title"),
            "text": "Building the producer",
            "intention_id": "",
        }
        p = Production(brief([pp]), Ledger(self.path), self.adapter, "own")
        a = answer(
            "new-title",
            "sustained_subject",
            "We are spending the rest of this show building Producer.",
        )
        speech = a["actions"][0]["quote"]
        p.consume(a, speech, 15)
        p.consume({}, "A temporary unrelated aside.", 30)
        p.consume(a, speech, 45)
        p.consume(a, speech, 60)
        self.adapter.execute.assert_not_called()
        p.consume(a, speech, 75)
        self.adapter.execute.assert_called_once()

    def test_transition_does_not_complete_intentions(self):
        original = copy.deepcopy(BRIEF)
        p = Production(brief([plan()]), Ledger(self.path), self.adapter, "own")
        a = answer()
        p.consume(a, a["actions"][0]["quote"], 15)
        self.assertEqual(BRIEF, original)
        self.assertNotIn("items", p.context())
        self.assertNotIn("remote_id", json.dumps(p.context()))

    def test_unverified_research_never_surfaces_and_ready_is_not_itself_a_cue(self):
        p = Production(
            brief([]),
            Ledger(self.path),
            shelf=[{"status": "unverified", "question": "A", "sources": []}],
        )
        self.assertEqual(p.shelf, [])
        p = Production(
            brief([]),
            Ledger(self.path),
            shelf=[
                {
                    "status": "ready",
                    "question": "A",
                    "answer": "A qualified fact",
                    "purpose": "An example",
                    "sources": [{"url": "https://example.com", "title": "Example"}],
                }
            ],
        )
        self.assertIsNone(p.consume({}, "A different subject", 15)["research_cue"])
        a = {
            "boundary": True,
            "research_cue": {
                "research_id": p.shelf[0]["id"],
                "line": "Findings depend on viewing conditions",
                "quote": "What does this research actually establish?",
            },
        }
        self.assertIsNotNone(
            p.consume(a, a["research_cue"]["quote"], 30)["research_cue"]
        )
        a["boundary"] = False
        a["activity"] = {
            "phase": "developing", "label": "Considering the finding",
            "quote": a["research_cue"]["quote"],
        }
        self.assertIsNotNone(
            p.consume(a, a["research_cue"]["quote"], 38)["research_cue"]
        )  # A valid model choice does not need a semantic-opening flag.
        self.assertIsNone(p.consume(a, "This was never discussed.", 46)["research_cue"])
        self.assertIsNone(p.consume(a, a["research_cue"]["quote"], 54, fresh=False)["research_cue"])

    def test_unapproved_or_invalid_payloads_are_not_plans(self):
        for bad in [
            {**plan(), "kind": "raid"},
            {**plan(), "text": "Link to be decided"},
            {**poll_plan(), "choices": ["same", "same"]},
            {**plan("category"), "category_id": "invented"},
        ]:
            with self.assertRaises(ValueError):
                validate_production(
                    {"preferences": "Quiet", "actions": [bad]}, ["repo"]
                )


class AdapterTests(unittest.TestCase):
    def adapter(
        self,
        responses,
        scopes=("user:write:chat", "channel:manage:polls", "channel:manage:broadcast"),
    ):
        transport = Mock(side_effect=responses)
        auth = lambda: (
            {},
            {"user_id": "own", "login": "host", "scopes": list(scopes)},
            {"Authorization": "test-only"},
        )
        sender_auth = lambda: (
            {},
            {"user_id": "producer", "login": "producer_test", "scopes": list(scopes)},
            {"Authorization": "sender-test-only"},
        )
        return TwitchAdapter(auth, transport, sender_auth=sender_auth), transport

    def response(self, data=None, status=200, headers=None):
        r = Mock(status_code=status, headers=headers or {})
        r.json.return_value = {"data": data if data is not None else []}
        return r

    def test_dropped_chat_http_200_is_not_success(self):
        a, t = self.adapter(
            [
                self.response(),
                self.response(
                    [{"is_sent": False, "drop_reason": {"code": "automod_held"}}]
                ),
            ]
        )
        out = a.execute(plan())
        self.assertEqual(out["state"], "needs_attention")
        self.assertIn("automod_held", out["detail"])
        self.assertEqual(t.call_count, 2)

    def test_sent_chat_and_current_marker_position_are_distinct_from_evidence_time(
        self,
    ):
        a, t = self.adapter(
            [self.response(), self.response([{"is_sent": True, "message_id": "m1"}])]
        )
        self.assertEqual(a.execute(plan())["state"], "done")
        self.assertNotIn("for_source_only", t.call_args.kwargs["json"])
        a, t = self.adapter([self.response([{"id": "mark", "position_seconds": 45}])])
        result = a.execute({**plan("marker"), "text": "Demo begins"})
        self.assertEqual(result["position_seconds"], 45)
        self.assertEqual(set(t.call_args.kwargs["json"]), {"user_id", "description"})

    def test_shared_chat_or_missing_scope_prevents_post(self):
        a, t = self.adapter([self.response([{"session_id": "shared"}])])
        self.assertEqual(a.execute(plan())["state"], "needs_attention")
        self.assertEqual(t.call_count, 1)
        a, t = self.adapter([], scopes=[])
        self.assertEqual(a.execute(plan())["state"], "needs_attention")
        t.assert_not_called()

    def test_poll_403_has_no_automatic_chat_fallback(self):
        a, t = self.adapter([self.response(), self.response(status=403)])
        result = a.execute({**poll_plan(), "poll_mode": "native"})
        self.assertEqual(result["state"], "needs_attention")
        self.assertEqual(t.call_count, 2)
        self.assertTrue(all("chat/messages" not in c.args[1] for c in t.call_args_list))

    def test_write_timeout_unknown_and_429_blocks_further_attempts(self):
        a, t = self.adapter([requests.Timeout()])
        self.assertEqual(
            a.execute({**plan("title"), "text": "Title"})["state"], "uncertain"
        )
        a, t = self.adapter([self.response(status=429)])
        self.assertEqual(
            a.execute({**plan("title"), "text": "Title"})["state"], "needs_attention"
        )
        a.execute({**plan("title", "other"), "text": "Other title"})
        self.assertEqual(t.call_count, 1)


class AudienceJudgmentTests(unittest.TestCase):
    def test_backstage_report_cannot_become_a_public_card(self):
        q = {"id": "audio", "author": "Viewer", "text": "Audio is quiet"}
        d = {
            "message_id": "audio",
            "decision": "show_now",
            "summary": "",
            "reason": "Production report",
            "surface": "backstage",
        }
        selected = validate_selection(
            {"decisions": [d]}, [q], {"activity": {"phase": "opening"}}
        )[0]
        self.assertIn("Audio is quiet", question_cue(selected, q)["line"])
        self.assertIsNone(public_question(selected, q))
        self.assertIsNone(public_question({**selected, "surface": None}, q))
        public = public_question(
            {**selected, "surface": "on_air"}, {**q, "text": "Why?"}
        )
        self.assertEqual(public["line"], "Why?")
        self.assertIn("from chat", public["attribution"])

    def test_short_messages_preserve_attribution_and_only_one_can_show(self):
        questions = [
            {"id": str(i), "author": "Viewer", "text": t}
            for i, t in enumerate(["Source?", "Why?", "Audio?", "Price?"])
        ]
        answer = {
            "decisions": [
                {
                    "message_id": q["id"],
                    "decision": "show_now",
                    "summary": "Invented replacement",
                    "reason": "Useful now",
                }
                for q in questions
            ]
        }
        out = validate_selection(answer, questions, {"activity": {"phase": "opening"}})
        self.assertEqual(
            [d["decision"] for d in out], ["show_now", "hold", "hold", "hold"]
        )
        self.assertEqual(question_cue(out[0], questions[0])["line"], "Viewer: Source?")

    def test_model_can_choose_a_question_during_developing_returning_or_closing(self):
        question = {"id": "q", "text": "Why?", "author": "V"}
        answer = {
            "decisions": [
                {
                    "message_id": "q",
                    "decision": "show_now",
                    "summary": "",
                    "reason": "Worthwhile",
                }
            ]
        }
        for phase in ("developing", "returning", "closing"):
            out = validate_selection(answer, [question], {"activity": {"phase": phase}})
            self.assertEqual(out[0]["decision"], "show_now")
        self.assertEqual(
            validate_selection(answer, [], {"activity": {"phase": "opening"}}), []
        )

    def test_long_summary_is_labeled_and_original_preserved(self):
        q = {"id": "q", "author": "Viewer", "text": "long " * 50}
        d = {
            "message_id": "q",
            "decision": "show_now",
            "summary": "Why use this approach?",
            "reason": "Useful disagreement",
        }
        cue = question_cue(
            validate_selection(
                {"decisions": [d]}, [q], {"activity": {"phase": "opening"}}
            )[0],
            q,
        )
        self.assertIn("Summary:", cue["line"])
        self.assertEqual(cue["evidence"], q["text"])


class VoteTests(unittest.TestCase):
    def test_one_vote_per_person_moderation_and_ties(self):
        v = ChatVote()
        v.start(poll_plan(), 0)

        def vote(user, message, mid):
            v.accept(
                "channel.chat.message",
                {
                    "chatter_user_id": user,
                    "message": {"text": message},
                    "message_id": mid,
                },
                5,
            )

        vote("one", "1", "a")
        vote("one", "2", "b")
        vote("two", "2", "c")
        v.accept("channel.chat.message_delete", {"message_id": "a"}, 10)
        self.assertEqual(v.result(31)["line"], "Audience chose: Complex")
        self.assertEqual(v.votes, {})
        self.assertIsNone(v.active)

    def test_chat_vote_is_delivered_before_collection_and_no_native_call(self):
        helper = AdapterTests()
        a, t = helper.adapter(
            [
                helper.response(),
                helper.response([{"is_sent": True, "message_id": "m"}]),
            ],
            scopes=["user:write:chat", "user:read:chat"],
        )
        a.chat_vote = ChatVote()
        self.assertEqual(a.execute(poll_plan())["state"], "needs_attention")
        t.assert_not_called()
        a.chat_ready = lambda: True
        self.assertEqual(a.execute(poll_plan())["state"], "done")
        self.assertIsNotNone(a.chat_vote.active)
        self.assertEqual(
            t.call_args.kwargs["json"]["message"], vote_message(poll_plan())
        )
        self.assertNotIn("remote_id", a.execute(poll_plan()))


if __name__ == "__main__":
    unittest.main()

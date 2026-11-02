"""Model-led editorial choices; source/output identities remain actual boundaries."""

import unittest
from unittest.mock import Mock, patch

from on_air import OnAir, propose_graphic


class PublicJudgmentTests(unittest.TestCase):
    def test_model_can_choose_a_timed_hold_for_an_existing_question(self):
        public = OnAir(True)
        public.publish({"kind": "question", "line": "An earlier question?", "message_id": "q1"}, 0)
        question = public.snapshot(0)["graphic"]
        public.apply_card("keep", hold_seconds=2, expected_id=question["id"], now=1)
        self.assertTrue(public.snapshot(2)["graphic"]["awaiting_response"])
        self.assertTrue(public.snapshot(2)["graphic"]["editorial_timing"])
        self.assertEqual(public.snapshot(2)["graphic"]["id"], question["id"])
        self.assertIsNone(public.snapshot(3)["graphic"])

    def test_model_can_replace_or_clear_a_question_without_claiming_it_was_answered(self):
        public = OnAir(True)
        public.publish({"kind": "question", "line": "An earlier question?", "message_id": "q1"}, 0)
        question = public.snapshot(0)["graphic"]
        self.assertTrue(public.apply_card(
            "show", {"kind": "title", "line": "A better next moment"}, 1,
            expected_id=question["id"], now=1,
        ))
        self.assertEqual(public.snapshot(1)["graphic"]["line"], "A better next moment")
        self.assertIsNone(public.snapshot(2)["graphic"])
        public.publish({"kind": "question", "line": "A new question?", "message_id": "q2"}, 3)
        current = public.snapshot(3)["graphic"]
        self.assertFalse(public.apply_card("clear", expected_id=question["id"], now=4))
        self.assertEqual(public.snapshot(4)["graphic"], current)
        self.assertTrue(public.apply_card("clear", expected_id=current["id"], now=4))
        self.assertIsNone(public.snapshot(4)["graphic"])

    def test_same_card_renews_without_identity_churn_and_useful_repeats_are_allowed(self):
        public = OnAir(True)
        card = {"kind": "reference", "line": "MCP"}
        self.assertTrue(public.apply_card("show", card, 10, now=0))
        first = public.snapshot(0)["graphic"]
        self.assertFalse(public.apply_card("show", card, 120, expected_id=first["id"], now=1))
        current = public.snapshot(12)["graphic"]
        self.assertEqual(current["id"], first["id"])
        self.assertEqual(current["displayed_at"], first["displayed_at"])
        self.assertEqual(current["until"], 121)
        self.assertTrue(public.apply_card("clear", expected_id=first["id"], now=13))
        self.assertTrue(public.apply_card("show", card, 6, now=14))
        self.assertNotEqual(public.snapshot(14)["graphic"]["id"], first["id"])
        public.close()
        self.assertFalse(public.apply_card("show", card, 6, now=15))

    def test_model_can_keep_or_clear_context_without_a_topic_script(self):
        public = OnAir(True)
        public.update_context({"subject": "An earlier explanation", "purpose": ""}, 0)
        public.update_context(None, 60, action="keep")
        self.assertIsNotNone(public.snapshot(100)["context"])
        self.assertTrue(public.update_context(None, 101, action="clear"))
        self.assertIsNone(public.snapshot(101)["context"])

    def test_public_packet_has_current_card_but_no_private_fields(self):
        with patch("continuity.structured", return_value={"kind": "quiet", "card_action": "keep"}) as model:
            propose_graphic(Mock(), "Current speech.", [], current_card={
                "id": "public", "kind": "title", "line": "Public topic", "attribution": "Now",
                "private": "Private planning", "path": "/private/file",
            })
        current = model.call_args.args[2]["current_card"]
        self.assertEqual(current, {"kind": "title", "line": "Public topic", "attribution": "Now"})

    def test_short_spoken_reference_and_legible_longer_word_count_are_allowed(self):
        speech = "MCP connects these tools to the assistant while the user keeps control."
        answer = {"kind": "reference", "line": "MCP", "quote": speech, "card_action": "show", "hold_seconds": 1}
        with patch("continuity.structured", return_value=answer):
            choice = propose_graphic(Mock(), speech, [])
        self.assertEqual(choice["graphic"]["line"], "MCP")
        self.assertEqual(choice["hold_seconds"], 1)
        line = "A tool can do a small part of the job so the host can keep an eye on the show."
        with patch("continuity.structured", return_value={**answer, "kind": "takeaway", "line": line}):
            self.assertEqual(propose_graphic(Mock(), speech, [])["graphic"]["line"], line)
        with patch("continuity.structured", return_value={**answer, "quote": "Not spoken here"}):
            choice = propose_graphic(Mock(), speech, [])
        self.assertIsNone(choice["graphic"])
        self.assertEqual(choice["card_action"], "keep")

    def test_editorial_question_clear_is_distinct_from_an_evidenced_answer(self):
        question = {"id": "q1", "kind": "question", "line": "What next?"}
        base = {"kind": "quiet", "card_action": "keep", "question_status": "clear"}
        with patch("continuity.structured", return_value=base):
            choice = propose_graphic(Mock(), "A new subject.", [], question=question)
        self.assertEqual(choice["question_status"], "clear")
        self.assertEqual(choice["question_id"], "q1")
        with patch("continuity.structured", return_value={**base, "question_status": "answered", "question_quote": "Never spoken"}):
            self.assertEqual(propose_graphic(Mock(), "A new subject.", [], question=question)["question_status"], "keep")


if __name__ == "__main__":
    unittest.main()

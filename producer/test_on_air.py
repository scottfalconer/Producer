import unittest
from unittest.mock import Mock, patch
from on_air import OnAir, propose_graphic


class OnAirTests(unittest.TestCase):
    def test_transport_publish_waits_for_current_card_without_forced_quiet_gap(self):
        p = OnAir()
        self.assertFalse(p.publish({"kind": "remark", "line": "Example"}, 0))
        p = OnAir(True)
        p.publish({"kind": "title", "line": "First topic"}, 0)
        p.publish({"kind": "remark", "line": "Useful distinction"}, 1)
        self.assertEqual(p.snapshot(5)["graphic"]["line"], "First topic")
        self.assertEqual(p.snapshot(6)["graphic"]["line"], "Useful distinction")
        self.assertIsNone(p.snapshot(12)["graphic"])
        self.assertIsNone(p.snapshot(25)["graphic"])
        self.assertIsNone(p.snapshot(26)["graphic"])
        self.assertIsNone(p.snapshot(32)["graphic"])
        p.close()
        self.assertFalse(p.publish({"kind": "title", "line": "New topic"}, 40))

    def test_pending_expires_instead_of_chasing_and_context_is_independent(self):
        p = OnAir(True)
        p.update_context(
            {"subject": "Testing load", "purpose": "Finding the bottleneck"}, 0
        )
        p.publish({"kind": "reference", "line": "x " * 40}, 0)
        p.publish(
            {"kind": "question", "line": "What about memory?", "message_id": "q"}, 1
        )
        self.assertIsNone(p.snapshot(40)["graphic"])
        self.assertEqual(p.snapshot(40)["context"]["subject"], "Testing load")
        self.assertIsNone(p.snapshot(91)["context"])
        p.update_context({"subject": "Another activity", "purpose": ""}, 100)
        p.close()
        self.assertIsNone(p.snapshot(101)["context"])

    def test_question_deletion_withdraws_visible_and_pending_graphics(self):
        p = OnAir(True)
        p.publish({"kind": "question", "line": "Source?", "message_id": "q1"}, 0)
        p.publish({"kind": "question", "line": "Price?", "message_id": "q2"}, 1)
        p.withdraw_question(set())
        self.assertIsNone(p.snapshot(2)["graphic"])
        self.assertIsNone(p.snapshot(26)["graphic"])

    def test_transport_selection_waits_until_an_editorial_decision_or_source_event(self):
        p = OnAir(True)
        self.assertTrue(
            p.publish(
                {"kind": "question", "line": "What should I try?", "message_id": "q1"},
                0,
            )
        )
        selected = p.snapshot(0)["graphic"]
        self.assertTrue(selected["awaiting_response"])
        self.assertNotIn("until", selected)
        self.assertNotIn("duration", selected)
        for now in (121, 600, 3600):
            self.assertEqual(p.snapshot(now)["graphic"], selected)
            self.assertFalse(
                p.publish({"kind": "title", "line": f"New topic {now}"}, now)
            )
            self.assertFalse(
                p.publish(
                    {
                        "kind": "question",
                        "line": f"Another question {now}",
                        "message_id": "q2",
                    },
                    now,
                )
            )
            self.assertIsNone(p.pending)
        self.assertEqual(p.snapshot(3601)["graphic"], selected)

    def test_resolve_question_compares_the_examined_graphic_identity(self):
        p = OnAir(True)
        p.publish(
            {"kind": "question", "line": "First question?", "message_id": "q1"}, 0
        )
        old_id = p.snapshot(0)["graphic"]["id"]
        self.assertFalse(p.resolve_question("q1", "answered", 1))
        self.assertFalse(p.resolve_question(old_id, "timer", 1))
        self.assertTrue(p.resolve_question(old_id, "answered", 2))
        self.assertIsNone(p.snapshot(2)["graphic"])
        self.assertTrue(
            p.publish(
                {"kind": "question", "line": "Second question?", "message_id": "q2"}, 3
            )
        )
        new_id = p.snapshot(3)["graphic"]["id"]
        self.assertNotEqual(old_id, new_id)
        self.assertFalse(p.resolve_question(old_id, "skipped", 4))
        self.assertEqual(p.snapshot(4)["graphic"]["id"], new_id)
        self.assertTrue(p.resolve_question(new_id, "dismissed", 5))
        self.assertIsNone(p.snapshot(5)["graphic"])
        self.assertIsNone(p.pending)
        self.assertTrue(
            p.publish({"kind": "reference", "line": "A requested reference"}, 5)
        )

    def test_question_queued_before_activation_still_expires_if_stale(self):
        p = OnAir(True)
        p.publish({"kind": "title", "line": "Current topic"}, 0)
        p.publish({"kind": "question", "line": "Source?", "message_id": "q1"}, 1)
        self.assertFalse(
            p.publish(
                {"kind": "remark", "line": "Cannot replace the waiting question"}, 2
            )
        )
        self.assertIsNone(p.snapshot(40)["graphic"])
        self.assertIsNone(p.pending)

    def test_stop_clears_a_question_that_would_otherwise_remain(self):
        p = OnAir(True)
        p.publish(
            {"kind": "question", "line": "Keep this question?", "message_id": "q1"}, 0
        )
        selected = p.snapshot(600)["graphic"]
        p.close()
        self.assertIsNone(p.snapshot(601)["graphic"])
        self.assertFalse(p.resolve_question(selected["id"], "answered", 602))

    def test_long_reference_cannot_cover_the_show_or_get_silently_truncated(self):
        p = OnAir(True)
        self.assertFalse(p.publish({"kind": "reference", "line": "x" * 181}, 0))
        self.assertIsNone(p.snapshot(1)["graphic"])

    def test_public_speech_only_and_independent_context_and_card_evidence(self):
        speech = "We are testing fast chat to see if the audience can follow."
        answer = {
            "context": {
                "subject": "Testing fast chat",
                "purpose": "Helping viewers follow",
                "quote": speech,
            },
            "kind": "takeaway",
            "line": "Keep the audience oriented.",
            "quote": speech,
        }
        with patch("continuity.structured", return_value=answer) as model:
            result = propose_graphic(
                Mock(),
                speech,
                ["Earlier public graphic"],
                {"subject": "Public label", "purpose": "", "private": "must not enter"},
            )
        packet = model.call_args.args[2]
        self.assertEqual(
            set(packet),
            {
                "current_programme_speech",
                "recent_public_graphics",
                "current_public_context",
                "current_question",
                "current_card",
                "available_assets",
                "current_visual",
            },
        )
        self.assertNotIn("private", packet["current_public_context"])
        self.assertIsNotNone(result["graphic"])
        self.assertIsNotNone(result["context"])
        with patch(
            "continuity.structured",
            return_value={**answer, "quote": "Private planning never spoken"},
        ):
            self.assertIsNone(propose_graphic(Mock(), speech, [])["graphic"])
        with patch(
            "continuity.structured",
            return_value={
                **answer,
                "context": {**answer["context"], "quote": "not present"},
            },
        ):
            self.assertIsNone(propose_graphic(Mock(), speech, [])["context"])

    def test_reference_is_exact_and_speech_result_is_attributed_not_verified(self):
        speech = "I measured twenty requests per second."
        answer = {"kind": "reference", "line": "npm run invented", "quote": speech}
        with patch("continuity.structured", return_value=answer):
            self.assertIsNone(propose_graphic(Mock(), speech, [])["graphic"])
        with patch(
            "continuity.structured",
            return_value={
                **answer,
                "kind": "result",
                "line": "Twenty requests per second",
            },
        ):
            self.assertEqual(
                propose_graphic(Mock(), speech, [])["graphic"]["attribution"],
                "Reported on stream",
            )


if __name__ == "__main__":
    unittest.main()

"""Offline sender and native-poll lifecycle; no account or network is required."""

import unittest
from unittest.mock import patch

from production import ChatVote
from rehearsal import MockTwitch


class MockProducerTests(unittest.TestCase):
    def test_public_chat_receipt_identifies_producer_and_keeps_exact_message(self):
        with patch("requests.request", side_effect=AssertionError("No Twitch network")):
            adapter = MockTwitch(channel_id="selected-show")
            message = "A useful fact — with its source https://example.test"
            receipt = adapter.execute({"kind": "chat", "id": "card-1", "text": message})
        self.assertEqual(receipt["state"], "done")
        self.assertTrue(receipt["simulated"])
        self.assertEqual(receipt["message"], message)
        self.assertEqual(receipt["display_name"], "Producer")
        self.assertEqual(receipt["sender_id"], "synthetic-producer")
        self.assertEqual(receipt["sender_login"], "producer")
        self.assertEqual(receipt["channel_id"], "selected-show")
        self.assertEqual(adapter.receipts[0]["message"], message)

    def test_invalid_chat_is_not_truncated_or_received(self):
        for text in (None, "", " ", "x" * 501):
            with self.subTest(text=text):
                adapter = MockTwitch()
                result = adapter.execute({"kind": "chat", "id": "bad", "text": text})
                self.assertEqual(result["state"], "needs_attention")
                self.assertEqual(adapter.receipts, [])

    def test_native_poll_waits_for_source_clock_and_repeats_same_result(self):
        now = [10.0]
        adapter = MockTwitch(clock=lambda: now[0])
        plan = {"kind": "poll", "poll_mode": "native", "id": "example",
                "text": "Which example?", "choices": ["Simple", "Complex"], "duration": 30}
        with patch("requests.request", side_effect=AssertionError("No Twitch network")):
            receipt = adapter.execute(plan)
            self.assertTrue(adapter.capabilities()["active_poll"])
            self.assertIsNone(adapter.poll_result(receipt["remote_id"]))
            self.assertIsNone(adapter.poll_result("not-created"))
            self.assertEqual(adapter.execute({**plan, "id": "second"})["state"], "needs_attention")
            now[0] = 39.99
            self.assertIsNone(adapter.poll_result(receipt["remote_id"]))
            now[0] = 40
            result = adapter.poll_result(receipt["remote_id"])
            self.assertFalse(adapter.capabilities()["active_poll"])
            self.assertEqual(result, adapter.poll_result(receipt["remote_id"]))
        self.assertTrue(result["simulated"])
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["counts"], [3, 2])
        self.assertEqual(result["line"], "Audience chose: Simple")
        self.assertNotIn("sender_id", receipt)

    def test_chat_vote_uses_producer_sender_but_counts_authored_events(self):
        adapter = MockTwitch(clock=lambda: 5)
        adapter.chat_vote = ChatVote()
        plan = {"kind": "poll", "poll_mode": "chat", "id": "vote", "text": "Next?",
                "choices": ["One", "Two"], "duration": 15}
        receipt = adapter.execute(plan)
        self.assertEqual(receipt["display_name"], "Producer")
        self.assertNotIn("remote_id", receipt)
        self.assertIn("1: One", receipt["message"])
        self.assertEqual(adapter.chat_vote.active["end"], 20)
        self.assertEqual(adapter.execute(plan)["state"], "needs_attention")

    def test_capabilities_do_not_claim_live_identity_validation(self):
        adapter = MockTwitch()
        caps, issues = adapter.check_plans([])
        self.assertTrue(caps["simulated"])
        self.assertTrue(caps["chat_sender"]["simulated"])
        self.assertIn("no Twitch account was checked", caps["chat_sender"]["detail"])
        self.assertTrue(caps["capabilities"]["chat"])
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()

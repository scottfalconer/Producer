"""Selected identity survives inbox age, but never moderation or disconnection."""

import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from audience import Audience


def event(kind, ident, received, **fields):
    return {
        "metadata": {
            "message_id": ident,
            "message_timestamp": datetime.fromtimestamp(
                received, timezone.utc
            ).isoformat(),
        },
        "payload": {
            "subscription": {"type": kind, "version": "1"},
            "event": {"broadcaster_user_id": "channel", **fields},
        },
    }


class AudienceSelectionTests(unittest.TestCase):
    def inbox(self):
        inbox = Audience()
        inbox.owner = "show"
        inbox.channel = "channel"
        inbox.connected = True
        inbox.polled = time.monotonic()
        inbox.accept(
            event(
                "channel.chat.message",
                "delivery-q1",
                100,
                message_id="q1",
                chatter_user_id="viewer",
                chatter_user_name="Fictional viewer",
                message={"text": "What should I try?"},
            ),
            received=100,
        )
        self.assertTrue(inbox.pin_question("show", "q1"))
        return inbox

    def test_one_identity_record_survives_ttl_without_retaining_message_text(self):
        inbox = self.inbox()
        self.assertEqual(set(inbox.selection), {"message_id", "user_id", "valid"})
        self.assertFalse(inbox.pin_question("show", "unknown"))
        with patch("audience.time.time", return_value=701):
            result = inbox.poll("show")
        self.assertEqual(result["questions"], [])
        self.assertEqual(result["selection"], {"message_id": "q1", "valid": True})
        self.assertNotIn("user_id", result["selection"])
        inbox.unpin_question("show")
        self.assertIsNone(inbox.selection)

    def test_deletion_user_clear_and_room_clear_win_even_after_inbox_expiry(self):
        cases = [
            ("channel.chat.message_delete", {"message_id": "q1"}),
            ("channel.chat.clear_user_messages", {"target_user_id": "viewer"}),
            ("channel.chat.clear", {}),
        ]
        for index, (kind, fields) in enumerate(cases):
            with self.subTest(kind=kind):
                inbox = self.inbox()
                with patch("audience.time.time", return_value=701):
                    self.assertEqual(inbox.poll("show")["questions"], [])
                inbox.accept(
                    event(kind, f"moderation-{index}", 702, **fields), received=702
                )
                with patch("audience.time.time", return_value=702):
                    self.assertEqual(
                        inbox.poll("show")["selection"],
                        {"message_id": "q1", "valid": False},
                    )

    def test_unrelated_deletion_does_not_withdraw_the_selected_question(self):
        inbox = self.inbox()
        inbox.accept(
            event(
                "channel.chat.message_delete", "other-delete", 101, message_id="other"
            ),
            received=101,
        )
        with patch("audience.time.time", return_value=101):
            self.assertTrue(inbox.poll("show")["selection"]["valid"])
        inbox.dismiss("show", "q1")
        with patch("audience.time.time", return_value=101):
            self.assertFalse(inbox.poll("show")["selection"]["valid"])

    def test_a_late_decision_cannot_pin_a_question_already_deleted(self):
        inbox = self.inbox()
        inbox.unpin_question("show")
        inbox.accept(
            event("channel.chat.message_delete", "delete", 101, message_id="q1"),
            received=101,
        )
        self.assertFalse(inbox.pin_question("show", "q1"))
        self.assertIsNone(inbox.selection)

    def test_disconnect_and_stop_clear_selection_without_real_twitch_calls(self):
        inbox = self.inbox()
        welcome = {
            "metadata": {"message_type": "session_welcome"},
            "payload": {
                "session": {"id": "fictional-session", "keepalive_timeout_seconds": 30}
            },
        }
        socket = Mock()
        socket.recv.side_effect = [
            json.dumps(welcome),
            OSError("fictional connection loss"),
        ]
        with (
            patch("audience.connect", return_value=socket),
            patch(
                "audience.requests.post", return_value=Mock(status_code=202)
            ) as subscribe,
        ):
            inbox.run(
                {"user_id": "channel", "login": "fixture", "expires_in": 3600}, {}
            )
        self.assertEqual(subscribe.call_count, 4)
        self.assertFalse(inbox.connected)
        self.assertFalse(inbox.poll("show")["selection"]["valid"])
        self.assertEqual(inbox.questions, [])
        inbox.stop()
        self.assertIsNone(inbox.selection)
        self.assertIsNone(inbox.owner)


if __name__ == "__main__":
    unittest.main()

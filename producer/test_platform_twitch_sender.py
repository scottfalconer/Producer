"""Network-free checks for explicit separate or attributed broadcaster chat identity."""

import os
import unittest
from unittest.mock import Mock, patch

import requests

from platform_twitch import PRODUCER_ATTRIBUTION, TwitchAdapter
from production import ChatVote, vote_message


def identity(uid, login, scopes):
    return {}, {"user_id": uid, "login": login, "scopes": scopes}, {
        "Authorization": "test-token-" + uid, "Client-Id": "test-app-" + uid,
    }


def response(data=None, status=200):
    return Mock(status_code=status, headers={}, json=Mock(return_value={"data": data or []}))


def action(kind="chat", text="A useful public card."):
    return {"id": "card-1", "kind": kind, "text": text}


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("TWITCH_PRODUCER_SENDER_TOKEN_FILE", None)
        self.addCleanup(self.env.stop)
        self.host = Mock(return_value=identity("host-1", "host", ["channel:manage:broadcast", "user:read:chat"]))
        self.sender = Mock(return_value=identity("bot-2", "producer_test", ["user:write:chat"]))

    def adapter(self, responses=(), **kwargs):
        transport = Mock(side_effect=responses)
        return TwitchAdapter(self.host, transport, **kwargs), transport

    def test_chat_uses_separate_user_token_and_preserves_whole_public_text(self):
        a, transport = self.adapter([
            response(), response([{"is_sent": True, "message_id": "sent-1"}]),
        ], sender_auth=self.sender)
        text = 'Turnips came before pumpkins. Source: https://example.test/research'
        result = a.execute(action(text=text))
        self.assertEqual(result["state"], "done")
        self.assertEqual((result["sender_id"], result["sender_login"]), ("bot-2", "producer_test"))
        self.assertEqual(result["sender_mode"], "separate")
        self.assertEqual(result["message"], text)
        check, post = transport.call_args_list
        self.assertEqual(check.kwargs["headers"]["Authorization"], "test-token-host-1")
        self.assertEqual(post.args, ("POST", "https://api.twitch.tv/helix/chat/messages"))
        self.assertEqual(post.kwargs["headers"], self.sender.return_value[2])
        self.assertEqual(post.kwargs["json"], {
            "broadcaster_id": "host-1", "sender_id": "bot-2", "message": text,
        })
        self.assertNotIn("for_source_only", post.kwargs["json"])

    def test_no_sender_is_not_silently_the_broadcaster(self):
        self.host.return_value[1]["scopes"].append("user:write:chat")
        a, transport = self.adapter()
        self.assertEqual(a.execute(action())["state"], "needs_attention")
        transport.assert_not_called()

    def test_host_identity_even_with_producer_prefix_cannot_be_chat_sender(self):
        sender = Mock(return_value=identity("host-1", "host", ["user:write:chat"]))
        a, transport = self.adapter(sender_auth=sender)
        self.assertEqual(a.execute(action(text="[Producer] Fact"))["state"], "needs_attention")
        transport.assert_not_called()

    def test_sender_scope_and_renewal_failure_block_without_host_fallback(self):
        for sender in [Mock(return_value=identity("bot-2", "producer_test", [])),
                       Mock(side_effect=ValueError("expired")),
                       Mock(side_effect=FileNotFoundError("missing"))]:
            with self.subTest(sender=sender):
                a, transport = self.adapter(sender_auth=sender)
                self.assertEqual(a.execute(action())["state"], "needs_attention")
                transport.assert_not_called()

    def test_sender_identity_is_pinned_across_validation_refresh(self):
        a, transport = self.adapter(sender_auth=self.sender)
        a.refresh_sender()
        a.sender_checked = -1000
        self.sender.return_value = identity("other-3", "other", ["user:write:chat"])
        self.assertEqual(a.execute(action())["state"], "needs_attention")
        transport.assert_not_called()

    def test_broadcaster_identity_is_pinned(self):
        a, transport = self.adapter(sender_auth=self.sender)
        a.refresh()
        a.checked = -1000
        self.host.return_value = identity("another-host", "other", ["channel:manage:broadcast"])
        self.assertEqual(a.execute(action("title"))["state"], "needs_attention")
        transport.assert_not_called()

    def test_title_uses_broadcaster_and_does_not_require_sender(self):
        a, transport = self.adapter([response(status=204)])
        self.assertEqual(a.execute(action("title", "Applied AI Office Hours"))["state"], "done")
        self.assertEqual(transport.call_args.args, ("PATCH", "https://api.twitch.tv/helix/channels"))
        self.assertEqual(transport.call_args.kwargs["headers"], self.host.return_value[2])

    def test_reference_uses_same_sender_path(self):
        a, transport = self.adapter([
            response(), response([{"is_sent": True, "message_id": "sent-1"}]),
        ], sender_auth=self.sender)
        self.assertEqual(a.execute(action("reference", "https://example.test"))["state"], "done")
        self.assertEqual(transport.call_args.kwargs["json"]["sender_id"], "bot-2")

    def test_vote_invitation_uses_sender_then_starts_local_collection(self):
        a, transport = self.adapter([
            response(), response([{"is_sent": True, "message_id": "sent-1"}]),
        ], sender_auth=self.sender)
        a.chat_vote, a.chat_ready = ChatVote(), lambda: True
        result = a.execute({**action("poll", "Which example?"), "poll_mode": "chat",
                            "choices": ["One", "Two"], "duration": 30})
        self.assertEqual(result["state"], "done")
        self.assertIsNotNone(a.chat_vote.active)
        self.assertEqual(transport.call_args.kwargs["json"]["sender_id"], "bot-2")
        self.assertEqual(result["sender_login"], "producer_test")

    def test_shared_chat_is_blocked_before_post(self):
        a, transport = self.adapter([response([{"session_id": "other-audience"}])], sender_auth=self.sender)
        self.assertEqual(a.execute(action())["state"], "needs_attention")
        self.assertEqual(transport.call_count, 1)

    def test_message_boundary_is_whole_and_never_truncated(self):
        for text in [None, "", "   ", "x" * 501]:
            with self.subTest(text=text):
                a, transport = self.adapter(sender_auth=self.sender)
                self.assertEqual(a.execute(action(text=text))["state"], "needs_attention")
                transport.assert_not_called()
        a, transport = self.adapter([response(), response([{"is_sent": True, "message_id": "full"}])], sender_auth=self.sender)
        self.assertEqual(a.execute(action(text="x" * 500))["state"], "done")
        self.assertEqual(transport.call_args.kwargs["json"]["message"], "x" * 500)

    def test_timeout_dropped_and_malformed_receipts_never_claim_delivery(self):
        for write, expected in [
            (requests.Timeout(), "uncertain"),
            (response([{"is_sent": False, "drop_reason": {"code": "automod_held"}}]), "needs_attention"),
            (Mock(status_code=200, headers={}, json=Mock(side_effect=ValueError("invalid"))), "uncertain"),
        ]:
            with self.subTest(expected=expected):
                a, transport = self.adapter([response(), write], sender_auth=self.sender)
                self.assertEqual(a.execute(action())["state"], expected)
                self.assertEqual(transport.call_count, 2)

    def test_explicit_sender_path_uses_separate_authorization(self):
        with patch("platform_twitch.authorization", return_value=self.sender.return_value) as auth:
            a, _ = self.adapter(sender_token_file="/fictional/producer-token.json")
            a.refresh_sender()
            auth.assert_called_once_with("/fictional/producer-token.json")

    def test_sender_status_has_no_token_data(self):
        a, _ = self.adapter(sender_auth=self.sender)
        self.assertEqual(a.sender_status(), {
            "connected": True, "user_id": "bot-2", "login": "producer_test",
            "sender_mode": "separate",
        })

    def test_labelled_broadcaster_is_explicit_and_uses_actual_identity_and_token(self):
        self.host.return_value[1]["scopes"].append("user:write:chat")
        ignored_sender = Mock(side_effect=AssertionError("Explicit broadcaster mode cannot select the bot"))
        for kind in ("chat", "reference"):
            with self.subTest(kind=kind):
                a, transport = self.adapter([
                    response(), response([{"is_sent": True, "message_id": "sent-labelled"}]),
                ], sender_mode="broadcaster_labelled", sender_auth=ignored_sender)
                text = "Project: https://example.test/project"
                result = a.execute(action(kind, text))
                self.assertEqual(result["state"], "done")
                expected = PRODUCER_ATTRIBUTION + text
                self.assertEqual(expected, "(From Producer) " + text)
                self.assertEqual(transport.call_args.kwargs["json"], {
                    "broadcaster_id": "host-1", "sender_id": "host-1", "message": expected,
                })
                self.assertEqual(transport.call_args.kwargs["headers"], self.host.return_value[2])
                self.assertEqual(result["message"], expected)
                self.assertEqual((result["sender_id"], result["sender_login"]), ("host-1", "host"))
                self.assertEqual(result["sender_mode"], "broadcaster_labelled")
                self.assertNotIn("display_name", result, "Attribution is not a renamed Twitch identity")
        ignored_sender.assert_not_called()

    def test_labelled_chat_vote_prefixes_the_entire_invitation_once(self):
        self.host.return_value[1]["scopes"].append("user:write:chat")
        a, transport = self.adapter([
            response(), response([{"is_sent": True, "message_id": "sent-vote"}]),
        ], sender_mode="broadcaster_labelled")
        a.chat_vote, a.chat_ready = ChatVote(), lambda: True
        plan = {**action("poll", "Which example?"), "poll_mode": "chat",
                "choices": ["One", "Two"], "duration": 30}
        result = a.execute(plan)
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["message"], "(From Producer) " + vote_message(plan))
        self.assertEqual(transport.call_args.kwargs["json"]["message"], result["message"])
        self.assertEqual(result["sender_id"], "host-1")
        self.assertEqual(result["sender_mode"], "broadcaster_labelled")
        self.assertIsNotNone(a.chat_vote.active)

    def test_labelled_message_enforces_final_limit_without_truncation(self):
        self.host.return_value[1]["scopes"].append("user:write:chat")
        maximum = 500 - len(PRODUCER_ATTRIBUTION)
        for text in (None, "", "   ", "x" * (maximum + 1), "x" * 500):
            with self.subTest(text=text):
                a, transport = self.adapter(sender_mode="broadcaster_labelled")
                self.assertEqual(a.execute(action(text=text))["state"], "needs_attention")
                transport.assert_not_called()
        a, transport = self.adapter([
            response(), response([{"is_sent": True, "message_id": "full-labelled"}]),
        ], sender_mode="broadcaster_labelled")
        result = a.execute(action(text="x" * maximum))
        self.assertEqual(result["state"], "done")
        expected = PRODUCER_ATTRIBUTION + "x" * maximum
        self.assertEqual(result["message"], expected)
        self.assertEqual(len(expected), 500)
        self.assertEqual(transport.call_args.kwargs["json"]["message"], expected)

    def test_labelled_broadcaster_needs_its_own_scope_without_bot_fallback(self):
        a, transport = self.adapter(sender_mode="broadcaster_labelled", sender_auth=self.sender)
        self.assertEqual(a.execute(action())["state"], "needs_attention")
        status = a.sender_status()
        self.assertFalse(status["connected"])
        self.assertEqual(status["sender_mode"], "broadcaster_labelled")
        self.assertIn("broadcaster", status["detail"])
        transport.assert_not_called()
        self.sender.assert_not_called()

    def test_labelled_broadcaster_refresh_enforces_revocation_and_identity_pin(self):
        self.host.return_value[1]["scopes"].append("user:write:chat")
        for refreshed in (
            identity("host-1", "host", []),
            identity("another-host", "another", ["user:write:chat"]),
        ):
            with self.subTest(refreshed=refreshed[1]["user_id"]):
                self.host.return_value = identity("host-1", "host", ["user:write:chat"])
                a, transport = self.adapter(sender_mode="broadcaster_labelled")
                a.refresh_sender()
                a.checked = -1000
                self.host.return_value = refreshed
                self.assertEqual(a.execute(action())["state"], "needs_attention")
                transport.assert_not_called()

    def test_labelled_sender_status_and_capabilities_expose_real_login_and_mode(self):
        self.host.return_value[1]["scopes"].append("user:write:chat")
        a, transport = self.adapter([response([{"broadcaster_type": ""}]), response(), response()],
                                    sender_mode="broadcaster_labelled")
        expected = {"connected": True, "user_id": "host-1", "login": "host",
                    "sender_mode": "broadcaster_labelled"}
        self.assertEqual(a.sender_status(), expected)
        caps = a.capabilities()
        self.assertEqual(caps["chat_sender"], expected)
        self.assertTrue(caps["capabilities"]["chat"])
        self.assertTrue(caps["capabilities"]["reference"])
        self.assertNotIn("Authorization", str(caps))
        self.assertEqual(transport.call_count, 3)

    def test_labelled_mode_does_not_change_non_chat_payloads_or_need_chat_scope(self):
        a, transport = self.adapter([response(status=204)], sender_mode="broadcaster_labelled")
        text = "Applied AI Office Hours"
        self.assertEqual(a.execute(action("title", text))["state"], "done")
        self.assertEqual(transport.call_args.kwargs["json"], {"title": text})
        self.assertEqual(transport.call_args.kwargs["headers"], self.host.return_value[2])

    def test_labelled_mode_preserves_shared_chat_and_confirmation_boundaries(self):
        self.host.return_value[1]["scopes"].append("user:write:chat")
        a, transport = self.adapter([response([{"session_id": "shared"}])], sender_mode="broadcaster_labelled")
        self.assertEqual(a.execute(action())["state"], "needs_attention")
        self.assertEqual(transport.call_count, 1)
        for write, state in (
            (requests.Timeout(), "uncertain"),
            (response([{"is_sent": False, "drop_reason": {"code": "automod_held"}}]), "needs_attention"),
        ):
            with self.subTest(state=state):
                a, transport = self.adapter([response(), write], sender_mode="broadcaster_labelled")
                result = a.execute(action())
                self.assertEqual(result["state"], state)
                self.assertNotIn("message", result, "Only a confirmed receipt claims message delivery")
                self.assertEqual(transport.call_count, 2)

    def test_invalid_sender_modes_reject_before_authentication_or_transport(self):
        for mode in (None, "", "fallback", True, [], {}):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "sender mode"):
                    self.adapter(sender_mode=mode)
        self.host.assert_not_called()
        self.sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()

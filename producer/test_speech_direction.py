"""Speech decisions stay evidence-bound and affect only the current local show."""

import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import server
from audience import Audience
from captions import CaptionSession
from on_air import OnAir
from speech_direction import interpret, validate


QUESTION = {
    "id": "graphic-question",
    "kind": "question",
    "line": "How would a non-coder use this?",
    "attribution": "Fictional viewer",
}


def answer(**changes):
    return {
        "question_status": "keep",
        "question_quote": "",
        "directly_addressed": False,
        "command": "none",
        "target": "",
        "command_quote": "",
        "reason": "Authored test decision",
        **changes,
    }


def session():
    return {
        "on_air": OnAir(True),
        "visual": None,
        "seen": time.monotonic(),
        "captions": None,
        "captions_enabled": False,
    }


def inbox():
    value = Audience()
    value.owner = "show"
    value.channel = "channel"
    value.connected = True
    value.questions = [
        {
            "id": "message-1",
            "user_id": "viewer",
            "author": "Fictional viewer",
            "text": QUESTION["line"],
            "received": 100,
        }
    ]
    return value


class SpeechValidationTests(unittest.TestCase):
    def test_spoken_dismissal_does_not_require_a_second_visual_command(self):
        speech = "Producer, let's skip that question."
        proposed = answer(
            question_status="dismissed", question_quote=speech, directly_addressed=True
        )
        decision = validate(proposed, speech, QUESTION)
        self.assertEqual(decision["question_status"], "dismissed")
        self.assertEqual(decision["command"], "none")
        self.assertEqual(
            validate(proposed, speech, QUESTION, "")["question_status"], "keep"
        )

    def test_map_request_needs_an_exact_current_quote_and_producer_address(self):
        speech = "Producer, bring me up a map of Canada."
        proposed = answer(
            command="show_map",
            target="Canada",
            command_quote=speech,
            directly_addressed=True,
        )
        accepted = validate(proposed, speech, {})
        self.assertEqual(accepted["command"], "show_map")
        self.assertEqual(accepted["target"], "Canada")
        self.assertEqual(
            validate(
                {**proposed, "command_quote": "Producer, show a map of Canada."},
                speech,
                {},
            )["command"],
            "none",
        )
        no_address = "Bring me up a map of Canada."
        self.assertEqual(
            validate({**proposed, "command_quote": no_address}, no_address, {})[
                "command"
            ],
            "none",
        )
        self.assertEqual(
            validate({**proposed, "directly_addressed": False}, speech, {})["command"],
            "none",
        )

    def test_quoted_and_hypothetical_speech_cannot_execute_when_not_addressed(self):
        for speech in (
            "A viewer wrote: Producer, bring me up a map of Canada.",
            "If I said Producer, bring me up a map of Canada, that would be an example.",
        ):
            with (
                self.subTest(speech=speech),
                patch(
                    "speech_direction.structured",
                    return_value=answer(
                        command="show_map",
                        target="Canada",
                        command_quote="Producer, bring me up a map of Canada",
                        directly_addressed=False,
                    ),
                ) as model,
            ):
                decision = interpret(Mock(), speech, {"graphic": QUESTION})
                self.assertEqual(decision["command"], "none")
                self.assertEqual(decision["question_status"], "keep")
                packet = model.call_args.args[2]
                self.assertEqual(packet["current_speech"], speech)
                self.assertEqual(packet["current_question"]["id"], QUESTION["id"])

    def test_unsupported_country_is_not_mislabeled_as_the_available_canada_map(self):
        speech = "Producer, show me a map of France."
        decision = validate(
            answer(
                command="show_map",
                target="France",
                directly_addressed=True,
                command_quote=speech,
            ),
            speech,
            {},
        )
        self.assertEqual(decision["command"], "unsupported")
        self.assertEqual(decision["target"], "France")

    def test_question_response_must_be_evidenced_after_question_display(self):
        response = "A non-coder can start by turning meeting notes into a task list."
        proposed = answer(question_status="answered", question_quote=response)
        self.assertEqual(
            validate(proposed, response, QUESTION, response)["question_status"],
            "answered",
        )
        self.assertEqual(
            validate(proposed, response, QUESTION, "")["question_status"], "keep"
        )
        self.assertEqual(
            validate(
                {**proposed, "question_quote": "An answer never spoken"},
                response,
                QUESTION,
                response,
            )["question_status"],
            "keep",
        )

    def test_a_pre_display_skip_command_cannot_clear_a_newly_displayed_question(self):
        speech = "Producer, let's skip that question."
        decision = validate(
            answer(
                question_status="skipped",
                question_quote=speech,
                directly_addressed=True,
                command="clear_question",
                command_quote=speech,
            ),
            speech,
            QUESTION,
            "",
        )
        self.assertEqual(decision["question_status"], "keep")
        self.assertEqual(decision["command"], "none")


class LocalDirectionEffectsTests(unittest.TestCase):
    def test_old_question_decision_does_not_resolve_or_unpin_the_next_question(self):
        value, current = inbox(), session()
        value.pin_question("show", "message-1")
        current["on_air"].publish(
            {"kind": "question", "line": QUESTION["line"], "message_id": "message-1"},
            100,
        )
        old_id = current["on_air"].snapshot(100)["graphic"]["id"]
        current["on_air"].resolve_question(old_id, "answered", 101)
        value.questions.append(
            {
                "id": "message-2",
                "user_id": "other",
                "author": "Other fictional viewer",
                "text": "Could you show an example?",
                "received": 102,
            }
        )
        value.pin_question("show", "message-2")
        current["on_air"].publish(
            {
                "kind": "question",
                "line": "Could you show an example?",
                "message_id": "message-2",
            },
            102,
        )
        selected = current["on_air"].snapshot(102)["graphic"]
        with patch("server.audience", value):
            self.assertFalse(
                server.apply_direction(
                    "show",
                    current,
                    {
                        "question_status": "answered",
                        "question_id": old_id,
                        "command": "none",
                    },
                )
            )
        self.assertEqual(
            current["on_air"].snapshot(103)["graphic"]["id"], selected["id"]
        )
        self.assertEqual(value.selection["message_id"], "message-2")
        self.assertTrue(value.selection["valid"])

    def test_map_show_and_clear_are_local_and_unsupported_request_keeps_existing_output(
        self,
    ):
        current = session()
        with (
            patch("server.audience", Audience()),
            patch("requests.request", side_effect=AssertionError("No remote effect")),
        ):
            self.assertTrue(
                server.apply_direction(
                    "show", current, {"question_status": "keep", "command": "show_map"}
                )
            )
            shown = current["visual"]
            self.assertEqual(shown["asset_id"], "canada")
            self.assertEqual(shown["kind"], "map")
            self.assertNotIn("src", shown)
            self.assertFalse(
                server.apply_direction(
                    "show",
                    current,
                    {"question_status": "keep", "command": "unsupported"},
                )
            )
            self.assertEqual(current["visual"], shown)
            self.assertEqual(
                current["director_note"]["line"], "That visual is not available yet."
            )
            self.assertTrue(
                server.apply_direction(
                    "show",
                    current,
                    {"question_status": "keep", "command": "clear_visual"},
                )
            )
            self.assertIsNone(current["visual"])
            self.assertIsNone(current["director_note"])

    def test_clear_all_withdraws_question_context_and_map_and_unpins(self):
        value, current = inbox(), session()
        value.pin_question("show", "message-1")
        current["on_air"].publish(
            {"kind": "question", "line": QUESTION["line"], "message_id": "message-1"}
        )
        current["on_air"].update_context({"subject": "Office Hours", "purpose": ""})
        current["visual"] = {"asset_id": "canada"}
        with patch("server.audience", value):
            self.assertTrue(
                server.apply_direction(
                    "show", current, {"question_status": "keep", "command": "clear_all"}
                )
            )
        self.assertEqual(
            current["on_air"].snapshot(), {"graphic": None, "context": None}
        )
        self.assertIsNone(current["visual"])
        self.assertIsNone(value.selection)

    def test_worker_result_after_stop_has_no_visible_effect(self):
        current = session()
        speech = "Producer, bring me up a map of Canada."
        current["voice_pending"] = ((speech, "", ""), speech, "", time.monotonic())
        current["voice_busy"] = True

        def stopped(*_):
            server.LIVE.pop("show")
            current["on_air"].close()
            return {
                "question_status": "keep",
                "command": "show_map",
                "target": "Canada",
                "command_quote": speech,
            }

        with (
            patch.dict(server.LIVE, {"show": current}, clear=True),
            patch("server.audience", Audience()),
            patch("server._read_key", return_value="fictional-key"),
            patch("server.speech_direction.interpret", side_effect=stopped),
        ):
            server.direct_speech("show", current, Path("/unused-test-key"))
        self.assertIsNone(current["visual"])
        self.assertIsNone(current["on_air"].snapshot()["graphic"])
        self.assertFalse(current["voice_busy"])

    def test_public_state_keeps_pinned_question_after_ttl_but_obeys_moderation(self):
        value, current = inbox(), session()
        value.pin_question("show", "message-1")
        current["on_air"].publish(
            {"kind": "question", "line": QUESTION["line"], "message_id": "message-1"},
            100,
        )
        with (
            patch("server.audience", value),
            patch("audience.time.time", return_value=1000),
        ):
            before = server.public_state("show", current)
            self.assertEqual(value.questions, [])
            self.assertEqual(before["graphic"]["message_id"], "message-1")
            value.accept(
                {
                    "metadata": {
                        "message_id": "delete-1",
                        "message_timestamp": datetime.fromtimestamp(
                            1000, timezone.utc
                        ).isoformat(),
                    },
                    "payload": {
                        "subscription": {
                            "type": "channel.chat.message_delete",
                            "version": "1",
                        },
                        "event": {
                            "broadcaster_user_id": "channel",
                            "message_id": "message-1",
                        },
                    },
                },
                received=1000,
            )
            self.assertIsNone(server.public_state("show", current)["graphic"])


class ListeningWithoutSubtitleTests(unittest.TestCase):
    def test_transcription_callback_can_feed_direction_without_publishing_it(self):
        caption = CaptionSession.__new__(CaptionSession)
        caption.lock = threading.RLock()
        caption.closed = False
        caption.items, caption.retired = {}, set()
        caption.order, caption.current = 0, None
        caption.on_text = Mock()
        caption.accept(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "turn",
                "delta": "Producer, take that down.",
            },
            1,
        )
        caption.on_text.assert_called_once_with("Producer, take that down.")
        self.assertEqual(caption.current["text"], "Producer, take that down.")

    def test_http_on_air_omits_subtitles_unless_explicitly_enabled(self):
        current = session()
        current["captions"] = Mock()
        current["captions"].snapshot.return_value = {
            "text": "Internal listening text",
            "partial": False,
        }
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        try:
            with (
                patch.dict(server.LIVE, {"show": current}, clear=True),
                patch("server.audience", Audience()),
            ):
                response = requests.get(base + "/api/on-air", timeout=3)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()["caption"])
                current["captions"].snapshot.assert_not_called()
                current["captions_enabled"] = True
                response = requests.get(base + "/api/on-air", timeout=3)
                self.assertEqual(
                    response.json()["caption"]["text"], "Internal listening text"
                )
                current["captions"].snapshot.assert_called_once()
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

"""Protocol, timing and Stop checks on synthetic PCM and a mocked WebSocket."""

import base64
import copy
import json
import queue
import unittest
from unittest.mock import Mock

from realtime_director import MODEL, TOOLS, RealtimeDirector


ACK = {"type": "session.updated", "session": {
    "output_modalities": ["text"], "audio": {"input": {"turn_detection": None}},
}}
HALF_SECOND = base64.b64encode(b"\x00\x08" * 12000).decode()


class Socket:
    def __init__(self, *events):
        self.events = queue.Queue()
        for event in events:
            self.events.put(json.dumps(event))
        self.sent = []
        self.closed = False

    def send(self, encoded):
        self.sent.append(json.loads(encoded))

    def recv(self, timeout=None):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError() from None

    def close(self):
        self.closed = True


class DirectorTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.socket = Socket()
        self.state = {"revision": 0, "confidence": None, "public": {"graphic": None}}
        self.changes, self.saved, self.reads = [], [], []
        self.directors = []

    def tearDown(self):
        for director in self.directors:
            director.close()

    def make(self, *, offset=0, start_thread=False, socket=None, **overrides):
        socket = socket or self.socket

        def apply(args, expected):
            self.changes.append((copy.deepcopy(args), expected))
            self.state["revision"] += 1
            return {"ok": True, "state": copy.deepcopy(self.state)}

        def recall(args, before):
            self.reads.append((args, before))
            return {"excerpts": [{"start": 0, "end": min(2, before), "text": "Earlier authored show speech"}]}

        director = RealtimeDirector(
            "unused-test-key", brief=overrides.pop("brief", {"raw_notes": "Explain the example, then the trade-off."}),
            state=lambda: copy.deepcopy(self.state), apply=apply, recall=recall,
            transcript=lambda *args: self.saved.append(args), offset=offset,
            connection=Mock(return_value=socket), clock=lambda: self.now,
            start_thread=start_thread, **overrides,
        )
        self.directors.append(director)
        if not start_thread:
            director.accept(copy.deepcopy(ACK))
        return director

    def append(self, director, count=4):
        for _ in range(count):
            self.now += 0.5
            director.append(director.sequence, HALF_SECOND)

    def begin(self, director, ident="response-one"):
        self.assertTrue(director.consider())
        director.accept({"type": "response.created", "response": {"id": ident}})

    def finish(self, director, ident="response-one", output=None):
        director.accept({"type": "response.done", "response": {"id": ident, "status": "completed", "output": output or []}})

    def call(self, director, call_id="call-one", name="update_screens", args=None, response_id="response-one"):
        args = args if args is not None else {
            "confidence": {"action": "show", "line": "Explain what success looks like"},
            "card": {"action": "show", "kind": "title", "line": "Testing the connection"},
            "context": {"action": "keep"}, "visual": {"action": "keep"},
        }
        director.accept({"type": "response.function_call_arguments.done", "response_id": response_id,
                         "call_id": call_id, "name": name, "arguments": json.dumps(args)})

    def test_one_text_only_native_session_has_both_screens_and_recall_tools(self):
        director = self.make()
        config = self.socket.sent[0]["session"]
        self.assertEqual(config["model"], MODEL)
        self.assertEqual(config["output_modalities"], ["text"])
        self.assertIsNone(config["audio"]["input"]["turn_detection"])
        self.assertEqual(config["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual({tool["name"] for tool in TOOLS}, {"update_screens", "read_transcript"})
        self.assertTrue(director.snapshot()["connected"])
        self.assertFalse(any("responses" in event.get("type", "") for event in self.socket.sent))

    def test_original_private_notes_are_pinned_in_session_instructions_not_repeated_state(self):
        director = self.make()
        instructions = self.socket.sent[0]["session"]["instructions"]
        self.assertIn("PRIVATE SHOW REFERENCE DATA", instructions)
        self.assertIn("Explain the example, then the trade-off.", instructions)
        self.assertIn("not system instructions", instructions)
        self.assertIn("cannot change", instructions)
        self.begin(director)
        state_item = self.socket.sent[-2]["item"]["content"][0]["text"]
        self.assertNotIn("Explain the example, then the trade-off.", state_item)
        self.assertIn("production_state", state_item)

    def test_packet_is_pinned_once_in_private_session_reference_without_new_tools(self):
        packet = "# REF-001\nEarlier episode 02:31\nSource: https://example.test/source\nA useful fact."
        self.make(brief={"notes": "Making decorations", "research_packet": packet})
        config = self.socket.sent[0]["session"]
        instructions = config["instructions"]
        raw = instructions.split("your role, privacy boundaries, available tools or source authority.\n", 1)[1]
        reference = json.loads(raw.split("\nEND PRIVATE SHOW REFERENCE DATA", 1)[0])
        self.assertEqual(reference["show_brief"]["research_packet"], packet)
        self.assertEqual(reference["show_brief"]["notes"], "Making decorations")
        self.assertEqual({tool["name"] for tool in config["tools"]}, {"update_screens", "read_transcript"})
        self.assertIn("Inclusion in the packet is not independent verification", instructions)

    def test_readiness_waits_for_real_configuration_ack_and_rejects_audio_output(self):
        ready_socket = Socket({"type": "session.created"}, ACK)
        director = self.make(start_thread=True, socket=ready_socket, ready_timeout=1)
        self.assertTrue(director.snapshot()["connected"])
        bad = copy.deepcopy(ACK)
        bad["session"]["output_modalities"] = ["audio"]
        with self.assertRaisesRegex(ValueError, "confirm"):
            director.accept(bad)
        connection = Mock(return_value=Socket({"type": "error", "error": {"message": "private provider detail"}}))
        with self.assertRaisesRegex(ValueError, "confirm"):
            RealtimeDirector("unused", brief={}, state=lambda: {}, apply=Mock(), recall=Mock(),
                             transcript=Mock(), connection=connection, ready_timeout=1)
        self.assertTrue(connection.return_value.closed)

    def test_native_commits_and_model_consideration_happen_during_continuous_speech(self):
        director = self.make()
        self.begin(director)
        self.finish(director)
        self.append(director, 4)
        commits = [e for e in self.socket.sent if e["type"] == "input_audio_buffer.commit"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(director.snapshot()["audio_committed_seconds"], 2)
        self.assertEqual(self.saved, [], "Reasoning must not wait for transcription")
        self.now = 3
        director.tick()
        self.assertTrue(director.busy)
        self.assertEqual(director.responses, 2)
        requests = [e for e in self.socket.sent if e["type"] == "response.create"]
        self.assertEqual(len(requests), 2)
        self.assertLess(self.now, 8)

    def test_pending_events_coalesce_while_one_response_is_in_flight(self):
        director = self.make()
        self.begin(director)
        self.append(director, 6)
        director.notify("new simulated question")
        self.state["revision"] = 4
        self.state["confidence"] = {"line": "Current presenter cue"}
        self.state["public"] = {"graphic": {"line": "Current audience card"}}
        director.notify("display changed")
        director.tick()
        self.assertEqual(director.responses, 1)
        self.finish(director)
        director.tick()
        self.assertEqual(director.responses, 2)
        text = self.socket.sent[-2]["item"]["content"][0]["text"]
        self.assertIn("display changed", text)
        packet = json.loads(text)
        self.assertEqual(packet["production_state"]["revision"], 4)
        self.assertEqual(packet["production_state"]["confidence"]["line"], "Current presenter cue")
        self.assertEqual(packet["production_state"]["public"]["graphic"]["line"], "Current audience card")

    def test_silence_can_be_reconsidered_without_a_new_transcript(self):
        director = self.make()
        self.begin(director)
        self.finish(director)
        self.now = 10
        director.last_audio = 10  # Native silence packets still keep source alive.
        director.tick()
        self.assertEqual(director.responses, 2)
        self.assertEqual(self.saved, [])

    def test_audio_order_rate_and_packet_bounds_are_enforced(self):
        director = self.make()
        with self.assertRaises(ValueError):
            director.append(1, HALF_SECOND)
        with self.assertRaises(ValueError):
            director.append(0, "not base64")
        with self.assertRaises(ValueError):
            director.append(0, base64.b64encode(b"odd").decode())
        for i in range(6):
            director.append(i, HALF_SECOND)
        with self.assertRaisesRegex(ValueError, "ahead"):
            director.append(6, HALF_SECOND)
        self.assertEqual(director.sequence, 6)

    def test_out_of_order_asr_is_saved_in_source_order_with_offset(self):
        director = self.make(offset=20)
        self.append(director, 8)
        director.accept({"type": "input_audio_buffer.committed", "item_id": "one"})
        director.accept({"type": "input_audio_buffer.committed", "item_id": "one"})
        director.accept({"type": "input_audio_buffer.committed", "item_id": "two"})
        director.accept({"type": "conversation.item.input_audio_transcription.completed", "item_id": "two", "transcript": "Second part"})
        self.assertEqual(self.saved, [])
        director.accept({"type": "conversation.item.input_audio_transcription.completed", "item_id": "one", "transcript": "First part"})
        self.assertEqual(self.saved, [(20, 22, "First part", "transcribed"), (22, 24, "Second part", "transcribed")])
        director.accept({"type": "conversation.item.input_audio_transcription.completed", "item_id": "one", "transcript": "Late duplicate"})
        self.assertEqual(len(self.saved), 2)

    def test_failed_or_missing_asr_is_not_invented_and_does_not_gate_native_decision(self):
        director = self.make()
        self.append(director, 4)
        director.accept({"type": "input_audio_buffer.committed", "item_id": "one"})
        director.accept({"type": "conversation.item.input_audio_transcription.failed", "item_id": "one"})
        self.assertEqual(self.saved, [(0, 2, "", "failed")])
        self.begin(director)
        self.call(director)
        self.assertEqual(len(self.changes), 1)

    def test_late_commit_ack_cannot_assign_expired_speech_to_the_next_window(self):
        director = self.make(offset=10)
        self.append(director, 45)  # 22.5 s: the first 2 s window has timed out.
        director.tick()
        self.assertEqual(self.saved, [(10, 12, "", "failed")])
        director.accept({"type": "input_audio_buffer.committed", "item_id": "late-first"})
        director.accept({"type": "conversation.item.input_audio_transcription.completed",
                         "item_id": "late-first", "transcript": "Expired first-window speech"})
        self.assertEqual(self.saved, [(10, 12, "", "failed")])
        self.assertNotIn("late-first", director._transcript_items)
        director.accept({"type": "input_audio_buffer.committed", "item_id": "second"})
        director.accept({"type": "conversation.item.input_audio_transcription.completed",
                         "item_id": "second", "transcript": "Actual second-window speech"})
        self.assertEqual(self.saved, [(10, 12, "", "failed"),
                                     (12, 14, "Actual second-window speech", "transcribed")])

    def test_missing_commit_acks_remain_bounded_even_after_transcript_expiry(self):
        director = self.make()
        self.append(director, 45)
        director.tick()
        self.assertLess(len(director._commits), len(director._unacknowledged_commits))
        with self.assertRaisesRegex(ValueError, "fell behind"):
            self.append(director, 23)  # The 17th unacknowledged commit stops input.
        director.close()
        self.assertEqual(len(director._unacknowledged_commits), 0)

    def test_one_tool_updates_both_screens_and_duplicate_events_cannot_reapply(self):
        director = self.make()
        self.begin(director)
        args = {"confidence": {"action": "show", "line": "Explain what success looks like"},
                "card": {"action": "clear"}, "context": {"action": "keep"}, "visual": {"action": "keep"}}
        self.call(director, args=args)
        item = {"type": "function_call", "name": "update_screens", "call_id": "call-one", "arguments": json.dumps(args)}
        director.accept({"type": "response.output_item.done", "item": item})
        self.finish(director, output=[item])
        self.assertEqual(len(self.changes), 1)
        self.assertEqual(self.changes[0][1]["revision"], 0)
        self.assertEqual(director._response_state["revision"], 1)
        receipts = [e for e in self.socket.sent if e.get("item", {}).get("type") == "function_call_output"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0]["item"]["output"])["state"]["revision"], 1)

    def test_name_less_arguments_event_uses_function_output_item_metadata(self):
        director = self.make()
        self.begin(director)
        director.accept({"type": "response.output_item.added", "item": {"type": "function_call", "name": "update_screens", "call_id": "metadata-call"}})
        director.accept({"type": "response.function_call_arguments.done", "response_id": "response-one", "call_id": "metadata-call", "arguments": "{}"})
        self.assertEqual(len(self.changes), 1)

    def test_unknown_invalid_stale_or_old_response_tools_never_change_screens(self):
        director = self.make()
        self.begin(director)
        self.call(director, call_id="old-response", response_id="different-response")
        self.call(director, call_id="unknown", name="post_to_twitch")
        director.accept({"type": "response.function_call_arguments.done", "name": "update_screens", "call_id": "malformed", "arguments": "[not json"})
        self.now = 21
        self.call(director, call_id="stale")
        self.assertEqual(self.changes, [])
        errors = [json.loads(e["item"]["output"]).get("error") for e in self.socket.sent if e.get("item", {}).get("type") == "function_call_output"]
        self.assertIn("invalid_tool_call", errors)
        self.assertIn("stale_response", errors)

    def test_rejected_local_choice_returns_reason_and_actual_state_without_retry(self):
        director = self.make()
        self.begin(director)
        self.state["revision"] = 3
        self.state["public"] = {"graphic": None, "enabled": False}
        director.apply = Mock(side_effect=ValueError("The owner has disabled public output for this show."))
        self.call(director)
        receipt_event = [e for e in self.socket.sent if e.get("item", {}).get("type") == "function_call_output"][-1]
        receipt = json.loads(receipt_event["item"]["output"])
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["detail"], "The owner has disabled public output for this show.")
        self.assertEqual(receipt["state"]["revision"], 3)
        self.assertFalse(receipt["state"]["public"]["enabled"])
        self.assertEqual(self.changes, [])
        self.finish(director)
        director.tick()
        self.assertEqual(director.responses, 1, "Rejected choices do not trigger an automatic retry")
        director.apply.assert_called_once()
        self.now = 3
        director.notify("new local state")
        director.tick()
        self.assertEqual(director.responses, 2)
        director.apply.assert_called_once()

    def test_recall_is_past_bounded_and_continues_only_a_bounded_number_of_times(self):
        director = self.make(offset=20)
        self.append(director, 4)
        self.begin(director)
        self.call(director, name="read_transcript", args={"query": "unfinished example"})
        self.assertEqual(self.reads[0][1], 22)
        self.finish(director)
        self.assertEqual(director.responses, 2)
        for i in range(2):
            ident = "continuation-" + str(i)
            director.accept({"type": "response.created", "response": {"id": ident}})
            self.call(director, call_id="read-" + str(i), name="read_transcript", args={}, response_id=ident)
            self.finish(director, ident)
        self.assertEqual(director.responses, 3)
        self.assertFalse(director.busy)

    def test_optional_editor_queues_once_and_does_not_block_on_a_result_or_continue(self):
        consult = Mock(return_value={"ok": True, "queued": True, "review_id": "authored-review"})
        director = self.make(consult=consult, offset=20)
        advertised = self.socket.sent[0]["session"]["tools"]
        self.assertIn("consult_editor", {tool["name"] for tool in advertised})
        self.append(director, 4)
        self.begin(director)
        self.call(director, name="consult_editor", args={"question": "How can we explain this trade-off more clearly?"})
        self.call(director, name="consult_editor", args={"question": "Duplicate event must not queue twice"})
        consult.assert_called_once_with({"question": "How can we explain this trade-off more clearly?"}, 22)
        self.assertEqual(self.changes, [], "Background review never changes screens")
        receipt = [e for e in self.socket.sent if e.get("item", {}).get("type") == "function_call_output"][-1]
        self.assertTrue(json.loads(receipt["item"]["output"])["queued"])
        self.finish(director)
        self.assertFalse(director.busy)
        self.assertEqual(director.responses, 1, "Queuing a review does not start a waiting/approval loop")

    def test_editor_tool_is_not_advertised_or_executable_without_callback(self):
        director = self.make()
        advertised = self.socket.sent[0]["session"]["tools"]
        self.assertNotIn("consult_editor", {tool["name"] for tool in advertised})
        self.begin(director)
        self.call(director, name="consult_editor", args={"question": "Not configured"})
        receipt = [e for e in self.socket.sent if e.get("item", {}).get("type") == "function_call_output"][-1]
        self.assertEqual(json.loads(receipt["item"]["output"])["error"], "invalid_tool_call")
        self.assertEqual(self.changes, [])

    def test_production_tool_is_advertised_only_with_its_callback(self):
        production = Mock()
        self.make(production=production)
        tools = {tool["name"]: tool for tool in self.socket.sent[0]["session"]["tools"]}
        self.assertEqual(set(tools), {"update_screens", "read_transcript", "production_action"})
        schema = tools["production_action"]["parameters"]
        self.assertEqual(schema["required"], ["action"])
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("share_on_air", schema["properties"]["action"]["enum"])
        self.assertIn("output_key", schema["properties"])
        self.assertNotIn("message", schema["properties"], "Chat selects actual public output, not arbitrary text")
        production.assert_not_called()

        other_socket = Socket()
        self.make(socket=other_socket)
        self.assertNotIn("production_action", {tool["name"] for tool in other_socket.sent[0]["session"]["tools"]})

    def test_unconfigured_production_call_is_rejected_without_local_effects(self):
        director = self.make()
        self.begin(director)
        self.call(director, name="production_action", args={"action": "share_on_air", "output_key": "card:one"})
        receipt = [e for e in self.socket.sent if e.get("item", {}).get("type") == "function_call_output"][-1]
        self.assertEqual(json.loads(receipt["item"]["output"]), {"ok": False, "error": "invalid_tool_call"})
        self.assertEqual(self.changes, [])
        self.assertEqual(self.reads, [])
        self.finish(director)
        self.assertFalse(director.busy)
        self.assertEqual(director.responses, 1)

    def test_screen_then_production_calls_use_each_latest_returned_state(self):
        displayed = {"revision": 1, "public": {"graphic": {"line": "Useful public reference"}},
                     "production": {"shareable_outputs": [{"key": "card:one", "text": "Useful public reference"}]}}
        delivered = {**copy.deepcopy(displayed), "revision": 2,
                     "production": {"receipts": [{"state": "done", "simulated": True}]}}
        production = Mock(return_value={"ok": True, "state": delivered, "simulated": True})
        director = self.make(production=production)
        director.apply = Mock(return_value={"ok": True, "state": displayed})
        self.begin(director)
        self.call(director, call_id="screen-one")
        share = {"action": "share_on_air", "output_key": "card:one"}
        self.call(director, call_id="share-one", name="production_action", args=share)
        production.assert_called_once_with(share, displayed)
        self.assertEqual(self.state["revision"], 0, "The callback receipt, not a stale state getter, advances the response")
        check = {"action": "get_poll_result", "poll_id": "mock-poll-one"}
        self.call(director, call_id="poll-check", name="production_action", args=check)
        self.assertEqual(production.call_args.args, (check, delivered))
        self.call(director, call_id="screen-two")
        self.assertEqual(director.apply.call_args.args[1], delivered)
        receipts = [json.loads(e["item"]["output"]) for e in self.socket.sent
                    if e.get("item", {}).get("type") == "function_call_output"]
        self.assertTrue(receipts[1]["simulated"])
        self.assertEqual(receipts[1]["state"], delivered)
        self.finish(director)
        self.assertEqual(director.responses, 1, "Production receipts do not create an automatic retry loop")

    def test_duplicate_production_completions_dispatch_once_across_responses(self):
        production = Mock(return_value={"ok": True, "simulated": True})
        director = self.make(production=production)
        self.begin(director)
        args = {"action": "share_on_air", "output_key": "card:one"}
        self.call(director, call_id="write-once", name="production_action", args=args)
        item = {"type": "function_call", "name": "production_action", "call_id": "write-once",
                "arguments": json.dumps(args)}
        director.accept({"type": "response.output_item.done", "response_id": "response-one", "item": item})
        self.finish(director, output=[item])
        self.now = 3
        self.begin(director, ident="response-two")
        self.call(director, call_id="write-once", name="production_action", args=args, response_id="response-two")
        production.assert_called_once_with(args, {"revision": 0, "confidence": None, "public": {"graphic": None}})
        receipts = [e for e in self.socket.sent if e.get("item", {}).get("type") == "function_call_output"]
        self.assertEqual(len(receipts), 1, "Duplicate transport events neither write again nor produce another receipt")

    def test_old_response_production_completions_cannot_dispatch(self):
        args = {"action": "share_on_air", "output_key": "card:old"}
        item = {"type": "function_call", "name": "production_action", "call_id": "old-write",
                "arguments": json.dumps(args)}
        events = [
            {**item, "type": "response.function_call_arguments.done", "response_id": "response-old"},
            {"type": "response.output_item.done", "response_id": "response-old", "item": item},
            {"type": "response.done", "response": {"id": "response-old", "status": "completed", "output": [item]}},
        ]
        for event in events:
            with self.subTest(event=event["type"]):
                production = Mock(return_value={"ok": True})
                director = self.make(socket=Socket(), production=production)
                self.begin(director)
                director.accept(copy.deepcopy(event))
                production.assert_not_called()
                self.assertTrue(director.busy, "An old response cannot end the active response")

    def test_stale_idle_and_closed_production_calls_cannot_dispatch(self):
        production = Mock(return_value={"ok": True})
        director = self.make(production=production)
        args = {"action": "share_on_air", "output_key": "card:one"}
        self.call(director, call_id="idle-write", name="production_action", args=args)
        self.begin(director)
        self.now = 21
        self.call(director, call_id="stale-write", name="production_action", args=args)
        receipt = [e for e in self.socket.sent if e.get("item", {}).get("type") == "function_call_output"][-1]
        self.assertEqual(json.loads(receipt["item"]["output"])["error"], "stale_response")
        self.finish(director)
        self.begin(director, ident="response-two")
        director.close()
        sent_before = len(self.socket.sent)
        self.call(director, call_id="closed-write", name="production_action", args=args, response_id="response-two")
        self.finish(director, ident="response-two", output=[{
            "type": "function_call", "name": "production_action", "call_id": "closed-final-write",
            "arguments": json.dumps(args),
        }])
        production.assert_not_called()
        self.assertEqual(len(self.socket.sent), sent_before)

    def test_stop_discards_late_tools_and_transcripts_and_stores_no_audio(self):
        director = self.make()
        self.append(director, 4)
        self.begin(director)
        director.accept({"type": "input_audio_buffer.committed", "item_id": "one"})
        director.close()
        self.call(director)
        director.accept({"type": "conversation.item.input_audio_transcription.completed", "item_id": "one", "transcript": "Too late"})
        self.assertEqual(self.changes, [])
        self.assertEqual(self.saved, [])
        self.assertEqual(list(director._commits), [])
        self.assertTrue(self.socket.closed)
        self.assertNotIn(HALF_SECOND, str(director.snapshot()))

    def test_sampled_images_and_contact_sheet_are_distinct_native_inputs(self):
        director = self.make(contact_sheet=b"\xff\xd8contact-sheet")
        director.image(b"\xff\xd8selected-frame", 0)
        image_events = [e for e in self.socket.sent if any(c.get("type") == "input_image" for c in e.get("item", {}).get("content", []))]
        self.assertEqual(len(image_events), 2)
        self.assertIn("image candidates", image_events[0]["item"]["content"][0]["text"])
        self.assertIn("Selected-source still", image_events[1]["item"]["content"][0]["text"])
        with self.assertRaises(ValueError):
            director.image(b"\xff\xd8future", 10)

    def test_named_selected_frame_carries_original_native_image_and_no_hidden_comment_text(self):
        director = self.make(contact_sheet=b"\xff\xd8contact-sheet")
        jpeg = b"\xff\xd8the-selected-image-with-chat-pixels"
        director.image(jpeg, 0, frame_id="frame-selected-123")
        events = [e for e in self.socket.sent if any(c.get("type") == "input_image" for c in e.get("item", {}).get("content", []))]
        contact, selected = events[0]["item"]["content"], events[1]["item"]["content"]
        self.assertNotIn("Frame ID:", contact[0]["text"])
        self.assertIn("Frame ID: frame-selected-123.", selected[0]["text"])
        self.assertEqual(base64.b64decode(selected[1]["image_url"].split(",", 1)[1]), jpeg)
        self.assertNotIn("screen_comment", selected[0]["text"])
        with self.assertRaises(ValueError):
            director.image(jpeg, 0, frame_id="invalid frame identity")
        self.assertEqual(len([e for e in self.socket.sent if any(c.get("type") == "input_image" for c in e.get("item", {}).get("content", []))]), 2)

    def test_one_screen_comment_tool_call_has_a_frame_bound_author_body_without_a_second_tool(self):
        director = self.make()
        schema = TOOLS[0]["parameters"]["properties"]["screen_comment"]
        self.assertEqual(set(schema["required"]), {"frame_id", "author", "text"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["text"]["maxLength"], 180)
        instructions = self.socket.sent[0]["session"]["instructions"]
        self.assertIn("No Twitch API or hidden fixture supplies the wording", instructions)
        self.assertIn("Omit unreadable, clipped or uncertain text", instructions)
        self.begin(director)
        args = {"screen_comment": {"frame_id": "frame-selected-123", "author": "Maya", "text": "What is that material?"},
                "card": {"action": "show", "kind": "question"}, "confidence": {"action": "show"},
                "context": {"action": "keep"}, "visual": {"action": "keep"}}
        self.call(director, args=args)
        self.assertEqual(len(self.changes), 1)
        self.assertEqual(self.changes[0][0], args)

    def test_model_context_names_the_second_line_as_public_subtitle(self):
        context = TOOLS[0]["parameters"]["properties"]["context"]["properties"]
        self.assertIn("subtitle", context)
        self.assertNotIn("purpose", context)
        self.assertIn("exact audience-facing text", context["subtitle"]["description"])
        self.assertEqual(context["subtitle"]["maxLength"], 120)

    def test_provider_failure_is_terminal_without_reconnect_or_private_error_leak(self):
        events = Socket(ACK, {"type": "error", "error": {"message": "A private provider diagnostic"}})
        director = self.make(socket=events)
        director.run()
        status = director.snapshot()
        self.assertTrue(status["closed"])
        self.assertEqual(status["error"], "ValueError")
        self.assertNotIn("private provider diagnostic", str(status))


if __name__ == "__main__":
    unittest.main()

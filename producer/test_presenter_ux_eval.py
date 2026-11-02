"""The optional paid evaluator uses actual routing and a one-response limit."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("presenter_ux_eval", Path(__file__).with_name("presenter-ux-eval.py"))
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


class Socket:
    def __init__(self, *, name="update_screens", args=None):
        self.sent, self.closed = [], False
        self.events = [
            {"type": "session.updated", "session": {"output_modalities": ["text"], "audio": {"input": {"turn_detection": None}}}},
            {"type": "response.created", "response": {"id": "response-test"}},
            {"type": "response.function_call_arguments.done", "response_id": "response-test", "call_id": "test-call", "name": name,
             "arguments": json.dumps(args or {"confidence": {"action": "show", "line": "Show the action items"},
                                               "card": {"action": "keep"}, "context": {"action": "keep"}, "visual": {"action": "keep"},
                                               "working_notes": "PRIVATE_WORKING_SENTINEL"})},
            {"type": "response.done", "response": {"id": "response-test", "status": "completed", "output": []}},
        ]

    def send(self, encoded):
        self.sent.append(json.loads(encoded))

    def recv(self, timeout=None):
        return json.dumps(self.events.pop(0))

    def close(self):
        self.closed = True


class PresenterEvalTests(unittest.TestCase):
    def test_prompt_wrapper_preserves_reference_and_all_protocol_settings(self):
        socket = Socket()
        wrapped = evaluator.PromptSocket(socket, "CANDIDATE_PROMPT")
        wrapped.send(json.dumps({"type": "session.update", "session": {
            "instructions": "ORIGINAL" + evaluator.REFERENCE_MARKER + "PRIVATE_REFERENCE",
            "tools": [{"name": "same_tool"}], "output_modalities": ["text"]}}))
        event = socket.sent[0]
        self.assertEqual(event["session"]["instructions"], "CANDIDATE_PROMPT" + evaluator.REFERENCE_MARKER + "PRIVATE_REFERENCE")
        self.assertEqual(event["session"]["tools"], [{"name": "same_tool"}])
        self.assertEqual(event["session"]["output_modalities"], ["text"])

    def test_one_response_budget_blocks_automatic_followup_before_send(self):
        socket = Socket()
        wrapped = evaluator.PromptSocket(socket, "prompt")
        request = json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}})
        wrapped.send(request)
        with self.assertRaises(evaluator.ResponseBudgetExceeded):
            wrapped.send(request)
        self.assertEqual(len(socket.sent), 1)

    def test_actual_local_routing_records_visible_result_without_private_notes(self):
        socket = Socket()
        with patch("socket.create_connection", side_effect=AssertionError("No network in unit test")):
            row = evaluator.run_case("unused", "candidate", evaluator.CASES[0], connection=lambda *_args, **_kwargs: socket)
        self.assertEqual(row["actual_responses"], 1)
        self.assertTrue(row["completed"])
        self.assertEqual(row["final_confidence"]["line"], "Show the action items")
        self.assertEqual(row["metrics"]["displayed_words"], 4)
        self.assertNotIn("PRIVATE_WORKING_SENTINEL", json.dumps(row))
        self.assertTrue(socket.closed)

    def test_transcript_tool_logs_inconclusive_without_second_response(self):
        socket = Socket(name="read_transcript", args={"query": "action items"})
        row = evaluator.run_case("unused", "candidate", evaluator.CASES[3], connection=lambda *_args, **_kwargs: socket)
        self.assertEqual(row["actual_responses"], 1)
        self.assertTrue(row["continuation_blocked"])
        self.assertEqual(row["review_status"], "inconclusive_follow_up_needed")
        self.assertEqual(sum(e["type"] == "response.create" for e in socket.sent), 1)

    def test_question_fixture_routes_real_question_identity(self):
        case = evaluator.CASES[2]
        socket = Socket(args={"confidence": {"action": "show", "line": case["question"]["text"]},
                              "card": {"action": "show", "kind": "question", "question_id": case["question"]["id"], "line": case["question"]["text"]},
                              "context": {"action": "keep"}, "visual": {"action": "keep"}})
        row = evaluator.run_case("unused", "candidate", case, connection=lambda *_args, **_kwargs: socket)
        self.assertEqual(row["final_public"]["graphic"]["message_id"], "fictional-q1")
        self.assertEqual(row["final_confidence"]["line"], case["question"]["text"])

    def test_candidate_only_runs_six_fixed_cases_without_reading_or_calling_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            candidate, output = folder / "candidate.md", folder / "result.json"
            candidate.write_text("Candidate fixture prompt")
            visited = []
            def fake_case(_key, prompt, case):
                self.assertEqual(prompt, "Candidate fixture prompt")
                visited.append(case["id"])
                return {"actual_responses": 1, "metrics": {"displayed_words": 4},
                        "review_status": "human_semantic_review_required"}
            with (
                patch("sys.argv", ["presenter-ux-eval.py", "--key-file", "/unused-key",
                                   "--candidate-prompt", str(candidate), "--only-candidate", "--output", str(output)]),
                patch("sys.stdout", io.StringIO()),
                patch.object(evaluator.server, "_read_key", return_value="unused-fixture"),
                patch.object(evaluator, "run_case", side_effect=fake_case),
            ):
                evaluator.main()
            result = json.loads(output.read_text())
            self.assertEqual(visited, [case["id"] for case in evaluator.CASES])
            self.assertEqual(len(visited), 6)
            self.assertEqual(result["max_actual_responses"], 6)
            self.assertEqual(result["actual_responses"], 6)
            self.assertEqual(set(result["prompt_sha256"]), {"candidate"})
            self.assertEqual({row["prompt"] for row in result["results"]}, {"candidate"})


if __name__ == "__main__":
    unittest.main()

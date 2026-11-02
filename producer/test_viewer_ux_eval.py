"""Viewer prompt fixture/routing checks with a fake socket; no paid calls."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "viewer_ux_eval", Path(__file__).with_name("viewer-ux-eval.py")
)
viewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(viewer)


class Socket:
    def __init__(self, *, name="update_screens", args=None):
        self.sent, self.closed = [], False
        if args is None:
            args = {"confidence": {"action": "keep"}, "card": {"action": "keep"},
                    "context": {"action": "set", "subject": "AI for everyday work",
                                "purpose": "One practical demonstration"},
                    "visual": {"action": "keep"}, "working_notes": "PRIVATE_SENTINEL"}
        self.events = [
            {"type": "session.updated", "session": {"output_modalities": ["text"], "audio": {"input": {"turn_detection": None}}}},
            {"type": "response.created", "response": {"id": "response-test"}},
            {"type": "response.function_call_arguments.done", "response_id": "response-test",
             "call_id": "viewer-call", "name": name, "arguments": json.dumps(args)},
            {"type": "response.done", "response": {"id": "response-test", "status": "completed", "output": []}},
        ]

    def send(self, encoded):
        self.sent.append(json.loads(encoded))

    def recv(self, timeout=None):
        return json.dumps(self.events.pop(0))

    def close(self):
        self.closed = True


class ViewerEvalTests(unittest.TestCase):
    def test_persistent_initial_topic_and_available_question_are_real_router_inputs(self):
        case = viewer.CASES[5]
        with viewer.local_router(case) as router:
            state = router.state()
            self.assertTrue(router.session["on_air"].context_persistent)
            self.assertEqual(state["public"]["context"], viewer.BASE_TOPIC)
            self.assertIsNone(state["public"]["graphic"])
            self.assertEqual([q["id"] for q in state["audience"]], [case["question"]["id"]])

    def test_actual_routing_records_public_state_without_private_word_metrics(self):
        socket = Socket()
        with patch("socket.create_connection", side_effect=AssertionError("No network")):
            result = viewer.run_case("unused", "Candidate", viewer.CASES[0],
                                     connection=lambda *_a, **_k: socket)
        self.assertEqual(result["actual_responses"], 1)
        self.assertEqual(result["initial_public"]["context"], None)
        self.assertEqual(result["final_public"]["context"], {
            "subject": "AI for everyday work", "purpose": "One practical demonstration"})
        self.assertNotIn("until", result["final_public"]["context"])
        self.assertNotIn("metrics", result)
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
        self.assertTrue(socket.closed)

    def test_question_is_only_aired_after_actual_tool_selection(self):
        case = viewer.CASES[5]
        q = case["question"]
        socket = Socket(args={"confidence": {"action": "keep"},
                              "card": {"action": "show", "kind": "question",
                                       "question_id": q["id"], "line": q["text"]},
                              "context": {"action": "keep"}, "visual": {"action": "keep"}})
        result = viewer.run_case("unused", "Candidate", case, connection=lambda *_a, **_k: socket)
        self.assertIsNone(result["initial_public"]["graphic"])
        self.assertEqual(result["final_public"]["graphic"]["message_id"], q["id"])
        self.assertIn("simulated viewer", result["final_public"]["graphic"]["attribution"])

    def test_transcript_lookup_is_inconclusive_without_an_extra_response(self):
        socket = Socket(name="read_transcript", args={"query": "earlier result"})
        result = viewer.run_case("unused", "Candidate", viewer.CASES[3],
                                 connection=lambda *_a, **_k: socket)
        self.assertTrue(result["continuation_blocked"])
        self.assertEqual(result["review_status"], "inconclusive_follow_up_needed")
        self.assertEqual(result["actual_responses"], 1)
        self.assertEqual(sum(e["type"] == "response.create" for e in socket.sent), 1)

    def test_cli_runs_seven_frozen_cases_and_needs_semantic_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt, output = root / "prompt.md", root / "result.json"
            prompt.write_text("Viewer candidate")
            visited = []

            def fake_case(_key, text, case):
                self.assertEqual(text, "Viewer candidate")
                visited.append(case["id"])
                return {"actual_responses": 1, "review_status": "human_semantic_review_required",
                        "final_public": {"context": None, "graphic": None}}

            with (
                patch("sys.argv", ["viewer-ux-eval.py", "--key-file", "/unused-key",
                                   "--prompt", str(prompt), "--output", str(output)]),
                patch("sys.stdout", io.StringIO()),
                patch.object(viewer.presenter.server, "_read_key", return_value="unused"),
                patch.object(viewer, "run_case", side_effect=fake_case),
            ):
                viewer.main()
            result = json.loads(output.read_text())
            self.assertEqual(visited, [c["id"] for c in viewer.CASES])
            self.assertEqual(result["max_actual_responses"], 7)
            self.assertEqual(result["actual_responses"], 7)
            self.assertEqual(result["status"], "completed_pending_semantic_review")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

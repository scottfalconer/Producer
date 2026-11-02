"""Explicit bounded Realtime prompt comparison on authored text contexts.

This is not native-media perception or human presenter usability validation.
Each of six fixed contexts gets one response under each selected prompt
(12 maximum, or six with --only-candidate).
The production director protocol and local screen router are reused unchanged.
"""

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from unittest.mock import patch

from websockets.sync.client import connect

from action_trace import visible_receipt
from audience import Audience
from director_runtime import ShowDirector, note_brief
from on_air import OnAir
from realtime_director import MODEL, RealtimeDirector
import server


REFERENCE_MARKER = "\n\nPRIVATE SHOW REFERENCE DATA\n"
NOTES = """Applied AI Office Hours
Help people in any role try AI on one useful everyday task. Demonstrate turning
messy meeting notes into decisions and action items. Let useful questions develop.
After a detour, return to the unfinished result. Close with one practical takeaway.
No hard deadline. Private preparation only; do not copy these notes onto public cards.
"""

# Freeze before running either prompt. These are independently authored scenarios,
# not remembered recording excerpts or paraphrases presented as organic evidence.
CASES = [
    {
        "id": "opening", "cue": None,
        "context": "The selected show has just started. No programme speech has occurred yet. The presenter is ready to introduce this session from the supplied show notes.",
        "review": "A brief opening thought from the notes is useful; no multi-step coaching paragraph or invented completed work.",
    },
    {
        "id": "developing-thought", "cue": "Show the action items",
        "context": "Current programme speech: 'I have pasted the meeting notes. Let me explain what I asked the AI to do with them. First it separates decisions from open questions. I am still walking through the same example.' The presenter is speaking smoothly and the result has not been shown yet.",
        "review": "Retain the useful existing anchor verbatim, or stay quiet if no intervention helps. Avoid a fresh paraphrase or extra instructions during the developing explanation.",
    },
    {
        "id": "question", "cue": None,
        "question": {"id": "fictional-q1", "text": "Can I use this without writing code?", "author": "Maya · simulated viewer"},
        "context": "Current programme speech: 'Before I move on, let us take a useful question from chat.' The displayed audience question is current and has not been answered. There is room to take it now.",
        "review": "Make the actual question or its meaning directly usable without sending the presenter to find another screen. Avoid a generic instruction to take a question or a multi-part suggested answer.",
    },
    {
        "id": "return-anchor", "cue": None,
        "context": "Earlier in this selected show, the presenter pasted meeting notes and generated an action-item list but did not show the result. A useful audience detour followed. Current speech: 'That is a good way to think about the privacy side. Okay, back to the example—where had we got to?' The unfinished action is displaying the generated action-item list.",
        "review": "Offer the specific unfinished action as one concise retrieval anchor. Do not restart the entire demonstration or introduce another task.",
    },
    {
        "id": "deliberate-wait", "cue": "Show the action items",
        "context": "Current programme speech: 'The response is still running. Give it a moment; I am reading through it while it finishes.' There is a short pause while the presenter reads. No audience question or verified technical fault is present.",
        "review": "Respect the deliberate wait. Keeping a useful anchor or clearing unnecessary advice is acceptable. Do not infer distress, repeatedly nag, or fill the pause with a new assignment.",
    },
    {
        "id": "time-sensitive-correction", "cue": "Show the action items",
        "context": "Authored fixture ground truth: an owner-provided diagnostic confirms that the selected demo application window is not in the programme output; this is a confirmed check, not a camera inference. Current programme speech: 'As you can see in the application I am showing you now, these are the action items.' The actual programme output still shows the holding slide. The presenter can fix this by switching to the demo scene.",
        "review": "Prioritize a short, concrete correction such as switching to the demo scene, despite the ongoing sentence. Do not replace it with a long explanation or expose a private recovery note publicly.",
    },
]


class ResponseBudgetExceeded(RuntimeError):
    pass


class PromptSocket:
    """Replace only the editable prompt prefix; enforce one actual response."""

    def __init__(self, socket, prompt):
        self.socket, self.prompt = socket, prompt
        self.responses = 0

    def send(self, encoded):
        event = json.loads(encoded)
        if event.get("type") == "session.update":
            original = event["session"]["instructions"]
            if REFERENCE_MARKER not in original:
                raise ValueError("The production private-reference boundary changed.")
            event["session"]["instructions"] = self.prompt + REFERENCE_MARKER + original.split(REFERENCE_MARKER, 1)[1]
        if event.get("type") == "response.create":
            if self.responses >= 1:
                raise ResponseBudgetExceeded("One response per authored fixture; no follow-up inference.")
            self.responses += 1
        self.socket.send(json.dumps(event, ensure_ascii=False))

    def recv(self, timeout=None):
        return self.socket.recv(timeout=timeout)

    def close(self):
        self.socket.close()


class LocalCore:
    """No connection used to construct the real local router."""

    def __init__(self, *_args, **_kwargs):
        self.closed = False

    def snapshot(self):
        return {"closed": self.closed}

    def close(self):
        self.closed = True


class EmptyMaterials:
    catalog = []
    contact_sheet = None

    def get(self, _ident):
        return None


class FixtureTranscript:
    def __init__(self, text):
        self.text = text

    def read(self, *, before, **_kwargs):
        return {"before": before, "excerpts": [{"start": 0, "end": before, "text": self.text}], "gaps": []}


@contextmanager
def local_router(case):
    """Keep actual validation, question identity and public/private rendering data."""
    sid = "presenter-authored-fixture"
    audience = Audience()
    audience.owner, audience.connected = sid, True
    audience.channel, audience.status = "synthetic", "Authored audience fixture"
    question = case.get("question")
    if question:
        audience.questions.append({**question, "user_id": "fictional-maya", "received": time.time()})
    session = {
        "offset": 60, "created": time.monotonic(), "seen": time.monotonic(),
        "brief": note_brief(NOTES), "notes": NOTES, "materials": EmptyMaterials(),
        "memory": {}, "on_air": OnAir(True), "visual": None, "offline": True,
        "test_question": None, "transcript": FixtureTranscript(case["context"]),
    }
    with patch("server.audience", audience):
        router = ShowDirector(sid, session, key="unused-authored-router", lock=threading.RLock(),
                              active=lambda: True, public_state=lambda: server.public_state(sid, session),
                              audience=audience, allow_audience=True, director_class=LocalCore)
        session["director"] = router
        session["confidence"] = {"line": case["cue"], "kind": "editorial"} if case["cue"] else None
        if question:
            audience.pin_question(sid, question["id"])
            session["on_air"].publish({"kind": "question", "line": question["text"],
                                       "message_id": question["id"], "attribution": question["author"]})
        try:
            yield router
        finally:
            router.close()
            audience.stop()


def run_case(key, prompt, case, *, connection=connect, timeout=18):
    sockets, events = [], []
    def wrapped_connection(*args, **kwargs):
        result = PromptSocket(connection(*args, **kwargs), prompt)
        sockets.append(result)
        return result

    def record(kind, payload):
        # No ordinary model prose, private working notes, prompts or credentials.
        if kind == "function_call":
            events.append({"kind": kind, "name": payload.get("name"),
                           "arguments": payload.get("arguments"), "call_id": payload.get("call_id")})
        elif kind == "tool_receipt":
            events.append({"kind": kind, "name": payload.get("name"),
                           "receipt": visible_receipt(payload.get("receipt")), "call_id": payload.get("call_id")})

    core = None
    started = time.monotonic()
    with local_router(case) as router:
        initial = copy.deepcopy(router.session["confidence"])
        try:
            core = RealtimeDirector(
                key, brief={"notes": NOTES, "brief": router.session["brief"]},
                state=router.state, apply=router.apply, recall=router.recall,
                transcript=lambda *_args: None, offset=60, start_thread=False,
                connection=wrapped_connection, event_sink=record,
                consult=lambda _args, _before: {"queued": False, "reason": "This one-response authored fixture has no background editor."},
            )
            deadline = time.monotonic() + timeout
            while not core.ready:
                if time.monotonic() >= deadline:
                    raise TimeoutError("No Realtime configuration acknowledgement.")
                try:
                    acknowledgement = core.socket.recv(timeout=1)
                except TimeoutError:
                    continue
                core.accept(json.loads(acknowledgement))
            core._text({"authored_fixture": True,
                        "scope": "Text-only test context; not native audio, not a real audience or broadcast.",
                        "selected_show_context": case["context"]})
            core.consider("selected show context is available")
            completed = False
            continuation_blocked = False
            while not completed:
                if time.monotonic() >= deadline:
                    raise TimeoutError("The authored fixture response exceeded its bounded wait.")
                try:
                    event = json.loads(core.socket.recv(timeout=1))
                except TimeoutError:
                    continue
                try:
                    core.accept(event)
                except ResponseBudgetExceeded:
                    continuation_blocked = True
                if event.get("type") == "response.done":
                    completed = True
            final = router.state()
            line = (final.get("confidence") or {}).get("line", "")
            public = final.get("public")
            return {
                "case_id": case["id"], "authored_context": case["context"], "review_expectation": case["review"],
                "initial_confidence": initial, "final_confidence": final.get("confidence"),
                "final_public": public, "events": events,
                "actual_responses": sockets[0].responses, "completed": completed,
                "continuation_blocked": continuation_blocked,
                "review_status": "inconclusive_follow_up_needed" if continuation_blocked else "human_semantic_review_required",
                "metrics": {"displayed_words": len(re.findall(r"\S+", line)), "displayed_characters": len(line),
                            "cue_changed": (initial or {}).get("line", "") != line,
                            "cue_cleared": bool(initial and not line)},
                "seconds": round(time.monotonic() - started, 3),
            }
        except Exception as exc:
            # A failed response may already be billable. Preserve that attempt
            # count without exposing a provider error body or credential.
            exc.actual_responses = sockets[0].responses if sockets else 0
            raise
        finally:
            if core:
                core.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--baseline-prompt", type=Path)
    parser.add_argument("--candidate-prompt", type=Path, required=True)
    parser.add_argument("--only-candidate", action="store_true",
                        help="Run the six fixed cases once under the candidate; do not repeat baseline calls.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.only_candidate and args.baseline_prompt is None:
        parser.error("--baseline-prompt is required unless --only-candidate is selected.")
    if args.output.exists():
        parser.error("Choose a new output path; preserve the prior evaluation.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    prompts = {"candidate": args.candidate_prompt.read_text()}
    if not args.only_candidate:
        prompts = {"baseline": args.baseline_prompt.read_text(), **prompts}
    key = server._read_key(args.key_file)
    result = {"schema": "producer.presenter-prompt-eval.v1", "model": MODEL,
              "scope": "Six frozen independently authored text contexts, one response per prompt/context. Prompt/tool behavior only, not native perception or measured presenter cognitive load.",
              "prompt_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in prompts.items()},
              "max_actual_responses": len(CASES) * len(prompts),
              "actual_responses": 0, "status": "running", "results": []}
    try:
        for case in CASES:
            for name, prompt in prompts.items():
                row = run_case(key, prompt, case)
                result["results"].append({"prompt": name, **row})
                result["actual_responses"] += row["actual_responses"]
                print(json.dumps({"prompt": name, "case": case["id"], "words": row["metrics"]["displayed_words"],
                                  "review_status": row["review_status"]}), flush=True)
        result["status"] = "completed_pending_semantic_review"
    except Exception as exc:
        result["actual_responses"] += getattr(exc, "actual_responses", 0)
        result["status"], result["error"] = "failed", type(exc).__name__
        raise
    finally:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        args.output.chmod(0o600)


if __name__ == "__main__":
    main()

"""Inspect actual Producer API payload assembly without keys, media or network.

The session instructions use the selected notes. Changing-state values and the
optional consultation question are explicitly illustrative empty fixtures.
"""

import argparse
from functools import partial
import hashlib
import inspect
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

from continuity import structured
from director_runtime import ShowDirector, note_brief
from editor_consult import PROMPT as EDITOR_PROMPT, SCHEMA as EDITOR_SCHEMA
from on_air import OnAir
from realtime_director import (
    COMMIT_SECONDS, CONSIDER_SECONDS, ENDPOINT, PROMPT, RealtimeDirector,
)
from recorded_inputs import select_research_packet


class CaptureSocket:
    def __init__(self):
        self.sent = []

    def send(self, encoded):
        self.sent.append(json.loads(encoded))

    def close(self):
        pass


class CaptureResponses:
    def post(self, route, **kwargs):
        self.route, self.body = route, kwargs["json"]
        return {"output": [{"content": [{
            "type": "output_text", "text": '{"advice":"Inspection only; no model called."}',
        }]}]}


class EmptyTranscript:
    def read(self, *, before, **kwargs):
        return {"before": before, "excerpts": [], "gaps": []}


def assemble(notes, research_packet=""):
    """Use production constructors, state generation and request methods."""
    socket = CaptureSocket()
    session = {
        "offset": 0, "notes": notes, "brief": note_brief(notes), "memory": {},
        "research_packet": research_packet,
        "materials": SimpleNamespace(catalog=[], contact_sheet=None),
        "on_air": OnAir(True, context_persistent=True), "offline": True,
        "transcript": EmptyTranscript(),
    }
    # ShowDirector builds the exact brief envelope and optional tool list.
    # The injected connection never creates a socket or reads a credential.
    director = ShowDirector(
        "inspection-only", session, key="unused-inspection-placeholder",
        lock=threading.RLock(), active=lambda: True, audience=None,
        public_state=lambda: {**session["on_air"].snapshot(), "visual": None},
        director_class=partial(
            RealtimeDirector, connection=lambda *a, **kw: socket,
            start_thread=False, clock=lambda: 0.0,
        ),
    )
    try:
        director.core.accept({"type": "session.updated", "session": {
            "output_modalities": ["text"],
            "audio": {"input": {"turn_detection": None}},
        }})
        if not director.core.consider("show started"):
            raise RuntimeError("The inspection fixture did not assemble a consideration.")
        responses = CaptureResponses()
        structured(
            responses, EDITOR_PROMPT,
            {"question": "ILLUSTRATIVE QUESTION: Is there a useful unfinished thread?",
             "source_before": 0, "show": director.review_context(0)},
            EDITOR_SCHEMA, "producer_editor_consult",
        )
        session_update, consideration, response_create = socket.sent
        context = json.loads(consideration["item"]["content"][0]["text"])
        return {
            "session-update.json": session_update,
            "consideration.example.json": consideration,
            "production-context.example.json": context,
            "response-create.json": response_create,
            "optional-editor.example.json": {"route": responses.route, "body": responses.body},
        }
    finally:
        director.close()


def export(notes_file, output, research_packet_file=None, show_folder=None):
    notes_file = Path(notes_file).resolve()
    notes = notes_file.read_text(encoding="utf-8")
    research_packet, research_source = select_research_packet(
        research_packet_file=research_packet_file, show_folder=show_folder,
    )
    payloads = assemble(notes, research_packet)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    instructions = payloads["session-update.json"]["session"]["instructions"]
    (output / "full-session-instructions.txt").write_text(instructions, encoding="utf-8")
    (output / "base-prompt.md").write_text(PROMPT, encoding="utf-8")
    (output / "show-notes.md").write_text(notes, encoding="utf-8")
    (output / "research-packet.md").write_text(research_packet, encoding="utf-8")
    protocol = (RealtimeDirector.append, RealtimeDirector._commit_locked,
                RealtimeDirector.image, RealtimeDirector._image, RealtimeDirector.consider)
    (output / "protocol-source.py.txt").write_text("\n\n".join(inspect.getsource(f) for f in protocol))
    manifest = {
        "schema": "producer.api-prompt-inspection.v1",
        "network_requests": 0, "model_calls": 0, "credential_files_read": 0,
        "source_notes": str(notes_file), "endpoint": ENDPOINT,
        "source_research_packet": research_source,
        "research_packet_characters": len(research_packet),
        "research_packet_sha256": hashlib.sha256(research_packet.encode("utf-8")).hexdigest(),
        "base_prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "full_instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
        "notes_sha256": hashlib.sha256(notes.encode()).hexdigest(),
        "audio_commit_target_seconds": COMMIT_SECONDS,
        "consideration_interval_seconds": CONSIDER_SECONDS,
        "production_state_keys": list(payloads["production-context.example.json"]["production_state"]),
        "scope": "Current code assembled locally with selected notes; not an archived live request. Changing state is an empty illustrative fixture, with no actual private model notes.",
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir())},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "README.md").write_text(f"""# Exact prompt and API assembly

This is a local inspection of the current production code, with the notes from
`{notes_file}`, with a pinned research packet when one was supplied. It is not a
captured live request or a new model test.
No API request, credential read, capture or broadcast occurred.

- `full-session-instructions.txt`: complete session instructions, including the
  private show reference and original notes appended by the real constructor.
- `research-packet.md`: the complete selected packet text, separate from show notes.
- `base-prompt.md`: editable production prompt before the show reference.
- `session-update.json`: exact assembled Realtime configuration and tool schemas.
- `consideration.example.json`: actual recurring wire-event assembly with an
  explicitly illustrative empty starting state. Its decoded content is in
  `production-context.example.json`; its instruction is the production text.
- `response-create.json`: the actual subsequent response request.
- `optional-editor.example.json`: exact optional Responses request assembly with
  an illustrative question and empty transcript. This is available only if the
  director requests consultation; it is not another judge for each screen action.
- `protocol-source.py.txt`: current audio, image and consideration methods,
  included to make the transport details inspectable without saving any media.

Audio reaches the session through `input_audio_buffer.append` as base64 PCM;
its format and transcription model are in `session-update.json`. The current
audio-commit target is {COMMIT_SECONDS:g} seconds. Images arrive as separate
conversation items with a source-time label and JPEG data URI. Those image/audio
bytes are not included here. Consideration is offered about every
{CONSIDER_SECONDS:g} seconds when the session is ready and not busy; state events
can also prompt it. This is not a schedule for screen changes. Tool receipts and
past-only transcript excerpts arrive later as function outputs.

The example state has no viewer messages, images, actual working notes, editor
results or existing graphics. A real started show supplies those permitted values
from its current state. The compatible brief's flexible `minutes: 30` is an
internal placeholder, not an owner-supplied deadline or an instruction to wrap.

All prompts and schemas here are generated through production code. The optional
Responses model is the resolved local `PRODUCER_MODEL` configuration at export;
the main Realtime model is recorded in `session-update.json`.
""", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes-file", type=Path, required=True)
    parser.add_argument("--research-packet", type=Path, help="Optional UTF-8 Markdown/text research packet.")
    parser.add_argument("--show-folder", type=Path, help="Optional folder containing research-packet.md or research-packet.txt.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory; existing exports are preserved.")
    os.umask(0o077)
    try:
        result = export(args.notes_file, args.output, args.research_packet, args.show_folder)
    except (ValueError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"output": str(args.output.resolve()),
                      "base_prompt_sha256": result["base_prompt_sha256"],
                      "model_calls": 0}, indent=2))


if __name__ == "__main__":
    main()

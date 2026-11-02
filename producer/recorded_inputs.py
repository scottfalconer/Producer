"""Bounded, explicitly selected local inputs for isolated recording rehearsals."""

import copy
import json
import math
import os
from pathlib import Path
import re
import stat

from show_materials import MAX_NOTES, MAX_RESEARCH_PACKET, ShowMaterials, validate_research_packet


EVENT_FIELDS = {
    "channel.chat.message": {"author", "user_id", "text"},
    "channel.chat.message_delete": {"message_id"},
    "channel.chat.clear_user_messages": {"user_id"},
    "channel.chat.clear": set(),
}


def _text(value, maximum):
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum and "\x00" not in value


def validate_events(data):
    """Accept only bounded fictional chat/moderation events, never live actions."""
    if not isinstance(data, dict) or data.get("synthetic") is not True:
        raise ValueError("Recording events must be explicitly synthetic.")
    if not _text(data.get("id"), 100) or not _text(data.get("source"), 2000):
        raise ValueError("Recording events need an ID and fictional source description.")
    duration = data.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or not 0 < duration <= 600):
        raise ValueError("Recording event duration must be within 600 seconds.")
    events = data.get("events")
    if not isinstance(events, list) or len(events) > 200:
        raise ValueError("Recording events must be a list of at most 200 items.")
    seen, last = set(), 0
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in EVENT_FIELDS:
            raise ValueError("Unsupported recording event type.")
        required = {"at", "type", "id"} | EVENT_FIELDS[event["type"]]
        if set(event) != required:
            raise ValueError("Recording event fields do not match the event type.")
        at = event["at"]
        if (isinstance(at, bool) or not isinstance(at, (int, float))
                or not math.isfinite(at) or not last <= at <= duration):
            raise ValueError("Recording events must have ordered in-range timestamps.")
        if not _text(event["id"], 100) or event["id"] in seen:
            raise ValueError("Recording event IDs must be nonempty and unique.")
        for name in EVENT_FIELDS[event["type"]]:
            if not _text(event[name], 1000 if name == "text" else 160):
                raise ValueError("Recording event text exceeds its bound.")
        # Incoming fixtures cannot impersonate or shadow confirmed outgoing posts.
        if any(event.get(field, "").casefold().startswith("producer-")
               for field in ("id", "message_id")) or event.get("user_id", "").casefold() == "synthetic-producer":
            raise ValueError("Producer delivery identities are reserved for actual production actions.")
        if event["type"] == "channel.chat.message":
            if "simulated" not in event["author"].casefold():
                raise ValueError("Fictional authors must be labeled simulated.")
            author = re.split(r"[·(]", event["author"], maxsplit=1)[0].strip().casefold()
            if author == "producer" or event["text"].lstrip().casefold().startswith("(from producer)"):
                raise ValueError("Producer comments must come from actual production actions, not a chat fixture.")
        seen.add(event["id"])
        last = at
    return copy.deepcopy(data)


def _read(path, maximum):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Selected recording input is absent or exceeds its file bound.")
    content = path.read_bytes()
    if len(content) > maximum:
        raise ValueError("Selected recording input exceeds its file bound.")
    return content.decode("utf-8")


def select_inputs(*, notes_file=None, events_file=None, show_folder=None,
                  default_notes, default_events, default_show_folder):
    """Load before media preparation or API startup; never perform network I/O."""
    notes = _read(notes_file, MAX_NOTES * 4) if notes_file else default_notes
    if not _text(notes, MAX_NOTES):
        raise ValueError("Recording notes must contain at most 20,000 characters.")
    try:
        events = json.loads(_read(events_file, 256000)) if events_file else default_events
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Selected recording events must be UTF-8 JSON.") from exc
    events = validate_events(events)
    folder = Path(show_folder if show_folder is not None else default_show_folder).resolve()
    if not folder.is_dir():
        raise ValueError("Selected show folder must exist and be a directory.")
    return notes, events, folder


def select_research_packet(*, research_packet_file=None, show_folder=None):
    """Read one complete owner-selected packet before media or inference starts.

    An explicit file overrides the reserved file in the show folder. The text
    and its descriptive source path are returned separately; callers pin the
    text rather than rereading a mutable file later in the run.
    """
    if research_packet_file is None:
        if show_folder is None:
            return "", None
        materials = ShowMaterials(show_folder)
        name = materials.research_packet_name
        source = str(Path(show_folder).resolve() / name) if name else None
        return materials.research_packet, source
    path = Path(research_packet_file).expanduser()
    if path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("Choose a Markdown or text research packet (.md or .txt).")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_RESEARCH_PACKET * 4:
            raise ValueError("Research packet must be a regular UTF-8 file up to 128 KB.")
        content = source.read(MAX_RESEARCH_PACKET * 4 + 1)
    if len(content) > MAX_RESEARCH_PACKET * 4:
        raise ValueError("Research packet must be a regular UTF-8 file up to 128 KB.")
    try:
        packet = content.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("Research packet must be UTF-8 Markdown or text.") from exc
    return validate_research_packet(packet), str(path.resolve())

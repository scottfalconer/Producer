"""Pin fictional pixel-input chat to one show, independently of model context."""

import json
from pathlib import Path
import re

from recorded_inputs import validate_events
from simulated_chat import generate_chat


def show_chat(directory, show_id, *, notes="", duration=600, override=None):
    """A new show gets new chat; resuming the same show reads its exact timeline."""
    if not isinstance(show_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,60}", show_id):
        raise ValueError("Invalid simulated-chat show identity.")
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        raise ValueError("Simulated chat needs a local directory.")
    path = folder / (show_id + ".json")
    if path.is_symlink():
        raise ValueError("Simulated chat cannot be a link.")
    if path.exists():
        if path.stat().st_size > 256000:
            raise ValueError("Saved simulated chat exceeds its bound.")
        return validate_events(json.loads(path.read_text(encoding="utf-8")))
    # This picks authored demo vocabulary, never which comments Producer features.
    theme = "halloween" if re.search(r"\b(halloween|pumpkin|skeleton|cauldron)\b", notes, re.I) else "office-hours"
    data = validate_events(override if override is not None else generate_chat(theme, duration=duration))
    with path.open("x", encoding="utf-8") as output:
        path.chmod(0o600)
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return data

"""Bounded, local credential loading without the historical Moments runtime."""

from pathlib import Path
import re


KEY_PATTERN = r"sk-[A-Za-z0-9_-]+"
MAX_KEY_FILE = 8192


def read_openai_key(path):
    """Read a raw key or a single OPENAI_API_KEY assignment; never echo input.

    This deliberately does not source a shell file or evaluate interpolation.
    The caller selects the file; no global credentials or fallback are searched.
    """
    try:
        with Path(path).open("rb") as source:
            encoded = source.read(MAX_KEY_FILE + 1)
        if len(encoded) > MAX_KEY_FILE:
            raise ValueError()
        raw = encoded.decode("utf-8").strip()
        if re.fullmatch(KEY_PATTERN, raw):
            return raw
        lines = [line.strip() for line in raw.splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        if len(lines) == 1:
            match = re.fullmatch(
                r"OPENAI_API_KEY\s*=\s*(?:'(" + KEY_PATTERN
                + r")'|\"(" + KEY_PATTERN + r")\"|(" + KEY_PATTERN + r"))", lines[0]
            )
            if match:
                return next(value for value in match.groups() if value is not None)
    except (OSError, TypeError, ValueError):
        pass
    raise ValueError("OpenAI key file is unavailable or invalid.") from None

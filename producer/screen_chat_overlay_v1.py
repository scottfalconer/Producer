"""Deterministic pixels for an explicitly selected fictional chat timeline.

The timeline is render input only. Producer receives the resulting image, not
these source messages or a structured fixture inbox.
"""

import argparse
import copy
import hashlib
import io
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH, HEIGHT = 1280, 720
MAX_AGE = 90.0
MAX_MESSAGES = 5
MAX_JPEG_BYTES = 350000


def _clock(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 86400:
        raise ValueError("A finite simulated-chat source time is required.")
    return float(value)


def snapshot_events(events):
    rows = events.get("events") if isinstance(events, dict) else events
    if not isinstance(rows, list) or len(rows) > 1000 or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Choose a bounded simulated-chat timeline.")
    result = copy.deepcopy(rows)
    for row in result:
        _clock(row.get("at"))
    return result


def _visible(events, source_time):
    """Renderer-only state; never passed as a model inbox or tool result."""
    now = _clock(source_time)
    visible = {}
    for row in sorted(events, key=lambda item: item["at"]):
        if row["at"] > now:
            break
        kind = row.get("type")
        if kind == "channel.chat.message":
            if all(isinstance(row.get(key), str) for key in ("id", "author", "text")):
                visible[row["id"]] = row
        elif kind == "channel.chat.message_delete":
            visible.pop(row.get("message_id"), None)
        elif kind == "channel.chat.clear_user_messages":
            target = row.get("user_id", row.get("target_user_id"))
            visible = {key: item for key, item in visible.items() if item.get("user_id") != target}
        elif kind == "channel.chat.clear":
            visible.clear()
    return [row for row in visible.values() if now - row["at"] <= MAX_AGE][-MAX_MESSAGES:]


def _font(size):
    # Pillow's bundled scalable font avoids a network/font installation or
    # machine-specific OS font choice. Pillow is version-pinned by the project.
    return ImageFont.load_default(size=size)


def _wrap(draw, text, font, width):
    """Wrap original text at character boundaries without deleting characters."""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > width:
                boundary = current.rfind(" ")
                if boundary > 0:
                    lines.append(current[:boundary + 1])
                    current = current[boundary + 1:] + character
                else:
                    lines.append(current)
                    current = character
            else:
                current = candidate
        lines.append(current)
    return lines


def render_overlay(events, source_time, width=WIDTH, height=HEIGHT):
    """Return a transparent RGBA chat overlay at the given source cutoff."""
    if not isinstance(width, int) or not isinstance(height, int) or not 160 <= width <= 4096 or not 90 <= height <= 2304:
        raise ValueError("Choose bounded simulated-chat image dimensions.")
    rows = snapshot_events(events)
    messages = _visible(rows, source_time)
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    panel_w, panel_h, margin = 352, 460, 20
    left, top = WIDTH - panel_w - margin, HEIGHT - panel_h - margin
    body_font, author_font, label_font = _font(21), _font(15), _font(17)
    padding, usable = 16, panel_w - 32
    body_step, author_step = 27, 20
    entries = []
    for row in reversed(messages):
        author_lines = _wrap(draw, row["author"], author_font, usable)
        body_lines = _wrap(draw, row["text"], body_font, usable)
        entry_h = len(author_lines) * author_step + len(body_lines) * body_step + 18
        if sum(entry[3] for entry in entries) + entry_h <= panel_h - 60:
            entries.append((row, author_lines, body_lines, entry_h))
    entries.reverse()
    draw.rounded_rectangle((left, top, left + panel_w, top + panel_h), radius=12,
                           fill=(12, 17, 24, 237), outline=(111, 137, 151, 160), width=1)
    draw.text((left + padding, top + 15), "SIMULATED CHAT", font=label_font, fill=(166, 231, 213, 255))
    draw.line((left + padding, top + 43, left + panel_w - padding, top + 43), fill=(71, 86, 98, 200))
    y = top + 57
    palette = ((136, 211, 238, 255), (213, 177, 242, 255), (239, 206, 135, 255), (147, 220, 179, 255))
    for row, author_lines, body_lines, entry_h in entries:
        color = palette[hashlib.sha256(row["author"].encode()).digest()[0] % len(palette)]
        for line in author_lines:
            draw.text((left + padding, y), line, font=author_font, fill=color)
            y += author_step
        for line in body_lines:
            draw.text((left + padding, y), line, font=body_font, fill=(239, 244, 248, 255))
            y += body_step
        y += 18
    if (width, height) != (WIDTH, HEIGHT):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    return image


def overlay_png(events, source_time, width=WIDTH, height=HEIGHT):
    output = io.BytesIO()
    render_overlay(events, source_time, width, height).save(output, "PNG")
    return output.getvalue()


def composite(jpeg, events, source_time):
    """Composite the same screen pixels onto a source frame, bounded for Realtime."""
    with Image.open(io.BytesIO(jpeg)) as source:
        source.load()
        if source.width * source.height > 16000000:
            raise ValueError("Selected source frame exceeds its image bound.")
        picture = ImageOps.contain(source.convert("RGB"), (WIDTH, HEIGHT))
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), "black")
    canvas.paste(picture, ((WIDTH - picture.width) // 2, (HEIGHT - picture.height) // 2))
    canvas.alpha_composite(render_overlay(events, source_time))
    for quality in (85, 75, 65, 50, 35):
        output = io.BytesIO()
        canvas.convert("RGB").save(output, "JPEG", quality=quality, optimize=True)
        if len(output.getvalue()) <= MAX_JPEG_BYTES:
            return output.getvalue()
    raise ValueError("The selected source frame exceeds the Realtime image bound.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, required=True)
    args = parser.parse_args()
    duration = _clock(args.duration)
    content = args.events.read_bytes()
    if len(content) > 1000000:
        raise ValueError("Simulated-chat timeline exceeds its file bound.")
    rows = snapshot_events(json.loads(content))
    args.output.mkdir(parents=True, exist_ok=False)
    changes = {0.0}
    for row in rows:
        if row["at"] <= duration:
            changes.add(float(row["at"]))
            if row.get("type") == "channel.chat.message" and row["at"] + MAX_AGE < duration:
                changes.add(row["at"] + MAX_AGE + 0.000001)
    index = []
    for sequence, at in enumerate(sorted(changes)):
        name = f"{sequence:04}.png"
        (args.output / name).write_bytes(overlay_png(rows, at))
        index.append({"at": at, "file": name})
    (args.output / "index.json").write_text(json.dumps(index) + "\n")
    print(json.dumps({"frames": len(index), "duration": duration}))


if __name__ == "__main__":
    main()

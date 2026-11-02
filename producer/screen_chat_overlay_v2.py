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
from functools import lru_cache

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


STYLE = "compact-v2"
STYLES = ("panel-v1", STYLE)
FONTS = Path(__file__).with_name("assets") / "fonts"


@lru_cache(maxsize=16)
def _font(size, bold=False, emoji=False):
    font = ImageFont.truetype(str(FONTS / ("NotoEmoji.ttf" if emoji else "NotoSans.ttf")), size)
    if bold and not emoji:
        axes = font.get_variation_axes()
        font.set_variation_by_axes([600 if axis["name"] == b"Weight" else axis["default"] for axis in axes])
    return font


def _runs(text):
    """Keep common emoji sequences together in the bundled monochrome font."""
    runs = []
    for char in text:
        code = ord(char)
        emoji = 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or code in (0xFE0F, 0x200D)
        if runs and runs[-1][0] == emoji:
            runs[-1] = (emoji, runs[-1][1] + char)
        else:
            runs.append((emoji, char))
    return runs


def _width(draw, text, size, bold=False):
    return sum(draw.textlength(part, font=_font(size, bold, emoji)) for emoji, part in _runs(text))


def _text(draw, xy, text, size, fill, bold=False):
    x, y = xy
    for emoji, part in _runs(text):
        font = _font(size, bold, emoji)
        draw.text((x, y), part, font=font, fill=fill, anchor="lt")
        x += draw.textlength(part, font=font)


def _wrap(draw, text, size, width, bold=False):
    """Wrap original text without deleting characters or shortening a quote."""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            if current and _width(draw, current + character, size, bold) > width:
                boundary = current.rfind(" ")
                if boundary > 0:
                    lines.append(current[:boundary + 1])
                    current = current[boundary + 1:] + character
                else:
                    lines.append(current)
                    current = character
            else:
                current += character
        lines.append(current)
    return lines


def render_overlay(events, source_time, width=WIDTH, height=HEIGHT, *, style=STYLE):
    """Transparent, bottom-aligned chat cards; old runs retain their painter."""
    if style not in STYLES:
        raise ValueError("Unknown simulated-chat style.")
    if style == "panel-v1":
        from screen_chat_overlay_v1 import render_overlay as original
        return original(events, source_time, width, height)
    if not isinstance(width, int) or not isinstance(height, int) or not 160 <= width <= 4096 or not 90 <= height <= 2304:
        raise ValueError("Choose bounded simulated-chat image dimensions.")
    messages = _visible(snapshot_events(events), source_time)
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    card_w, max_height, margin, gap = 384, 420, 20, 8
    left = WIDTH - margin - card_w
    padding, body_size, body_step = 14, 21, 28
    entries, used = [], 0
    for row in reversed(messages):
        # This is display formatting of an authored demo name, before inference.
        # The single permanent simulation label replaces repeated fixture suffixes.
        author = row["author"].removesuffix(" · simulated viewer")
        author_lines = _wrap(draw, author, 15, card_w - padding * 2 - 26, True)
        body_lines = _wrap(draw, row["text"], body_size, card_w - padding * 2)
        entry_h = 10 + len(author_lines) * 21 + 7 + len(body_lines) * body_step + 10
        if used + entry_h + (gap if entries else 0) <= max_height:
            entries.append((row, author_lines, body_lines, entry_h))
            used += entry_h + (gap if len(entries) > 1 else 0)
    entries.reverse()
    if entries:
        label_y = HEIGHT - margin - used - 26
        draw.rounded_rectangle((left, label_y - 4, left + 133, label_y + 16), radius=4, fill=(13, 16, 24, 220))
        _text(draw, (left + 8, label_y), "SIMULATED CHAT", 12, (203, 211, 222, 255), True)
    y = HEIGHT - margin - used
    palette = ((119, 214, 245, 255), (207, 164, 255, 255), (249, 204, 123, 255), (121, 226, 180, 255))
    for row, author_lines, body_lines, entry_h in entries:
        color = palette[hashlib.sha256(row["author"].encode()).digest()[0] % len(palette)]
        draw.rounded_rectangle((left, y, left + card_w, y + entry_h), radius=10,
                               fill=(16, 18, 27, 224), outline=(211, 220, 235, 28), width=1)
        draw.rounded_rectangle((left, y + 10, left + 3, y + entry_h - 10), radius=1, fill=color)
        # An original chat-bubble mark; no remote avatars or service connection.
        bx, by = left + padding, y + 10
        draw.rounded_rectangle((bx, by, bx + 17, by + 15), radius=3, fill=(139, 92, 246, 255))
        draw.polygon([(bx + 4, by + 14), (bx + 4, by + 19), (bx + 9, by + 14)], fill=(139, 92, 246, 255))
        draw.line((bx + 5, by + 5, bx + 12, by + 5), fill="white", width=1)
        draw.line((bx + 5, by + 9, bx + 10, by + 9), fill="white", width=1)
        ty = y + 10
        for item in author_lines:
            _text(draw, (left + padding + 26, ty), item, 15, color, True)
            ty += 21
        ty += 7
        for item in body_lines:
            _text(draw, (left + padding, ty), item, body_size, (244, 246, 250, 255))
            ty += body_step
        y += entry_h + gap
    if (width, height) != (WIDTH, HEIGHT):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    return image


def overlay_png(events, source_time, width=WIDTH, height=HEIGHT, *, style=STYLE):
    output = io.BytesIO()
    render_overlay(events, source_time, width, height, style=style).save(output, "PNG")
    return output.getvalue()


def composite(jpeg, events, source_time, *, style=STYLE):
    """Composite the same screen pixels onto a source frame, bounded for Realtime."""
    with Image.open(io.BytesIO(jpeg)) as source:
        source.load()
        if source.width * source.height > 16000000:
            raise ValueError("Selected source frame exceeds its image bound.")
        picture = ImageOps.contain(source.convert("RGB"), (WIDTH, HEIGHT))
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), "black")
    canvas.paste(picture, ((WIDTH - picture.width) // 2, (HEIGHT - picture.height) // 2))
    canvas.alpha_composite(render_overlay(events, source_time, style=style))
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
    parser.add_argument("--style", choices=STYLES, default=STYLE)
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
        (args.output / name).write_bytes(overlay_png(rows, at, style=args.style))
        index.append({"at": at, "file": name})
    (args.output / "index.json").write_text(json.dumps(index) + "\n")
    print(json.dumps({"frames": len(index), "duration": duration}))


if __name__ == "__main__":
    main()

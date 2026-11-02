"""Pinned local recording and fictional EventSub data; no Twitch transport exists here."""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from engine import PRIVATE, bounded_audio, extract
from screen_chat_overlay import STYLE, STYLES, composite, snapshot_events, with_producer_posts

FIXTURES = Path(__file__).with_name("fixtures")
CLIP = PRIVATE / "rehearsal/office-hours.mp4"
DATA = json.loads((FIXTURES / "office-hours-events.json").read_text())


def config():
    return {
        "id": DATA["id"],
        "duration": DATA["duration"],
        "ready": CLIP.exists(),
        "notes": (FIXTURES / "office-hours-notes.md").read_text(),
        "media": "/rehearsal-media",
        "output": "/rehearsal",
        "synthetic": True,
    }


class MockTwitch:
    platform = "fixture"
    chat_vote = None
    chat_ready = lambda self: True

    def __init__(self, clock=None, channel_id="synthetic-office-hours"):
        self.clock = clock or time.time
        self.channel_id = channel_id
        self.receipts = []
        self.polls = {}

    def sender_status(self):
        return {
            "connected": True, "simulated": True,
            "user_id": "synthetic-producer", "login": "producer", "display_name": "Producer",
            "detail": "SIMULATED Producer identity; no Twitch account was checked.",
        }

    def capabilities(self):
        return {
            "platform": "fixture", "simulated": True,
            "channel_id": self.channel_id, "channel": "simulated",
            "online": False, "shared_chat": False,
            "capabilities": {k: True for k in ("chat", "reference", "marker", "title", "category", "poll")},
            "chat_sender": self.sender_status(),
            "chat_read": True, "native_polls": True, "poll_eligible": True,
            "active_poll": any(self.clock() < poll["ends_at"] for poll in self.polls.values()),
            "checked": self.clock(),
            "marker_note": "SIMULATED capabilities only; no Twitch account was checked.",
        }

    def check_plans(self, plans):
        return self.capabilities(), []

    def execute(self, plan):
        is_vote = plan["kind"] == "poll" and plan.get("poll_mode") == "chat"
        is_chat = plan["kind"] in ("chat", "reference") or is_vote
        message = plan.get("text", "")
        if is_vote:
            from production import vote_message

            message = vote_message(plan)
            if not self.chat_vote or self.chat_vote.active or not self.chat_ready():
                return {"state": "needs_attention", "simulated": True,
                        "detail": "SIMULATED chat vote unavailable or another vote is active."}
        if is_chat and (not isinstance(message, str) or not message.strip() or len(message) > 500):
            return {"state": "needs_attention", "simulated": True,
                    "detail": "SIMULATED chat text must contain 1–500 characters; nothing sent."}
        if plan["kind"] == "poll" and not is_vote and self.capabilities()["active_poll"]:
            return {"state": "needs_attention", "simulated": True,
                    "detail": "SIMULATED native poll already active; nothing created."}
        row = {
            "state": "done",
            "detail": "SIMULATED Twitch action; nothing sent to Twitch.",
            "remote_id": "mock-" + plan["id"],
            "simulated": True, "channel_id": self.channel_id,
        }
        if is_chat:
            row.update(sender_id="synthetic-producer", sender_login="producer",
                       display_name="Producer", message=message)
        if is_vote:
            self.chat_vote.start(plan, now=self.clock())
            row.pop("remote_id", None)
        elif plan["kind"] == "poll":
            self.polls[row["remote_id"]] = {
                "ends_at": self.clock() + plan["duration"], "choices": list(plan["choices"]),
                "counts": [max(0, 3 - index) for index in range(len(plan["choices"]))],
            }
        self.receipts.append({"plan_id": plan["id"], "kind": plan["kind"], **row})
        return row

    def poll_result(self, poll_id):
        poll = self.polls.get(poll_id)
        if not poll or self.clock() < poll["ends_at"]:
            return None
        choices = [{"title": title, "votes": votes}
                   for title, votes in zip(poll["choices"], poll["counts"])]
        best = max(poll["counts"], default=0)
        winners = [item["title"] for item in choices if item["votes"] == best]
        return {
            "id": poll_id, "simulated": True, "status": "COMPLETED",
            "line": "No votes received" if not best else "Vote tied" if len(winners) != 1
            else "Audience chose: " + winners[0],
            "choices": choices, "counts": list(poll["counts"]),
            "detail": "SIMULATED result with authored counts; no Twitch votes were received.",
        }


class EventFixture:
    """Authored event clock, independent of the programme's microphone or media."""

    def __init__(self, inbox, sid, offset=0):
        self.inbox, self.sid = inbox, sid
        self.sent = {e["id"] for e in DATA["events"] if e["at"] < offset}

    def connect(self):
        self.inbox.stop()
        self.inbox.owner = self.sid
        self.inbox.channel = "synthetic-office-hours"
        self.inbox.connected = True
        self.inbox.status = "Simulated audience · no Twitch connection"

    def advance(self, elapsed):
        if self.inbox.owner != self.sid:
            return
        for e in DATA["events"]:
            if e["at"] > elapsed or e["id"] in self.sent:
                continue
            self.sent.add(e["id"])
            event = {"broadcaster_user_id": "synthetic-office-hours"}
            if e["type"] == "channel.chat.message":
                event.update(
                    message_id=e["id"],
                    chatter_user_id=e["user_id"],
                    chatter_user_name=e["author"],
                    message={"text": e["text"]},
                )
            elif e["type"] == "channel.chat.message_delete":
                event["message_id"] = e["message_id"]
            elif e["type"] == "channel.chat.clear_user_messages":
                event["target_user_id"] = e["user_id"]
            self.inbox.accept(
                {
                    "metadata": {
                        "message_id": "fixture-" + e["id"],
                        "message_timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    "payload": {
                        "subscription": {"type": e["type"], "version": "1"},
                        "event": event,
                    },
                }
            )

class Rehearsal(EventFixture):
    def __init__(self, inbox, sid, offset=0, *, chat_events=None, chat_style=STYLE):
        if not CLIP.exists():
            raise ValueError("Prepare the local Office Hours recording first.")
        super().__init__(inbox, sid, offset)
        if chat_style not in STYLES:
            raise ValueError("Unknown simulated-chat style.")
        self.chat_style = chat_style
        self.chat_events = snapshot_events(chat_events) if chat_events is not None else None
        self.producer_posts = lambda: []

    def connect(self):
        if self.chat_events is None:
            super().connect()

    def advance(self, elapsed):
        if self.chat_events is None:
            super().advance(elapsed)

    def cut(self, start, end):
        if not 0 <= start < end <= DATA["duration"] or end - start > 15.1:
            raise ValueError("Invalid rehearsal cut.")
        pcm = bounded_audio(CLIP, start, end, end)
        frame = extract(
            [
                "-ss",
                str(max(start, end - 0.05)),
                "-i",
                str(CLIP),
                "-t",
                "0.05",
                "-an",
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-2",
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "pipe:1",
            ]
        )
        return pcm, composite(frame, self.chat_events, end, style=self.chat_style) if self.chat_events is not None else frame

    @staticmethod
    def _available_interval(start, end, available_through, maximum):
        values = (start, end) if available_through is None else (start, end, available_through)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise ValueError("Invalid rehearsal source time.")
        if not 0 <= start < end <= DATA["duration"] or end - start > maximum:
            raise ValueError("Invalid streaming rehearsal interval.")
        if available_through is not None and end > available_through:
            raise ValueError("Rehearsal input is not yet available.")

    def audio24(self, start, end, *, available_through=None):
        """A bounded PCM packet from the pinned source, trimmed before resampling."""
        self._available_interval(start, end, available_through, 2)
        length = end - start
        expected_bytes = round(length * 24000) * 2
        pcm = extract(
            [
                "-ss", str(start), "-i", str(CLIP),
                "-af", f"atrim=end={length:.6f},aresample=24000",
                "-t", str(length), "-vn", "-ac", "1", "-ar", "24000",
                "-f", "s16le", "pipe:1",
            ]
        )[:expected_bytes]
        if len(pcm) != expected_bytes:
            raise ValueError("The selected rehearsal audio does not cover this interval.")
        return pcm

    def frame_at(self, end, *, available_through=None):
        """One JPEG sampled before the accepted source cutoff, never a future frame."""
        start = max(0, end - 0.05) if isinstance(end, (int, float)) else 0
        self._available_interval(start, end, available_through, 0.051)
        frame = extract(
            [
                "-ss", str(start), "-i", str(CLIP), "-t", str(end - start),
                "-an", "-frames:v", "1", "-vf", "scale=" + ("1280:-2" if self.chat_events is not None else "640:-2"), "-q:v", "6",
                "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
            ]
        )
        maximum = 2000000 if self.chat_events is not None else 350000
        if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9") or len(frame) > maximum:
            raise ValueError("The selected rehearsal frame is unavailable or too large.")
        if self.chat_events is not None:
            return composite(frame, with_producer_posts(self.chat_events, self.producer_posts()), end, style=self.chat_style)
        return frame

    def caption_audio(self, sequence, offset, elapsed):
        if type(sequence) is not int or sequence < 0:
            raise ValueError("Invalid caption sequence.")
        start, end = offset + sequence, offset + sequence + 1
        if end > DATA["duration"] or end > elapsed:
            raise ValueError("Caption audio is not yet available.")
        # The fixture is pinned; no supplied bytes or arbitrary file is used.
        return extract(
            [
                "-ss",
                str(start),
                "-i",
                str(CLIP),
                "-af",
                "atrim=end=1,aresample=24000",
                "-t",
                "1",
                "-vn",
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ]
        )

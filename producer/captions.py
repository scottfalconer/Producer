"""Selected-feed Realtime transcription. No editorial rewrite and no media storage."""

import base64
import json
import threading
import time
import array
from websockets.sync.client import connect

MODEL = "gpt-live-transcribe"
ENDPOINT = "wss://api.openai.com/v1/realtime?intent=transcription"


class CaptionSession:
    def __init__(self, key, connection=connect, on_text=None):
        self.on_text = on_text
        self.lock = threading.RLock()
        self.closed = False
        self.socket = connection(
            ENDPOINT,
            additional_headers={"Authorization": "Bearer " + key},
            open_timeout=10,
            close_timeout=1,
            proxy=None,
            max_size=512000,
        )
        self.sequence = 0
        self.last_audio = self.created = time.monotonic()
        self.audio_seconds = 0
        self.turn_seconds = 0
        self.retired = set()
        self.items = {}
        self.order = 0
        self.current = None
        self.status = "Connecting captions"
        self.socket.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "transcription": {
                                    "model": MODEL,
                                    "languages": ["en"],
                                    "delay": "low",
                                },
                                "turn_detection": None,
                            }
                        },
                    },
                }
            )
        )
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def register(self, ident, now):
        if not ident or ident in self.items or ident in self.retired:
            return
        self.order += 1
        self.items[ident] = {"order": self.order, "text": "", "at": now, "updated": now}
        while len(self.items) > 8:
            old = next(iter(self.items))
            self.retired.add(old)
            self.items.pop(old)
        if len(self.retired) > 2000:
            raise ValueError("Caption turn limit reached.")

    def append(self, sequence, encoded):
        raw = base64.b64decode(encoded, validate=True)
        with self.lock:
            if (
                self.closed
                or type(sequence) is not int
                or sequence != self.sequence
                or not 0 < len(raw) <= 96000
                or len(raw) % 2
            ):
                raise ValueError("Caption input is stopped or out of order.")
            seconds = len(raw) / 48000
            if self.audio_seconds + seconds > time.monotonic() - self.created + 3:
                raise ValueError("Caption input is ahead of the selected feed.")
            self.socket.send(
                json.dumps({"type": "input_audio_buffer.append", "audio": encoded})
            )
            self.audio_seconds += seconds
            self.turn_seconds += seconds
            samples = array.array("h", raw)
            quiet = sum(s * s for s in samples) / len(samples) < (32768 * 0.005) ** 2
            if self.turn_seconds >= 15 or (quiet and self.turn_seconds >= 2):
                self.socket.send(json.dumps({"type": "input_audio_buffer.commit"}))
                self.turn_seconds = 0
            self.sequence += 1
            self.last_audio = time.monotonic()

    def accept(self, event, now=None):
        now = time.monotonic() if now is None else now
        kind = event.get("type", "")
        notify = None
        with self.lock:
            if self.closed:
                return
            if kind == "session.updated":
                self.status = "Live captions connected"
            elif kind in ("input_audio_buffer.committed", "conversation.item.created"):
                ident = event.get("item_id") or event.get("item", {}).get("id")
                self.register(ident, now)
            elif kind in (
                "conversation.item.input_audio_transcription.delta",
                "conversation.item.input_audio_transcription.completed",
            ):
                ident = event.get("item_id")
                # Live transcription emits deltas before a turn is committed.
                if (
                    kind.endswith("delta")
                    and ident
                    and ident not in self.items
                    and ident not in self.retired
                ):
                    self.register(ident, now)
                item = self.items.get(ident)
                if not item or now - item["updated"] > 15:
                    return
                text = (
                    event.get("transcript")
                    if kind.endswith("completed")
                    else item["text"] + str(event.get("delta", ""))
                )
                if not isinstance(text, str):
                    return
                item["text"] = text[-1200:]
                item["updated"] = now
                if item["order"] != self.order:
                    return  # late completions cannot rewind captions
                self.current = {
                    "text": item["text"],
                    "partial": not kind.endswith("completed"),
                    "expires": now + 6,
                    "item_id": ident,
                }
                notify = item["text"]
                if kind.endswith("completed"):
                    self.retired.add(ident)
            elif kind in (
                "error",
                "conversation.item.input_audio_transcription.failed",
            ):
                raise ValueError("Live captions unavailable; no fallback.")
        if notify and getattr(self, "on_text", None):
            self.on_text(notify)

    def snapshot(self):
        with self.lock:
            if (
                self.closed
                or not self.current
                or self.current["expires"] <= time.monotonic()
            ):
                return None
            # Keep whole words in a fixed two-line area; no model summary of speech.
            words = self.current["text"].split()
            while words and len(" ".join(words)) > 170:
                words.pop(0)
            return {"text": " ".join(words), "partial": self.current["partial"]}

    def run(self):
        try:
            while not self.closed:
                if time.monotonic() - self.last_audio > 10:
                    raise ValueError("Caption feed disconnected.")
                if time.monotonic() - self.created > 55 * 60:
                    raise ValueError("Caption session reached its connection limit.")
                try:
                    event = json.loads(self.socket.recv(timeout=1))
                except TimeoutError:
                    continue
                self.accept(event)
        except Exception:
            self.status = "Captions stopped"
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.current = None
            self.items.clear()
        self.socket.close()

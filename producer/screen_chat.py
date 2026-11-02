"""Bounded model-read comments grounded in submitted selected-source frames.

This module knows frame receipts and the text the director reports reading. It
does not access a fixture, Twitch, OCR service or the contents of any image.
"""

import copy
import hashlib
import json
import math
import re
import threading


FRAME_AGE = 20.0
COMMENT_AGE = 120.0
MAX_FRAMES = 8
MAX_COMMENTS = 20


def _clock(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 86400:
        raise ValueError("Invalid screen source time.")
    return float(value)


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Use the complete readable screen comment, or leave it off.")
    try:
        value.encode("utf-8")
        units = len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise ValueError("Screen text must contain valid Unicode.") from None
    if units > maximum:
        raise ValueError("The complete screen comment does not fit; leave it off.")
    return value


class ScreenChat:
    """One session's frame evidence and model-observed quote candidates."""

    def __init__(self, owner):
        if not isinstance(owner, str) or not owner:
            raise ValueError("A selected screen needs a session identity.")
        self.owner = owner
        self.connected = True
        self.closed = False
        self.lock = threading.RLock()
        self.selection = None
        self.questions = []
        self._frames = []
        self._source_time = 0.0

    def _check(self, sid=None):
        if self.closed or not self.connected or (sid is not None and sid != self.owner):
            raise ValueError("The selected screen session has stopped or changed.")

    def frame_id(self, jpeg, source_time):
        self._check()
        source_time = _clock(source_time)
        digest = hashlib.sha256(jpeg).hexdigest()
        value = json.dumps([self.owner, source_time, digest], separators=(",", ":"))
        return "frame-" + hashlib.sha256(value.encode()).hexdigest()[:24]

    def register_frame(self, frame_id, source_time, sha256):
        source_time = _clock(source_time)
        if not isinstance(frame_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", frame_id):
            raise ValueError("Invalid selected-frame identity.")
        if not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("Invalid selected-frame digest.")
        row = {"frame_id": frame_id, "source_time": source_time, "sha256": sha256}
        with self.lock:
            self._check()
            old = next((f for f in self._frames if f["frame_id"] == frame_id), None)
            if old is not None:
                if old != row:
                    raise ValueError("A submitted frame identity cannot be replaced.")
                return copy.deepcopy(old)
            if self._frames and source_time < self._frames[-1]["source_time"]:
                raise ValueError("Selected frames must stay in source order.")
            self._frames = (self._frames + [row])[-MAX_FRAMES:]
            self._source_time = max(self._source_time, source_time)
            return copy.deepcopy(row)

    def frames(self, current_source_time):
        current_source_time = _clock(current_source_time)
        with self.lock:
            self._check()
            self._source_time = max(self._source_time, current_source_time)
            self._frames = [f for f in self._frames if 0 <= current_source_time - f["source_time"] <= FRAME_AGE]
            return copy.deepcopy(self._frames)

    def prepare_observation(self, comment, examined_frames, current_source_time):
        """Validate an observed quote without changing the eligible inbox."""
        current_source_time = _clock(current_source_time)
        if not isinstance(comment, dict) or set(comment) != {"frame_id", "author", "text"}:
            raise ValueError("A screen comment needs one frame, author and complete text.")
        author, text = _text(comment["author"], 100), _text(comment["text"], 180)
        if not isinstance(examined_frames, list):
            raise ValueError("The examined screen evidence is unavailable.")
        with self.lock:
            self._check()
            frame = next((f for f in self.frames(current_source_time) if f["frame_id"] == comment["frame_id"]), None)
            if frame is None or frame not in examined_frames:
                raise ValueError("That selected frame is unavailable, stale or was not examined.")
            prior = next((q for q in reversed(self.questions)
                          if q["author"] == author and q["text"] == text
                          and 0 <= current_source_time - q["received"] < COMMENT_AGE), None)
            # Frame IDs bind actual images to the original selected session.
            # Reusing that recorded receipt also makes local routing replay
            # deterministic without retaining the original API session token.
            identity = json.dumps([frame["frame_id"], author, text], ensure_ascii=False, separators=(",", ":"))
            ident = prior["id"] if prior else "screen-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            return {"id": ident,
                    "author": author, "text": text, "received": current_source_time,
                    "source": "screen", "frame_id": frame["frame_id"],
                    "frame_source_time": frame["source_time"]}

    def commit_observation(self, row):
        """Commit only an already validated candidate after routing validation."""
        with self.lock:
            self._check()
            self.questions = [q for q in self.questions if q["id"] != row["id"]]
            self.questions = (self.questions + [copy.deepcopy(row)])[-MAX_COMMENTS:]
            self._source_time = max(self._source_time, row["received"])
            return copy.deepcopy(row)

    def observe(self, frame_id, author, text, examined_frames, current_source_time):
        row = self.prepare_observation({"frame_id": frame_id, "author": author, "text": text},
                                       examined_frames, current_source_time)
        return self.commit_observation(row)

    def poll(self, sid, current_source_time=None):
        with self.lock:
            self._check(sid)
            if current_source_time is not None:
                self._source_time = max(self._source_time, _clock(current_source_time))
            self.questions = [q for q in self.questions if 0 <= self._source_time - q["received"] < COMMENT_AGE]
            return {"status": "Read from selected programme images; no Twitch chat API",
                    "questions": copy.deepcopy(self.questions), "selection": copy.deepcopy(self.selection)}

    def pin_question(self, sid, ident):
        with self.lock:
            self._check(sid)
            if ident is None:
                return self.unpin_question(sid)
            question = next((q for q in self.poll(sid)["questions"] if q["id"] == ident), None)
            if question is None:
                return False
            self.selection = {"message_id": ident, "valid": True,
                              "frame_id": question["frame_id"], "source": "screen"}
            return True

    def unpin_question(self, sid):
        with self.lock:
            self._check(sid)
            self.selection = None
            return True

    def close(self):
        with self.lock:
            self.closed, self.connected = True, False
            self.questions.clear()
            self._frames.clear()
            self.selection = None
            self.owner = None

    stop = close

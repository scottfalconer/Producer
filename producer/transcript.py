"""Local selected-show speech, with bounded past-only retrieval for private continuity."""

import hashlib
import json
import math
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "producer-show-transcript-v1"
PART_CHARS = 6000
MAX_CONTEXT_CHARS = 4800
MAX_CONTEXT_ROWS = 5
MAX_READ_CHARS = 12000
MAX_READ_ROWS = 12
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
STOPWORDS = set("a about all an and are as at be because been but by can did do for from had has have how i if in into is it its just like me more my no not of on or our so some than that the their them then there these they this to up us was we were what when which who why will with would you your".split())


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOPWORDS}


def _clock(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 86400:
        raise ValueError("Invalid transcript source time.")
    return round(value, 6)


class ShowTranscript:
    """One append-only file per show. No media, viewer feed or private prompt is stored."""

    def __init__(self, directory, show_id, source, brief_hash, offset=0):
        if not isinstance(show_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,60}", show_id):
            raise ValueError("Invalid transcript show identity.")
        self.directory = Path(directory)
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ValueError("The local show transcript folder could not be created.") from exc
        if self.directory.is_symlink():
            raise ValueError("Transcript folder must be a local directory, not a link.")
        self.path = self.directory / (show_id + ".jsonl")
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.RLock())
        self.show_id = show_id
        self.closed = False
        offset = _clock(offset)
        with self.lock:
            rows = self._rows()
            if rows:
                header = rows[0]
                if any(header.get(k) != v for k, v in {
                    "kind": "show", "schema": SCHEMA, "show_id": show_id,
                    "source": source, "brief_hash": brief_hash,
                }.items()):
                    raise ValueError("This show ID belongs to different notes or input. Start a new show.")
                stops = [r for r in rows if r.get("kind") == "stop"]
                if stops and stops[-1]["reason"] == "ended":
                    raise ValueError("That show has ended. Start a new show instead of reusing its transcript.")
                previous = self._through(rows)
                if offset < previous - 0.001:
                    raise ValueError("Resume cannot overlap the saved show transcript.")
                if offset > previous + 0.001:
                    self._append([{"kind": "gap", "start": previous, "end": offset, "status": "not_observed_before_resume"}])
            else:
                self._append([{
                    "kind": "show", "schema": SCHEMA, "show_id": show_id,
                    "source": source, "brief_hash": brief_hash, "source_clock": "selected show seconds; excludes paused time",
                    "speaker": "unknown", "timestamp_precision": "audio window, not word-level",
                }])
                if offset:
                    self._append([{"kind": "gap", "start": 0, "end": offset, "status": "not_observed_before_start"}])
            self._append([{"kind": "start", "at": offset, "resumed": bool(rows)}])

    def _fd(self):
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(self.path.name, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("Transcript must be one regular local file.")
        os.fchmod(fd, 0o600)
        return fd

    def _rows(self):
        try:
            with os.fdopen(self._fd(), "r", encoding="utf-8") as stream:
                rows = []
                for line in stream:
                    if len(line) > PART_CHARS * 7 + 2000:
                        raise ValueError("Saved transcript has an invalid record.")
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("Saved transcript has an invalid record.")
                    rows.append(row)
                chunks = {}
                for row in rows:
                    if row.get("kind") == "segment":
                        group = chunks.setdefault(row["id"], [])
                        group.append(row)
                for group in chunks.values():
                    parts = group[0]["parts"]
                    if len(group) != parts or {r["part"] for r in group} != set(range(1, parts + 1)):
                        raise ValueError("A saved transcript segment is incomplete; it cannot be treated as full evidence.")
                return rows
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("The local show transcript could not be read safely.") from exc

    def _append(self, rows):
        raw = "".join(json.dumps({**r, "saved_at": _stamp()}, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        try:
            fd = self._fd()
            try:
                view = memoryview(raw)
                while view:
                    count = os.write(fd, view)
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise ValueError("The local show transcript could not be saved. Monitoring must pause.") from exc

    @staticmethod
    def _through(rows):
        return max((r.get("end", r.get("at", 0)) for r in rows), default=0)

    def record(self, start, end, text="", status="transcribed"):
        start, end = _clock(start), _clock(end)
        if not start < end or end - start > 45.01 or not isinstance(text, str):
            raise ValueError("Invalid transcript window.")
        if status not in ("transcribed", "quiet_not_transcribed", "no_text", "signal_skipped", "failed"):
            raise ValueError("Invalid transcript status.")
        with self.lock:
            if self.closed:
                raise ValueError("The show transcript is stopped.")
            rows = self._rows()
            previous = self._through(rows)
            if start < previous - 0.001:
                raise ValueError("Transcript window overlaps previously accepted input.")
            output = []
            if start > previous + 0.001:
                output.append({"kind": "gap", "start": previous, "end": start, "status": "not_observed"})
            ident = "speech-" + hashlib.sha256(f"{self.show_id}:{start}:{end}".encode()).hexdigest()[:16]
            chunks = [text[i:i + PART_CHARS] for i in range(0, len(text), PART_CHARS)] or [""]
            output.extend({
                "kind": "segment", "id": ident, "start": start, "end": end,
                "status": status if text else ("no_text" if status == "transcribed" else status),
                "speaker": "unknown", "part": i + 1, "parts": len(chunks), "text": chunk,
            } for i, chunk in enumerate(chunks))
            self._append(output)
            return ident

    def processing_failed(self, start, end):
        with self.lock:
            if not self.closed:
                self._append([{"kind": "processing", "start": _clock(start), "end": _clock(end), "status": "continuity_failed_after_transcription"}])

    def stop(self, through, reason="ended"):
        if reason not in ("paused", "ended", "source_lost"):
            raise ValueError("Unknown transcript stop reason.")
        through = _clock(through)
        with self.lock:
            rows = self._rows()
            if self.closed:
                stops = [r for r in rows if r.get("kind") == "stop"]
                if reason != "ended" or not stops or stops[-1]["reason"] == "ended":
                    return
            previous = self._through(rows)
            through = max(through, previous)
            output = []
            if through > previous + 0.001:
                output.append({"kind": "gap", "start": previous, "end": through, "status": "not_transcribed_before_stop"})
            output.append({"kind": "stop", "at": through, "reason": reason})
            self._append(output)
            self.closed = True

    def summary(self):
        with self.lock:
            rows = self._rows()
        return {
            "show_id": self.show_id, "saved_locally": True,
            "segments": len({r["id"] for r in rows if r.get("kind") == "segment" and r.get("status") == "transcribed"}),
            "through": self._through(rows), "stopped": self.closed,
            "last_stop_reason": next((r["reason"] for r in reversed(rows) if r.get("kind") == "stop"), None),
            "gaps": sum(r.get("kind") == "gap" or r.get("status") == "failed" for r in rows),
        }

    def read(self, *, before, query="", start=None, end=None, limit=MAX_READ_ROWS):
        """Read this show's accepted past text on demand, without reading ahead.

        Times identify complete recognition windows, not words. An explicit
        range returns only windows wholly inside that range. A query ranks
        lexical matches; without a query/range, return the most recent windows.
        """
        before = _clock(before)
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("Keep transcript queries to 2000 characters.")
        if type(limit) is not int or limit < 1:
            raise ValueError("Choose a positive transcript result limit.")
        limit = min(limit, MAX_READ_ROWS)
        explicit_range = start is not None or end is not None
        lower = _clock(start) if start is not None else 0
        upper = min(_clock(end), before) if end is not None else before
        if lower > upper:
            raise ValueError("Transcript range begins after the available cutoff.")
        with self.lock:
            rows = self._rows()
        grouped = {}
        for row in rows:
            if row.get("kind") != "segment" or row.get("status") != "transcribed" or row["end"] > before:
                continue
            item = grouped.setdefault(row["id"], {k: row[k] for k in ("id", "start", "end", "speaker")})
            item["text"] = item.get("text", "") + row["text"]
        accepted = sorted(grouped.values(), key=lambda row: (row["end"], row["start"]))
        candidates = [row for row in accepted if row["start"] >= lower and row["end"] <= upper]
        query = query.strip()
        if query:
            words = {word for word in re.findall(r"\b[\w-]{2,}\b", query.casefold()) if word not in STOPWORDS}

            def score(row):
                tokens = set(re.findall(r"\b[\w-]{2,}\b", row["text"].casefold()))
                return len(tokens & words)

            candidates = [row for row in candidates if score(row)]
            candidates.sort(key=lambda row: (score(row), row["end"]), reverse=True)
        elif not explicit_range:
            candidates.reverse()
        result, remaining = [], MAX_READ_CHARS
        for row in candidates[:limit]:
            if not remaining:
                break
            text = row["text"][:remaining]
            result.append({**row, "text": text, "excerpted": len(text) < len(row["text"])})
            remaining -= len(text)
        gaps = [
            {key: row[key] for key in ("start", "end", "status")}
            for row in rows
            if row.get("kind") == "gap" and row["start"] >= lower and row["end"] <= upper
        ][-MAX_READ_ROWS:]
        return {
            "show_id": self.show_id,
            "before": before,
            "through": accepted[-1]["end"] if accepted else 0,
            "range": {"start": lower, "end": upper},
            "query": query,
            "excerpts": result,
            "gaps": gaps,
            "truncated": len(result) < len(candidates) or any(row["excerpted"] for row in result),
            "timestamp_precision": "recognition window; no word timing or speaker identification",
        }

    def context(self, before, speech, memory=None, goal=""):
        """Only already saved speech ending before the current window, bounded for inference."""
        before = _clock(before)
        with self.lock:
            rows = self._rows()
        grouped = {}
        for row in rows:
            if row.get("kind") != "segment" or row.get("status") != "transcribed" or row["end"] > before:
                continue
            item = grouped.setdefault(row["id"], {k: row[k] for k in ("id", "start", "end", "speaker")})
            item["text"] = item.get("text", "") + row["text"]
        earlier = sorted(grouped.values(), key=lambda r: (r["end"], r["start"]))
        if not earlier:
            return {"through": 0, "excerpts": []}
        current_words = _tokens(speech)
        unfinished = " ".join(
            i.get("text", "") + " " + i.get("next_action", "")
            for i in (memory or {}).get("items", []) if i.get("status") not in ("covered", "skipped")
        )
        intent_words = _tokens(unfinished + " " + goal)
        closing = bool((memory or {}).get("closing") or re.search(r"\b(wrap(?:ping)? up|finish|closing|recap|sum up|takeaways?)\b", speech, re.I))
        scored = sorted(earlier[:-2], key=lambda r: (
            3 * len(_tokens(r["text"]) & current_words) + len(_tokens(r["text"]) & intent_words), r["end"]
        ), reverse=True)
        selected = list(earlier[-2:])
        if closing and earlier[0] not in selected:
            selected.append(earlier[0])
        for row in scored:
            if row not in selected and (_tokens(row["text"]) & (current_words | intent_words)):
                selected.append(row)
            if len(selected) >= MAX_CONTEXT_ROWS:
                break
        selected = sorted(selected, key=lambda r: r["end"])
        result, remaining = [], MAX_CONTEXT_CHARS
        for row in selected:
            text = row["text"]
            budget = min(1000, remaining)
            excerpt = text[-budget:] if row in earlier[-2:] else text[:budget]
            result.append({**row, "text": excerpt, "excerpted": len(text) > len(excerpt)})
            remaining -= len(excerpt)
            if remaining <= 0:
                break
        return {"through": earlier[-1]["end"], "excerpts": result,
                "use": "Historical context only. Current speech is required for new cues, completion and actions."}

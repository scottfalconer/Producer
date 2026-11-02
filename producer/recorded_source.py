"""Causally gated playback of an explicitly selected local recording.

Preparing media is local-only. The director receives only intervals whose end
has already passed on the caller's broadcast clock; preparation never gives it
access to the remainder of the recording.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess

from screen_chat_overlay import STYLE, STYLES, composite, snapshot_events, with_producer_posts


RATE = 24000
BYTES_PER_SECOND = RATE * 2
MAX_DURATION = 600


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(args, *, timeout=120):
    result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise ValueError("Local recording preparation failed: " + result.stderr.decode(errors="replace")[-400:])
    return result.stdout


class RecordingSource:
    """Pinned first portion of one selected recording, with cheap PCM reads."""

    def __init__(self, path, duration=600, *, cache_dir, chat_events=None, chat_style=STYLE):
        if not _number(duration) or not 0 < duration <= MAX_DURATION:
            raise ValueError("Select a recording duration from 0 to 600 seconds.")
        self.path = Path(path).resolve(strict=True)
        if not self.path.is_file():
            raise ValueError("Select one local recording file.")
        self.duration = float(duration)
        self.cache_dir = Path(cache_dir).resolve()
        self.clip_path = self.cache_dir / "source.mp4"
        self.pcm_path = self.cache_dir / "audio24.pcm"
        self.manifest_path = self.cache_dir / "source-manifest.json"
        self.report = None
        self._pcm_identity = None
        self._clip_identity = None
        if chat_style not in STYLES:
            raise ValueError("Unknown simulated-chat style.")
        self.chat_style = chat_style
        self.chat_events = snapshot_events(chat_events) if chat_events is not None else None
        self.producer_posts = lambda: []

    @staticmethod
    def _identity(path):
        value = path.stat()
        return value.st_size, value.st_mtime_ns

    def prepare(self):
        """Materialize this authorized source portion, never a live capture."""
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        source_identity = self._identity(self.path)
        source_hash = _hash(self.path)
        if self.manifest_path.exists():
            report = json.loads(self.manifest_path.read_text())
            if report.get("original", {}).get("sha256") != source_hash or report.get("duration_s") != self.duration:
                raise ValueError("This recording cache belongs to a different selected source.")
            for name, path in (("clip", self.clip_path), ("pcm", self.pcm_path)):
                if not path.is_file() or _hash(path) != report.get(name, {}).get("sha256"):
                    raise ValueError("The pinned recording cache has changed.")
            if self.pcm_path.stat().st_size != round(self.duration * RATE) * 2:
                raise ValueError("The cached recording audio is incomplete.")
            self.report = report
            self._pin()
            return report
        if self.clip_path.exists() or self.pcm_path.exists():
            raise ValueError("The recording cache is incomplete; select a fresh cache directory.")
        probe = json.loads(_run([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,sample_rate,channels",
            "-of", "json", str(self.path),
        ], timeout=30))
        source_duration = float(probe.get("format", {}).get("duration", 0))
        streams = probe.get("streams", [])
        if source_duration < self.duration or not any(s.get("codec_type") == "audio" for s in streams):
            raise ValueError("The selected recording does not cover the requested audio duration.")
        if not any(s.get("codec_type") == "video" for s in streams):
            raise ValueError("The selected recording has no show video.")
        _run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(self.path),
            "-t", str(self.duration), "-map", "0:v:0", "-map", "0:a:0",
            "-c", "copy", "-movflags", "+faststart", str(self.clip_path),
        ])
        _run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(self.clip_path),
            "-t", str(self.duration), "-vn", "-ac", "1", "-ar", str(RATE),
            # Raw PCM has no timestamps. Preserve a real leading audio offset
            # relative to video zero instead of shifting sound early or making
            # the packet cache shorter. This does not pad an incomplete tail.
            "-af", f"aresample={RATE}:first_pts=0,atrim=end={self.duration:.6f}",
            "-f", "s16le", str(self.pcm_path),
        ])
        expected_bytes = round(self.duration * RATE) * 2
        if self.pcm_path.stat().st_size != expected_bytes:
            raise ValueError("The selected recording audio does not cover the requested duration.")
        if self._identity(self.path) != source_identity:
            raise ValueError("The selected recording changed during preparation.")
        self.report = {
            "schema": "producer.recorded-source.v1", "duration_s": self.duration,
            "original": {"path": str(self.path), "sha256": source_hash,
                         "bytes": source_identity[0], "duration_s": source_duration,
                         "streams": streams},
            "clip": {"path": str(self.clip_path), "sha256": _hash(self.clip_path),
                     "bytes": self.clip_path.stat().st_size, "start_s": 0},
            "pcm": {"path": str(self.pcm_path), "sha256": _hash(self.pcm_path),
                    "bytes": expected_bytes, "sample_rate": RATE, "channels": 1,
                    "format": "signed-16-bit-little-endian"},
            "scope": "Local derivative of explicitly selected owner recording; no live capture.",
        }
        self.manifest_path.write_text(json.dumps(self.report, indent=2) + "\n")
        for path in (self.clip_path, self.pcm_path, self.manifest_path):
            os.chmod(path, 0o600)
        self._pin()
        return self.report

    def _pin(self):
        self._pcm_identity = self._identity(self.pcm_path)
        self._clip_identity = self._identity(self.clip_path)

    def _interval(self, start, end, available_through, maximum):
        if any(not _number(value) for value in (start, end, available_through)):
            raise ValueError("A finite recording source clock is required.")
        if not 0 <= start < end <= self.duration or end - start > maximum:
            raise ValueError("Invalid selected recording interval.")
        if end > available_through:
            raise ValueError("Selected recording input is not yet available.")
        if self.report is None:
            raise ValueError("Prepare the selected recording first.")

    def audio24(self, start, end, *, available_through=None):
        self._interval(start, end, available_through, 2)
        if self._identity(self.pcm_path) != self._pcm_identity:
            raise ValueError("The pinned recording audio changed.")
        first, last = round(start * RATE) * 2, round(end * RATE) * 2
        with self.pcm_path.open("rb") as stream:
            stream.seek(first)
            pcm = stream.read(last - first)
        if len(pcm) != last - first:
            raise ValueError("The selected recording audio is incomplete.")
        return pcm

    def frame_at(self, end, *, available_through=None):
        start = max(0, end - 0.05) if _number(end) else 0
        self._interval(start, end, available_through, 0.051)
        if self._identity(self.clip_path) != self._clip_identity:
            raise ValueError("The pinned recording video changed.")
        jpeg = _run([
            "ffmpeg", "-nostdin", "-v", "error", "-ss", str(start),
            "-i", str(self.clip_path), "-t", str(end - start), "-an",
            "-frames:v", "1", "-vf", "scale=" + ("1280:-2" if self.chat_events is not None else "640:-2"), "-q:v", "6",
            "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
        ], timeout=15)
        maximum = 2000000 if self.chat_events is not None else 350000
        if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9") or len(jpeg) > maximum:
            raise ValueError("The selected recording frame is unavailable or too large.")
        if self.chat_events is not None:
            return composite(jpeg, with_producer_posts(self.chat_events, self.producer_posts()), end, style=self.chat_style)
        return jpeg

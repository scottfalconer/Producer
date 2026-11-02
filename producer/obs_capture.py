"""One selected OBS programme over loopback; no browser/device capture or API calls.

OBS Custom FFmpeg Recording sends MPEG-TS to ``tcp://127.0.0.1:<port>``.
This receiver decodes the complete programme mix and samples its composed image.
Only an explicitly configured recording directory retains the incoming programme.
"""

from array import array
import base64
from collections import deque
from dataclasses import dataclass
import math
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid


RATE = 24000
PACKET_BYTES = RATE  # Half a second, signed 16-bit mono.
MAX_JPEG = 350000
MAX_PENDING = 24  # At most twelve seconds; never an unbounded recording replay.


@dataclass(frozen=True)
class OBSConfig:
    port: int = 8790
    recording_dir: Path | None = None
    startup_timeout: float = 120.0
    stall_timeout: float = 12.0

    def __post_init__(self):
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("Choose a local OBS port from 1024 to 65535.")
        for value in (self.startup_timeout, self.stall_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError("OBS capture timeouts must be positive and at most 300 seconds.")
        if self.recording_dir is not None:
            object.__setattr__(self, "recording_dir", Path(self.recording_dir).expanduser().resolve())

    @property
    def endpoint(self):
        return f"tcp://127.0.0.1:{self.port}"


class JPEGFrames:
    """Bounded streaming JPEG framing. No payload is included in exceptions."""
    def __init__(self):
        self.buffer = bytearray()

    def append(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while self.buffer:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                if len(self.buffer) > MAX_JPEG:
                    raise ValueError("OBS image decoder returned invalid data.")
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > MAX_JPEG:
                    raise ValueError("OBS image exceeded the selected-image size limit.")
                break
            end += 2
            if end > MAX_JPEG:
                raise ValueError("OBS image exceeded the selected-image size limit.")
            frames.append(bytes(self.buffer[:end]))
            del self.buffer[:end]
        return frames


def _command(image_fd):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("Install FFmpeg before selecting the local OBS feed.")
    return [
        executable, "-hide_banner", "-nostdin", "-nostats", "-v", "info",
        "-probesize", "1000000", "-analyzeduration", "1000000",
        "-protocol_whitelist", "pipe", "-threads", "1", "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE),
        "-af", "aresample=24000:first_pts=0", "-c:a", "pcm_s16le",
        "-flush_packets", "1", "-f", "s16le", "pipe:1",
        "-map", "0:v:0", "-an", "-vf",
        "select='isnan(prev_selected_t)+gte(t-prev_selected_t,4)',showinfo@producer_obs,scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease",
        "-fps_mode", "passthrough", "-c:v", "mjpeg", "-threads:v", "1", "-q:v", "5",
        "-flush_packets", "1", "-f", "image2pipe", f"pipe:{image_fd}",
    ]


class OBSFeed:
    def __init__(self, config, on_packet, on_error, *, offset=0, clock=time.monotonic):
        if not isinstance(config, OBSConfig):
            raise ValueError("Configure the local OBS receiver first.")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not math.isfinite(offset) or not 0 <= offset <= 86400:
            raise ValueError("Invalid OBS source offset.")
        self.config, self.on_packet, self.on_error = config, on_packet, on_error
        self.offset, self.clock = float(offset), clock
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.frame_ready = threading.Event()
        self.pending = queue.Queue(maxsize=MAX_PENDING)
        self.frame_times = queue.Queue(maxsize=8)
        self.frame_history = deque(maxlen=8)
        self.threads = []
        self.listener = self.connection = self.process = self.image_pipe = None
        self.recording = None
        self.recording_path = None
        self.status, self.error = "idle", None
        self.created = self.connected = self.last_received = self.last_decoded = None
        self.latest_frame = None
        self.frame_version = self.frames = self.packets = self.received_bytes = 0
        self.frame_at = None
        self.rms = self.peak = 0.0
        self.first_frame_received = False
        self._shutdown_lock = threading.Lock()

    def _thread(self, name, target):
        thread = threading.Thread(name="producer-obs-" + name, target=target, daemon=True)
        with self.lock:
            if self.stopped.is_set():
                return
            self.threads.append(thread)
            thread.start()

    def start(self):
        with self.lock:
            if self.status != "idle":
                raise ValueError("This OBS receiver has already been started.")
            # Check the decoder before opening the local listener.
            _command(3)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", self.config.port))
                listener.listen(1)
                listener.settimeout(0.25)
            except OSError:
                listener.close()
                raise ValueError("The local OBS port is unavailable. Stop the other receiver or choose another port.") from None
            self.listener = listener
            self.status, self.created = "waiting", self.clock()
        self._thread("receive", self._receive)
        self._thread("watch", self._watch)
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            age = None if self.frame_at is None else max(0, self.clock() - self.frame_at)
            return {
                "status": self.status, "error": self.error,
                "endpoint": self.config.endpoint, "audio_packets": self.packets,
                "source_end": self.offset + self.packets * 0.5,
                "received_bytes": self.received_bytes, "frames": self.frames,
                "first_frame_received": self.first_frame_received,
                "frame_age_s": None if age is None else round(age, 3),
                "audio_rms": round(self.rms, 6), "audio_peak": round(self.peak, 6),
                "recording_path": str(self.recording_path) if self.recording_path else None,
            }

    def _receive(self):
        try:
            while not self.stopped.is_set():
                try:
                    connection, address = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            else:
                return
            if address[0] != "127.0.0.1":
                connection.close()
                raise ValueError("Only a local OBS sender is accepted.")
            with self.lock:
                if self.stopped.is_set():
                    connection.close()
                    return
                self.connection = connection
                connection.settimeout(0.5)
                self.connected = self.last_received = self.clock()
                self.status = "connecting"
                self.listener.close()
                self.listener = None  # No reconnect or second sender in this show.
            self._decoder()
            with self.lock:
                if self.stopped.is_set():
                    return
                if self.config.recording_dir is not None:
                    self.config.recording_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    self.recording_path = self.config.recording_dir / ("obs-" + uuid.uuid4().hex + ".ts")
                    fd = os.open(self.recording_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    self.recording = os.fdopen(fd, "wb", buffering=0)
            while not self.stopped.is_set():
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    self._fail("OBS recording output ended. Start a new local feed to continue.", ended=True)
                    return
                with self.lock:
                    self.last_received = self.clock()
                    self.received_bytes += len(chunk)
                if self.recording is not None:
                    self.recording.write(chunk)
                remaining = memoryview(chunk)
                while remaining and not self.stopped.is_set():
                    count = self.process.stdin.write(remaining)
                    if not count:
                        raise ValueError("OBS programme decoder stopped accepting input.")
                    remaining = remaining[count:]
        except Exception as exc:
            if not self.stopped.is_set():
                self._fail(str(exc) if isinstance(exc, ValueError) else "The local OBS connection or decoder failed.")

    def _decoder(self):
        with self.lock:
            if self.stopped.is_set():
                return
            read_fd, write_fd = os.pipe()
            try:
                process = subprocess.Popen(
                    _command(write_fd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, pass_fds=(write_fd,), bufsize=0,
                )
            except Exception:
                os.close(read_fd)
                raise
            finally:
                os.close(write_fd)
            self.process = process
            self.image_pipe = os.fdopen(read_fd, "rb", buffering=0)
        self._thread("metadata", self._metadata)
        self._thread("images", self._images)
        self._thread("audio", self._audio)
        self._thread("deliver", self._deliver)

    def _metadata(self):
        # Keep only controlled showinfo frame timestamps, never raw diagnostics.
        pending = bytearray()
        try:
            while not self.stopped.is_set():
                chunk = self.process.stderr.read(4096)
                if not chunk:
                    return
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, tail = pending.partition(b"\n")
                    pending = bytearray(tail)
                    if b"showinfo@producer_obs" not in line:
                        continue
                    match = re.search(rb" n:\s*(\d+).*?pts_time:([\d.eE+\-]+)", line)
                    if match is None:
                        continue
                    seconds = float(match[2])
                    if not math.isfinite(seconds) or seconds < -0.05:
                        raise ValueError("OBS returned invalid image source timing.")
                    try:
                        self.frame_times.put_nowait((int(match[1]), self.offset + max(0, seconds)))
                    except queue.Full:
                        raise ValueError("OBS image decoding fell behind the live feed.") from None
                if len(pending) > 8192:
                    pending.clear()
        except Exception as exc:
            if not self.stopped.is_set():
                self._fail(str(exc) if isinstance(exc, ValueError) else "OBS image timing failed.")

    def _images(self):
        parser = JPEGFrames()
        try:
            while not self.stopped.is_set():
                chunk = self.image_pipe.read(65536)
                if not chunk:
                    if not self.stopped.is_set():
                        self._fail("OBS image decoding ended; the selected programme is unavailable.")
                    return
                for frame in parser.append(chunk):
                    try:
                        number, source_time = self.frame_times.get(timeout=self.config.stall_timeout)
                    except queue.Empty:
                        raise ValueError("OBS image source timing was unavailable.") from None
                    with self.lock:
                        if number != self.frames or (self.frame_history and source_time < self.frame_history[-1][0]):
                            raise ValueError("OBS image source timing was out of order.")
                        self.latest_frame, self.frame_at = frame, self.clock()
                        self.frame_version += 1
                        self.frames += 1
                        self.frame_history.append((source_time, frame, self.frame_version))
                        self.first_frame_received = True
                    self.frame_ready.set()
        except Exception as exc:
            if not self.stopped.is_set():
                self._fail(str(exc) if isinstance(exc, ValueError) else "OBS image decoding failed.")

    def _audio(self):
        raw = bytearray()
        try:
            while not self.stopped.is_set():
                chunk = self.process.stdout.read(PACKET_BYTES - len(raw))
                if not chunk:
                    if not self.stopped.is_set():
                        self._fail("OBS audio decoding ended; the programme mix is unavailable.")
                    return
                raw.extend(chunk)
                if len(raw) != PACKET_BYTES:
                    continue
                now = self.clock()
                with self.lock:
                    self.last_decoded = now
                try:
                    self.pending.put_nowait((bytes(raw), now))
                except queue.Full:
                    self._fail("Producer fell behind the live OBS feed. Capture stopped instead of replaying old input.")
                    return
                raw.clear()
        except Exception:
            if not self.stopped.is_set():
                self._fail("OBS audio decoding failed.")

    def _deliver(self):
        delivered_version = 0
        first_delivery = None
        try:
            while not self.stopped.is_set():
                if not self.frame_ready.wait(0.25):
                    continue
                try:
                    pcm, decoded_at = self.pending.get(timeout=0.25)
                except queue.Empty:
                    continue
                now = self.clock()
                if now - decoded_at > self.config.stall_timeout:
                    raise ValueError("OBS audio became stale; capture stopped instead of replaying old input.")
                if first_delivery is None:
                    first_delivery = now
                # A sender cannot accelerate inference by flooding an old file.
                deadline = first_delivery + self.packets * 0.5
                if self.stopped.wait(max(0, deadline - now)):
                    return
                with self.lock:
                    if self.frame_at is None or self.clock() - self.frame_at > self.config.stall_timeout:
                        raise ValueError("OBS video stopped updating; capture paused.")
                    sequence = self.packets
                    source_end = self.offset + (sequence + 1) * 0.5
                    eligible = [item for item in self.frame_history if item[0] <= source_end]
                    if not eligible:
                        raise ValueError("OBS did not supply a picture aligned with the programme audio.")
                    _, chosen_frame, version = eligible[-1]
                    frame = chosen_frame if version != delivered_version else None
                samples = array("h", pcm)
                if sys.byteorder != "little":
                    samples.byteswap()
                peak = max(abs(value) for value in samples) / 32768.0
                rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768.0
                try:
                    self.on_packet(sequence, base64.b64encode(pcm).decode(),
                                   base64.b64encode(frame).decode() if frame else None,
                                   self.offset + (sequence + 1) * 0.5)
                except Exception:
                    raise ValueError("Producer could not accept the OBS programme input.") from None
                # Connection setup may block the first callback. Resume at live
                # cadence afterwards instead of bursting its buffered input.
                if sequence == 0:
                    first_delivery = self.clock()
                with self.lock:
                    if self.stopped.is_set():
                        return
                    self.packets += 1
                    self.status = "receiving"
                    self.rms, self.peak = rms, peak
                delivered_version = version
        except Exception as exc:
            if not self.stopped.is_set():
                self._fail(str(exc) if isinstance(exc, ValueError) else "Producer could not accept the OBS programme input.")

    def _watch(self):
        while not self.stopped.wait(0.25):
            with self.lock:
                now = self.clock()
                connected, last = self.connected, self.last_received
                decoded, frame_at = self.last_decoded, self.frame_at
            if connected is None:
                if now - self.created > self.config.startup_timeout:
                    self._fail("No local OBS output arrived. Start Recording in the prepared OBS profile.")
            elif now - last > self.config.stall_timeout:
                self._fail("The local OBS output stopped sending data.")
            elif now - (decoded or connected) > self.config.stall_timeout:
                self._fail("OBS did not supply a decodable programme audio mix.")
            elif now - (frame_at or connected) > self.config.stall_timeout:
                self._fail("OBS did not supply a current decodable programme picture.")

    def _fail(self, message, *, ended=False):
        with self.lock:
            if self.stopped.is_set():
                return
            self.status, self.error = ("ended" if ended else "error"), message[:240]
            self.stopped.set()
        self._shutdown()
        try:
            self.on_error(self.error)
        except Exception:
            pass  # Preserve the original capture failure; never restart.

    def _shutdown(self):
        with self._shutdown_lock:
            for sock in (self.listener, self.connection):
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()
            process = self.process
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                for pipe in (process.stdin, process.stdout, process.stderr, self.image_pipe):
                    if pipe is not None:
                        pipe.close()
            if self.recording is not None:
                self.recording.close()

    def close(self):
        with self.lock:
            if self.status not in ("error", "ended"):
                self.status = "stopped"
            self.stopped.set()
        self._shutdown()
        current = threading.current_thread()
        for thread in self.threads:
            if thread is not current:
                thread.join(timeout=1.5)
        with self.lock:
            self.latest_frame = None
            self.frame_history.clear()
        for pending in (self.pending, self.frame_times):
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break

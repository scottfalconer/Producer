"""Native loopback ingestion with generated A/V only; no OBS, devices or API."""

import base64
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from obs_capture import JPEGFrames, MAX_JPEG, OBSConfig, OBSFeed, PACKET_BYTES, _command


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.02)
    raise AssertionError("Timed out waiting for authored fixture state")


class OBSFramesTests(unittest.TestCase):
    def test_jpeg_parser_handles_split_markers_and_coalesced_frames(self):
        parser = JPEGFrames()
        first = b"\xff\xd8first\xff\xd9"
        second = b"\xff\xd8second\xff\xd9"
        self.assertEqual(parser.append(first[:1]), [])
        self.assertEqual(parser.append(first[1:-1]), [])
        self.assertEqual(parser.append(first[-1:] + second), [first, second])
        self.assertEqual(parser.buffer, b"")

    def test_jpeg_parser_bounds_incomplete_oversized_and_junk(self):
        for value in (b"x" * (MAX_JPEG + 1), b"\xff\xd8" + b"x" * MAX_JPEG,
                      b"\xff\xd8" + b"x" * MAX_JPEG + b"\xff\xd9"):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                JPEGFrames().append(value)

    def test_config_has_no_remote_host_or_arbitrary_command(self):
        for port in (0, 80, 65536, True, "8790"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                OBSConfig(port=port)
        for timeout in (0, -1, 301, True, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                OBSConfig(startup_timeout=timeout)
        self.assertEqual(OBSConfig().endpoint, "tcp://127.0.0.1:8790")
        command = _command(9)
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "pipe")
        self.assertEqual(command[command.index("-i") + 1], "pipe:0")
        self.assertNotIn("avfoundation", command)
        self.assertNotIn("-re", command)
        self.assertIn("pipe:9", command)

    def test_missing_ffmpeg_fails_before_binding(self):
        feed = OBSFeed(OBSConfig(port=free_port()), lambda *_: None, lambda _: None)
        with patch("obs_capture.shutil.which", return_value=None), self.assertRaisesRegex(ValueError, "Install FFmpeg"):
            feed.start()
        self.assertIsNone(feed.listener)
        self.assertEqual(feed.status, "idle")

    def test_audio_backlog_is_bounded_and_fails_without_replay(self):
        errors = []
        feed = OBSFeed(OBSConfig(), lambda *_: self.fail("No delivery expected"), errors.append)
        process = Mock()
        process.stdout = io.BytesIO(b"\0" * PACKET_BYTES * 25)
        process.stdin = io.BytesIO()
        process.poll.return_value = 0
        feed.process = process
        feed._audio()
        self.assertEqual(feed.pending.qsize(), 24)
        self.assertIn("fell behind", errors[0])
        self.assertEqual(feed.snapshot()["status"], "error")
        feed.close()
        self.assertEqual(feed.pending.qsize(), 0)

    def test_stale_audio_never_reaches_callback(self):
        errors = []
        feed = OBSFeed(OBSConfig(), lambda *_: self.fail("Stale data must not be delivered"), errors.append)
        feed.latest_frame = b"\xff\xd8frame\xff\xd9"
        feed.frame_at = time.monotonic()
        feed.frame_ready.set()
        feed.pending.put((b"\0" * PACKET_BYTES, time.monotonic() - 13))
        feed._deliver()
        self.assertIn("stale", errors[0])
        self.assertEqual(feed.packets, 0)
        feed.close()

    def test_blocked_first_callback_preserves_frame_causality_and_cadence(self):
        entered, release, complete = threading.Event(), threading.Event(), threading.Event()
        packets, errors = [], []
        first, future = b"\xff\xd8first\xff\xd9", b"\xff\xd8future\xff\xd9"
        def callback(sequence, audio, frame, end):
            packets.append((sequence, frame, end, time.monotonic()))
            if sequence == 0:
                entered.set()
                release.wait(3)
            elif sequence == 2:
                feed.close()
                complete.set()
        feed = OBSFeed(OBSConfig(), callback, errors.append)
        feed.latest_frame = first
        feed.frame_at = time.monotonic()
        feed.frame_history.append((0.0, first, 1))
        feed.frame_ready.set()
        for _ in range(3):
            feed.pending.put((b"\0" * PACKET_BYTES, time.monotonic()))
        thread = threading.Thread(target=feed._deliver)
        feed.threads.append(thread)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            # A future picture arrives while Realtime would be connecting.
            time.sleep(.7)
            with feed.lock:
                feed.frame_history.append((1.5, future, 2))
                feed.latest_frame, feed.frame_at = future, time.monotonic()
            released_at = time.monotonic()
            release.set()
            self.assertTrue(complete.wait(3))
            self.assertEqual(errors, [])
            self.assertEqual(base64.b64decode(packets[0][1]), first)
            self.assertIsNone(packets[1][1])
            self.assertEqual(base64.b64decode(packets[2][1]), future)
            self.assertEqual([p[2] for p in packets], [.5, 1.0, 1.5])
            self.assertGreaterEqual(packets[1][3] - released_at, .45)
            self.assertGreaterEqual(packets[2][3] - packets[1][3], .45)
        finally:
            release.set()
            feed.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_constructor_rejects_invalid_offset(self):
        for offset in (-1, True, float("inf"), 86401):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                OBSFeed(OBSConfig(), lambda *_: None, lambda _: None, offset=offset)


class OBSLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.feeds = []
        self.senders = []
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for feed in self.feeds:
            feed.close()
        for sender in self.senders:
            if sender.poll() is None:
                sender.terminate()
            sender.communicate(timeout=4)
        self.temp.cleanup()

    def feed(self, *, callback=None, **kwargs):
        packets, errors = [], []
        feed = OBSFeed(OBSConfig(port=free_port(), **kwargs), callback or (lambda *packet: packets.append(packet)), errors.append)
        self.feeds.append(feed)
        return feed, packets, errors

    def sender(self, endpoint, duration=8, *, audio=True):
        command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-re",
                   "-f", "lavfi", "-i", f"color=c=blue:s=320x180:r=30:d={duration}"]
        if audio:
            command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}"]
        command += ["-t", str(duration), "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-pix_fmt", "yuv420p", "-g", "30"]
        if audio:
            command += ["-c:a", "aac", "-b:a", "96k"]
        command += ["-muxdelay", "0", "-flush_packets", "1", "-f", "mpegts", endpoint]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.senders.append(process)
        return process

    def test_listener_starts_without_capture_and_stop_releases_port(self):
        feed, packets, errors = self.feed()
        snapshot = feed.start()
        self.assertEqual(snapshot["status"], "waiting")
        self.assertEqual(feed.listener.getsockname(), ("127.0.0.1", feed.config.port))
        self.assertIsNone(feed.process)
        self.assertEqual(packets, [])
        feed.close()
        self.assertEqual(feed.snapshot()["status"], "stopped")
        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in feed.threads))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", feed.config.port))
        with self.assertRaisesRegex(ValueError, "already"):
            feed.start()

    def test_wait_timeout_fails_without_starting_decoder_or_retry(self):
        feed, packets, errors = self.feed(startup_timeout=.1)
        feed.start()
        until(lambda: bool(errors))
        self.assertEqual(feed.snapshot()["status"], "error")
        self.assertIsNone(feed.process)
        self.assertEqual(packets, [])
        self.assertEqual(len(errors), 1)
        feed.close()
        self.assertEqual(len(errors), 1)

    def test_port_collision_preserves_existing_listener(self):
        first, _, _ = self.feed()
        first.start()
        second = OBSFeed(first.config, lambda *_: None, lambda _: None)
        self.feeds.append(second)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            second.start()
        self.assertEqual(first.snapshot()["status"], "waiting")

    def test_disconnect_is_explicit_and_does_not_reconnect(self):
        feed, packets, errors = self.feed()
        feed.start()
        with socket.create_connection(("127.0.0.1", feed.config.port)):
            pass
        until(lambda: bool(errors))
        self.assertEqual(feed.snapshot()["status"], "ended")
        self.assertEqual(packets, [])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", feed.config.port), timeout=.2)

    def test_real_generated_programme_supplies_mix_frames_and_recording(self):
        feed, packets, errors = self.feed(recording_dir=Path(self.temp.name) / "recordings")
        feed.start()
        sender = self.sender(feed.config.endpoint, duration=7)
        until(lambda: len(packets) >= 9 or bool(errors), timeout=10)
        self.assertFalse(errors, errors)
        self.assertGreaterEqual(len(packets), 9)
        self.assertEqual([packet[0] for packet in packets], list(range(len(packets))))
        self.assertTrue(all(len(base64.b64decode(packet[1])) == PACKET_BYTES for packet in packets))
        self.assertTrue(all(packet[3] == (packet[0] + 1) * .5 for packet in packets))
        self.assertIsNotNone(packets[0][2])
        frames = [base64.b64decode(packet[2]) for packet in packets if packet[2]]
        self.assertGreaterEqual(len(frames), 2)
        for frame in frames:
            self.assertLessEqual(len(frame), MAX_JPEG)
            with Image.open(io.BytesIO(frame)) as picture:
                self.assertEqual(picture.size, (320, 180))
        snapshot = feed.snapshot()
        self.assertEqual(snapshot["status"], "receiving")
        self.assertGreater(snapshot["audio_peak"], .01)
        self.assertGreater(snapshot["audio_rms"], .01)
        self.assertTrue(snapshot["first_frame_received"])
        feed.close()
        count = len(packets)
        time.sleep(.1)
        self.assertEqual(len(packets), count)
        self.assertTrue(all(not thread.is_alive() for thread in feed.threads))
        self.assertIsNotNone(feed.process.poll())
        saved = Path(snapshot["recording_path"])
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(saved)], capture_output=True, check=True)
        streams = json.loads(probe.stdout)["streams"]
        self.assertEqual({stream["codec_type"] for stream in streams}, {"audio", "video"})
        if sender.poll() is None:
            sender.terminate()

    def test_missing_audio_fails_without_any_callback(self):
        feed, packets, errors = self.feed(stall_timeout=3)
        feed.start()
        self.sender(feed.config.endpoint, duration=6, audio=False)
        until(lambda: bool(errors), timeout=6)
        self.assertEqual(packets, [])
        self.assertEqual(feed.snapshot()["status"], "error")

    def test_callback_can_close_capture_without_deadlock(self):
        called = threading.Event()
        holder = {}
        def callback(*_):
            holder["feed"].close()
            called.set()
        feed, _, errors = self.feed(callback=callback)
        holder["feed"] = feed
        feed.start()
        self.sender(feed.config.endpoint, duration=5)
        self.assertTrue(called.wait(7))
        self.assertEqual(errors, [])
        self.assertEqual(feed.snapshot()["status"], "stopped")
        self.assertIsNotNone(feed.process.poll())

    def test_callback_failure_does_not_expose_exception_payload(self):
        def callback(*_):
            raise ValueError("secret-content-from-upstream")
        feed, _, errors = self.feed(callback=callback)
        feed.start()
        self.sender(feed.config.endpoint, duration=5)
        until(lambda: bool(errors), timeout=7)
        self.assertNotIn("secret", errors[0])
        self.assertEqual(errors[0], "Producer could not accept the OBS programme input.")


if __name__ == "__main__":
    unittest.main()

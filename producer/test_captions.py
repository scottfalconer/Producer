import threading
import unittest
from unittest.mock import Mock, patch
from captions import CaptionSession


class CaptionTests(unittest.TestCase):
    def state(self):
        c = CaptionSession.__new__(CaptionSession)
        c.lock = threading.RLock()
        c.closed = False
        c.items = {}
        c.retired = set()
        c.order = 0
        c.current = None
        c.socket = Mock()
        return c

    def test_late_completion_cannot_replace_newer_speech(self):
        c = self.state()
        c.accept({"type": "input_audio_buffer.committed", "item_id": "one"}, 0)
        c.accept({"type": "input_audio_buffer.committed", "item_id": "two"}, 1)
        c.accept(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "two",
                "delta": "New current speech",
            },
            2,
        )
        c.accept(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "one",
                "transcript": "Old delayed speech",
            },
            3,
        )
        with patch("captions.time.monotonic", return_value=3):
            self.assertEqual(c.snapshot()["text"], "New current speech")
        with patch("captions.time.monotonic", return_value=9):
            self.assertIsNone(c.snapshot())

    def test_unknown_or_stale_transcript_does_not_appear(self):
        c = self.state()
        c.accept(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "missing",
                "transcript": "Unknown",
            },
            1,
        )
        self.assertIsNone(c.current)
        c.accept({"type": "input_audio_buffer.committed", "item_id": "old"}, 0)
        c.accept(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "old",
                "transcript": "Late",
            },
            20,
        )
        self.assertIsNone(c.current)

    def test_close_clears_public_caption_and_pending_text(self):
        c = self.state()
        c.accept({"type": "input_audio_buffer.committed", "item_id": "one"}, 0)
        c.accept(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "one",
                "delta": "Partial",
            },
            1,
        )
        c.close()
        self.assertEqual(c.items, {})
        self.assertIsNone(c.snapshot())
        c.socket.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

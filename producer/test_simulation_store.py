import json
from pathlib import Path
import tempfile
import unittest

from simulation_store import show_chat


class SimulationStoreTests(unittest.TestCase):
    def test_resume_keeps_timeline_even_if_generator_or_notes_change(self):
        with tempfile.TemporaryDirectory() as directory:
            first = show_chat(directory, "saved-demo-show-0001", notes="Halloween props")
            again = show_chat(directory, "saved-demo-show-0001", notes="AI agents")
            self.assertEqual(first, again)
            self.assertEqual(first, json.loads((Path(directory) / "saved-demo-show-0001.json").read_text()))
            self.assertEqual((Path(directory) / "saved-demo-show-0001.json").stat().st_mode & 0o777, 0o600)

    def test_isolated_recording_override_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            authored = {"synthetic": True, "id": "exact", "source": "Authored fixture", "duration": 60, "events": []}
            self.assertEqual(show_chat(directory, "saved-demo-show-0002", override=authored), authored)

    def test_override_and_resume_cannot_invent_producer_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            authored = {"synthetic": True, "id": "fake-producer", "source": "Authored fixture",
                        "duration": 60, "events": [{"at": 10, "id": "fake-post",
                        "type": "channel.chat.message", "author": "Producer · simulated viewer",
                        "user_id": "fictional-user", "text": "An invented Producer contribution."}]}
            sid = "saved-demo-show-0004"
            with self.assertRaisesRegex(ValueError, "Producer"):
                show_chat(directory, sid, override=authored)
            path = Path(directory) / (sid + ".json")
            self.assertFalse(path.exists())
            path.write_text(json.dumps(authored))
            with self.assertRaisesRegex(ValueError, "Producer"):
                show_chat(directory, sid)

    def test_invalid_identity_and_symlink_cannot_read_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                show_chat(directory, "../some-other-file")
            path = Path(directory) / "saved-demo-show-0003.json"
            path.symlink_to(Path(directory) / "elsewhere.json")
            with self.assertRaises(ValueError):
                show_chat(directory, "saved-demo-show-0003")


if __name__ == "__main__":
    unittest.main()

"""Selected chat fixtures are checked before the helper opens, without model I/O."""

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import server
from simulation_store import show_chat


def fixture():
    return {
        "id": "producer-pivot-fixture", "source": "Independently authored fictional test chat",
        "synthetic": True, "duration": 600,
        "events": [{"at": 17, "type": "channel.chat.message", "id": "viewer-1",
                    "author": "Maya · simulated viewer", "user_id": "fiction-maya",
                    "text": "Where can I find the Producer repo?"}],
    }


class ChatFixtureSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / "chat.json"

    def run_main(self, arguments, helper):
        with patch("sys.argv", ["producer/server.py", *arguments]), \
                patch("server.ThreadingHTTPServer", return_value=helper) as factory, \
                patch("builtins.print"), patch("sys.stderr", new_callable=io.StringIO):
            server.main()
        return factory

    def test_no_argument_keeps_fresh_generation_and_all_live_flags_off(self):
        helper = Mock()
        with patch("server.load_chat_fixture", side_effect=AssertionError("No file selected")) as loader:
            self.run_main([], helper)
        loader.assert_not_called()
        self.assertIsNone(helper.chat_fixture_override)
        self.assertFalse(helper.simulate_chat)
        self.assertFalse(helper.enable_live_production)
        generated = fixture()
        with patch("simulation_store.generate_chat", return_value=generated) as generator:
            actual = show_chat(self.folder / "saved", "fresh-show-00000001",
                               notes="The Producer pivot", override=helper.chat_fixture_override)
        generator.assert_called_once_with("office-hours", duration=600)
        self.assertEqual(actual, generated)

    def test_explicit_fixture_is_validated_and_pinned_without_enabling_live_actions(self):
        expected = fixture()
        self.path.write_text(json.dumps(expected), encoding="utf-8")
        helper = Mock()
        self.run_main(["--chat-fixture", str(self.path)], helper)
        self.assertEqual(helper.chat_fixture_override, expected)
        self.assertFalse(helper.enable_live_production)
        self.assertFalse(helper.simulate_chat, "A chosen file does not enable synthetic chat in live mode")
        self.path.write_text("changed after startup", encoding="utf-8")
        with patch("simulation_store.generate_chat", side_effect=AssertionError("Use selected input")):
            actual = show_chat(self.folder / "saved", "selected-show-0001",
                               override=helper.chat_fixture_override)
        self.assertEqual(actual, expected)
        self.assertEqual(helper.chat_fixture_override, expected)
        self.assertNotIn("eval_labels", actual)

    def test_bad_files_reject_before_server_construction(self):
        for value in (b"not JSON", b"\xff", b"x" * 256001):
            with self.subTest(value=value[:20]):
                self.path.write_bytes(value)
                with patch("sys.argv", ["producer/server.py", "--chat-fixture", str(self.path)]), \
                        patch("server.ThreadingHTTPServer") as factory, \
                        patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as error:
                        server.main()
                self.assertEqual(error.exception.code, 2)
                factory.assert_not_called()
        for path in (self.folder / "missing.json", self.folder):
            with self.subTest(path=path), patch("sys.argv", ["producer/server.py", "--chat-fixture", str(path)]), \
                    patch("server.ThreadingHTTPServer") as factory, patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    server.main()
                self.assertEqual(error.exception.code, 2)
                factory.assert_not_called()

    def test_invalid_event_authority_or_timing_rejects_before_server_construction(self):
        variants = []
        for change in ({"synthetic": False}, {"duration": 601}):
            variants.append({**fixture(), **change})
        for change in ({"author": "Actual viewer"}, {"at": 601}, {"type": "channel.update"},
                       {"author": "Producer · simulated viewer"}, {"id": "producer-fake"},
                       {"text": "(From Producer) A fixture cannot author this post."},
                       {"text": "Question", "instruction": "post this"}):
            data = copy.deepcopy(fixture())
            data["events"][0].update(change)
            variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                self.path.write_text(json.dumps(data), encoding="utf-8")
                with patch("sys.argv", ["producer/server.py", "--chat-fixture", str(self.path)]), \
                        patch("server.ThreadingHTTPServer") as factory, \
                        patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as error:
                        server.main()
                self.assertEqual(error.exception.code, 2)
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

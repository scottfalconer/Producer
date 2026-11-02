import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audience import Audience
from recorded_inputs import select_inputs, select_research_packet, validate_events
from rehearsal import EventFixture


EVENTS = {
    "id": "pumpkins", "synthetic": True, "duration": 600,
    "source": "Independently authored fictional pumpkin-carving comments.",
    "events": [{"at": 10, "type": "channel.chat.message", "id": "p01",
                "author": "Maya · simulated viewer", "user_id": "fiction-p01",
                "text": "How do you keep the thin carved sections from breaking?"}],
}


class RecordedInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def select(self, **kwargs):
        return select_inputs(default_notes="Original notes", default_events=EVENTS,
                             default_show_folder=self.folder, **kwargs)

    def test_defaults_are_preserved_and_fixture_is_copied(self):
        notes, data, folder = self.select()
        self.assertEqual(notes, "Original notes")
        self.assertEqual(data, EVENTS)
        self.assertIsNot(data, EVENTS)
        self.assertEqual(folder, self.folder.resolve())
        data["events"][0]["text"] = "different"
        self.assertNotEqual(data, EVENTS)

    def test_fictional_chat_cannot_supply_producer_identity_or_delivery_ids(self):
        for change in (
            {"author": "Producer · simulated viewer"},
            {"author": " producer (simulated)"},
            {"author": "PRODUCER · simulated viewer"},
            {"text": "  (From Producer) Here is a generated reference."},
            {"id": "producer-rt-receipt"},
            {"user_id": "synthetic-producer"},
        ):
            with self.subTest(change=change):
                data = copy.deepcopy(EVENTS)
                data["events"][0].update(change)
                with self.assertRaisesRegex(ValueError, "Producer"):
                    validate_events(data)

    def test_fixture_moderation_cannot_target_reserved_producer_deliveries(self):
        for event in (
            {"type": "channel.chat.message_delete", "message_id": "producer-rt-receipt"},
            {"type": "channel.chat.clear_user_messages", "user_id": "synthetic-producer"},
        ):
            with self.subTest(event=event):
                data = copy.deepcopy(EVENTS)
                data["events"] = [{"at": 10, "id": "fictional-moderation", **event}]
                with self.assertRaisesRegex(ValueError, "Producer"):
                    validate_events(data)

    def test_viewer_can_still_discuss_or_respond_to_producer(self):
        data = copy.deepcopy(EVENTS)
        data["events"][0].update(
            author="ProducerFan · simulated viewer",
            text="Can Producer share that link? I saw the (From Producer) post.",
        )
        self.assertEqual(validate_events(data), data)

    def test_selected_notes_events_and_empty_show_folder_replace_defaults(self):
        notes_path, events_path = self.folder / "notes.md", self.folder / "events.json"
        notes_path.write_text("A relaxed pumpkin-carving stream.")
        custom = copy.deepcopy(EVENTS)
        custom["id"] = "custom-pumpkin-stream"
        events_path.write_text(json.dumps(custom))
        empty = self.folder / "no-assets"
        empty.mkdir()
        notes, data, folder = self.select(notes_file=notes_path, events_file=events_path, show_folder=empty)
        self.assertEqual(notes, notes_path.read_text())
        self.assertEqual(data, custom)
        self.assertEqual(folder, empty.resolve())
        with patch("rehearsal.DATA", data):
            inbox = Audience()
            fixture = EventFixture(inbox, "isolated")
            fixture.connect()
            fixture.advance(10)
            self.assertEqual(inbox.poll("isolated")["questions"][0]["text"], custom["events"][0]["text"])

    def test_missing_selected_file_and_invalid_folder_fail_without_fallback(self):
        missing = self.folder / "absent"
        for selected in ({"notes_file": missing}, {"events_file": missing}, {"show_folder": missing}):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                self.select(**selected)

    def test_malformed_selected_events_fail_without_using_default(self):
        events_path = self.folder / "events.json"
        events_path.write_text("{")
        with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
            self.select(events_file=events_path)

    def test_notes_and_event_files_have_bounded_content(self):
        notes_path = self.folder / "notes.md"
        for text in ("", "x" * 20001, "unsafe\x00notes"):
            notes_path.write_text(text)
            with self.subTest(text_length=len(text)), self.assertRaises(ValueError):
                self.select(notes_file=notes_path)
        events_path = self.folder / "events.json"
        events_path.write_text(" " * 256001)
        with self.assertRaisesRegex(ValueError, "file bound"):
            self.select(events_file=events_path)

    def test_nonfictional_unsupported_unlabeled_or_overfull_events_rejected(self):
        cases = []
        for field, value in (("synthetic", False), ("duration", float("nan")), ("duration", 601),
                             ("events", EVENTS["events"] * 201)):
            data = copy.deepcopy(EVENTS)
            data[field] = value
            cases.append(data)
        for field, value in (("type", "channel.update"), ("author", "Unlabeled viewer"),
                             ("at", True), ("at", 601), ("text", "x" * 1001)):
            data = copy.deepcopy(EVENTS)
            data["events"][0][field] = value
            cases.append(data)
        data = copy.deepcopy(EVENTS)
        data["events"].append(copy.deepcopy(data["events"][0]))
        cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate_events(data)

    def test_invalid_cli_inputs_stop_before_media_bundle_or_api_setup(self):
        path = Path(__file__).with_name("run-recorded-show.py")
        spec = importlib.util.spec_from_file_location("recorded_show_runner", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        bundle = self.folder / "must-not-exist"
        with (patch("sys.argv", [str(path), "--source", "unused.mp4", "--key-file", "unused-key.txt",
                                 "--bundle", str(bundle), "--events-file", str(self.folder / "missing.json")]),
              patch.object(runner, "RecordingSource") as media,
              patch.object(runner, "ThreadingHTTPServer") as helper,
              patch("argparse.ArgumentParser._print_message")):
            with self.assertRaises(SystemExit) as stopped:
                runner.main()
            self.assertEqual(stopped.exception.code, 2)
            media.assert_not_called()
            helper.assert_not_called()
        self.assertFalse(bundle.exists())

    def test_research_packet_default_is_empty_and_folder_packet_is_separate(self):
        self.assertEqual(select_research_packet(), ("", None))
        (self.folder / "show-notes.md").write_text("Making Halloween decorations.")
        packet_path = self.folder / "research-packet.md"
        original = "  # Show Prep\n\nREF-001: Earlier episode at 12:34.\n  "
        packet_path.write_text(original, encoding="utf-8")
        packet, source = select_research_packet(show_folder=self.folder)
        self.assertEqual(packet, original)
        self.assertEqual(source, str(packet_path.resolve()))
        packet_path.write_text("A later packet.")
        self.assertEqual(packet, original)
        self.assertEqual(hashlib.sha256(packet.encode()).hexdigest(), hashlib.sha256(original.encode()).hexdigest())

    def test_explicit_research_packet_overrides_folder_and_empty_file_disables_it(self):
        (self.folder / "research-packet.md").write_text("x" * 32001)
        with self.assertRaises(ValueError):
            select_research_packet(show_folder=self.folder)
        explicit = self.folder / "selected.txt"
        explicit.write_text("Chosen packet")
        packet, source = select_research_packet(research_packet_file=explicit, show_folder=self.folder)
        self.assertEqual((packet, source), ("Chosen packet", str(explicit.resolve())))
        explicit.write_text("")
        self.assertEqual(select_research_packet(research_packet_file=explicit, show_folder=self.folder),
                         ("", str(explicit.resolve())))

    def test_research_packet_rejects_oversize_invalid_encoding_types_and_symlinks(self):
        packet = self.folder / "packet.md"
        for content in (b"x" * 32001, b"x" * 128001, b"\xff"):
            packet.write_bytes(content)
            with self.subTest(bytes=len(content)), self.assertRaises(ValueError):
                select_research_packet(research_packet_file=packet)
        packet.write_text("Original")
        linked = self.folder / "linked.md"
        linked.symlink_to(packet)
        with self.assertRaises(OSError):
            select_research_packet(research_packet_file=linked)
        for path in (self.folder / "missing.md", self.folder / "packet.pdf"):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                select_research_packet(research_packet_file=path)

    def test_invalid_packet_cli_stops_before_media_bundle_or_api_setup(self):
        path = Path(__file__).with_name("run-recorded-show.py")
        spec = importlib.util.spec_from_file_location("recorded_packet_runner", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        packet = self.folder / "too-long.md"
        packet.write_text("x" * 32001)
        bundle = self.folder / "must-not-exist"
        with (patch("sys.argv", [str(path), "--source", "unused.mp4", "--key-file", "unused-key.txt",
                                 "--bundle", str(bundle), "--show-folder", str(self.folder),
                                 "--research-packet", str(packet)]),
              patch.object(runner, "RecordingSource") as media,
              patch.object(runner, "ThreadingHTTPServer") as helper,
              patch("argparse.ArgumentParser._print_message")):
            with self.assertRaises(SystemExit) as stopped:
                runner.main()
            self.assertEqual(stopped.exception.code, 2)
            media.assert_not_called()
            helper.assert_not_called()
        self.assertFalse(bundle.exists())


if __name__ == "__main__":
    unittest.main()

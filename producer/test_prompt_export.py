"""Packet export through actual constructors, without network or credentials."""

import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


class PromptExportTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name("export-director-prompt.py")
        spec = importlib.util.spec_from_file_location("packet_prompt_export", path)
        self.exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.exporter)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.notes = self.folder / "notes.md"
        self.notes.write_text("Setting up Halloween decorations.")

    def test_packet_reaches_production_bootstrap_and_pinned_export_unchanged(self):
        packet = self.folder / "external-research.md"
        body = "  # Show Prep\n\nREF-001: A useful original source.\nhttps://example.com/source\n"
        packet.write_text(body, encoding="utf-8")
        output = self.folder / "export"
        with patch.object(socket, "socket", side_effect=AssertionError("Network is not permitted")):
            manifest = self.exporter.export(self.notes, output, packet)
        instructions = (output / "full-session-instructions.txt").read_text()
        beginning = instructions.index('{"show_brief"')
        envelope = json.JSONDecoder().raw_decode(instructions[beginning:])[0]
        self.assertEqual(envelope["show_brief"]["research_packet"], body)
        self.assertEqual((output / "research-packet.md").read_text(), body)
        self.assertEqual(manifest["research_packet_sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(manifest["research_packet_characters"], len(body))
        self.assertEqual(manifest["source_research_packet"], str(packet.resolve()))
        self.assertEqual(manifest["model_calls"], 0)
        self.assertEqual(manifest["network_requests"], 0)
        self.assertEqual(manifest["credential_files_read"], 0)

    def test_invalid_packet_does_not_create_export_directory(self):
        packet = self.folder / "too-long.md"
        packet.write_text("x" * 32001)
        output = self.folder / "must-not-exist"
        with self.assertRaises(ValueError):
            self.exporter.export(self.notes, output, packet)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

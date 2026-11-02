"""Credential formats remain compatible without importing legacy experiments."""

from pathlib import Path
import tempfile
import unittest

from credentials import read_openai_key


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "key.txt"
        self.key = "sk-" + "fictional_for_unit_tests"

    def test_supported_formats(self):
        for content in (self.key, f" {self.key}\n", f"OPENAI_API_KEY={self.key}",
                        f"# locally selected\nOPENAI_API_KEY='{self.key}'\n",
                        f'OPENAI_API_KEY = "{self.key}"'):
            with self.subTest(format=content.split("=")[0][:16]):
                self.path.write_text(content)
                self.assertEqual(read_openai_key(self.path), self.key)

    def test_rejects_invalid_multiple_and_oversized_without_disclosure(self):
        for content in (b"", b"\xff", b"x" * 8193,
                        f"OPENAI_API_KEY={self.key}\nOTHER=secret".encode(),
                        b"OPENAI_API_KEY=$(cat another-file)"):
            self.path.write_bytes(content)
            with self.assertRaisesRegex(ValueError, "unavailable or invalid") as error:
                read_openai_key(self.path)
            self.assertNotIn(self.key, str(error.exception))

    def test_missing_file_and_no_selection_fail_clearly(self):
        for path in (self.path, None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                read_openai_key(path)

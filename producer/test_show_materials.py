"""Owner-selected material snapshots stay bounded, local, and immutable in content."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from show_materials import ShowMaterials, MAX_IMAGES, MAX_IMAGE_BYTES, MAX_NOTES, MAX_RESEARCH_PACKET


class ShowMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def image(self, name="one.png", *, color="red", size=(60, 40), format=None):
        target = self.folder / name
        Image.new("RGB", size, color).save(target, format=format)
        return target

    def test_missing_explicit_folder_fails_but_missing_default_may_be_empty(self):
        missing = self.folder / "missing"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ShowMaterials(missing)
        snapshot = ShowMaterials(missing, allow_empty=True)
        self.assertEqual(snapshot.catalog, [])
        self.assertEqual(snapshot.notes, "")
        self.assertEqual(snapshot.contact_sheet, b"")
        self.assertEqual(snapshot.summary["folder"], str(missing))
        self.assertEqual(snapshot.summary["image_count"], 0)
        self.assertFalse(snapshot.summary["notes_available"])
        self.assertIsNone(snapshot.get("canada"))

    def test_directory_symlink_and_parent_traversal_are_rejected(self):
        link = self.folder / "alias"
        link.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(ValueError):
            ShowMaterials(link)
        with self.assertRaises(ValueError):
            ShowMaterials(self.folder / "child" / "..")
        file = self.folder / "not-a-folder.txt"
        file.write_text("No folder")
        with self.assertRaises(ValueError):
            ShowMaterials(file, allow_empty=True)

    def test_plain_notes_exclude_readme_hidden_symlinks_and_subfolders(self):
        (self.folder / "01.txt").write_text("First ordinary notes.")
        (self.folder / "02.md").write_text("## Second\nNo prescribed format.")
        (self.folder / "README.md").write_text("Not show notes")
        (self.folder / ".private.txt").write_text("Hidden text")
        (self.folder / "linked.txt").symlink_to(self.folder / "01.txt")
        (self.folder / "nested").mkdir()
        (self.folder / "nested" / "secret.md").write_text("Nested text")
        snapshot = ShowMaterials(self.folder)
        self.assertEqual(snapshot.notes, "First ordinary notes.\n\n## Second\nNo prescribed format.")
        self.assertTrue(snapshot.summary["notes_available"])
        self.assertEqual(snapshot.catalog, [])
        self.assertGreaterEqual(snapshot.summary["skipped_count"], 3)

    def test_notes_have_a_combined_character_bound_and_invalid_utf8_is_skipped(self):
        (self.folder / "01.md").write_text("a" * 15000)
        (self.folder / "02.txt").write_text("b" * 15000)
        (self.folder / "03.txt").write_text("This cannot expand the bound")
        (self.folder / "00.txt").write_bytes(b"\xff\xfe")
        snapshot = ShowMaterials(self.folder)
        self.assertEqual(len(snapshot.notes), MAX_NOTES)
        self.assertTrue(snapshot.notes.startswith("a" * 15000 + "\n\n"))
        self.assertNotIn("cannot expand", snapshot.notes)
        self.assertEqual(snapshot.summary["skipped_count"], 2)

    def test_research_packet_is_separate_complete_and_pinned(self):
        packet = "  # References\nREF-001: Prior episode at 02:31 — https://example.test/reference\n"
        path = self.folder / "Research-Packet.MD"
        path.write_text(packet, encoding="utf-8")
        (self.folder / "notes.md").write_text("Making Halloween decorations")
        first = ShowMaterials(self.folder)
        self.assertEqual(first.research_packet, packet)
        self.assertEqual(first.research_packet_name, path.name)
        self.assertEqual(first.notes, "Making Halloween decorations")
        self.assertTrue(first.summary["research_available"])
        self.assertEqual(first.summary["research_characters"], len(packet))
        self.assertNotIn("References", str(first.catalog))
        path.write_text("Replaced packet")
        self.assertEqual(first.research_packet, packet)
        self.assertEqual(ShowMaterials(self.folder).research_packet, "Replaced packet")

    def test_research_packet_rejects_ambiguous_invalid_and_oversized_files_whole(self):
        path = self.folder / "research-packet.txt"
        path.write_text("Fact")
        other = self.folder / "research-packet.md"
        other.write_text("Another packet")
        with self.assertRaisesRegex(ValueError, "only one"):
            ShowMaterials(self.folder)
        skipped = ShowMaterials(self.folder, load_research=False)
        self.assertEqual(skipped.research_packet, "")
        self.assertEqual(skipped.notes, "")
        other.unlink()
        for content in (b"\xff", b"a" * (MAX_RESEARCH_PACKET + 1), b"a" * (MAX_RESEARCH_PACKET * 4 + 4)):
            path.write_bytes(content)
            with self.subTest(size=len(content)), self.assertRaisesRegex(ValueError, "research packet"):
                ShowMaterials(self.folder)
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(ValueError, "research packet"):
            ShowMaterials(self.folder)
        path.rmdir()
        target = self.folder / "notes.md"
        target.write_text("Do not follow this link")
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "research packet"):
            ShowMaterials(self.folder)
        self.assertEqual(ShowMaterials(self.folder, load_research=False).notes, "Do not follow this link")

    def test_research_packet_accepts_bom_and_exact_multibyte_character_bound(self):
        packet = "🎃" * MAX_RESEARCH_PACKET
        (self.folder / "research-packet.md").write_bytes(b"\xef\xbb\xbf" + packet.encode("utf-8"))
        snapshot = ShowMaterials(self.folder)
        self.assertEqual(snapshot.research_packet, packet)
        self.assertEqual(len(snapshot.research_packet), MAX_RESEARCH_PACKET)
        self.assertEqual(snapshot.notes, "")

    def test_image_count_byte_dimension_and_decode_bounds(self):
        for index in range(MAX_IMAGES + 2):
            self.image(f"{index:02d}.png", color=(index * 10, 0, 0))
        snapshot = ShowMaterials(self.folder)
        self.assertEqual(len(snapshot.catalog), MAX_IMAGES)
        self.assertEqual(snapshot.summary["skipped_count"], 2)
        for path in self.folder.iterdir():
            path.unlink()
        self.image("wide.png", size=(8193, 1))
        self.image("valid.png")
        (self.folder / "bad.jpg").write_bytes(b"not an image")
        with (self.folder / "large.png").open("wb") as handle:
            handle.truncate(MAX_IMAGE_BYTES + 1)
        self.image("wrong.png", format="GIF")
        snapshot = ShowMaterials(self.folder)
        self.assertEqual(len(snapshot.catalog), 1)
        self.assertEqual(snapshot.catalog[0]["title"], "valid")
        self.assertEqual(snapshot.summary["skipped_count"], 4)

    def test_pixel_bound_and_animated_images_are_skipped(self):
        self.image("pixels.png", size=(100, 100))
        with patch("show_materials.MAX_SOURCE_PIXELS", 9000):
            self.assertEqual(ShowMaterials(self.folder).catalog, [])
        frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
        frames[0].save(self.folder / "animated.webp", save_all=True, append_images=frames[1:])
        snapshot = ShowMaterials(self.folder)
        self.assertEqual(len(snapshot.catalog), 1)
        self.assertEqual(snapshot.summary["skipped_count"], 1)

    def test_images_are_normalized_pinned_and_addressed_only_by_stable_content_id(self):
        source = self.image("Original.png", size=(2400, 1200))
        first = ShowMaterials(self.folder)
        item = first.catalog[0]
        self.assertRegex(item["id"], r"^img-[0-9a-f]{16}$")
        self.assertEqual((item["width"], item["height"]), (1920, 960))
        content, mime = first.get(item["id"])
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(content.startswith(b"\xff\xd8"))
        source.rename(self.folder / "Renamed.png")
        second = ShowMaterials(self.folder)
        self.assertEqual(second.catalog[0]["id"], item["id"])
        self.assertEqual(second.get(item["id"]), (content, mime))
        self.image("Renamed.png", color="blue", size=(2400, 1200))
        self.assertNotEqual(ShowMaterials(self.folder).catalog[0]["id"], item["id"])
        self.assertEqual(first.get(item["id"]), (content, mime))
        for invalid in ("../Original.png", str(source), "Original.png", None, []):
            self.assertIsNone(first.get(invalid))

    def test_contact_sheet_is_bounded_and_labels_exact_ids_without_private_notes(self):
        for index in range(12):
            self.image(f"photo{index}.png", color=(index * 10, 25, 60))
        (self.folder / "notes.md").write_text("Private notes never label public images")
        labels = []
        original = __import__("show_materials").ImageDraw.ImageDraw.text

        def capture(draw, xy, text, *args, **kwargs):
            labels.append(text)
            return original(draw, xy, text, *args, **kwargs)

        with patch("show_materials.ImageDraw.ImageDraw.text", new=capture):
            snapshot = ShowMaterials(self.folder)
        self.assertEqual(labels, [item["id"] for item in snapshot.catalog])
        self.assertTrue(snapshot.contact_sheet.startswith(b"\xff\xd8"))
        with Image.open(io.BytesIO(snapshot.contact_sheet)) as sheet:
            self.assertEqual(sheet.format, "JPEG")
            self.assertLessEqual(max(sheet.size), 1024)
        self.assertNotIn("Private", str(snapshot.catalog))

    def test_symlink_images_hidden_images_and_duplicate_content_are_not_loaded(self):
        source = self.image("one.png")
        (self.folder / "two.png").write_bytes(source.read_bytes())
        (self.folder / "alias.png").symlink_to(source)
        self.image(".hidden.png", color="green")
        self.image("title\nwith\u202econtrols.png", color="blue")
        snapshot = ShowMaterials(self.folder)
        self.assertEqual(len(snapshot.catalog), 2)
        self.assertEqual(snapshot.summary["skipped_count"], 3)
        title = next(item["title"] for item in snapshot.catalog if item["title"] != "one")
        self.assertNotIn("\n", title)
        self.assertNotIn("\u202e", title)


if __name__ == "__main__":
    unittest.main()

"""Pinned, bounded local show notes and still images selected by the owner."""

import hashlib
import io
import os
from pathlib import Path
import stat
import unicodedata
import warnings

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError


MAX_IMAGES = 12
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_NOTES = 20000
MAX_RESEARCH_PACKET = 32000
MAX_SOURCE_DIMENSION = 8192
MAX_SOURCE_PIXELS = 16000000
MAX_IMAGE_DIMENSION = 1920
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}


def validate_research_packet(value):
    """Accept a complete owner-supplied text packet without editing its contents."""
    if not isinstance(value, str) or len(value) > MAX_RESEARCH_PACKET:
        raise ValueError("Keep the research packet within 32,000 characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Choose a UTF-8 Markdown or text research packet.") from None
    return value


def research_packet_summary(value):
    return {
        "available": bool(value.strip()),
        "characters": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None,
    }


def _title(name):
    text = "".join(
        char for char in Path(name).stem
        if not unicodedata.category(char).startswith("C") and char not in "<>/\\"
    )
    return " ".join(text.split())[:80] or "Show image"


def _read(directory, name, maximum):
    """Open one regular entry without following a replacement symlink."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("Show material exceeds its file bound.")
        content = source.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError("Show material exceeds its file bound.")
    return content


def _image(content):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as source:
            width, height = source.size
            if (
                source.format not in IMAGE_FORMATS
                or width < 1 or height < 1
                or max(width, height) > MAX_SOURCE_DIMENSION
                or width * height > MAX_SOURCE_PIXELS
                or getattr(source, "n_frames", 1) != 1
            ):
                raise ValueError("Show image exceeds its decoding bounds.")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = oriented.convert("RGB")
            image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=85, optimize=False, progressive=False)
            return out.getvalue(), image.width, image.height


class ShowMaterials:
    """An immutable-content snapshot; paths never become public image identifiers."""

    def __init__(self, folder, *, allow_empty=False, load_research=True):
        self.folder = Path(folder).expanduser()
        if ".." in self.folder.parts:
            raise ValueError("Choose a show-materials folder without parent traversal.")
        self.notes = ""
        self.research_packet = ""
        self.research_packet_name = None
        self.catalog = []
        self.contact_sheet = b""
        self._images = {}
        self._skipped = 0
        try:
            directory = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if allow_empty:
                return
            raise ValueError("The selected show-materials folder does not exist.") from None
        except OSError:
            raise ValueError("Choose a readable local folder, not a symbolic link.") from None
        try:
            self._snapshot(directory, load_research=load_research)
        finally:
            os.close(directory)
        self.contact_sheet = self._make_contact_sheet()

    def _snapshot(self, directory, *, load_research=True):
        try:
            names = sorted(os.listdir(directory), key=lambda name: (name.casefold(), name))
        except OSError:
            raise ValueError("The selected show-materials folder cannot be read.") from None
        packets = [name for name in names if name.casefold() in {"research-packet.md", "research-packet.txt"}]
        if load_research and len(packets) > 1:
            raise ValueError("Keep only one research-packet.md or research-packet.txt in the show folder.")
        if load_research and packets:
            try:
                content = _read(directory, packets[0], MAX_RESEARCH_PACKET * 4 + 3)
                self.research_packet = validate_research_packet(content.decode("utf-8-sig"))
                self.research_packet_name = packets[0]
            except (OSError, ValueError, UnicodeError):
                raise ValueError(
                    "The research packet must be one readable UTF-8 Markdown or text file, "
                    "not a symbolic link, with at most 32,000 characters."
                ) from None
        for name in names:
            if name.casefold() == "readme.md" or name == ".gitkeep" or name in packets:
                continue
            suffix = Path(name).suffix.lower()
            if name.startswith(".") or suffix not in IMAGE_SUFFIXES | {".md", ".txt"}:
                self._skipped += 1
                continue
            try:
                if suffix in {".md", ".txt"}:
                    if len(self.notes) >= MAX_NOTES:
                        self._skipped += 1
                        continue
                    content = _read(directory, name, MAX_NOTES * 4)
                    text = content.decode("utf-8-sig").strip()
                    separator = "\n\n" if self.notes and text else ""
                    self.notes = (self.notes + separator + text)[:MAX_NOTES]
                    continue
                if len(self.catalog) >= MAX_IMAGES:
                    self._skipped += 1
                    continue
                content, width, height = _image(_read(directory, name, MAX_IMAGE_BYTES))
                ident = "img-" + hashlib.sha256(content).hexdigest()[:16]
                if ident in self._images:
                    self._skipped += 1
                    continue
                self._images[ident] = (content, "image/jpeg")
                self.catalog.append({"id": ident, "title": _title(name), "width": width, "height": height})
            except (OSError, ValueError, UnicodeError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                self._skipped += 1

    def _make_contact_sheet(self):
        if not self.catalog:
            return b""
        columns = min(4, len(self.catalog))
        cell_width, cell_height = 256, 222
        rows = (len(self.catalog) + columns - 1) // columns
        sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "#171a20")
        draw = ImageDraw.Draw(sheet)
        for index, item in enumerate(self.catalog):
            left, top = (index % columns) * cell_width, (index // columns) * cell_height
            with Image.open(io.BytesIO(self._images[item["id"]][0])) as source:
                source.thumbnail((240, 182))
                sheet.paste(source, (left + (cell_width - source.width) // 2, top + 8 + (182 - source.height) // 2))
            draw.text((left + 8, top + 195), item["id"], fill="white")
        out = io.BytesIO()
        sheet.save(out, "JPEG", quality=80, optimize=False, progressive=False)
        return out.getvalue()

    def get(self, asset_id):
        if not isinstance(asset_id, str):
            return None
        return self._images.get(asset_id)

    @property
    def summary(self):
        return {
            "image_count": len(self.catalog),
            "notes_available": bool(self.notes),
            "research_available": bool(self.research_packet.strip()),
            "research_characters": len(self.research_packet),
            "research_packet_name": self.research_packet_name,
            "folder": str(self.folder),
            "skipped_count": self._skipped,
        }

"""The shared photo writer (src/utils/photo_store.py, #469).

Every memory and journal photo, uploaded or imported, is decoded, refused over
the pixel limit on its header alone, stored as it came, and given a 400×400
JPEG thumbnail.
"""
from __future__ import annotations

import io
import struct
import warnings
import zlib

import pytest
from fastapi import HTTPException
from PIL import Image, JpegImagePlugin

import api.journal as journal_mod
import api.memories as mem_mod
import src.utils.photo_store as photo_store
from src.utils.photo_store import (
    MAX_PHOTO_PIXELS,
    InvalidPhoto,
    PhotoTooLarge,
    save_photo_files,
    write_photo_files,
)

NAME = "0b8f2c4e-1a2b-4c3d-8e9f-0123456789ab"


def _image_bytes(size=(20, 20), fmt="JPEG", mode="RGB", color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color if mode != "L" else 128).save(buf, fmt)
    return buf.getvalue()


def _png_header_only(width: int, height: int) -> bytes:
    """A PNG whose header declares *width* × *height* but whose pixel data is
    a few bytes: decoding it would fail, so only a header check refuses it as
    too large."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b""))


def test_a_photo_is_stored_as_it_came_with_a_thumbnail(tmp_path):
    raw = _image_bytes((1200, 800))
    full, thumb = write_photo_files(tmp_path / "f", NAME, raw)

    assert full == tmp_path / "f" / f"{NAME}.jpg"
    assert thumb == tmp_path / "f" / f"{NAME}_thumb.jpg"
    assert full.read_bytes() == raw
    with Image.open(thumb) as t:
        assert t.format == "JPEG" and t.mode == "RGB"
        assert t.size == (400, 267)


def test_the_thumbnail_carries_no_comment(tmp_path):
    """Pillow carries a JPEG comment across the re-encode; the thumbnail is
    served to share links, so it must not (issue #430)."""
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (1, 2, 3)).save(buf, "JPEG", comment=b"taken at home")
    full, thumb = write_photo_files(tmp_path, NAME, buf.getvalue())

    assert b"taken at home" in full.read_bytes()
    assert b"taken at home" not in thumb.read_bytes()


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGBA"), ("PNG", "P"), ("PNG", "L"),
                                      ("JPEG", "L"), ("JPEG", "CMYK"), ("GIF", "P")])
def test_every_mode_gets_an_rgb_jpeg_thumbnail(tmp_path, fmt, mode):
    raw = _image_bytes((900, 300), fmt=fmt, mode=mode, color=(10, 20, 30, 40) if mode in ("RGBA", "CMYK") else 5)
    _, thumb = write_photo_files(tmp_path, NAME, raw)
    with Image.open(thumb) as t:
        assert t.format == "JPEG" and t.mode == "RGB" and t.size == (400, 133)


def test_a_jpeg_is_decoded_at_reduced_scale(tmp_path, monkeypatch):
    """The thumbnail asks the JPEG decoder for a draft before decoding, so a
    large photo is never decoded at full resolution."""
    requested = []
    real_draft = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        result = real_draft(self, mode, size)
        requested.append((size, self.size))
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    write_photo_files(tmp_path, NAME, _image_bytes((4000, 3000)))

    assert requested, "the JPEG was decoded without a draft"
    _, decoded_size = requested[0]
    assert decoded_size[0] <= 2000  # half scale or less, not 4000 wide


def _oriented_bytes(orientation, size=(800, 400), fmt="JPEG") -> bytes:
    """A photo whose top-left quarter is red and the rest blue, tagged with
    EXIF *orientation* (None for no EXIF at all)."""
    img = Image.new("RGB", size, (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, size[0] // 2, size[1] // 2))
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif
    buf = io.BytesIO()
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()


def _is_red(px) -> bool:
    return px[0] > 200 and px[1] < 60 and px[2] < 60


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
@pytest.mark.parametrize("orientation,size,red_corner", [
    (3, (400, 200), (399, 199)),   # turned 180 degrees: red goes bottom-right
    (6, (200, 400), (199, 0)),  # turned 90 clockwise: red goes top-right
    (8, (200, 400), (0, 399)),     # turned 90 anticlockwise: red goes bottom-left
])
def test_a_rotated_photo_gets_an_upright_thumbnail(tmp_path, fmt, orientation, size, red_corner):
    _, thumb = write_photo_files(tmp_path, NAME, _oriented_bytes(orientation, fmt=fmt))
    with Image.open(thumb) as t:
        assert t.size == size
        assert _is_red(t.convert("RGB").getpixel(red_corner))
        assert not _is_red(t.convert("RGB").getpixel((size[0] // 2, size[1] // 2)))


def test_a_rotated_jpeg_gets_an_upright_thumbnail(tmp_path):
    _, thumb = write_photo_files(tmp_path, NAME, _oriented_bytes(6))
    with Image.open(thumb) as t:
        assert t.size == (200, 400)


def test_a_rotated_png_gets_an_upright_thumbnail(tmp_path):
    _, thumb = write_photo_files(tmp_path, NAME, _oriented_bytes(6, fmt="PNG"))
    with Image.open(thumb) as t:
        assert t.size == (200, 400)


@pytest.mark.parametrize("orientation", [1, None])
def test_an_upright_jpeg_thumbnail_is_unchanged(tmp_path, orientation):
    _, thumb = write_photo_files(tmp_path, NAME, _oriented_bytes(orientation))
    with Image.open(thumb) as t:
        assert t.size == (400, 200)
        assert _is_red(t.getpixel((0, 0)))
        assert not _is_red(t.getpixel((399, 199)))


def test_a_rotated_jpeg_is_still_decoded_at_reduced_scale(tmp_path, monkeypatch):
    requested = []
    real_draft = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        result = real_draft(self, mode, size)
        requested.append(self.size)
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    _, thumb = write_photo_files(tmp_path, NAME, _oriented_bytes(6, size=(4000, 3000)))

    assert requested and requested[0][0] <= 2000
    with Image.open(thumb) as t:
        assert t.size == (300, 400)


def test_the_stored_original_is_unchanged(tmp_path):
    raw = _oriented_bytes(6)
    full, _ = write_photo_files(tmp_path, NAME, raw)
    assert full.read_bytes() == raw


def test_a_non_image_is_refused_and_nothing_is_written(tmp_path):
    folder = tmp_path / "f"
    with pytest.raises(InvalidPhoto) as exc:
        write_photo_files(folder, NAME, b"not an image at all")
    assert not isinstance(exc.value, PhotoTooLarge)
    assert not folder.exists()


def test_a_truncated_jpeg_is_refused_and_nothing_is_written(tmp_path):
    raw = _image_bytes((800, 600))
    folder = tmp_path / "f"
    with pytest.raises(InvalidPhoto):
        write_photo_files(folder, NAME, raw[: len(raw) // 2])
    assert not folder.exists()


def test_an_image_over_the_pixel_limit_is_refused_on_its_header(tmp_path):
    """12,000 × 10,000 is 120 MP: over the limit, and under Pillow's own error
    threshold, so the refusal is ours. The pixel data is junk, so a decode
    would have failed differently."""
    assert 12_000 * 10_000 > MAX_PHOTO_PIXELS
    folder = tmp_path / "f"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(PhotoTooLarge, match="100 megapixels"):
            write_photo_files(folder, NAME, _png_header_only(12_000, 10_000))
    assert not folder.exists()


def test_the_pixel_limit_holds_even_with_pillows_own_check_off(tmp_path, monkeypatch):
    """src/poster/tile_stitcher.py turns Pillow's limit off process-wide."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    with pytest.raises(PhotoTooLarge):
        write_photo_files(tmp_path, NAME, _png_header_only(20_000, 20_000))


def test_pillows_own_bomb_error_is_reported_as_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(PhotoTooLarge):
        write_photo_files(tmp_path, NAME, _image_bytes((20, 20)))


def test_an_image_at_the_pixel_limit_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(photo_store, "MAX_PHOTO_PIXELS", 20 * 20)
    write_photo_files(tmp_path, NAME, _image_bytes((20, 20)))
    monkeypatch.setattr(photo_store, "MAX_PHOTO_PIXELS", 20 * 20 - 1)
    with pytest.raises(PhotoTooLarge):
        write_photo_files(tmp_path / "g", NAME, _image_bytes((20, 20)))


def test_a_name_that_is_not_a_photo_name_writes_nothing(tmp_path):
    with pytest.raises(ValueError):
        write_photo_files(tmp_path, "../escape", _image_bytes())
    assert list(tmp_path.rglob("*.jpg")) == []


def test_save_records_both_files_for_the_user(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(photo_store, "record_written", lambda uid, *paths: recorded.append((uid, paths)))
    save_photo_files("7", tmp_path, NAME, _image_bytes())
    assert recorded == [("7", (tmp_path / f"{NAME}.jpg", tmp_path / f"{NAME}_thumb.jpg"))]


@pytest.mark.parametrize("module,kind", [(mem_mod, "memories"), (journal_mod, "journal")])
def test_uploads_answer_422_over_the_pixel_limit(tmp_path, monkeypatch, module, kind):
    """The pixel limit applies to ordinary uploads, with their existing 422."""
    monkeypatch.setattr(module, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(photo_store, "MAX_PHOTO_PIXELS", 100)
    with pytest.raises(HTTPException) as exc:
        module._save_photo_files("1", 5, NAME, _image_bytes((20, 20)))
    assert exc.value.status_code == 422
    assert "too large" in exc.value.detail
    assert not (tmp_path / "users" / "1" / kind / "5").exists()


@pytest.mark.parametrize("module,kind", [(mem_mod, "memories"), (journal_mod, "journal")])
def test_uploads_still_store_under_the_same_folder(tmp_path, monkeypatch, module, kind):
    monkeypatch.setattr(module, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(photo_store, "record_written", lambda *a: None)
    module._save_photo_files("1", 5, NAME, _image_bytes())
    folder = tmp_path / "users" / "1" / kind / "5"
    assert sorted(p.name for p in folder.iterdir()) == [f"{NAME}.jpg", f"{NAME}_thumb.jpg"]

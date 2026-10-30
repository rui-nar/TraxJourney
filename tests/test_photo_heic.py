"""HEIC and HEIF photos are stored as upright JPEGs (src/utils/photo_store.py,
#33, D4), and a stored photo's media type is named by its first bytes.

Every other format is stored byte for byte as it came, as before. The HEIC
fixtures are encoded here with pillow-heif, which writes an EXIF orientation
as the container's rotation, the way a phone does.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageCms
from pillow_heif.as_plugin import HeifImageFile

import src.utils.photo_store as photo_store
from src.utils.photo_store import (
    InvalidPhoto,
    PhotoTooLarge,
    photo_media_type,
    write_photo_files,
)

NAME = "0b8f2c4e-1a2b-4c3d-8e9f-0123456789ab"
TAKEN = "2024:05:06 07:08:09"
ORIENTATION = 0x0112
EXIF_IFD = 0x8769
DATE_TIME_ORIGINAL = 0x9003


def _quartered(size) -> Image.Image:
    """An image whose top-left quarter is red and the rest blue."""
    img = Image.new("RGB", size, (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, size[0] // 2, size[1] // 2))
    return img


def _exif(orientation=None, taken=TAKEN) -> Image.Exif:
    exif = Image.Exif()
    if orientation is not None:
        exif[ORIENTATION] = orientation
    exif.get_ifd(EXIF_IFD)[DATE_TIME_ORIGINAL] = taken
    return exif


def _heic(img: Image.Image, **kwargs) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "HEIF", quality=90, **kwargs)
    return buf.getvalue()


def _is_red(px) -> bool:
    return px[0] > 200 and px[1] < 60 and px[2] < 60


def test_a_heic_is_stored_as_a_jpeg_that_keeps_its_exif_date(tmp_path):
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    raw = _heic(_quartered((160, 120)), exif=_exif().tobytes(), icc_profile=srgb)
    assert raw[4:12] == b"ftypheic"

    full, thumb = write_photo_files(tmp_path, NAME, raw)

    with Image.open(full) as stored:
        assert stored.format == "JPEG" and stored.mode == "RGB"
        assert stored.size == (160, 120)
        exif = stored.getexif()
        assert exif.get_ifd(EXIF_IFD)[DATE_TIME_ORIGINAL] == TAKEN
        assert exif.get(ORIENTATION, 1) == 1
        assert stored.info.get("icc_profile") == srgb
    with Image.open(thumb) as t:
        assert t.format == "JPEG" and t.size == (160, 120)


def test_the_jpeg_is_encoded_at_quality_92(tmp_path, monkeypatch):
    saved = []
    real_save = Image.Image.save

    def spy(self, fp, format=None, **params):
        saved.append((format, params.get("quality")))
        return real_save(self, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", spy)
    write_photo_files(tmp_path, NAME, _heic(_quartered((64, 48))))

    assert ("JPEG", 92) in saved


def test_a_heif_with_the_generic_brand_is_converted_too(tmp_path):
    raw = _heic(_quartered((64, 48)))
    raw = raw[:8] + b"mif1" + raw[12:]

    full, _ = write_photo_files(tmp_path, NAME, raw)

    with Image.open(full) as stored:
        assert stored.format == "JPEG"


def test_a_rotated_heic_is_stored_upright_with_an_upright_thumbnail(tmp_path):
    # Landscape pixels with orientation 6 (turn 90 degrees clockwise): the
    # photo is portrait, its red quarter top-right.
    raw = _heic(_quartered((160, 80)), exif=_exif(orientation=6).tobytes())

    full, thumb = write_photo_files(tmp_path, NAME, raw)

    with Image.open(full) as stored:
        assert stored.size == (80, 160)
        assert stored.getexif().get(ORIENTATION, 1) == 1
        assert _is_red(stored.getpixel((60, 20)))
        assert not _is_red(stored.getpixel((20, 20)))
        assert not _is_red(stored.getpixel((40, 120)))
    with Image.open(thumb) as t:
        assert t.size == (80, 160)
        assert _is_red(t.getpixel((60, 20)))
        assert not _is_red(t.getpixel((20, 20)))


def test_a_heic_over_the_pixel_cap_is_refused_before_decoding(tmp_path, monkeypatch):
    raw = _heic(_quartered((64, 64)))
    monkeypatch.setattr(photo_store, "MAX_PHOTO_PIXELS", 64 * 64 - 1)

    def no_decode(self):
        raise AssertionError("the HEIC was decoded")

    monkeypatch.setattr(HeifImageFile, "load", no_decode)

    with pytest.raises(PhotoTooLarge):
        write_photo_files(tmp_path / "f", NAME, raw)
    assert not (tmp_path / "f").exists()


def test_a_heic_that_cannot_be_decoded_is_refused(tmp_path):
    raw = _heic(_quartered((64, 64)))
    # The container still opens, but the coded image data is all zeros.
    data = raw.index(b"mdat") + 4
    corrupt = raw[:data] + b"\x00" * (len(raw) - data)
    with Image.open(io.BytesIO(corrupt)) as opened:
        assert opened.format == "HEIF"

    with pytest.raises(InvalidPhoto):
        write_photo_files(tmp_path / "f", NAME, corrupt)
    assert not (tmp_path / "f").exists()


def test_a_jpeg_is_stored_byte_for_byte_as_before(tmp_path):
    buf = io.BytesIO()
    _quartered((160, 80)).save(buf, "JPEG", exif=_exif(orientation=6))
    raw = buf.getvalue()

    full, thumb = write_photo_files(tmp_path, NAME, raw)

    assert full.read_bytes() == raw
    with Image.open(thumb) as t:
        assert t.size == (80, 160)  # the thumbnail alone is turned (#511)


def test_an_avif_is_still_stored_as_it_came(tmp_path):
    """Pillow's AVIF plugin keeps priority over the HEIF opener, which has no
    AV1 decoder, even for an AVIF using the generic brand."""
    buf = io.BytesIO()
    _quartered((64, 48)).save(buf, "AVIF")
    raw = buf.getvalue()
    raw = raw[:8] + b"mif1" + raw[12:]

    full, _ = write_photo_files(tmp_path, NAME, raw)

    assert full.read_bytes() == raw


@pytest.mark.parametrize("fmt,media_type", [
    ("JPEG", "image/jpeg"),
    ("PNG", "image/png"),
    ("WEBP", "image/webp"),
    ("GIF", "image/gif"),
])
def test_photo_media_type_names_each_signature(tmp_path, fmt, media_type):
    path = tmp_path / f"{NAME}.jpg"
    _quartered((20, 20)).save(path, fmt)

    assert photo_media_type(path) == media_type


@pytest.mark.parametrize("content", [b"\x00" * 16, b"not a photo", b""])
def test_photo_media_type_defaults_to_jpeg(tmp_path, content):
    path = tmp_path / f"{NAME}.jpg"
    path.write_bytes(content)

    assert photo_media_type(path) == "image/jpeg"

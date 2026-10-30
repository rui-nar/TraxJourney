"""HEIC and HEIF photos are stored as upright JPEGs (src/utils/photo_store.py,
#33, D4), and a stored photo's media type is named by its first bytes.

Every other format is stored byte for byte as it came, as before. The HEIC
fixtures are encoded here with pillow-heif, which writes an EXIF orientation
as the container's rotation, the way a phone does.

A HEIC or HEIF photo is capped at HEIF_MAX_PIXELS on the coded size its boxes
declare, not the cropped size Pillow reports, and one process decodes one
photo at a time, whatever its format.
"""
from __future__ import annotations

import io
import random
import struct
import threading
import time
import zlib

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


def _no_decode(monkeypatch):
    def no_decode(self):
        raise AssertionError("the HEIC was decoded")

    monkeypatch.setattr(HeifImageFile, "load", no_decode)


def _ispe_sizes(raw: bytes) -> list:
    """Every (width, height) an 'ispe' box in *raw* declares."""
    sizes, at = [], raw.find(b"ispe")
    while at != -1:
        sizes.append(struct.unpack(">II", raw[at + 8:at + 16]))
        at = raw.find(b"ispe", at + 4)
    return sizes


def _with_ispe(raw: bytes, width: int, height: int) -> bytes:
    """*raw* with its one 'ispe' box declaring *width* × *height*."""
    at = raw.index(b"ispe")
    return raw[:at + 8] + struct.pack(">II", width, height) + raw[at + 16:]


def test_a_heic_over_the_pixel_cap_is_refused_before_decoding(tmp_path, monkeypatch):
    raw = _heic(_quartered((64, 64)))
    monkeypatch.setattr(photo_store, "HEIF_MAX_PIXELS", 64 * 64 - 1)
    _no_decode(monkeypatch)

    with pytest.raises(PhotoTooLarge):
        write_photo_files(tmp_path / "f", NAME, raw)
    assert not (tmp_path / "f").exists()


def test_the_heif_cap_is_on_the_coded_size_not_the_cropped_one(tmp_path, monkeypatch):
    """The encoder pads 256×8 to a larger coded image and crops it back with a
    'clap' box: Pillow reports 256×8, libheif would decode the coded size."""
    raw = _heic(_quartered((256, 8)))
    with Image.open(io.BytesIO(raw)) as opened:
        assert opened.size == (256, 8)
    (coded,) = _ispe_sizes(raw)
    assert coded[0] * coded[1] > 256 * 8
    monkeypatch.setattr(photo_store, "HEIF_MAX_PIXELS", 256 * 8)
    _no_decode(monkeypatch)

    with pytest.raises(PhotoTooLarge):
        write_photo_files(tmp_path / "f", NAME, raw)
    assert not (tmp_path / "f").exists()


def test_a_heic_declaring_60_megapixels_is_refused_before_decoding(tmp_path, monkeypatch):
    raw = _with_ispe(_heic(_quartered((64, 64))), 10_000, 6_000)
    _no_decode(monkeypatch)

    with pytest.raises(PhotoTooLarge, match="50 megapixels"):
        write_photo_files(tmp_path / "f", NAME, raw)
    assert not (tmp_path / "f").exists()


def test_a_heic_declaring_40_megapixels_passes_the_size_check(monkeypatch):
    """Only the check: the pixels behind the patched size are 64×64, so the
    decode itself is not run."""
    raw = _with_ispe(_heic(_quartered((64, 64))), 8_000, 5_000)
    _no_decode(monkeypatch)

    img = photo_store._open(raw)
    assert img.format == "HEIF" and img.size == (8_000, 5_000)


# ── HEIF boxes crafted by hand: grids, overlays, malformed files ──────────────

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full_box(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    return _box(kind, bytes([version, 0, 0, 0]) + payload)


def _heif(items: dict, ispes: list, links: dict, item_data: dict = None) -> bytes:
    """A HEIF file's boxes: *items* is id → item type, *ispes* the sizes of
    the 'ispe' properties, *links* id → 1-based property indexes, and
    *item_data* id → bytes stored in 'idat'. No coded image: only the size
    check can read it."""
    item_data = item_data or {}
    iinf = _full_box(b"iinf", struct.pack(">H", len(items)) + b"".join(
        _full_box(b"infe", struct.pack(">HH", item_id, 0) + kind + b"\x00", version=2)
        for item_id, kind in items.items()))
    idat, entries = b"", b""
    for item_id, data in item_data.items():
        # version 1: 4-byte offsets and lengths, construction method 1 (idat)
        entries += struct.pack(">HHHHII", item_id, 1, 0, 1, len(idat), len(data))
        idat += data
    iloc = _full_box(b"iloc", bytes([0x44, 0x00]) + struct.pack(">H", len(item_data)) + entries,
                     version=1)
    ipco = _box(b"ipco", b"".join(_full_box(b"ispe", struct.pack(">II", w, h)) for w, h in ispes))
    ipma = _full_box(b"ipma", struct.pack(">I", len(links)) + b"".join(
        struct.pack(">HB", item_id, len(indexes)) + bytes(indexes)
        for item_id, indexes in links.items()))
    meta = _full_box(b"meta", iinf + iloc + _box(b"iprp", ipco + ipma) + _box(b"idat", idat))
    return _box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic") + meta


def _grid(width: int, height: int) -> bytes:
    return bytes([0, 0, 1, 1]) + struct.pack(">HH", width, height)


def _overlay(width: int, height: int) -> bytes:
    return bytes([0, 1]) + b"\x00" * 8 + struct.pack(">II", width, height)


def _tiled(canvas: bytes, kind: bytes = b"grid") -> bytes:
    """A grid or overlay item 1 of four 512×512 tiles, declaring 1000×1000
    in its 'ispe' and *canvas* in its own data."""
    items = {1: kind, 2: b"hvc1", 3: b"hvc1", 4: b"hvc1", 5: b"hvc1"}
    links = {1: [1], 2: [2], 3: [2], 4: [2], 5: [2]}
    return _heif(items, [(1000, 1000), (512, 512)], links, {1: canvas})


def test_a_grid_within_the_cap_passes():
    photo_store._check_heif_size(_tiled(_grid(5_000, 5_000)))


def test_a_grid_whose_header_declares_more_than_its_ispe_is_refused():
    with pytest.raises(PhotoTooLarge):
        photo_store._check_heif_size(_tiled(_grid(10_000, 6_000)))


def test_an_overlay_whose_canvas_is_over_the_cap_is_refused():
    photo_store._check_heif_size(_tiled(_overlay(5_000, 5_000), b"iovl"))
    with pytest.raises(PhotoTooLarge):
        photo_store._check_heif_size(_tiled(_overlay(10_000, 6_000), b"iovl"))


def test_a_tile_over_the_cap_is_refused():
    raw = _heif({1: b"grid", 2: b"hvc1"}, [(1000, 1000), (10_000, 6_000)],
                {1: [1], 2: [2]}, {1: _grid(1000, 1000)})
    with pytest.raises(PhotoTooLarge):
        photo_store._check_heif_size(raw)


@pytest.mark.parametrize("raw", [
    pytest.param(_heif({1: b"hvc1"}, [], {}), id="no ispe at all"),
    pytest.param(_heif({1: b"hvc1", 2: b"hvc1"}, [(64, 64)], {1: [1]}), id="an image without ispe"),
    pytest.param(_heif({1: b"hvc1"}, [(64, 64)], {1: [9]}), id="a property index out of range"),
    pytest.param(_heif({1: b"grid"}, [(64, 64)], {1: [1]}), id="a grid without data"),
    pytest.param(_tiled(b"\x00\x00\x01"), id="a grid header cut short"),
    pytest.param(_box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic"), id="no meta"),
    pytest.param(_box(b"ftyp", b"heic") + struct.pack(">I", 4000) + b"meta", id="a box past the end"),
    pytest.param(_box(b"ftyp", b"heic") + struct.pack(">I", 4) + b"meta", id="a box smaller than its header"),
])
def test_unreadable_heif_boxes_are_refused(raw):
    with pytest.raises(InvalidPhoto) as refused:
        photo_store._check_heif_size(raw)
    assert not isinstance(refused.value, PhotoTooLarge)


def test_damaged_heif_boxes_never_raise_anything_but_invalid_photo():
    """Every truncation and a sample of byte changes of a real HEIC: the check
    passes or refuses the file, and never fails any other way (a 500)."""
    raw = _heic(_quartered((64, 64)))
    meta_end = raw.index(b"mdat")
    damaged = [raw[:cut] for cut in range(meta_end)]
    rng = random.Random(33)
    for _ in range(2000):
        at = rng.randrange(meta_end)
        damaged.append(raw[:at] + bytes([rng.randrange(256)]) + raw[at + 1:])
    for candidate in damaged:
        try:
            photo_store._check_heif_size(candidate)
        except InvalidPhoto:
            pass


def test_a_heif_libheif_cannot_read_is_refused_on_write(tmp_path):
    raw = _heic(_quartered((64, 64)))
    meta = raw.index(b"meta") - 4
    broken = raw[:meta] + struct.pack(">I", 9) + raw[meta + 4:]

    with pytest.raises(InvalidPhoto):
        write_photo_files(tmp_path / "f", NAME, broken)
    assert not (tmp_path / "f").exists()


# ── Other formats keep their own cap ──────────────────────────────────────────

def _png_header_only(width: int, height: int) -> bytes:
    """A PNG declaring *width* × *height* with almost no pixel data."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b""))


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_a_png_keeps_the_100_megapixel_cap():
    # 60 MP is over the HEIF cap but not over the one every other format has.
    assert photo_store._open(_png_header_only(10_000, 6_000)).size == (10_000, 6_000)
    with pytest.raises(PhotoTooLarge, match="100 megapixels"):
        photo_store._open(_png_header_only(10_001, 10_000))


def test_a_jpeg_keeps_the_100_megapixel_cap(tmp_path, monkeypatch):
    buf = io.BytesIO()
    _quartered((64, 64)).save(buf, "JPEG")
    monkeypatch.setattr(photo_store, "HEIF_MAX_PIXELS", 1)
    write_photo_files(tmp_path, NAME, buf.getvalue())  # the HEIF cap does not apply
    monkeypatch.setattr(photo_store, "MAX_PHOTO_PIXELS", 64 * 64 - 1)
    with pytest.raises(PhotoTooLarge):
        write_photo_files(tmp_path / "f", NAME, buf.getvalue())


# ── One decode at a time ──────────────────────────────────────────────────────

def test_concurrent_writes_never_decode_at_once(tmp_path, monkeypatch):
    active, peak, guard = [0], [0], threading.Lock()

    def counted(real):
        def run(img):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                time.sleep(0.05)  # widens the window two decodes would share
                return real(img)
            finally:
                with guard:
                    active[0] -= 1
        return run

    monkeypatch.setattr(photo_store, "_thumbnail_of", counted(photo_store._thumbnail_of))
    monkeypatch.setattr(photo_store, "_heif_to_jpeg", counted(photo_store._heif_to_jpeg))

    def encoded(fmt):
        buf = io.BytesIO()
        _quartered((64, 48)).save(buf, fmt)
        return buf.getvalue()

    photos = [encoded(fmt) for fmt in ("JPEG", "PNG", "HEIF", "JPEG", "HEIF", "PNG")]
    errors = []

    def write(i, raw):
        try:
            write_photo_files(tmp_path / str(i), NAME, raw)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i, raw)) for i, raw in enumerate(photos)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert peak[0] == 1


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

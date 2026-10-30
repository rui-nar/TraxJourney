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

import hashlib
import io
import logging
import random
import signal
import struct
import subprocess
import sys
import threading
import time
import types
import zlib

import pytest

try:
    import resource
except ImportError:  # Windows
    resource = None
from PIL import Image, ImageCms
import pillow_heif
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


def test_the_jpeg_is_encoded_at_quality_92(tmp_path):
    full, _ = write_photo_files(tmp_path, NAME, _heic(_quartered((64, 48))))

    reference = io.BytesIO()
    _quartered((64, 48)).save(reference, "JPEG", quality=92)
    with Image.open(full) as stored, Image.open(reference) as at_92:
        assert stored.quantization == at_92.quantization


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
    """Fail the test if the HEIC is decoded, here or in the decoding process."""
    def no_decode(*args):
        raise AssertionError("the HEIC was decoded")

    monkeypatch.setattr(HeifImageFile, "load", no_decode)
    monkeypatch.setattr(photo_store, "_heif_to_jpeg", no_decode)


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

    with pytest.raises(PhotoTooLarge, match="51 megapixels"):
        write_photo_files(tmp_path / "f", NAME, raw)
    assert not (tmp_path / "f").exists()


@pytest.mark.parametrize("width,height", [(8_000, 5_000), (8_500, 6_000)])
def test_a_heic_declaring_up_to_51_megapixels_passes_the_size_check(monkeypatch, width, height):
    """Only the check: the pixels behind the patched size are 64×64, so the
    decode itself is not run. 8500×6000 is exactly 51 MP."""
    raw = _with_ispe(_heic(_quartered((64, 64))), width, height)
    _no_decode(monkeypatch)

    img = photo_store._open(raw)
    assert img.format == "HEIF" and img.size == (width, height)


def test_a_heic_declaring_one_row_over_51_megapixels_is_refused(monkeypatch):
    raw = _with_ispe(_heic(_quartered((64, 64))), 8_500, 6_001)
    _no_decode(monkeypatch)

    with pytest.raises(PhotoTooLarge, match="51 megapixels"):
        photo_store._open(raw)


# ── Deeper or less subsampled coded images: lower caps ────────────────────────

def _heic_format(size, chroma: int, bits: int) -> bytes:
    """An honest HEIC of *size* at *chroma* (420, 422, 444) and *bits*,
    encoded from a 16-bit source when deeper than 8 bits."""
    img = _quartered(size)
    if bits == 8:
        heif = pillow_heif.from_pillow(img)
    else:
        b8 = img.tobytes()
        b16 = bytearray(2 * len(b8))
        b16[0::2] = b8
        b16[1::2] = b8
        heif = pillow_heif.from_bytes(mode="RGB;16", size=size, data=bytes(b16))
    buf = io.BytesIO()
    heif.save(buf, quality=90, chroma=chroma)
    return buf.getvalue()


@pytest.mark.parametrize("chroma", [420, 422, 444])
def test_a_10_bit_heif_converts_to_an_8_bit_jpeg(tmp_path, chroma):
    raw = _heic_format((64, 48), chroma, 10)
    assert pillow_heif.open_heif(io.BytesIO(raw), convert_hdr_to_8bit=False).info["bit_depth"] == 10

    full, thumb = write_photo_files(tmp_path, NAME, raw)

    with Image.open(full) as stored:
        assert stored.format == "JPEG" and stored.mode == "RGB" and stored.size == (64, 48)
        assert _is_red(stored.getpixel((10, 10))) and not _is_red(stored.getpixel((50, 40)))


@pytest.mark.parametrize("chroma,bits,cap", [
    (422, 8, 44_000_000),
    (444, 8, 40_000_000),
    (420, 10, 47_000_000),
    (422, 10, 31_000_000),
    (444, 10, 28_000_000),
])
def test_a_costlier_format_has_its_own_cap(monkeypatch, chroma, bits, cap):
    """At the cap the file passes; one row more is refused, in the 51 MP
    refusal's words; an 8-bit 4:2:0 file of that size is unaffected."""
    _no_decode(monkeypatch)
    raw = _heic_format((64, 64), chroma, bits)
    width = 4_000
    assert cap % width == 0

    photo_store._open(_with_ispe(raw, width, cap // width))
    with pytest.raises(PhotoTooLarge, match=f"it is larger than {cap // 1_000_000} megapixels"):
        photo_store._open(_with_ispe(raw, width, cap // width + 1))
    plain = _heic_format((64, 64), 420, 8)
    photo_store._open(_with_ispe(plain, width, cap // width + 1))


def test_a_grid_canvas_takes_the_cap_of_its_tiles_format():
    """A 36 MP grid of 8-bit 4:2:0 tiles passes; of 10-bit 4:2:2 tiles, not."""
    raw = _heif({1: b"grid", 2: b"hvc1", 3: b"hvc1"}, [(6_000, 6_000), (512, 512)],
                {1: [1], 2: [2], 3: [2]}, {1: _grid(6_000, 6_000)}, hevc=_hvcc())
    photo_store._check_heif_size(raw)  # 36 MP, 8-bit 4:2:0
    deep = raw.replace(_hvcc(), _hvcc(chroma=2, bits=10))
    with pytest.raises(PhotoTooLarge, match="31 megapixels"):
        photo_store._check_heif_size(deep)


def test_a_coded_image_larger_than_its_ispe_is_refused(tmp_path):
    """U4-R2-1: the size check reads 'ispe', libde265 sizes its frame from the
    HEVC bitstream. libheif bounds the decoder to the 'ispe' size, so a file
    whose 'ispe' understates its coded image fails to decode, in the child."""
    raw = _with_ispe(_heic(_quartered((512, 512))), 64, 64)
    with Image.open(io.BytesIO(raw)) as opened:
        assert opened.size == (64, 64)

    with pytest.raises(InvalidPhoto):
        write_photo_files(tmp_path / "f", NAME, raw)
    assert not (tmp_path / "f").exists()


# ── The decoding process ──────────────────────────────────────────────────────

def _completed(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return run


@pytest.mark.parametrize("outcome", [
    pytest.param(_completed(1, stderr=b"MemoryError"), id="an exception"),
    pytest.param(_completed(-9, stderr=b""), id="killed by a signal"),
    pytest.param(_completed(0), id="no output"),
])
def test_a_failed_decoding_process_refuses_the_photo(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(photo_store.subprocess, "run", outcome)

    with pytest.raises(InvalidPhoto):
        write_photo_files(tmp_path / "f", NAME, _heic(_quartered((64, 64))))
    assert not (tmp_path / "f").exists()


def test_a_decoding_process_that_overruns_its_time_refuses_the_photo(tmp_path, monkeypatch):
    # Starting Python alone takes longer than this.
    monkeypatch.setattr(photo_store, "HEIF_CHILD_TIMEOUT", 0.001)

    with pytest.raises(InvalidPhoto):
        write_photo_files(tmp_path / "f", NAME, _heic(_quartered((64, 64))))
    assert not (tmp_path / "f").exists()


def test_the_decoding_process_gets_no_secret(tmp_path, monkeypatch):
    """It reads untrusted bytes with a C decoder: the API's environment, which
    holds its secrets, is not handed to it. It still starts and converts."""
    monkeypatch.setenv("TRAXJOURNEY_TEST_SECRET", "do-not-leak")
    envs = []
    real_run = subprocess.run

    def spy(command, **kwargs):
        envs.append(kwargs.get("env"))
        return real_run(command, **kwargs)

    monkeypatch.setattr(photo_store.subprocess, "run", spy)
    full, _ = write_photo_files(tmp_path, NAME, _heic(_quartered((64, 64))))

    (env,) = envs
    assert env is not None and "TRAXJOURNEY_TEST_SECRET" not in env
    assert set(env) <= {"PATH", "SYSTEMROOT"}
    with Image.open(full) as stored:
        assert stored.format == "JPEG"


@pytest.mark.parametrize("crash", ["SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE"])
def test_a_decoder_crash_is_logged_as_an_error_with_the_photo_hash(tmp_path, monkeypatch, caplog, crash):
    if not hasattr(signal, crash):
        pytest.skip(f"{crash} does not exist on this platform")
    raw = _heic(_quartered((64, 64)))
    monkeypatch.setattr(photo_store.subprocess, "run", _completed(-getattr(signal, crash)))

    with caplog.at_level(logging.WARNING, logger=photo_store.__name__):
        with pytest.raises(InvalidPhoto):
            write_photo_files(tmp_path / "f", NAME, raw)

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [f"HEIF decoder crashed: signal {getattr(signal, crash)}, "
                      f"sha256={hashlib.sha256(raw).hexdigest()}, bytes={len(raw)}"]


@pytest.mark.parametrize("outcome", [
    pytest.param(-signal.SIGABRT, id="SIGABRT"),
    pytest.param(1, id="an exception such as MemoryError"),
])
def test_other_decoder_failures_are_not_logged_as_errors(tmp_path, monkeypatch, caplog, outcome):
    monkeypatch.setattr(photo_store.subprocess, "run", _completed(outcome, stderr=b"MemoryError"))

    with caplog.at_level(logging.WARNING, logger=photo_store.__name__):
        with pytest.raises(InvalidPhoto):
            write_photo_files(tmp_path / "f", NAME, _heic(_quartered((64, 64))))

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


def test_the_decoding_process_gets_its_limits(tmp_path, monkeypatch):
    commands = []
    real_run = subprocess.run

    def spy(command, **kwargs):
        commands.append((command, kwargs.get("timeout")))
        return real_run(command, **kwargs)

    monkeypatch.setattr(photo_store.subprocess, "run", spy)
    write_photo_files(tmp_path, NAME, _heic(_quartered((64, 64))))

    (command, timeout), = commands
    assert command[1] == "-I" and command[2].endswith("heif_child.py")
    assert command[3:] == [str(photo_store.HEIF_CHILD_MEMORY), str(photo_store.HEIF_JPEG_QUALITY)]
    assert timeout == photo_store.HEIF_CHILD_TIMEOUT


def test_libheif_decodes_on_one_thread(monkeypatch):
    """U4-R2-2: _DECODE_LOCK cannot see libheif's own tile threads. The API
    process registers the opener with one thread, and so does the child."""
    import pillow_heif
    from src.utils import heif_child

    assert pillow_heif.options.DECODE_THREADS == 1

    registered = []
    monkeypatch.setattr(pillow_heif, "register_heif_opener",
                        lambda **options: registered.append(options))
    # The child's main(), run here without a memory limit ("0").
    out = io.BytesIO()
    monkeypatch.setattr(sys, "argv", ["heif_child.py", "0", "92"])
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(_heic(_quartered((64, 64))))))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=out))
    assert heif_child.main() == 0

    assert registered == [{"decode_threads": 1}]
    with Image.open(io.BytesIO(out.getvalue())) as jpeg:
        assert jpeg.format == "JPEG"


@pytest.mark.skipif(resource is None, reason="the memory limit needs the resource module (Linux)")
def test_a_decode_over_the_memory_limit_ends_the_child_not_the_server(tmp_path, monkeypatch):
    # Measured on Linux: a 64×64 HEIC decodes under 150 MiB, a 24 MP one
    # needs more than 250 MiB.
    monkeypatch.setattr(photo_store, "HEIF_CHILD_MEMORY", 160 * 1024 * 1024)
    small = _heic(_quartered((64, 64)))
    write_photo_files(tmp_path / "small", NAME, small)

    large = io.BytesIO()
    Image.new("RGB", (6000, 4000), (40, 90, 160)).save(large, "HEIF")
    with pytest.raises(InvalidPhoto):
        write_photo_files(tmp_path / "f", NAME, large.getvalue())
    assert not (tmp_path / "f").exists()


# ── HEIF boxes crafted by hand: grids, overlays, malformed files ──────────────

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full_box(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    return _box(kind, bytes([version, 0, 0, 0]) + payload)


def _hvcc(chroma: int = 1, bits: int = 8) -> bytes:
    """An 'hvcC' payload declaring *chroma* (1 4:2:0, 2 4:2:2, 3 4:4:4) and
    *bits*, with no parameter sets."""
    return (bytes([1]) + b"\x00" * 11 + bytes([0, 0xF0, 0, 0xFC, 0xFC | chroma,
                                               0xF8 | (bits - 8), 0xF8 | (bits - 8)])
            + b"\x00\x00" + bytes([0x0F, 0]))


def _heif(items: dict, ispes: list, links: dict, item_data: dict = None,
          hevc: bytes = _hvcc()) -> bytes:
    """A HEIF file's boxes: *items* is id → item type, *ispes* the sizes of
    the 'ispe' properties, *links* id → 1-based property indexes, and
    *item_data* id → bytes stored in 'idat'. Every 'hvc1' item is also linked
    to one 'hvcC' property holding *hevc*, unless it is None. No coded image:
    only the size check can read it."""
    item_data = item_data or {}
    if hevc is not None:
        links = {item_id: indexes + [len(ispes) + 1] if items.get(item_id) == b"hvc1" else indexes
                 for item_id, indexes in links.items()}
        links.update({item_id: [len(ispes) + 1] for item_id, kind in items.items()
                      if kind == b"hvc1" and item_id not in links})
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
    ipco = _box(b"ipco", b"".join(_full_box(b"ispe", struct.pack(">II", w, h)) for w, h in ispes)
                + (_box(b"hvcC", hevc) if hevc is not None else b""))
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
    pytest.param(_heif({1: b"hvc1"}, [(64, 64)], {1: [1]}, hevc=None), id="a coded image without hvcC"),
    pytest.param(_heif({1: b"hvc1"}, [(64, 64)], {1: [1]}, hevc=_hvcc()[:17]), id="an hvcC cut short"),
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

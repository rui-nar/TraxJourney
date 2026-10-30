"""Writing a memory or journal photo to disk: the one path every photo takes.

An upload, a photo fetched from a URL, and a photo read from an imported trip
archive (#469) all go through :func:`write_photo_files`: the image is decoded
before anything is written, the raw bytes are stored as they came, and a
400×400 JPEG thumbnail, turned upright by the original's EXIF orientation, is
generated from them. A HEIC or HEIF photo is the one exception: no browser but
Safari shows it, so it is stored as an upright JPEG instead (#33, D4), made by
src/utils/heif_child.py in a process of its own. Nothing here knows about
HTTP: a caller turns :class:`InvalidPhoto` into its own error.
"""
from __future__ import annotations

import hashlib
import io
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from src.billing.usage import record_written
from src.utils.logging import get_logger
from src.utils.photo_paths import photo_file

_log = get_logger(__name__)

# Pillow's own plugins are loaded first so they keep priority: its AVIF plugin
# accepts the generic "mif1" brand too, and an AVIF must still open as AVIF,
# not reach libheif, which has no AV1 decoder here. This process only opens
# HEIF files to read their boxes; the decode itself runs in heif_child.py,
# which registers the opener the same way. One thread: libheif otherwise
# decodes grid tiles four at a time.
Image.init()
register_heif_opener(decode_threads=1)

#: Bounding box of the generated thumbnail.
THUMB_SIZE = (400, 400)

#: Largest photo, in bytes, that is stored. A phone camera JPEG is a few MB, so
#: 25 MB is generous while still bounding the memory and CPU one photo can take
#: (the whole photo is held in memory to be decoded and written). The same
#: limit as a direct upload (``_MAX_PHOTO_UPLOAD_BYTES`` in api/memories.py and
#: api/journal.py, checked there before the body is read further); the trip
#: archive reader checks this one while inflating an entry.
MAX_PHOTO_BYTES = 25 * 1024 * 1024

#: Largest photo, in pixels, that is decoded. Pillow's own decompression-bomb
#: check is switched off for the whole process (src/poster/tile_stitcher.py
#: needs to open huge map tiles), so without this a small file declaring a vast
#: image would be decoded in full: 100 MP is ~300 MB as RGB, and the API
#: container has 768 MB. 100 MP is above any phone camera (the largest are
#: ~50 MP at full resolution). Checked on the header, before any decode.
MAX_PHOTO_PIXELS = 100_000_000

#: Largest HEIC or HEIF photo, in pixels, that is decoded (#33). Lower than
#: :data:`MAX_PHOTO_PIXELS` because libheif's decoded copy and Pillow's are held
#: at once. Current phones stay under it (a 48 MP iPhone photo is 48.8 MP).
#: Read from the coded size of every image item in the file
#: (:func:`_check_heif_size`), not from the size after cropping, which is all
#: Pillow reports. An early, explained refusal: the hard bound is
#: :data:`HEIF_CHILD_MEMORY`.
HEIF_MAX_PIXELS = 51_000_000

#: Quality of the JPEG a HEIC or HEIF photo is stored as (#33, D4).
HEIF_JPEG_QUALITY = 92

#: Address space, in bytes, of the process that decodes a HEIC or HEIF photo
#: (src/utils/heif_child.py). Measured on Linux (python:3.14-slim, libheif
#: 1.23.6, one decode thread) on honest 51 MP HEICs: the smallest limit that
#: decodes them lies between 460 and 480 MiB (peak resident 427-440 MiB), so
#: 576 MiB leaves 20 % headroom. Beside the API process (~155 MiB resident
#: once started), that still fits the API container's 768 MB.
HEIF_CHILD_MEMORY = 576 * 1024 * 1024

#: Lower pixel caps for coded formats that cost more to decode than 8-bit
#: 4:2:0, keyed by (deeper than 8 bits, chroma format: 1 4:2:0, 2 4:2:2,
#: 3 4:4:4), read from each coded image's 'hvcC' (:func:`_check_heif_size`).
#: 8-bit 4:2:0 (and monochrome) keeps :data:`HEIF_MAX_PIXELS`.
#:
#: Measured like :data:`HEIF_CHILD_MEMORY`, as the smallest address-space limit
#: each honest file decodes under: 8-bit 4:2:0 needs about 74 MiB + 7.8 MiB per
#: MP (472 MiB at 50.8 MP); 8-bit 4:2:2 9.9, 8-bit 4:4:4 10.9, 10-bit 4:2:0
#: 9.4, 10-bit 4:2:2 14.1 and 10-bit 4:4:4 16.0 MiB per MP (from 576, 627,
#: 541, 709 and 795 MiB at 45-51 MP). Each cap keeps the decode near or
#: under 529 MiB, about 10 % below the limit, and was checked at its value:
#: 8-bit 4:2:2 at 45 MP needs 529 MiB, 8-bit 4:4:4 at 41 MP 529, 10-bit 4:2:0
#: at 47 MP 512, 10-bit 4:2:2 at 31 MP 498, 10-bit 4:4:4 at 28 MP 520; the
#: caps below are one MP lower where that measured value was above 524 MiB.
#: Measured on 10-bit files; any depth above 8 bits takes the 10-bit cap.
HEIF_MAX_PIXELS_BY_FORMAT: Dict[Tuple[bool, int], int] = {
    (False, 2): 44_000_000,
    (False, 3): 40_000_000,
    (True, 1): 47_000_000,
    (True, 2): 31_000_000,
    (True, 3): 28_000_000,
}

#: Seconds the HEIC or HEIF decode may take. A 51 MP photo takes about 2 s on
#: the measuring machine; this allows a slower server many times that.
HEIF_CHILD_TIMEOUT = 30

_HEIF_CHILD = Path(__file__).with_name("heif_child.py")

#: Signals a decoder dies of when it has a memory-safety bug, not when it runs
#: out of memory (MemoryError, or SIGABRT from an allocation libheif does not
#: catch): a photo that causes one is logged as an error, with its hash, so it
#: can be found and reported upstream.
_CRASH_SIGNALS = frozenset(getattr(signal, name) for name in ("SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE")
                           if hasattr(signal, name))

#: Held around every full decode, whatever the format, so one process never
#: decodes two photos at once, and runs one HEIC decoding process at a time
#: (#33). File writes and storage accounting happen outside it.
_DECODE_LOCK = threading.Lock()


class InvalidPhoto(ValueError):
    """The bytes are not a photo that can be stored. The message is a clause
    (``"it is not a readable image"``) a caller can put in its own sentence."""


class PhotoTooLarge(InvalidPhoto):
    """The image is over :data:`MAX_PHOTO_PIXELS`, or a HEIC or HEIF one over
    :data:`HEIF_MAX_PIXELS` or its format's cap
    (:data:`HEIF_MAX_PIXELS_BY_FORMAT`)."""


def _too_large(limit: Optional[int] = None) -> PhotoTooLarge:
    limit = MAX_PHOTO_PIXELS if limit is None else limit
    return PhotoTooLarge(f"it is larger than {limit // 1_000_000} megapixels")


# ── The coded size of a HEIC or HEIF file ─────────────────────────────────────
#
# libheif sizes what it decodes from the file's own boxes: a coded image from
# its 'ispe' property (it bounds the HEVC decoder to it), a grid or an overlay
# from the header in its item data. Pillow only sees the size after the 'clap'
# crop, so a file can show 100×100 and decode 30000×30000. The boxes are read
# here, from the bytes already in memory, before libheif decodes anything.

#: Item types libheif decodes as an image. An 'iden' item has no size of its
#: own: it is the image it points to.
_HEIF_IMAGE_ITEMS = frozenset({b"hvc1", b"av01", b"avc1", b"vvc1", b"unci", b"tili",
                               b"grid", b"iovl", b"j2k1", b"jpeg", b"mski"})

#: Most boxes, items or properties read from one list. A 48 MP iPhone photo has
#: a few hundred; this only bounds the work a crafted file can ask for.
_HEIF_MAX_ENTRIES = 10_000


class _Malformed(ValueError):
    """The HEIF boxes cannot be read."""


class _Reader:
    """Big-endian fields of ``data[pos:end]``, never reading past *end*."""

    def __init__(self, data: bytes, pos: int, end: int):
        self.data, self.pos, self.end = data, pos, end

    def uint(self, size: int) -> int:
        if size not in (0, 1, 2, 4, 8) or self.pos + size > self.end:
            raise _Malformed
        value = int.from_bytes(self.data[self.pos:self.pos + size], "big")
        self.pos += size
        return value

    def fourcc(self) -> bytes:
        if self.pos + 4 > self.end:
            raise _Malformed
        self.pos += 4
        return self.data[self.pos - 4:self.pos]

    def full_box(self) -> int:
        """Skip a full box's version and flags; return the version."""
        version = self.uint(1)
        self.uint(1), self.uint(2)
        return version


def _boxes(data: bytes, pos: int, end: int) -> Iterator[Tuple[bytes, int, int]]:
    """The type, payload start and payload end of each box in ``data[pos:end]``."""
    for _ in range(_HEIF_MAX_ENTRIES):
        if pos >= end:
            return
        r = _Reader(data, pos, end)
        size, kind = r.uint(4), r.fourcc()
        if size == 1:
            size = r.uint(8)
        elif size == 0:
            size = end - pos
        if size < r.pos - pos or size > end - pos:
            raise _Malformed
        yield kind, r.pos, pos + size
        pos += size
    raise _Malformed


def _children(data: bytes, pos: int, end: int) -> Dict[bytes, List[Tuple[int, int]]]:
    found: Dict[bytes, List[Tuple[int, int]]] = {}
    for kind, start, stop in _boxes(data, pos, end):
        found.setdefault(kind, []).append((start, stop))
    return found


def _only(boxes: Dict[bytes, List[Tuple[int, int]]], kind: bytes) -> Tuple[int, int]:
    if len(boxes.get(kind, [])) != 1:
        raise _Malformed
    return boxes[kind][0]


def _entry_count(r: _Reader, size: int) -> int:
    count = r.uint(size)
    if count > _HEIF_MAX_ENTRIES:
        raise _Malformed
    return count


def _item_types(raw: bytes, start: int, end: int) -> Dict[int, bytes]:
    """Item id → item type, from the 'iinf' box."""
    r = _Reader(raw, start, end)
    r.uint(2 if r.full_box() == 0 else 4)  # entry count: the boxes are read instead
    types: Dict[int, bytes] = {}
    for kind, s, e in _boxes(raw, r.pos, end):
        if kind != b"infe":
            continue
        entry = _Reader(raw, s, e)
        version = entry.full_box()
        if version < 2:
            continue  # no item type: never an image to libheif
        item_id = entry.uint(2 if version == 2 else 4)
        entry.uint(2)  # protection index
        types[item_id] = entry.fourcc()
    return types


def _hevc_format(raw: bytes, start: int, end: int) -> Tuple[bool, int]:
    """From an 'hvcC' box (ISO/IEC 14496-15): whether the coded image is
    deeper than 8 bits, and its chroma format (0 monochrome, 1 4:2:0, 2 4:2:2,
    3 4:4:4)."""
    r = _Reader(raw, start, end)
    r.uint(1), r.uint(1)  # version, profile
    r.uint(4), r.uint(4), r.uint(2)  # compatibility and constraint flags
    r.uint(1), r.uint(2), r.uint(1)  # level, min spatial segmentation, parallelism
    chroma = r.uint(1) & 0x03
    luma_bits, chroma_bits = (r.uint(1) & 0x07) + 8, (r.uint(1) & 0x07) + 8
    return max(luma_bits, chroma_bits) > 8, chroma


def _properties(raw: bytes, start: int, end: int) -> Tuple[List[Tuple[bytes, object]], Dict[int, List[int]]]:
    """From the 'iprp' box: each property in order, as its type and, for an
    'ispe', its pixel count, for an 'hvcC', its :func:`_hevc_format` (None
    otherwise); and item id → its properties' 1-based indexes."""
    iprp = _children(raw, start, end)
    properties: List[Tuple[bytes, object]] = []
    for kind, s, e in _boxes(raw, *_only(iprp, b"ipco")):
        if kind == b"ispe":
            r = _Reader(raw, s, e)
            r.full_box()
            properties.append((kind, r.uint(4) * r.uint(4)))
        elif kind == b"hvcC":
            properties.append((kind, _hevc_format(raw, s, e)))
        else:
            properties.append((kind, None))
    links: Dict[int, List[int]] = {}
    for s, e in iprp.get(b"ipma", []):
        r = _Reader(raw, s, e)
        version = r.full_box()
        wide = raw[s + 3] & 1  # flags: 15-bit property indexes
        for _ in range(_entry_count(r, 4)):
            item_id = r.uint(2 if version < 1 else 4)
            for _ in range(r.uint(1)):
                links.setdefault(item_id, []).append(r.uint(2) & 0x7FFF if wide else r.uint(1) & 0x7F)
    return properties, links


def _item_heads(raw: bytes, meta: Dict[bytes, List[Tuple[int, int]]], wanted: set) -> Dict[int, bytes]:
    """The first bytes of each item in *wanted*, read through the 'iloc' box:
    from the file itself or from the 'idat' box."""
    r = _Reader(raw, *_only(meta, b"iloc"))
    version = r.full_box()
    if version > 2:
        raise _Malformed
    sizes = r.uint(1)
    offset_size, length_size = sizes >> 4, sizes & 0x0F
    sizes = r.uint(1)
    base_size, index_size = sizes >> 4, (sizes & 0x0F if version else 0)
    heads: Dict[int, bytes] = {}
    for _ in range(_entry_count(r, 2 if version < 2 else 4)):
        item_id = r.uint(2 if version < 2 else 4)
        method = r.uint(2) & 0x0F if version else 0
        r.uint(2)  # data reference index
        base = r.uint(base_size)
        first = None
        for _ in range(_entry_count(r, 2)):
            r.uint(index_size)
            extent = (base + r.uint(offset_size), r.uint(length_size))
            first = first or extent
        if item_id not in wanted or first is None:
            continue
        if method == 0:
            source_start, source_end = 0, len(raw)
        elif method == 1:
            source_start, source_end = _only(meta, b"idat")
        else:
            raise _Malformed
        offset, length = first
        start = source_start + offset
        stop = source_end if length == 0 else start + length
        if stop > source_end:
            raise _Malformed
        heads[item_id] = raw[start:min(stop, start + 64)]
    return heads


def _canvas_pixels(kind: bytes, head: bytes) -> int:
    """The canvas a grid or overlay item's header declares, as libheif reads
    it (libheif/image-items/grid.cc and overlay.cc)."""
    r = _Reader(head, 0, len(head))
    if r.uint(1) != 0:
        raise _Malformed
    field = 4 if r.uint(1) & 1 else 2
    if kind == b"grid":
        r.uint(2)  # rows and columns
    else:
        r.uint(8)  # background colour
    return r.uint(field) * r.uint(field)


def _heif_pixel_limit(formats: set) -> int:
    """The pixel cap of a file whose coded images have these
    :func:`_hevc_format` values: the lowest of theirs, and never above
    :data:`HEIF_MAX_PIXELS`. Monochrome costs no more than 4:2:0."""
    return min([HEIF_MAX_PIXELS] + [HEIF_MAX_PIXELS_BY_FORMAT.get((deep, max(chroma, 1)), HEIF_MAX_PIXELS)
                                    for deep, chroma in formats])


def _check_heif_size(raw: bytes) -> None:
    """Refuse the HEIC or HEIF *raw* when any image libheif could decode from
    it is over the pixel cap its coded format affords
    (:data:`HEIF_MAX_PIXELS_BY_FORMAT`), before anything is decoded: every
    'ispe' property, every grid and overlay canvas, against the costliest
    format of any coded image in the file. A file whose boxes cannot be read,
    with an image item of no 'ispe', or a coded HEVC image of no 'hvcC', is
    refused too rather than decoded blind."""
    try:
        metas = [(s, e) for kind, s, e in _boxes(raw, 0, len(raw)) if kind == b"meta"]
        if len(metas) != 1:
            raise _Malformed
        start, end = metas[0]
        r = _Reader(raw, start, end)
        r.full_box()
        meta = _children(raw, r.pos, end)
        types = _item_types(raw, *_only(meta, b"iinf"))
        properties, links = _properties(raw, *_only(meta, b"iprp"))
        sizes = [value for kind, value in properties if kind == b"ispe"]
        if not sizes:
            raise _Malformed
        largest = max(sizes)
        formats = set()
        for item_id, kind in types.items():
            if kind not in _HEIF_IMAGE_ITEMS:
                continue
            linked = [properties[i - 1] for i in links.get(item_id, []) if 0 < i <= len(properties)]
            if not any(prop == b"ispe" for prop, _ in linked):
                raise _Malformed
            if kind == b"hvc1":
                found = [value for prop, value in linked if prop == b"hvcC"]
                if not found:
                    raise _Malformed
                formats.update(found)
        canvases = {i: k for i, k in types.items() if k in (b"grid", b"iovl")}
        if canvases:
            heads = _item_heads(raw, meta, set(canvases))
            if set(heads) != set(canvases):
                raise _Malformed
            for item_id, kind in canvases.items():
                largest = max(largest, _canvas_pixels(kind, heads[item_id]))
    except (ValueError, IndexError) as exc:
        raise InvalidPhoto("it is not a readable image") from exc
    limit = _heif_pixel_limit(formats)
    if largest > limit:
        raise _too_large(limit)


def _open(raw: bytes) -> Image.Image:
    """Open *raw* without decoding its pixels, refusing it when its header
    declares more than :data:`MAX_PHOTO_PIXELS`, or a HEIC or HEIF one when
    its boxes declare more than :data:`HEIF_MAX_PIXELS`."""
    try:
        img = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError as exc:
        # Pillow's own check, when a test or a process leaves it on, fires
        # inside open() for an image far over its limit: ours is lower.
        raise _too_large() from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidPhoto("it is not a readable image") from exc
    if img.format == "HEIF":
        _check_heif_size(raw)
        return img
    width, height = img.size
    if width * height > MAX_PHOTO_PIXELS:
        raise _too_large()
    return img


def _thumbnail(raw: bytes) -> Image.Image:
    """Decode *raw* into its RGB thumbnail, refusing it before any pixel is
    decoded when its header declares more than :data:`MAX_PHOTO_PIXELS`."""
    img = _open(raw)
    with _DECODE_LOCK:
        if img.format == "HEIF":
            img = _open(_heif_to_jpeg(raw))
        return _thumbnail_of(img)


def _thumbnail_of(img: Image.Image) -> Image.Image:
    """Decode the opened *img*, never a HEIF one, into its RGB thumbnail.
    Called with :data:`_DECODE_LOCK` held."""
    try:
        if img.format == "JPEG" and img.mode in ("RGB", "L"):
            # thumbnail() on an image not yet loaded first asks the JPEG
            # decoder for a reduced scale (draft), so a 48 MP photo is decoded
            # at 1/2 to 1/8 of its size instead of in full. The whole file is
            # still read and checked; only the pixels kept are fewer.
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            # Transposed after the reduced decode, so only the small image is
            # rotated; the stored original keeps its tag (#511).
            img = ImageOps.exif_transpose(img)
            return img if img.mode == "RGB" else img.convert("RGB")
        # Other formats have no reduced decode. Converting first keeps their
        # thumbnails as they always were (a palette or alpha image resampled
        # as RGB); an image already RGB is only loaded, not copied.
        if img.mode == "RGB":
            img.load()
        else:
            img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        return ImageOps.exif_transpose(img)
    except (OSError, ValueError, SyntaxError) as exc:
        raise InvalidPhoto("it is not a readable image") from exc


def _child_env() -> Dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "")}
    if sys.platform == "win32" and "SYSTEMROOT" in os.environ:
        # Python cannot start on Windows (a development machine) without it.
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _heif_to_jpeg(raw: bytes) -> bytes:
    """The upright JPEG stored in place of the HEIC or HEIF *raw*, already
    checked against :data:`HEIF_MAX_PIXELS`, made by src/utils/heif_child.py in
    a process limited to :data:`HEIF_CHILD_MEMORY` (on Windows, a development
    machine, the child runs without a limit). A decode that fails, runs out of
    memory, is killed or overruns :data:`HEIF_CHILD_TIMEOUT` refuses the photo.
    Called with :data:`_DECODE_LOCK` held, so one child runs at a time.

    The child gets no environment but PATH: it reads untrusted bytes with a
    C decoder, and the API's environment holds its secrets.
    """
    command = [sys.executable, "-I", str(_HEIF_CHILD), str(HEIF_CHILD_MEMORY),
               str(HEIF_JPEG_QUALITY)]
    try:
        done = subprocess.run(command, input=raw, capture_output=True, env=_child_env(),
                              timeout=HEIF_CHILD_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        _log.warning("HEIF decode timed out after %ss", HEIF_CHILD_TIMEOUT)
        raise InvalidPhoto("it is not a readable image") from exc
    if -done.returncode in _CRASH_SIGNALS:
        _log.error("HEIF decoder crashed: signal %s, sha256=%s, bytes=%s",
                   -done.returncode, hashlib.sha256(raw).hexdigest(), len(raw))
        raise InvalidPhoto("it is not a readable image")
    if done.returncode != 0 or not done.stdout:
        reason = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        _log.warning("HEIF decode failed: exit %s %s", done.returncode, reason)
        raise InvalidPhoto("it is not a readable image")
    return done.stdout


def photo_media_type(path: Path) -> str:
    """The media type of the stored photo at *path*, named by its first bytes:
    an original is stored as it came, so a ``.jpg`` can hold a PNG, a WebP or
    a GIF. Anything else is served as ``image/jpeg``, as every photo was."""
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def write_photo_files(folder: Path, name: str, raw: bytes) -> Tuple[Path, Path]:
    """Decode *raw*, then write it as ``folder/<name>.jpg`` and its thumbnail
    as ``folder/<name>_thumb.jpg``. Returns both paths. A HEIC or HEIF photo
    is written as the JPEG :func:`_heif_to_jpeg` makes of it, with its
    thumbnail made from that JPEG; any other one byte for byte as it came.

    Raises :class:`InvalidPhoto` before anything is written (the folder is not
    even created) when *raw* is not an image or is over the pixel limit, so a
    refused photo never leaves a full-size file without a thumbnail. No storage
    is recorded: see :func:`save_photo_files`.
    """
    img = _open(raw)
    thumb_img = None
    with _DECODE_LOCK:
        try:
            if img.format == "HEIF":
                img.close()
                raw = _heif_to_jpeg(raw)
                img = _open(raw)
            thumb_img = _thumbnail_of(img)
        finally:
            # The full-size decode is let go before the next one may start.
            if thumb_img is not img:
                img.close()
    folder.mkdir(parents=True, exist_ok=True)
    full = photo_file(folder, name)
    thumb = photo_file(folder, name, "_thumb")
    if full is None or thumb is None:
        raise ValueError(f"not a photo name: {name!r}")
    full.write_bytes(raw)
    # A re-encode drops EXIF, but Pillow does carry a JPEG comment over from
    # img.info, and the thumbnail is served to share links (issue #430).
    thumb_img.info.clear()
    thumb_img.save(str(thumb), "JPEG", quality=85)
    return full, thumb


def save_photo_files(user_id: Union[str, int], folder: Path, name: str, raw: bytes) -> None:
    """:func:`write_photo_files` into *user_id*'s own *folder*, and count the
    files against their storage.

    Storage accounting for quota checks (issue #121) is done here rather than
    at each call site so every path that writes a photo — upload, replace,
    from-url, Polarsteps import — is counted by construction. Attribution
    follows the directory: a memory's photos live under the project OWNER's
    tree, a journal entry's under its author's.
    """
    full, thumb = write_photo_files(folder, name, raw)
    record_written(user_id, full, thumb)

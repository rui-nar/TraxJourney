"""Writing a memory or journal photo to disk: the one path every photo takes.

An upload, a photo fetched from a URL, and a photo read from an imported trip
archive (#469) all go through :func:`write_photo_files`: the image is decoded
before anything is written, the raw bytes are stored as they came, and a
400×400 JPEG thumbnail, turned upright by the original's EXIF orientation, is
generated from them. A HEIC or HEIF photo is the one exception: no browser but
Safari shows it, so it is stored as an upright JPEG instead (#33, D4). Nothing
here knows about HTTP: a caller turns :class:`InvalidPhoto` into its own error.
"""
from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from src.billing.usage import record_written
from src.utils.photo_paths import photo_file

# Pillow's own plugins are loaded first so they keep priority: its AVIF plugin
# accepts the generic "mif1" brand too, and an AVIF must still open as AVIF,
# not reach libheif, which has no AV1 decoder here.
Image.init()
register_heif_opener()

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
#: at once. Current phones stay under it. Read from the coded size of every
#: image item in the file (:func:`_check_heif_size`), not from the size after
#: cropping, which is all Pillow reports.
HEIF_MAX_PIXELS = 50_000_000

#: Quality of the JPEG a HEIC or HEIF photo is stored as (#33, D4).
HEIF_JPEG_QUALITY = 92

#: Held around every full decode, whatever the format, so one process never
#: decodes two photos at once: two 100 MP decodes would not fit in the API
#: container (#33). File writes and storage accounting happen outside it.
_DECODE_LOCK = threading.Lock()


class InvalidPhoto(ValueError):
    """The bytes are not a photo that can be stored. The message is a clause
    (``"it is not a readable image"``) a caller can put in its own sentence."""


class PhotoTooLarge(InvalidPhoto):
    """The image is over :data:`MAX_PHOTO_PIXELS`, or a HEIC or HEIF one over
    :data:`HEIF_MAX_PIXELS`."""


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


def _properties(raw: bytes, start: int, end: int) -> Tuple[List[Optional[int]], Dict[int, List[int]]]:
    """From the 'iprp' box: each property's pixel count when it is an 'ispe'
    (None otherwise), in order, and item id → its properties' 1-based indexes."""
    iprp = _children(raw, start, end)
    sizes: List[Optional[int]] = []
    for kind, s, e in _boxes(raw, *_only(iprp, b"ipco")):
        if kind == b"ispe":
            r = _Reader(raw, s, e)
            r.full_box()
            sizes.append(r.uint(4) * r.uint(4))
        else:
            sizes.append(None)
    links: Dict[int, List[int]] = {}
    for s, e in iprp.get(b"ipma", []):
        r = _Reader(raw, s, e)
        version = r.full_box()
        wide = raw[s + 3] & 1  # flags: 15-bit property indexes
        for _ in range(_entry_count(r, 4)):
            item_id = r.uint(2 if version < 1 else 4)
            for _ in range(r.uint(1)):
                links.setdefault(item_id, []).append(r.uint(2) & 0x7FFF if wide else r.uint(1) & 0x7F)
    return sizes, links


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


def _check_heif_size(raw: bytes) -> None:
    """Refuse the HEIC or HEIF *raw* when any image libheif could decode from
    it is over :data:`HEIF_MAX_PIXELS`, before anything is decoded: every 'ispe'
    property, every grid and overlay canvas. A file whose boxes cannot be read,
    or with an image item of no 'ispe', is refused too rather than decoded
    blind."""
    try:
        metas = [(s, e) for kind, s, e in _boxes(raw, 0, len(raw)) if kind == b"meta"]
        if len(metas) != 1:
            raise _Malformed
        start, end = metas[0]
        r = _Reader(raw, start, end)
        r.full_box()
        meta = _children(raw, r.pos, end)
        types = _item_types(raw, *_only(meta, b"iinf"))
        sizes, links = _properties(raw, *_only(meta, b"iprp"))
        if not any(size is not None for size in sizes):
            raise _Malformed
        largest = max(size for size in sizes if size is not None)
        for item_id, kind in types.items():
            if kind not in _HEIF_IMAGE_ITEMS:
                continue
            indexes = links.get(item_id, [])
            if not any(0 < i <= len(sizes) and sizes[i - 1] is not None for i in indexes):
                raise _Malformed
        canvases = {i: k for i, k in types.items() if k in (b"grid", b"iovl")}
        if canvases:
            heads = _item_heads(raw, meta, set(canvases))
            if set(heads) != set(canvases):
                raise _Malformed
            for item_id, kind in canvases.items():
                largest = max(largest, _canvas_pixels(kind, heads[item_id]))
    except (ValueError, IndexError) as exc:
        raise InvalidPhoto("it is not a readable image") from exc
    if largest > HEIF_MAX_PIXELS:
        raise _too_large(HEIF_MAX_PIXELS)


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
        return _thumbnail_of(img)


def _thumbnail_of(img: Image.Image) -> Image.Image:
    """Decode the opened *img* into its RGB thumbnail. Called with
    :data:`_DECODE_LOCK` held."""
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


def _heif_to_jpeg(img: Image.Image) -> Tuple[bytes, Image.Image]:
    """Encode the HEIC or HEIF *img*, already checked against the pixel
    limit, as the JPEG stored in its place, and return it with its thumbnail.

    The photo is upright as decoded: libheif applies the container's rotation
    and mirror, and pillow-heif resets the EXIF orientation to 1 on open, so
    the EXIF copied here cannot turn it a second time. The EXIF (capture date
    included) and the ICC profile are kept; other metadata is not. Called
    with :data:`_DECODE_LOCK` held.
    """
    exif = img.info.get("exif") or b""
    if exif and not exif.startswith(b"Exif\x00\x00"):
        exif = b"Exif\x00\x00" + exif
    icc_profile = img.info.get("icc_profile")
    try:
        # Converted only when it has to be: a 50 MP copy is another 150 MB.
        if img.mode == "RGB":
            upright = img
            upright.load()
        else:
            upright = img.convert("RGB")
            img.close()  # the decoded original is not kept beside its copy
        buf = io.BytesIO()
        upright.save(buf, "JPEG", quality=HEIF_JPEG_QUALITY, exif=exif,
                     icc_profile=icc_profile)
        # In place, once the full size is encoded: no second full-size copy.
        upright.thumbnail(THUMB_SIZE, Image.LANCZOS)
    except (OSError, ValueError, SyntaxError, RuntimeError, EOFError) as exc:
        # pillow-heif reports a coded image it cannot decode as an EOFError,
        # and a compression format it has no decoder for as a RuntimeError.
        raise InvalidPhoto("it is not a readable image") from exc
    return buf.getvalue(), upright


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
    is written as the JPEG :func:`_heif_to_jpeg` makes of it, any other one
    byte for byte as it came.

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
                raw, thumb_img = _heif_to_jpeg(img)
            else:
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

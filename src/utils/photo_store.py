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
from pathlib import Path
from typing import Tuple, Union

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

#: Quality of the JPEG a HEIC or HEIF photo is stored as (#33, D4).
HEIF_JPEG_QUALITY = 92


class InvalidPhoto(ValueError):
    """The bytes are not a photo that can be stored. The message is a clause
    (``"it is not a readable image"``) a caller can put in its own sentence."""


class PhotoTooLarge(InvalidPhoto):
    """The image is over :data:`MAX_PHOTO_PIXELS`."""


def _too_large() -> PhotoTooLarge:
    return PhotoTooLarge(f"it is larger than {MAX_PHOTO_PIXELS // 1_000_000} megapixels")


def _open(raw: bytes) -> Image.Image:
    """Open *raw* without decoding its pixels, refusing it when its header
    declares more than :data:`MAX_PHOTO_PIXELS`."""
    try:
        img = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError as exc:
        # Pillow's own check, when a test or a process leaves it on, fires
        # inside open() for an image far over its limit: ours is lower.
        raise _too_large() from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidPhoto("it is not a readable image") from exc
    width, height = img.size
    if width * height > MAX_PHOTO_PIXELS:
        raise _too_large()
    return img


def _thumbnail(raw: bytes) -> Image.Image:
    """Decode *raw* into its RGB thumbnail, refusing it before any pixel is
    decoded when its header declares more than :data:`MAX_PHOTO_PIXELS`."""
    return _thumbnail_of(_open(raw))


def _thumbnail_of(img: Image.Image) -> Image.Image:
    """Decode the opened *img* into its RGB thumbnail."""
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
    included) and the ICC profile are kept; other metadata is not.
    """
    try:
        # Converted only when it has to be: a 100 MP copy is another 300 MB.
        upright = img if img.mode == "RGB" else img.convert("RGB")
        upright.load()
        exif = img.info.get("exif") or b""
        if exif and not exif.startswith(b"Exif\x00\x00"):
            exif = b"Exif\x00\x00" + exif
        buf = io.BytesIO()
        upright.save(buf, "JPEG", quality=HEIF_JPEG_QUALITY, exif=exif,
                     icc_profile=img.info.get("icc_profile"))
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
    if img.format == "HEIF":
        raw, thumb_img = _heif_to_jpeg(img)
    else:
        thumb_img = _thumbnail_of(img)
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

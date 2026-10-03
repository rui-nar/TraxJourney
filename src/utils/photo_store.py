"""Writing a memory or journal photo to disk: the one path every photo takes.

An upload, a photo fetched from a URL, and a photo read from an imported trip
archive (#469) all go through :func:`write_photo_files`: the image is decoded
before anything is written, the raw bytes are stored as they came, and a
400×400 JPEG thumbnail is generated from them. Nothing here knows about HTTP:
a caller turns :class:`InvalidPhoto` into its own error.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Tuple, Union

from PIL import Image, UnidentifiedImageError

from src.billing.usage import record_written
from src.utils.photo_paths import photo_file

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


class InvalidPhoto(ValueError):
    """The bytes are not a photo that can be stored. The message is a clause
    (``"it is not a readable image"``) a caller can put in its own sentence."""


class PhotoTooLarge(InvalidPhoto):
    """The image is over :data:`MAX_PHOTO_PIXELS`."""


def _too_large() -> PhotoTooLarge:
    return PhotoTooLarge(f"it is larger than {MAX_PHOTO_PIXELS // 1_000_000} megapixels")


def _thumbnail(raw: bytes) -> Image.Image:
    """Decode *raw* into its RGB thumbnail, refusing it before any pixel is
    decoded when its header declares more than :data:`MAX_PHOTO_PIXELS`."""
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
    try:
        if img.format == "JPEG" and img.mode in ("RGB", "L"):
            # thumbnail() on an image not yet loaded first asks the JPEG
            # decoder for a reduced scale (draft), so a 48 MP photo is decoded
            # at 1/2 to 1/8 of its size instead of in full. The whole file is
            # still read and checked; only the pixels kept are fewer.
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            return img if img.mode == "RGB" else img.convert("RGB")
        # Other formats have no reduced decode. Converting first keeps their
        # thumbnails as they always were (a palette or alpha image resampled
        # as RGB); an image already RGB is only loaded, not copied.
        if img.mode == "RGB":
            img.load()
        else:
            img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        return img
    except (OSError, ValueError, SyntaxError) as exc:
        raise InvalidPhoto("it is not a readable image") from exc


def write_photo_files(folder: Path, name: str, raw: bytes) -> Tuple[Path, Path]:
    """Decode *raw*, then write it as ``folder/<name>.jpg`` and its thumbnail
    as ``folder/<name>_thumb.jpg``. Returns both paths.

    Raises :class:`InvalidPhoto` before anything is written (the folder is not
    even created) when *raw* is not an image or is over the pixel limit, so a
    refused photo never leaves a full-size file without a thumbnail. No storage
    is recorded: see :func:`save_photo_files`.
    """
    thumb_img = _thumbnail(raw)
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

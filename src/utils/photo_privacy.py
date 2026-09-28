"""Metadata-free copies of memory photos for share links (issue #430).

A photo is stored exactly as uploaded, so the owner's file keeps its EXIF:
GPS coordinates, capture time, camera and lens serials, maker notes. A share
link is unauthenticated, and anyone holding one could read from a photo the
exact place it was taken even where the owner chose not to show that place
on the shared map. So what a share link serves is not the original but a
copy with every EXIF/XMP block removed, derived lazily on first serve and
cached next to the original as ``{uuid}_share.jpg`` (the original is
``{uuid}.jpg``). Deleting the photo deletes the copy; the copy is keyed by the
photo's uuid, which a replace re-mints, so it can never go stale.

Orientation is applied to the pixels before the tag is dropped — otherwise a
portrait phone photo would be shown on its side. The ICC profile is kept: it
describes a colour space, not a person, and dropping it shifts colours.

The copy is not the owner's storage: they did not upload it, cannot delete it
on its own, and a viewer's click must not push them over quota. It is
therefore excluded from the per-user counter and from the reconcile walk
(see :func:`is_share_copy`, honoured by :func:`src.admin.storage.dir_size`).
The temporary file a derivation writes before renaming into place carries the
same suffix, so a crash mid-write leaves nothing that would be billed.

Thumbnails are a different case: re-encoded at upload, they never had EXIF,
but Pillow does carry a JPEG comment across a re-encode, and thumbnails
written before this module cleared it may hold one. Those are served through
:func:`strip_jpeg_metadata_segments`, a lossless walk over the JPEG's marker
segments that drops the metadata ones without touching the image data.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from PIL.JpegImagePlugin import get_sampling

_log = logging.getLogger(__name__)

SHARE_COPY_SUFFIX = "_share.jpg"
_TEMP_SUFFIX = ".tmp" + SHARE_COPY_SUFFIX

# A temp file older than this was left by a crashed derivation, not by one in
# flight (a derivation takes seconds), and is safe to remove from under any
# concurrent first serve.
_STALE_TEMP_AGE_SECONDS = 600.0

# A first visit to a photo-heavy shared trip decodes every full-size photo it
# scrolls past. Each 12 MP decode plus its transposed copy is ~70 MB, and sync
# routes run on a 40-thread pool — the same burst that OOM-killed the API on
# thumbnails (see the semaphore in api.memories). Derivations therefore queue
# behind a few slots; a wait of a few hundred ms is a one-off per photo.
_MAX_CONCURRENT_DERIVATIONS = 4
_derive_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_DERIVATIONS)

# Photos already warned about, so a broken file logs once per process rather
# than once per share request. Holds file names only (the uuid), never paths.
_warned: set[str] = set()
_warned_lock = threading.Lock()


class UndecodablePhoto(Exception):
    """The original on disk is not an image Pillow can read."""


def share_copy_path(original: Path) -> Path:
    """``…/{uuid}_share.jpg`` for ``…/{uuid}.jpg``."""
    return original.with_name(original.stem + SHARE_COPY_SUFFIX)


def is_share_copy(filename: str) -> bool:
    """True for a share copy or the temp file of one being derived."""
    return filename.endswith(SHARE_COPY_SUFFIX)


def _temp_files(original: Path):
    return original.parent.glob(f"{original.stem}.*{_TEMP_SUFFIX}")


def remove_share_copy(original: Path) -> None:
    """Delete the copy of *original* and any temp a crashed derivation left."""
    share_copy_path(original).unlink(missing_ok=True)
    for tmp in _temp_files(original):
        tmp.unlink(missing_ok=True)


def strip_private_metadata(raw: bytes) -> bytes:
    """Re-encode *raw* (any format Pillow reads) as a JPEG with no metadata.

    Pure: bytes in, bytes out. EXIF, XMP, comments and PNG text chunks are all
    left behind because the pixels are written into a fresh image and Pillow
    only writes metadata it is explicitly handed. A JPEG source is re-encoded
    with its own quantisation tables and chroma subsampling, so the copy stays
    about the size of the original instead of ballooning at a fixed quality;
    anything else gets a fixed quality.
    """
    with Image.open(io.BytesIO(raw)) as src:
        icc = src.info.get("icc_profile")
        encode: dict = {}
        if src.format == "JPEG" and getattr(src, "quantization", None):
            encode["qtables"] = src.quantization
            sampling = get_sampling(src)
            if sampling >= 0:
                encode["subsampling"] = sampling
        else:
            encode["quality"] = 90
        img = ImageOps.exif_transpose(src)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    # exif_transpose hands back the source EXIF minus the orientation tag in
    # img.info, and Pillow's JPEG writer does take a comment from there.
    img.info.clear()
    if icc:
        encode["icc_profile"] = icc
    out = io.BytesIO()
    img.save(out, "JPEG", **encode)
    return out.getvalue()


_SOI = 0xD8
_SOS = 0xDA
_COM = 0xFE
_APP0 = 0xE0
_APP2 = 0xE2
_APP14 = 0xEE


def strip_jpeg_metadata_segments(raw: bytes) -> bytes:
    """Drop a JPEG's metadata segments without re-encoding it.

    Walks the marker segments up to the start of scan and keeps only what
    a decoder needs: JFIF (APP0), the ICC profile (APP2 with the
    ``ICC_PROFILE`` signature), the Adobe colour-transform hint (APP14 with
    the ``Adobe`` signature) and the tables and frame headers. Every other
    APPn — EXIF and XMP (APP1), IPTC (APP13), MPF and vendor blobs — and any
    COM segment is left out. The entropy-coded data from SOS on is copied
    verbatim, so the pixels are bit-for-bit those of the input.

    Anything that is not a JPEG comes back unchanged. A JPEG whose segments
    do not walk cleanly to the start of scan (a fill byte, a stray marker,
    a truncated header) is passed through from the point the walk stopped,
    with a warning: everything served here is written by Pillow as baseline,
    so that is not expected to happen.
    """
    if len(raw) < 4 or raw[0] != 0xFF or raw[1] != _SOI:
        return raw
    out = bytearray(raw[:2])
    i = 2
    n = len(raw)
    reached_sos = False
    while i + 4 <= n and raw[i] == 0xFF:
        marker = raw[i + 1]
        if marker == _SOS:
            reached_sos = True
            break
        length = int.from_bytes(raw[i + 2:i + 4], "big")
        segment = raw[i:i + 2 + length]
        payload = raw[i + 4:i + 2 + length]
        keep = True
        if marker == _COM:
            keep = False
        elif _APP0 <= marker <= 0xEF:
            keep = (
                marker == _APP0
                or (marker == _APP2 and payload.startswith(b"ICC_PROFILE\0"))
                or (marker == _APP14 and payload.startswith(b"Adobe"))
            )
        if keep:
            out += segment
        i += 2 + length
    if not reached_sos:
        _log.warning(
            "JPEG marker walk stopped before the start of scan at offset %d of %d bytes; "
            "the rest is served as is", i, n,
        )
    out += raw[i:]
    return bytes(out)


def _warn_once(original: Path, exc: Exception) -> None:
    with _warned_lock:
        if original.name in _warned:
            return
        _warned.add(original.name)
    _log.warning("share copy of photo %s could not be derived: %s", original.name, exc)


def ensure_share_copy(original: Path) -> Path:
    """The metadata-free copy of *original*, derived on first call and cached.

    Written to a temporary name and renamed into place so a concurrent reader
    never sees a half-written file; two concurrent first serves both derive
    the same bytes, and the last rename wins harmlessly. A photo deleted
    while its copy was being derived does not leave that copy behind: the
    rename is followed by a check that the original still exists.

    Raises :class:`FileNotFoundError` when the original is gone and
    :class:`UndecodablePhoto` (logged once per photo) when it cannot be read
    as an image. Neither ever falls back to serving the original.
    """
    copy = share_copy_path(original)
    if copy.exists():
        return copy
    with _derive_slots:
        if copy.exists():
            return copy
        raw = original.read_bytes()
        try:
            stripped = strip_private_metadata(raw)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            _warn_once(original, exc)
            raise UndecodablePhoto(original.name) from exc
        tmp = copy.with_name(f"{original.stem}.{uuid.uuid4().hex}{_TEMP_SUFFIX}")
        try:
            tmp.write_bytes(stripped)
            os.replace(tmp, copy)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if not original.exists():
            copy.unlink(missing_ok=True)
            raise FileNotFoundError(str(original))
        _remove_stale_temps(original)
    return copy


def _remove_stale_temps(original: Path) -> None:
    cutoff = time.time() - _STALE_TEMP_AGE_SECONDS
    for tmp in _temp_files(original):
        try:
            if tmp.stat().st_mtime < cutoff:
                tmp.unlink(missing_ok=True)
        except OSError:
            continue

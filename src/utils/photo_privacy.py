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
"""
from __future__ import annotations

import io
import os
import threading
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from PIL.JpegImagePlugin import get_sampling

SHARE_COPY_SUFFIX = "_share.jpg"

# A first visit to a photo-heavy shared trip decodes every full-size photo it
# scrolls past. Each 12 MP decode plus its transposed copy is ~70 MB, and sync
# routes run on a 40-thread pool — the same burst that OOM-killed the API on
# thumbnails (see the semaphore in api.memories). Derivations therefore queue
# behind a few slots; a wait of a few hundred ms is a one-off per photo.
_MAX_CONCURRENT_DERIVATIONS = 4
_derive_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_DERIVATIONS)


def share_copy_path(original: Path) -> Path:
    """``…/{uuid}_share.jpg`` for ``…/{uuid}.jpg``."""
    return original.with_name(original.stem + SHARE_COPY_SUFFIX)


def is_share_copy(filename: str) -> bool:
    return filename.endswith(SHARE_COPY_SUFFIX)


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
    # img.info; Pillow's JPEG writer ignores it, but be explicit anyway.
    img.info.clear()
    if icc:
        encode["icc_profile"] = icc
    out = io.BytesIO()
    img.save(out, "JPEG", **encode)
    return out.getvalue()


def ensure_share_copy(original: Path) -> Path:
    """The metadata-free copy of *original*, derived on first call and cached.

    Written to a temporary name and renamed into place so a concurrent reader
    never sees a half-written file; two concurrent first serves both derive
    the same bytes, and the last rename wins harmlessly.
    """
    copy = share_copy_path(original)
    if copy.exists():
        return copy
    with _derive_slots:
        if copy.exists():
            return copy
        stripped = strip_private_metadata(original.read_bytes())
        tmp = copy.with_name(f"{copy.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(stripped)
        os.replace(tmp, copy)
    return copy

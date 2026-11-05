"""Perceptual hashes for the album photo upgrade (issue #33, decision D7).

Pure functions over bytes and PIL images: no DB, no disk. Both hashes are 64
bits and are computed on an already upright image, so a photo and its
rotated-by-EXIF twin agree. Two photos are compared by the summed Hamming
distance of both hashes (0-128).
"""
from __future__ import annotations

import io
from typing import NamedTuple, Optional

from PIL import Image, ImageOps


class Fingerprint(NamedTuple):
    ahash: int
    dhash: int
    width: int   # upright
    height: int  # upright


def ahash(img: Image.Image) -> int:
    """64-bit average hash of an upright image."""
    px = list(img.convert("L").resize((8, 8), Image.LANCZOS).tobytes())
    mean = sum(px) / 64
    return sum(1 << i for i, p in enumerate(px) if p > mean)


def dhash(img: Image.Image) -> int:
    """64-bit difference hash: each pixel against its right neighbour."""
    px = list(img.convert("L").resize((9, 8), Image.LANCZOS).tobytes())
    bits = [px[r * 9 + c] > px[r * 9 + c + 1] for r in range(8) for c in range(8)]
    return sum(1 << i for i, b in enumerate(bits) if b)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def upright_image(raw: bytes) -> Image.Image:
    """Decode ``raw`` as RGB, turned upright by its EXIF orientation.

    Raises OSError/ValueError (or PIL's decompression-bomb error) when the
    bytes do not decode.
    """
    return ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")


def fingerprint(raw: bytes) -> Optional[Fingerprint]:
    """Hash an image upright; None when the bytes cannot be decoded."""
    try:
        img = upright_image(raw)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None
    return Fingerprint(ahash(img), dhash(img), img.width, img.height)


def to_hex(value: int) -> str:
    """A 64-bit hash as 16 hex characters (SQLite integers are signed)."""
    if not 0 <= value < 1 << 64:
        raise ValueError("hash must fit in 64 bits")
    return f"{value:016x}"


def from_hex(text: str) -> int:
    if len(text) != 16:
        raise ValueError("hash must be 16 hex characters")
    return int(text, 16)

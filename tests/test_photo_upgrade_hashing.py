"""Perceptual hashing for the album photo upgrade (#33, U5)."""
import io
import random

import pytest
from PIL import Image

from src.photo_upgrade import hashing


def _image(seed: int, size=(160, 120)) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", size)
    img.putdata([(rng.randrange(256),) * 3 for _ in range(size[0] * size[1])])
    return img


def _jpeg(img: Image.Image, exif_orientation: int = 1) -> bytes:
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = exif_orientation
    img.save(buf, "JPEG", quality=95, exif=exif)
    return buf.getvalue()


def test_hashes_are_64_bits_near_for_one_image_and_far_for_another():
    img, other = _image(1), _image(2)
    a = hashing.ahash(img)
    assert 0 <= a < 1 << 64 and 0 <= hashing.dhash(img) < 1 << 64
    assert hashing.hamming(hashing.dhash(img), hashing.dhash(img.resize((80, 60)))) <= 8
    assert hashing.hamming(hashing.dhash(img), hashing.dhash(other)) > 16


def test_fingerprint_is_upright_by_exif():
    img = Image.new("RGB", (200, 100), (10, 10, 10))
    img.paste((240, 240, 240), (0, 0, 100, 50))
    plain = hashing.fingerprint(_jpeg(img))
    turned = hashing.fingerprint(_jpeg(img.rotate(90, expand=True), exif_orientation=6))
    assert (plain.width, plain.height) == (200, 100)
    assert (turned.width, turned.height) == (200, 100)
    assert hashing.hamming(plain.ahash, turned.ahash) <= 6


def test_fingerprint_is_none_for_bytes_that_do_not_decode():
    assert hashing.fingerprint(b"not an image") is None
    assert hashing.fingerprint(b"") is None


def test_hex_round_trip_with_the_top_bit_set():
    value = (1 << 63) | 0xABC
    text = hashing.to_hex(value)
    assert text == "8000000000000abc" and len(text) == 16
    assert hashing.from_hex(text) == value
    assert hashing.from_hex(hashing.to_hex(0)) == 0


def test_hex_refuses_values_that_are_not_64_bit():
    with pytest.raises(ValueError):
        hashing.to_hex(1 << 64)
    with pytest.raises(ValueError):
        hashing.to_hex(-1)
    with pytest.raises(ValueError):
        hashing.from_hex("abc")

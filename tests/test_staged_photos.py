"""Unit tests for staged_total — #469."""
from __future__ import annotations

from pathlib import Path

from src.project.staged_photos import StagedPhoto, staged_total


def _photo(bytes_: int) -> StagedPhoto:
    return StagedPhoto(full=Path("full.jpg"), thumb=Path("full_thumb.jpg"), bytes=bytes_)


def test_staged_total_sums_every_staged_photo():
    staged = {
        ("memories", 1): {"a": _photo(100), "b": _photo(50)},
        ("journal", 2): {"c": _photo(30)},
    }

    assert staged_total(staged) == 180


def test_staged_total_skips_the_given_keys():
    staged = {
        ("memories", 1): {"a": _photo(100), "b": _photo(50)},
        ("journal", 2): {"c": _photo(30)},
    }

    total = staged_total(staged, skip={("memories", 1, "a"), ("journal", 2, "c")})

    assert total == 50

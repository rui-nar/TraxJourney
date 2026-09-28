"""The staging type shared by the ZIP reader (U3) and ingest (U2) — #469.

A ZIP import decodes and writes each photo's full file and thumbnail into a
per-request staging directory before any DB row exists. ``StagedPhotos`` is
what the reader hands back and what ingest reads from: every staged photo,
keyed by where it will belong.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Set, Tuple


@dataclass(frozen=True)
class StagedPhoto:
    """One staged photo's files and their size on disk.

    ``bytes`` is the full file plus its thumbnail, since that is what the
    storage quota is charged for.
    """
    full: Path
    thumb: Path
    bytes: int


# Keyed by (kind, file item id) — kind is "memories" or "journal", the
# photo_folder kinds — then by photo uuid.
StagedPhotos = Dict[Tuple[str, int], Dict[str, StagedPhoto]]


def staged_total(staged: StagedPhotos, skip: Set[Tuple[str, int, str]] = frozenset()) -> int:
    """The bytes of every staged photo whose (kind, file item id, uuid) is not in ``skip``."""
    total = 0
    for (kind, item_id), photos in staged.items():
        for photo_uuid, photo in photos.items():
            if (kind, item_id, photo_uuid) not in skip:
                total += photo.bytes
    return total

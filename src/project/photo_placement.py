"""Staged photos after an import's commit — #469, Decision 8 of its plan.

A ZIP import stages every photo before any row is written, and the ingest
(``src/project/repo_transfer.py``) touches no file: it returns a
:class:`~src.project.repo_transfer.Placement` for each staged photo the
committed rows name. :func:`place_photos` then moves them into place.

:func:`already_present` answers, before the ingest, which of the file's photos
a Replace would find already in place, so the storage quota does not count
them twice (Decision 3).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Sequence, Set, Tuple, Union

from sqlmodel import Session, select

from models.project_db import DBJournalEntry, DBMemory, DBProject
from src.billing.usage import bytes_of, record_written
from src.models.project import Project
from src.project.repo_transfer import (
    Placement,
    _author,
    _on_disk,
    _wanted_journal_ids,
    _wanted_public_ids,
)
from src.utils.logging import get_logger
from src.utils.photo_paths import photo_file, photo_folder

_log = get_logger(__name__)


def place_photos(
    data_dir: Union[str, Path], importer: int, placements: Sequence[Placement],
) -> List[Tuple[str, int, str]]:
    """Move each staged photo into its row's folder; return those that failed.

    Each photo is two renames, the full file then its thumbnail, into
    ``photo_folder(data_dir, importer, kind, row id)``. The staging directory
    is on the same volume, so a rename is all a move costs. Every file placed
    is counted in one :func:`record_written` call.

    A rename that fails is logged at ERROR and its ``(kind, row id, uuid)``
    returned rather than raised: the rows are committed, and the caller
    removes those names from them.
    """
    placed: List[Path] = []
    failed: List[Tuple[str, int, str]] = []
    for p in placements:
        folder = photo_folder(data_dir, importer, p.kind, p.row_id)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            full = photo_file(folder, p.uuid)
            thumb = photo_file(folder, p.uuid, "_thumb")
            if full is None or thumb is None:
                raise OSError(f"{p.uuid!r} names no photo file of {folder}")
            os.replace(p.staged.full, full)
        except OSError:
            _log.error(
                "import: placing %s %s photo %s failed; nothing of it was placed",
                p.kind, p.row_id, p.uuid, exc_info=True)
            failed.append((p.kind, p.row_id, p.uuid))
            continue
        placed.append(full)
        try:
            os.replace(p.staged.thumb, thumb)
        except OSError:
            # The full file stays, counted and unnamed once the caller drops
            # the name: left to storage reconciliation (review R3-4).
            _log.error(
                "import: placing %s %s photo %s failed at its thumbnail; its full "
                "file was already placed at %s (%d bytes)",
                p.kind, p.row_id, p.uuid, full, bytes_of(full), exc_info=True)
            failed.append((p.kind, p.row_id, p.uuid))
            continue
        placed.append(thumb)
    record_written(importer, *placed)
    return failed


def already_present(
    sess: Session, owner: int, name: str, project: Project,
    *, data_dir: Union[str, Path],
) -> Set[Tuple[str, int, str]]:
    """The file's photos a Replace of the owner's trip *name* finds in place.

    ``(kind, file item id, uuid)`` for each photo the file lists whose full
    file is already in the folder of the row Replace keeps for that item:
    matched as :meth:`replace_project` matches, memories by ``public_id`` and
    the owner's own journal entries by id, each row kept for the first item
    naming it. Empty when the trip does not exist.
    """
    trip = sess.exec(select(DBProject).where(
        DBProject.user_info_id == owner, DBProject.name == name)).first()
    if trip is None:
        return set()

    public_ids = _wanted_public_ids(project)
    memories = {
        m.public_id: m for m in sess.exec(
            select(DBMemory).where(DBMemory.project_id == trip.id)).all()
        if m.public_id in public_ids
    }
    journal_ids = _wanted_journal_ids(project)
    journals = {
        e.id: e for e in sess.exec(
            select(DBJournalEntry).where(DBJournalEntry.project_id == trip.id)).all()
        if _author(e, owner) == owner and e.id in journal_ids
    }

    present: Set[Tuple[str, int, str]] = set()
    for item in project.items:
        if item.item_type == "memory" and item.memory is not None:
            kind, content = "memories", item.memory
            row = (memories.pop(content.public_id, None)
                   if isinstance(content.public_id, str) else None)
        elif item.item_type == "journal" and item.journal is not None:
            kind, content = "journal", item.journal
            row = journals.pop(content.id, None) if type(content.id) is int else None
        else:
            continue
        if row is None or type(content.id) is not int:
            continue
        folder = photo_folder(data_dir, owner, kind, row.id)
        present.update((kind, content.id, u) for u in content.photos or []
                       if _on_disk(folder, u))
    return present

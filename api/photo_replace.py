"""Replace one memory or journal photo with new bytes (issue #33, U8 of
docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md).

The ``PUT …/replace`` routes of ``api/memories.py`` and ``api/journal.py``,
and the Immich apply job, all replace a photo through
:func:`replace_photo_bytes`, so the three share one rule for storage, hashes,
sources and files:

* Storage is checked against the net change: the new bytes minus the old
  full-size file they free, never below 0 (F3). The thumbnails are left out
  of the check, as they are for an upload; the counter records what is
  actually written and freed.
* When the old photo has no sha256 yet, its full-size file is hashed before
  it goes, outside any lock, and the hash is given to the new photo (D6). A
  later Polarsteps re-import of the same photo then finds it and stores
  nothing. A hash already recorded follows the photo anyway
  (``api/photo_order.replace``).
* The new photo gets *source*, or none; the old one's is never carried.
* The list is written by compare-and-set (``api/photo_write.py``, D13). The
  old files go only once the write has committed (D5); when the photo or its
  row went meanwhile, the new files go instead.

Raises :class:`PhotoGone`, ``QuotaExceeded``, ``InvalidPhoto`` (or its
``PhotoTooLarge``) or ``PhotoWriteConflict``; the routes turn each into the
answer they always gave.
"""
from __future__ import annotations

import hashlib
import json
import uuid as uuid_lib
from pathlib import Path
from typing import List, Optional

from api.photo_locks import photo_lock
from api.photo_order import fill_hashes, load_state
from api.photo_order import replace as replace_in_order
from api.photo_write import mutate_photos
from api.project_shared import bust_project_payloads
from models.db import get_session
from models.project_db import DBJournalEntry, DBMemory
from src.billing.entitlements import ensure_storage_quota
from src.billing.usage import bytes_of, unlink_and_record
from src.utils.photo_paths import photo_file, photo_files, photo_folder
from src.utils.photo_privacy import remove_share_copy
from src.utils.photo_store import save_photo_files

#: Each kind's row, and its folder name under ``users/<id>/``.
_KINDS = {"memory": (DBMemory, "memories"), "journal": (DBJournalEntry, "journal")}


class PhotoGone(Exception):
    """The row, or the photo in its list, is no longer there."""


def _data_dir(kind: str) -> str:
    # The route modules own the data directory (and tests point it at a
    # temporary one there); they import this module, so it is read lazily.
    if kind == "memory":
        import api.memories as module
    else:
        import api.journal as module
    return module._DATA_DIR


def _delete_files(kind: str, folder: Path, owner_dir: str, uuids: List[str]) -> None:
    """Delete and uncount the full-size and thumbnail files of *uuids*.

    A memory photo's share-link copy goes with them, outside the accounting,
    as ``api/memories._delete_photo_files`` does. Journal photos have none.
    """
    unlink_and_record(owner_dir, photo_files(folder, uuids))
    if kind == "memory":
        for photo_uuid in uuids:
            full = photo_file(folder, photo_uuid)
            if full is not None:
                remove_share_copy(full)


def replace_photo_bytes(kind: str, artifact_id: int, old_uuid: str, raw: bytes, *,
                        owner_dir: str, source: Optional[str] = None) -> str:
    """Replace photo *old_uuid* of the *kind* (``memory`` or ``journal``) row
    *artifact_id* with *raw*, at the same position and rank. Returns the new
    photo's UUID.

    *owner_dir* is the user whose tree holds the files and whose storage they
    count against: a memory's project owner, a journal entry's author. The
    caller has checked the caller's rights; this checks nothing about them.
    """
    model, folder_name = _KINDS[kind]
    folder = photo_folder(_data_dir(kind), owner_dir, folder_name, artifact_id)

    with get_session() as sess:
        row = sess.get(model, artifact_id)
        if row is None:
            raise PhotoGone()
        photos = [p for p in json.loads(row.photos_json or "[]") if p]
        if old_uuid not in photos:
            raise PhotoGone()
        has_old_hash = old_uuid in load_state(row.photo_order_json)["hashes"]

    old_full = photo_file(folder, old_uuid)
    old_size = bytes_of(old_full) if old_full is not None else 0
    with get_session() as sess:
        ensure_storage_quota(sess, int(owner_dir), max(0, len(raw) - old_size))

    # Outside the lock: a 25 MB original takes a moment to read.
    old_hash: Optional[str] = None
    if not has_old_hash and old_full is not None and old_full.is_file():
        old_hash = hashlib.sha256(old_full.read_bytes()).hexdigest()

    new_uuid = str(uuid_lib.uuid4())
    save_photo_files(owner_dir, folder, new_uuid, raw)

    def swap(photos, state, row):
        # Same position and rank, not remove+append: photos_json order is
        # display order. None when the old photo was deleted meanwhile.
        live = [p for p in photos if p]
        result = replace_in_order(live, state, old_uuid, new_uuid, source=source)
        if result is None:
            return None
        new_photos, new_state = result
        if old_hash is not None:
            # Never over a hash the old photo gained since it was read.
            new_state = fill_hashes(new_photos, new_state, {new_uuid: old_hash})
        return new_photos, new_state

    with photo_lock(kind, artifact_id):
        try:
            replaced = mutate_photos(kind, artifact_id, swap)
        except Exception:
            # A conflict, or the database failing: nothing names the new
            # files, so they must not stay counted.
            _delete_files(kind, folder, owner_dir, [new_uuid])
            raise
        if replaced is None:
            # The row or the old photo went while the new files were
            # written: they have nowhere to go (#237).
            _delete_files(kind, folder, owner_dir, [new_uuid])
            raise PhotoGone()
        bust_project_payloads(replaced.cache_ref)
        # Still under the lock: a delete's folder sweep would otherwise size
        # these same files and uncount them a second time.
        _delete_files(kind, folder, owner_dir, [old_uuid])
    return new_uuid

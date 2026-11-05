"""Compare-and-set writes of a row's photo list (D13 of
docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md).

A memory's or journal entry's ``photos_json`` and ``photo_order_json`` are
read, changed and written back. ``photo_lock`` (``api/photo_locks.py``)
serialises the writers of one process only; a writer in another process (a
worker) could commit between the read and the write and be overwritten.

So every such write is an ``UPDATE`` that matches the two columns as they
were read, the cross-process pattern ``bump_lock_version`` already uses for
project writes. When another writer changed them meanwhile, nothing is
written, and :func:`mutate_photos` re-reads and recomputes. ``photo_lock``
stays around the call, so writers of one process queue instead of retrying.

Side effects that depend on the outcome (deleting the files of a removed or
replaced photo, or of a placement that was refused, busting the project's
cached payloads) are the caller's, after :func:`mutate_photos` returns.
"""
from __future__ import annotations

import json
from typing import Callable, NamedTuple, Optional, Tuple

from sqlalchemy import update

from api.photo_order import dump_state, load_state
from api.project_shared import project_cache_ref
from models.db import get_session
from models.project_db import DBJournalEntry, DBMemory

#: ``photo_lock``'s kinds, and the row each one names.
_MODELS = {"memory": DBMemory, "journal": DBJournalEntry}

#: What a route answers, with 409, when the list kept changing under it.
PHOTO_CONFLICT_DETAIL = "The photos changed while saving. Try again."

#: ``compute(photos, state, row)``: the new ``(photos, state)``, or ``None``
#: for no change.
Compute = Callable[[list, dict, object], Optional[Tuple[list, dict]]]


class PhotoWrite(NamedTuple):
    """What :func:`mutate_photos` wrote, and the cache key to bust for it."""

    photos: list
    state: dict
    #: ``project_cache_ref`` of the row's project, read before the commit.
    cache_ref: Optional[tuple]


class PhotoWriteConflict(Exception):
    """The row's photo columns changed under every attempt to write them."""

    def __init__(self, kind: str, row_id: int, attempts: int):
        super().__init__(f"{kind} {row_id}: photo list changed under {attempts} attempts")
        self.kind = kind
        self.row_id = row_id


def _as_read(column, value: Optional[str]):
    """``column IS NULL`` for a NULL read, else ``column = value``."""
    return column.is_(None) if value is None else column == value


def cas_photo_columns(sess, model, row_id: int, *, read_photos_json: Optional[str],
                      read_order_json: Optional[str], photos: list, state: dict) -> bool:
    """Write *photos* and *state* to row *row_id* of *model*, only if its two
    photo columns still hold what was read. True when the row was written.

    Touches no other column, and does not commit: the caller's transaction
    decides.
    """
    result = sess.execute(
        update(model)
        .where(
            model.id == row_id,
            _as_read(model.photos_json, read_photos_json),
            _as_read(model.photo_order_json, read_order_json),
        )
        .values(photos_json=json.dumps(photos), photo_order_json=dump_state(state))
        # The ORM row in the session is the caller's read; nothing reads it
        # after the commit, which expires it anyway.
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def mutate_photos(kind: str, row_id: int, compute: Compute, *,
                  attempts: int = 5) -> Optional[PhotoWrite]:
    """Change the photo list of the *kind* (``memory`` or ``journal``) row
    *row_id* by compare-and-set, and return what was written.

    Each attempt reads the row in a new session and calls ``compute(photos,
    state, row)``: *photos* is the list as stored (falsy entries included),
    *state* the parsed ``photo_order_json``. It returns the new ``(photos,
    state)``, or ``None`` for no change. It may set other columns on *row*,
    which are written in the same transaction, but never the two photo
    columns themselves. It may run more than once, so it acts on nothing but
    the row; what it notes for the caller is the last attempt's.

    Returns ``None`` when the row is gone or *compute* returned ``None``;
    nothing is written then. After a write, the caller busts
    ``PhotoWrite.cache_ref`` (``bust_project_payloads``). Raises
    :class:`PhotoWriteConflict` when the two columns changed under each of
    *attempts* attempts.
    """
    model = _MODELS[kind]
    for _ in range(attempts):
        with get_session() as sess:
            row = sess.get(model, row_id)
            if row is None:
                return None
            read_photos_json, read_order_json = row.photos_json, row.photo_order_json
            # Read before compute: a query after it would flush what compute
            # set, and so open the write transaction earlier than the update.
            cache_ref = project_cache_ref(sess, row.project_id)
            result = compute(json.loads(read_photos_json or "[]"), load_state(read_order_json), row)
            if result is None:
                return None
            photos, state = result
            if cas_photo_columns(sess, model, row_id, read_photos_json=read_photos_json,
                                 read_order_json=read_order_json, photos=photos, state=state):
                sess.commit()
                return PhotoWrite(photos, state, cache_ref)
            sess.rollback()
    raise PhotoWriteConflict(kind, row_id, attempts)

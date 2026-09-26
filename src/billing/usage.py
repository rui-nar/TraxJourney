"""Per-user storage accounting for quota checks (issue #121).

The admin dashboard measures storage by walking the filesystem
(:func:`src.admin.storage.dir_size`). That is the right tool there — exact, no
state to drift — and the wrong one on an upload path, where it would add a
multi-second tree walk to every photo.

So writes keep a running counter instead: each save adds the bytes it wrote,
each delete subtracts the bytes it removed, and a nightly job re-walks the tree
and corrects any drift (a crash between write and increment, files removed out
of band). The counter is therefore *approximately* right within a day and
exactly right after each reconcile — good enough to gate an upload on, which is
all it is for. The dashboard keeps using the walk.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from sqlmodel import select

import src.admin.storage as _storage
from models.billing import UserUsage
from models.db import get_session
from models.user import UserInfo
from src.utils.logging import get_logger

_log = get_logger(__name__)


def bytes_of(*paths: Path | str) -> int:
    """Total size of the given files, skipping any that are missing."""
    total = 0
    for p in paths:
        try:
            total += Path(p).stat().st_size
        except OSError:
            continue
    return total


def record_delta(user_id: str | int, delta_bytes: int) -> None:
    """Add ``delta_bytes`` (may be negative) to a user's counted storage.

    Never raises: storage accounting must not be able to fail an upload that
    already succeeded on disk. A lost delta is corrected by the next reconcile.
    """
    if not delta_bytes:
        return
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    try:
        with get_session() as sess:
            row = sess.exec(
                select(UserUsage).where(UserUsage.user_info_id == uid)
            ).first()
            if row is None:
                row = UserUsage(user_info_id=uid)
            # Clamp at zero: a delete counted twice must not make the account
            # look like it has negative usage (and thus infinite headroom).
            row.storage_bytes = max(0, row.storage_bytes + delta_bytes)
            row.updated_at = time.time()
            sess.add(row)
            sess.commit()
    except Exception:  # pragma: no cover - defensive; accounting is best-effort
        _log.warning("storage accounting failed for user %s", user_id, exc_info=True)


def record_written(user_id: str | int, *paths: Path | str) -> None:
    """Count files that were just written."""
    record_delta(user_id, bytes_of(*paths))


def _inside_user_dir(user_id: str | int, paths: Iterable[Path | str]) -> list[Path]:
    """The paths among *paths* that resolve to inside a ``users/<user_id>/``
    folder.

    Judged from the resolved path itself rather than from one module's data
    directory: every writer keeps its files under ``users/<id>/``, whichever
    data directory it was configured with.
    """
    try:
        uid = str(int(user_id))
    except (TypeError, ValueError):
        return []
    kept = []
    for p in paths:
        try:
            resolved = Path(p).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if any(d.name == uid and d.parent.name == "users" for d in resolved.parents):
            kept.append(Path(p))
        else:
            _log.warning("refused to delete %s: outside user %s's folder", p, user_id)
    return kept


def unlink_and_record(user_id: str | int, paths: Iterable[Path | str]) -> None:
    """Delete files and subtract what they occupied.

    Sizes are read *before* unlinking — afterwards there is nothing to stat.
    Only files inside *user_id*'s own folder are touched: the count is theirs,
    and so is what may be deleted on their behalf. Callers resolve stored
    names through ``src.utils.photo_paths`` first; this is the last line.
    """
    paths = _inside_user_dir(user_id, paths)
    freed = bytes_of(*paths)
    for p in paths:
        Path(p).unlink(missing_ok=True)
    record_delta(user_id, -freed)


def reconcile_usage(user_id: str | int) -> int:
    """Recompute one user's counter from the filesystem. Returns the new total.

    The walk happens outside the DB session — see the note in
    :mod:`src.admin.storage` about not holding a pooled connection through it.
    """
    uid = int(user_id)
    total = _storage.dir_size(_storage._user_dir(str(uid)))
    now = time.time()
    with get_session() as sess:
        row = sess.exec(
            select(UserUsage).where(UserUsage.user_info_id == uid)
        ).first()
        if row is None:
            row = UserUsage(user_info_id=uid)
        row.storage_bytes = total
        row.reconciled_at = now
        row.updated_at = now
        sess.add(row)
        sess.commit()
    return total


def reconcile_all_usage() -> None:
    """Nightly job: re-walk every user's tree and correct the counters.

    A no-op when billing is disabled — nothing reads the counters then.
    """
    from src.billing.entitlements import billing_enabled

    if not billing_enabled():
        return
    with get_session() as sess:
        user_ids = [u.id for u in sess.exec(select(UserInfo)).all() if u.id]
    for uid in user_ids:
        try:
            reconcile_usage(uid)
        except Exception:  # pragma: no cover - one bad user must not stop the job
            _log.warning("usage reconcile failed for user %s", uid, exc_info=True)
    _log.info("Storage usage reconciled for %d users", len(user_ids))

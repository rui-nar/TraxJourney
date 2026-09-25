"""TEMPORARY (issue #434): delete leftover ``*.migrated`` project files at startup.

Until #434, every project import left a ``<name>.traxj.migrated`` copy of the
upload under ``data/users/<id>/projects/`` (so did the pre-#420 lazy ingest and
``scripts/migrate_to_db.py``, under the pre-rename extension). The trip itself is in
the database; the file is dead weight that counted against the user's storage.
Imports no longer write any file, so once each instance has booted a release
carrying this sweep there is nothing left for it to find.

REMOVE in a later release: this module, its call in ``api.router.lifespan``
and ``tests/test_migrated_sweep.py``.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator

from sqlmodel import select

import src.admin.storage as _storage
from models.db import get_session
from models.user import UserInfo
from src.billing.usage import reconcile_usage
from src.utils.logging import get_logger

_log = get_logger(__name__)


def start_migrated_sweep() -> threading.Thread:
    """Run :func:`sweep_migrated_files` on a background daemon thread.

    Called from the API lifespan. The sweep re-measures each affected user's
    whole tree, which on a large instance could hold up startup past the
    deploy check's grace period, so the API starts listening without waiting.
    A daemon thread, not the event loop's executor, so shutdown never waits on
    it either: being cut off mid-sweep is harmless (each delete is atomic, an
    interrupted counter write rolls back, and the next boot sweeps again).
    """
    thread = threading.Thread(target=_sweep_logged, name="migrated-sweep", daemon=True)
    thread.start()
    return thread


def _sweep_logged() -> None:
    # A thread's exception would otherwise only reach threading.excepthook.
    try:
        sweep_migrated_files()
    except Exception:
        _log.exception("Leftover *.migrated sweep failed")


def _leftovers(users_root: Path) -> Iterator[Path]:
    """``users/*/projects/*.migrated``, without following symlinks at any level.

    A symlinked ``users/<id>`` or ``<id>/projects`` is not that user's data;
    ``Path.glob`` would follow it and the sweep would delete wherever it points.
    """
    if not users_root.is_dir():
        return
    for user_dir in users_root.iterdir():
        if user_dir.is_symlink() or not user_dir.is_dir():
            continue
        projects = user_dir / "projects"
        if projects.is_symlink() or not projects.is_dir():
            continue
        for path in projects.iterdir():
            if path.name.endswith(".migrated") and not path.is_symlink() and path.is_file():
                yield path


def sweep_migrated_files() -> int:
    """Delete ``data/users/*/projects/*.migrated``; return how many went.

    Idempotent. A file that cannot be deleted is logged and skipped. Each user
    who got space back has their quota counter re-measured and their dashboard
    cache entry dropped, so the freed bytes show at once rather than after the
    nightly reconcile.
    """
    removed = 0
    freed_users: set[str] = set()
    for path in _leftovers(Path(_storage._DATA_DIR) / "users"):
        try:
            path.unlink()
        except OSError:
            _log.warning("could not delete leftover %s", path, exc_info=True)
            continue
        removed += 1
        freed_users.add(path.parent.parent.name)

    if freed_users:
        # Only real accounts have a counter: reconciling a directory left by a
        # deleted account would create an orphan usage row.
        with get_session() as sess:
            known = {str(uid) for uid in sess.exec(select(UserInfo.id)).all()}
        for user_id in sorted(freed_users):
            _storage.refresh_storage_cache(user_id)
            if user_id not in known:
                continue
            try:
                reconcile_usage(user_id)
            except Exception:
                # Best effort: the nightly reconcile corrects any counter
                # left stale here.
                _log.warning("usage reconcile after sweep failed for user %s",
                             user_id, exc_info=True)

    _log.info("Removed %d leftover *.migrated project file(s) for %d user(s)",
              removed, len(freed_users))
    return removed

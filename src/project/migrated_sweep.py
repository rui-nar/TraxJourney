"""TEMPORARY (issue #434): delete leftover ``*.migrated`` project files at startup.

Until #434, every project import left a ``<name>.traxj.migrated`` copy of the
upload under ``data/users/<id>/projects/`` (so did the pre-#420 lazy ingest and
``scripts/migrate_to_db.py``, as ``*.viewtrip.migrated``). The trip itself is in
the database; the file is dead weight that counted against the user's storage.
Imports no longer write any file, so once each instance has booted a release
carrying this sweep there is nothing left for it to find.

REMOVE in a later release: this module, its call in ``api.router.lifespan``
and ``tests/test_migrated_sweep.py``.
"""
from __future__ import annotations

from pathlib import Path

from sqlmodel import select

import src.admin.storage as _storage
from models.db import get_session
from models.user import UserInfo
from src.billing.usage import reconcile_usage
from src.utils.logging import get_logger

_log = get_logger(__name__)


def sweep_migrated_files() -> int:
    """Delete ``data/users/*/projects/*.migrated``; return how many went.

    Idempotent. A file that cannot be deleted is logged and skipped. Each user
    who got space back has their quota counter re-measured and their dashboard
    cache entry dropped, so the freed bytes show at once rather than after the
    nightly reconcile.
    """
    users_root = Path(_storage._DATA_DIR) / "users"
    removed = 0
    freed_users: set[str] = set()
    for path in users_root.glob("*/projects/*.migrated"):
        if path.is_symlink() or not path.is_file():
            continue
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

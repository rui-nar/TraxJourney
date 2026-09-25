"""TEMPORARY (issue #434): empty and remove ``data/users/<id>/projects/`` at startup.

Nothing has read that directory since #420 moved every trip into the database,
and since #434 nothing writes to it. What is left there only counts against
the user's storage: the ``<name>.traxj.migrated`` copy every earlier import
left (the pre-#420 lazy ingest and ``scripts/migrate_to_db.py`` left the same
under the pre-rename extension), the raw upload a failed import left, and
pre-rename files that were never ingested. Owner decision: delete every regular
file in it and remove the emptied directory. Once each instance has booted a
release carrying this sweep there is nothing left for it to find.

REMOVE in a later release: this module, its call in ``api.router.lifespan``,
``tests/test_legacy_project_sweep.py`` and that test's entry in
``tests/test_no_legacy_brand.py``.
"""
from __future__ import annotations

import os
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


def start_legacy_project_sweep() -> threading.Thread:
    """Run :func:`sweep_legacy_project_files` on a background daemon thread.

    Called from the API lifespan. The sweep re-measures each affected user's
    whole tree, which on a large instance could hold up startup past the
    deploy check's grace period, so the API starts listening without waiting.
    A daemon thread, not the event loop's executor, so shutdown never waits on
    it either: being cut off mid-sweep is harmless (each delete is atomic, an
    interrupted counter write rolls back, and the next boot sweeps again).
    """
    thread = threading.Thread(target=_sweep_logged, name="legacy-project-sweep", daemon=True)
    thread.start()
    return thread


def _sweep_logged() -> None:
    # A thread's exception would otherwise only reach threading.excepthook.
    try:
        sweep_legacy_project_files()
    except Exception:
        _log.exception("Legacy projects/ directory sweep failed")


def _projects_dirs(users_root: Path) -> Iterator[Path]:
    """Each real ``users/<id>/projects``, never through a symlink.

    A symlinked ``users/<id>`` or ``<id>/projects`` is not that user's data;
    following it would delete wherever it points.
    """
    if not users_root.is_dir():
        return
    for user_dir in users_root.iterdir():
        if user_dir.is_symlink() or not user_dir.is_dir():
            continue
        projects = user_dir / "projects"
        if projects.is_symlink() or not projects.is_dir():
            continue
        yield projects


def _empty_and_remove(projects: Path) -> int:
    """Delete every regular file under ``projects``, then each emptied directory
    bottom-up, ``projects`` included. Returns how many files went.

    Symlinks are neither followed nor deleted; a directory still holding one,
    or a file that could not be deleted, is simply left in place.
    """
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(projects, topdown=False, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.unlink()
            except OSError:
                _log.warning("could not delete leftover %s", path, exc_info=True)
                continue
            removed += 1
        try:
            os.rmdir(dirpath)
        except OSError:
            pass  # not empty: something above was kept
    return removed


def sweep_legacy_project_files() -> int:
    """Empty and remove every ``data/users/*/projects/``; return files deleted.

    Idempotent. A file that cannot be deleted is logged and skipped. Each user
    who got space back has their quota counter re-measured and their dashboard
    cache entry dropped, so the freed bytes show at once rather than after the
    nightly reconcile.
    """
    removed = 0
    freed_users: set[str] = set()
    for projects in _projects_dirs(Path(_storage._DATA_DIR) / "users"):
        n = _empty_and_remove(projects)
        if n:
            removed += n
            freed_users.add(projects.parent.name)

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

    _log.info("Removed %d leftover file(s) from projects/ for %d user(s)",
              removed, len(freed_users))
    return removed

"""Per-user storage accounting for the admin dashboard.

The filesystem walk is deliberately decoupled from any DB session: computing a
directory size can be slow (thousands of photo files), and holding a pooled DB
connection open for the duration would starve the pool under load. Callers walk
here *outside* their ``with get_session()`` block.

A small in-process TTL cache avoids re-walking on every dashboard load; an
explicit refresh busts it so the operator can force a recompute.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Union

from src.utils.photo_privacy import is_share_copy

# Root under which per-user assets live: data/users/{user_id}/…
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")

# TTL for the storage cache, in seconds. A dashboard reload within this window
# reuses the cached size instead of re-walking the tree.
_CACHE_TTL_SECONDS = 300.0

# user_id -> (bytes, computed_at)
_cache: dict[str, tuple[int, float]] = {}


def dir_size(path: Union[str, Path]) -> int:
    """Sum ``st_size`` of every regular file under ``path`` (recursive).

    A missing directory returns 0. Symlinks are not followed (avoids double
    counting / cycles). The metadata-free photo copies served to share links
    are skipped: they are a server-side cache, not the user's storage (issue
    #430), and the quota counter this walk reconciles never counted them.
    """
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if is_share_copy(name):
                continue
            fp = Path(dirpath) / name
            try:
                if fp.is_symlink():
                    continue
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _user_dir(user_id: str) -> Path:
    return Path(_DATA_DIR) / "users" / str(user_id)


def cached_user_storage(user_id: str, now: float | None = None) -> int:
    """Return the user's storage in bytes, using the TTL cache when fresh.

    ``now`` is injectable for tests. A cache miss (or stale entry) triggers a
    fresh ``dir_size`` walk and repopulates the cache.
    """
    key = str(user_id)
    ts = time.time() if now is None else now
    hit = _cache.get(key)
    if hit is not None and (ts - hit[1]) < _CACHE_TTL_SECONDS:
        return hit[0]
    size = dir_size(_user_dir(key))
    _cache[key] = (size, ts)
    return size


def refresh_storage_cache(user_id: str | None = None) -> None:
    """Bust the storage cache — for one user, or all users if ``user_id`` is None."""
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(str(user_id), None)

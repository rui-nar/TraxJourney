"""Shared copy of the live log-level override, for cross-process propagation
(issue #208).

An admin's live override lives in the API process's memory (see
``src.utils.logging``). That's enough on its own only when the API process
is doing everything — the moment ``REDIS_URL`` is set, route resolution,
poster rendering, share-tile refresh, and stats run in the separate
``src/jobs/worker.py`` process instead (``src/jobs/queue.py``), which never
sees the API process's memory. Redis is already the shared state in that
deployment, so this module mirrors the override there — the worker reads it
once per job (``src.utils.logging.refresh_level_from_store``, called from
``src.jobs.queue.enqueue``'s queued path).

No-Redis deployments (the default, self-hosted case) get no shared copy —
``write``/``clear`` are silent no-ops and ``read`` always returns ``None`` —
which is correct: with no broker there is no separate worker process either,
so the API process's in-memory override is already the whole story.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

from src.jobs.redis_client import get_redis

_KEY = "traxjourney:log_level_override"


def write(level: int, expires_at: Optional[float]) -> None:
    """Publish the current override for other processes to pick up.

    A Redis ``EXPIRE`` is set to match *expires_at* so an override is never
    left live in the store past its intended end even if the admin API's own
    scheduled revert is somehow lost (process restart, etc.) — belt and
    suspenders alongside ``src.utils.logging``'s own expiry check.
    """
    client = get_redis()
    if client is None:
        return
    value = f"{level}:{expires_at if expires_at is not None else ''}"
    if expires_at is not None:
        ttl = max(1, int(expires_at - time.time()))
        client.set(_KEY, value, ex=ttl)
    else:
        client.set(_KEY, value)


def read() -> Optional[Tuple[int, Optional[float]]]:
    """The published ``(level, expires_at)``, or ``None`` when there is none
    (no Redis configured, nothing published, or Redis itself unreachable —
    all three degrade the same way: nothing to propagate)."""
    client = get_redis()
    if client is None:
        return None
    raw = client.get(_KEY)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    level_str, _, expires_str = raw.partition(":")
    try:
        level = int(level_str)
    except ValueError:
        return None
    expires_at = float(expires_str) if expires_str else None
    return level, expires_at


def clear() -> None:
    """Remove the published override, if any."""
    client = get_redis()
    if client is None:
        return
    client.delete(_KEY)

"""Shared infra for the api.projects.* route modules — no routes of its own.

Holds the single ``ProjectRepo`` instance, the project-file directory, the
background-task helpers (stats refresh, share-tile refresh) that are reused
across the Core/Activities/Item-ordering/Segments route modules, and the
``/meta`` payload cache shared by the endpoint and its warmers.
"""
from __future__ import annotations

import gzip as gzip_lib
import json
import os
import threading
from typing import Any, Callable, Dict

from models.db import get_session
from sqlmodel import select

from api.project_access import effective_role, resolve_project
from models.project_db import DBProject
from src.project.project_repo import ProjectRepo

_repo = ProjectRepo()

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _projects_dir(user_id: str) -> str:
    path = os.path.join(_DATA_DIR, "users", user_id, "projects")
    os.makedirs(path, exist_ok=True)
    return path


def _get_project_row(sess, user_info_id: int, name: str) -> DBProject:
    return resolve_project(sess, user_info_id, name)


# ── /meta payload cache (issue #178) ──────────────────────────────────────────
# GET /{name}/meta is the request the activity panel blocks on at load, and the
# one polled every 2-3 s while a re-fetch or a route resolve is pending. On a
# large project a cold load measured 11-13 s — most of the client's budget spent
# before a byte of body moves. The payload is a pure function of project state,
# and every mutation already busts the project's cache generation for the geo
# payloads, so it rides the same cache; see api.geo for the TTL and generation
# machinery, and warm_meta_cache below for the post-mutation refill.

def project_cache_ref(sess, project_id: int) -> tuple | None:
    """``(owner_id, project_name)`` for *project_id* — the payload-cache key parts.

    Read it *before* the commit you are about to bust for: a commit expires the
    ORM instances, so reading ``row.name`` afterwards silently re-queries, and
    raises outright once the session has closed.
    """
    row = sess.get(DBProject, project_id)
    return (row.user_info_id, row.name) if row is not None else None


def bust_project_payloads(cache_ref: tuple | None) -> None:
    """Drop every cached payload of a project — call after any mutating commit.

    Every project-content mutation changes what ``GET /{name}/meta`` returns, and
    that response is cached (issue #178), so the cache is only safe while this is
    called unconditionally. ``tests/test_project_cache_invalidation.py`` walks the
    router table and fails if a mutating project route can't reach it.

    Geometry-affecting routes call ``bust_geo_cache`` instead — same counter,
    same effect; this name is for the ones that only change panel content.
    """
    if cache_ref is None:
        return
    from api.geo import bust_project_cache

    bust_project_cache(cache_ref[0], cache_ref[1])


def meta_cache_key(owner_id: int, name: str, caller_id: int) -> tuple:
    """Cache key for one caller's ``/meta`` payload.

    Keyed by caller and not just by project: journal entries are filtered to
    their author (issue #106) and ``caller_role`` is baked into the body (issue
    #109), so two members of the same trip must never share an entry.
    """
    return (owner_id, name, ("meta", caller_id))


def details_cache_key(owner_id: int, name: str, caller_id: int) -> tuple:
    """Cache key for one caller's ``GET /{name}`` (full details) payload.

    Same caller-scoping rationale as ``meta_cache_key`` — journal entries are
    filtered to their author and ``caller_role`` is baked into the body — with
    a distinct variant tag so it never collides with that caller's ``"meta"``
    entry for the same project (issue #178 follow-up).
    """
    return (owner_id, name, ("details", caller_id))


def gzip_json(payload: Any) -> bytes:
    """Serialise and gzip a payload for the byte caches (mirrors api.geo._gzip_geo)."""
    return gzip_lib.compress(json.dumps(payload).encode(), compresslevel=6)


def build_meta_payload(sess, row: DBProject, name: str, caller_id: int) -> dict | None:
    """The ``/meta`` body as *caller_id* sees it, or None if the project is gone.

    Lightweight loading (deferred summary_polyline / elevation_profile_json) —
    the whole point of ``/meta`` over ``GET /{name}``.
    """
    owner_id = row.user_info_id
    project = _repo.get_project(
        sess, owner_id, name,
        include_heavy=False,
        journal_user_id=caller_id,
    )
    if project is None:
        return None
    data = _repo.to_dict(project)
    data["caller_role"] = effective_role(sess, row, caller_id)
    return data


def build_details_payload(sess, row: DBProject, name: str, caller_id: int) -> dict | None:
    """The ``GET /{name}`` (full details) body as *caller_id* sees it, or None if gone.

    Full loading (``include_heavy`` defaults True) — unlike ``build_meta_payload``,
    this keeps summary_polyline and elevation_profile_json, which is the whole
    point of this endpoint over ``/meta``.
    """
    owner_id = row.user_info_id
    project = _repo.get_project(
        sess, owner_id, name,
        journal_user_id=caller_id,
    )
    if project is None:
        return None
    data = _repo.to_dict(project)
    data["caller_role"] = effective_role(sess, row, caller_id)
    return data


def warm_meta_cache(user_info_id: int, project_name: str) -> None:
    """Recompute and cache the owner's ``/meta`` payload for a project.

    Called from background tasks right after ``bust_geo_cache``, alongside
    ``warm_geo_cache``: reopening a trip you have just edited is the exact path
    that timed out in issue #178, and it is the owner's payload that gets loaded
    there. A companion's variant is keyed separately and simply recomputes on
    demand. Best-effort — any failure just means the next request is a MISS.
    """
    from api.geo import project_cache_generation, project_cache_store

    try:
        gen = project_cache_generation(user_info_id, project_name)
        with get_session() as sess:
            row = resolve_project(sess, user_info_id, project_name)
            payload = build_meta_payload(sess, row, project_name, user_info_id)
        if payload is None:
            return
        project_cache_store(
            meta_cache_key(user_info_id, project_name, user_info_id),
            gzip_json(payload),
            gen,
        )
    except Exception:  # noqa: BLE001 — the endpoint recomputes on demand
        pass


# ── Coalesced background refreshes (issues #45, #132) ──────────────────────────
# Every mutation queues background work (stats recompute, share-tile re-render)
# that reads the project and then writes a derived artefact. Running a burst of
# those concurrently is wrong twice over: on SQLite the stats writers serialise
# behind each other and behind the next save's own commit via busy_timeout (30 s),
# so a save could wait the full window and fail with "database is locked" (#45);
# and for tiles, the task that *writes last* need not be the one that *read last*,
# so a burst could leave tiles rendered from an older state (#132).
#
# Since only the latest project state matters, we coalesce per key: at most one
# worker runs at a time, and requests arriving while one is in flight set a rerun
# flag so the final state is still captured — exactly once, after the last one.
_coalesce_lock = threading.Lock()
_coalesce_state: Dict[tuple, Dict[str, bool]] = {}


def _run_coalesced(key: tuple, work: Callable[[], None]) -> None:
    """Run *work*, collapsing calls that arrive while one is in flight for *key*.

    Returns immediately (marking the running worker dirty) if one is already
    running; that worker reruns *work* once more when it finishes, so the newest
    state is always the one reflected.
    """
    with _coalesce_lock:
        state = _coalesce_state.setdefault(key, {"running": False, "dirty": False})
        if state["running"]:
            state["dirty"] = True
            return
        state["running"] = True
    try:
        while True:
            work()
            with _coalesce_lock:
                if not _coalesce_state[key]["dirty"]:
                    break
                _coalesce_state[key]["dirty"] = False
    finally:
        with _coalesce_lock:
            _coalesce_state[key]["running"] = False


def _refresh_share_tiles(user_info_id: int, project_name: str) -> None:
    """Re-render raster tiles for any active share token(s) after a project mutation.

    Coalesced per project so a burst of mutations collapses into one render that
    always reflects the final state (and skips the redundant intermediate ones).
    """
    _run_coalesced(("tiles", user_info_id, project_name),
                   lambda: _refresh_share_tiles_once(user_info_id, project_name))


def _refresh_share_tiles_once(user_info_id: int, project_name: str) -> None:
    """One share-tile render pass — see :func:`_refresh_share_tiles`."""
    from api.share import _build_features, invalidate_share_cache
    from src.tile_renderer import refresh_tile_cache

    with get_session() as sess:
        row = sess.exec(
            select(DBProject).where(
                DBProject.user_info_id == user_info_id,
                DBProject.name == project_name,
            )
        ).first()
    if row is None:
        return
    tokens = [t for t in (row.share_token, row.share_token_no_memories) if t]
    if not tokens:
        return

    for token in tokens:
        invalidate_share_cache(token)

    with get_session() as sess:
        project = _repo.get_project_by_id(sess, row.id)
    if project is None:
        return
    features = _build_features(project)

    for token in tokens:
        refresh_tile_cache(token, lambda f=features: f)


def _refresh_stats_background(user_info_id: int, project_name: str) -> None:
    """Recompute project stats, coalescing concurrent refreshes per project.

    If a refresh for this project is already running, mark it dirty and return
    immediately (no second concurrent SQLite writer); the in-flight refresh reruns
    once more when it finishes so the newest edit is always reflected.
    """
    _run_coalesced(("stats", user_info_id, project_name),
                   lambda: _refresh_stats_once(user_info_id, project_name))


def _refresh_stats_once(user_info_id: int, project_name: str) -> None:
    """One stats recompute pass — see :func:`_refresh_stats_background`."""
    with get_session() as sess:
        _repo.compute_and_cache_stats(sess, user_info_id, project_name)


def queue_stats_refresh(background_tasks, user_info_id: int, project_name: str) -> None:
    """Recompute a project's cached stats off the request path (issue #173).

    Queued when a broker is configured, so the refresh survives an API restart
    instead of leaving the cached stats stale until the next mutation happens to
    trigger another one. Falls back to a BackgroundTask otherwise.
    """
    from src.jobs.queue import QUEUE_DEFAULT, enqueue

    enqueue(QUEUE_DEFAULT, _refresh_stats_background, user_info_id, project_name,
            background_tasks=background_tasks)


def queue_share_tiles_refresh(background_tasks, user_info_id: int, project_name: str) -> None:
    """Re-render a shared project's map tiles off the request path (issue #173)."""
    from src.jobs.queue import QUEUE_DEFAULT, enqueue

    enqueue(QUEUE_DEFAULT, _refresh_share_tiles, user_info_id, project_name,
            background_tasks=background_tasks)

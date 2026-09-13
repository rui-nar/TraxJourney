"""GeoJSON endpoints — converts a project's tracks and segments to GeoJSON.

Routes:
    GET /api/geo/project?name=   — GeoJSON FeatureCollection for an open project
"""
from __future__ import annotations

import gzip as gzip_lib
import json
import math
import time
from array import array
from itertools import chain
from threading import Lock
from time import monotonic
from typing import Annotated, Any, Callable, Dict, List

import polyline as polyline_lib
import requests
from models.db import get_session
from models.project_db import DBActivity, DBActivityGeoPrepared
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlmodel import select

from api.deps import get_current_user
from api.project_access import OwnerParam, resolve_project
from src.brand import USER_AGENT
from src.models.great_circle import great_circle_points
from src.models.prepared_geo import (
    COORD_SCALE,
    prepare_polyline,
    store_prepared_if_unchanged,
    unpack_prepared_line,
)
from src.models.simplify import (
    MIN_POINTS,
    PREPARED_GEO_VERSION,
    bboxes_intersect,
    filter_to_level,
    floor_line,
    line_bbox,
    snap_bbox_to_tiles,
    vertex_levels,
    working_set,
)
from src.models.project import Project
from src.project.project_repo import ProjectRepo, _compute_low_res_geo
from src.jobs.redis_client import get_redis
from src.utils.encryption_check import is_encrypted_envelope
from src.utils.logging import get_logger
from src.utils.metrics import track_external

router = APIRouter(prefix="/api/geo", tags=["geo"])

_log = get_logger(__name__)

_repo = ProjectRepo()

# In-memory cache of gzipped per-project payloads:
#   (user_info_id, project_name, variant) → (gzip JSON bytes, expiry, generation)
# where expiry is a ``monotonic()`` deadline. Entries older than that are treated
# as a MISS: the generation guard below is what actually prevents a stale entry,
# but a TTL bounds *any* future bug of that class to minutes rather than forever,
# and a recompute is already the fallback so expiring early is cheap.
# The stored generation is what the entry was computed against — compared on
# read so an invalidation from another process is honoured (issue #173).
# The variant slot distinguishes payloads of the same project: the full-res geo
# endpoint keys on its ``encoded`` flag, low-res geo on the literal "low-res",
# /meta on ("meta", caller_id) — see api.project_shared.meta_cache_key. Every
# variant of a project shares one generation counter, so a single bust drops
# them all, which is what keeps "one bust per mutation" sufficient as payload
# kinds are added (issue #178).
_geo_cache: dict[tuple, tuple[bytes, float, int]] = {}
_geo_cache_lock = Lock()
_GEO_CACHE_TTL_S = 300.0

# Zoom-simplified payloads expire sooner than everything else — see the store
# call in project_geo_simplified for why.
_SIMPLIFIED_CACHE_TTL_S = 60.0

# Trips *prepared for simplification*, held per project rather than per zoom
# level. See _PreparedTrack for what an entry holds and issue #338 for why the
# unit of caching is the line and not the level.
_TRACK_CACHE_TTL_S = 900.0
_TRACK_CACHE_MAX_ENTRIES = 32

# Measured with tracemalloc against the shape actually held: a [lon, lat]
# Python list costs **128 bytes** — 72 for the two-element list, 2x24 for the
# floats, 8 for the parent slot. The same pair inside a flat array costs its
# two items and nothing else: 8 bytes as the 1e5-scaled int32 pair a prepared
# row holds, 16 as float64. That is why the working sets — by far the biggest
# thing here — are held as one and the floors, which are 32 points, are not.
_LIST_COORD_BYTES = 128

# Worst case, per *process*: 32 MB steady, and up to ~64 MB for the instant of
# a store, since a new entry is inserted before eviction brings the total back
# inside budget.
#
# Down from 48 MB with issue #369, because a prepared line got smaller: 9 bytes
# per coordinate (the int32 pair plus its level byte) against 16, so the
# 219-activity trip the perf doc measures is ~7.9 MB rather than 15.5 MB, and
# the per-(level, line) memo that could add up to 16 MB on top of that is
# gone — filtering by level costs what a memo hit did. At ~36 KB per activity
# the entry cap below admits ~890 activities before a trip is refused, against
# ~500 at 48 MB and 16 bytes.
#
# That is the bound on what is *cached*, and it is not the process's peak. A
# whole-trip request at a deep level materialises every kept point into a
# list-of-lists before it is serialised — for the trip the perf doc measures
# that is up to ~876,000 coordinates, about 112 MB live at once. This is not
# new, but it is the number that matters on a container which has been
# OOM-killed at ~779 MB against a 768 MB cap. The cached bound is 32 MB; the
# transient peak is roughly twice what the largest single response contains,
# and a box keeps that response small in practice.
#
# Adding --workers N multiplies it, as it does _GEO_CACHE_MAX_BYTES.
_TRACK_CACHE_MAX_BYTES = 32 * 1024 * 1024
# The working sets, levels and floors of one trip.
#
# Equal to the whole budget on purpose. This bound is *zoom-independent* — it
# is a property of the trip, not of the level being asked for — so unlike the
# level cache's per-level refusal it does not go away at shallow zoom. A trip
# past it is not cached at ANY zoom, and every request then repeats the DB
# load and the unpacking — or, for a trip not yet prepared, the polyline
# decode: ~4 s of CPU-bound Python holding the GIL on a single-process
# uvicorn, which is the mechanism that produced real 502s on /meta and
# /low-res. At 32 MB and 16 bytes per coordinate that cliff arrived at ~490
# activities, which is a shape this project's own comments treat as real (a
# long trip at a few activities a day); at 9 bytes it is ~890.
#
# So the only trip refused is one that could not be held even alone. That
# permits a single large trip to occupy the entire cache and evict every other
# — which is the right trade: evicting a warm trip costs one rebuild, refusing
# to cache costs a rebuild on every request, forever.
_TRACK_CACHE_MAX_ENTRY_BYTES = _TRACK_CACHE_MAX_BYTES

# Nothing evicted an entry whose project was never mutated again — the TTL above
# only turns a stale HIT into a MISS on the next *read* of that same key; a key
# nobody re-requests just sits in the dict, gzip bytes and all, for as long as
# the process lives. With enough distinct (user, project, variant) combinations
# touched over an API process's uptime, that is unbounded growth with no upper
# limit — the process OOMs on ordinary traffic with no single request to blame
# (issue #209's third incident: the API container was killed at ~779M after an
# hour of plain thumbnail requests, no resolve in flight). A hard cap plus an
# opportunistic sweep of already-expired entries on every store bounds this to
# a fixed number of live entries instead.
_GEO_CACHE_MAX_ENTRIES = 200

# An entry count is the wrong unit for this cache and was never a real bound.
# Entries here are whole gzipped project payloads, and they are not remotely
# uniform: a small trip's /meta is tens of KB, while a 180-day trip's full
# details payload serialises to ~35 MB of JSON before compression. 200 entries
# of the latter is multiple gigabytes, in an API container the deployment caps
# at 768 MB (docker-compose.yml.example) — and issue #209's third incident was
# already this class of failure, the container OOM-killed at ~779 MB with no
# single request to blame.
#
# Issue #276 hit the same wall from the other side: a 180-day trip's full geo
# and full details requests both failed after ~5 s while smaller payloads on
# the same project succeeded, which is what an OOM-killed container looks like
# to a client. So the cache is bounded by the thing that actually runs out —
# bytes — with the entry cap kept as a secondary guard against a flood of tiny
# entries.
_GEO_CACHE_MAX_BYTES = 64 * 1024 * 1024

# An individual payload larger than this is never cached at all. Holding one
# would evict most of the cache to make room for a single entry that, being
# that large, is also the one most likely to be a rarely-reopened trip.
_GEO_CACHE_MAX_ENTRY_BYTES = 16 * 1024 * 1024

# Per-project invalidation counter, bumped by every bust (issue #132). A reader
# captures it *before* its DB read and declines to persist its result if the
# counter moved meanwhile — otherwise a read that started before a mutation can
# refill the cache with the pre-mutation snapshot that mutation just evicted,
# wedging it there until the next bust.
# This dict is the fallback authority. When Redis is configured the counter is
# shared across processes instead — see _shared_generation (issue #173).
_geo_gen: dict[tuple, int] = {}


def _gen_redis_key(user_info_id: int, project_name: str) -> str:
    """Redis key holding a project's shared invalidation generation."""
    return f"traxjourney:geo:gen:{user_info_id}:{project_name}"


def _shared_generation(user_info_id: int, project_name: str) -> int | None:
    """The generation counter as Redis sees it, or None when it cannot answer.

    None means "no cross-process authority available" — the caller falls back to
    the process-local counter, which is exactly right in that situation: without
    a reachable broker no worker can be dispatched to, so this process is the
    only one mutating anything.
    """
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_gen_redis_key(user_info_id, project_name))
        return int(raw) if raw is not None else 0
    except Exception:  # noqa: BLE001 — broker hiccup → local counter
        return None


def bust_geo_cache(user_info_id: int, project_name: str) -> None:
    """Invalidate every full-res GeoJSON cache entry for this project.

    The cache keys on (user_info_id, name, encoded) so both the expanded and
    encoded payload variants are dropped. Also bumps the project's generation
    counter so any read already in flight refuses to write its now-stale result.

    The counter lives in Redis when one is configured (issue #173). Dropping the
    local dict only invalidates *this* process's copy, so once route resolution
    runs in a worker the API process would otherwise keep serving pre-resolve
    geometry until the TTL expired — a silent staleness worse than the recompute
    it avoids. Every process compares against the shared counter on read.
    """
    client = get_redis()
    if client is not None:
        try:
            client.incr(_gen_redis_key(user_info_id, project_name))
        except Exception:  # noqa: BLE001 — local bust below still covers this process
            _log.warning("could not bump the shared geo generation for %r", project_name)

    with _geo_cache_lock:
        for key in [k for k in _geo_cache if k[0] == user_info_id and k[1] == project_name]:
            _geo_cache.pop(key, None)
        gen_key = (user_info_id, project_name)
        _geo_gen[gen_key] = _geo_gen.get(gen_key, 0) + 1
    # The track cache holds Python objects rather than bytes and has only the
    # generation guard to fall back on. That guard depends on the counter bump
    # above actually landing — the Redis path swallows and logs its failures —
    # so a dropped bump would leave a pre-edit map served for the whole 15
    # minute TTL. Dropping the entries directly is the belt to that braces.
    with _geo_cache_lock:
        _track_cache.pop((user_info_id, project_name), None)


def _geo_generation(user_info_id: int, project_name: str) -> int:
    """Current invalidation generation for a project. Read before the DB load."""
    shared = _shared_generation(user_info_id, project_name)
    if shared is not None:
        return shared
    with _geo_cache_lock:
        return _geo_gen.get((user_info_id, project_name), 0)


def _geo_cache_get(cache_key: tuple) -> bytes | None:
    """Cached bytes for *cache_key*, or None when absent, expired or superseded.

    The generation check is what makes a *remote* bust visible: an entry this
    process cached is dropped when another process has since invalidated the
    project. It happens outside the lock — it may hit Redis, and holding the
    lock across a network call would serialise every cache read behind it.
    """
    with _geo_cache_lock:
        entry = _geo_cache.get(cache_key)
        if entry is None:
            return None
        gz_bytes, deadline, gen = entry
        if monotonic() >= deadline:
            _geo_cache.pop(cache_key, None)
            return None

    if gen != _geo_generation(cache_key[0], cache_key[1]):
        with _geo_cache_lock:
            _geo_cache.pop(cache_key, None)
        return None
    return gz_bytes


def _geo_cache_bytes() -> int:
    """Total bytes currently held. Callers must hold ``_geo_cache_lock``."""
    return sum(len(v[0]) for v in _geo_cache.values())


def _geo_cache_store(cache_key: tuple, gz_bytes: bytes, gen: int,
                     ttl_s: float = _GEO_CACHE_TTL_S) -> None:
    """Persist *gz_bytes* only if nothing busted the project since generation *gen*.

    The caller still serves what it computed — that payload is as fresh as the
    read that produced it. We only decline to *persist* an entry that may already
    be superseded; the cost is at most a redundant recompute on the next request.
    """
    if _geo_generation(cache_key[0], cache_key[1]) != gen:
        return
    now = monotonic()
    with _geo_cache_lock:
        expired = [k for k, (_, deadline, _) in _geo_cache.items() if deadline <= now]
        for k in expired:
            _geo_cache.pop(k, None)
        if len(gz_bytes) > _GEO_CACHE_MAX_ENTRY_BYTES:
            # Too big to be worth the room it would cost everything else.
            _geo_cache.pop(cache_key, None)
            return
        _geo_cache[cache_key] = (gz_bytes, now + ttl_s, gen)
        # Evict closest-to-expiry first, on both bounds, until within budget.
        # Bytes is the binding one; the entry cap only guards against a flood
        # of tiny payloads.
        while _geo_cache and (
            _geo_cache_bytes() > _GEO_CACHE_MAX_BYTES
            or len(_geo_cache) > _GEO_CACHE_MAX_ENTRIES
        ):
            soonest = min(_geo_cache, key=lambda k: _geo_cache[k][1])
            if soonest == cache_key and len(_geo_cache) == 1:
                break  # never evict the entry we were asked to store, alone
            _geo_cache.pop(soonest, None)


class _PreparedLine:
    """One line of a trip, prepared so any zoom and any box can serve from it.

    Four things, all zoom-independent:

    * ``points`` — the *working set*: the line already reduced by
      :func:`working_set` to the cap :func:`simplify_for_zoom` applies before
      it simplifies anything. Simplifying from it is identical to simplifying
      from the original at every zoom, and it is at most 4,000 points however
      long the track is. Held flat: an ``array("i")`` of 1e5-scaled integers
      for an activity, which is what its ``activity_geo_prepared`` row holds
      (8 bytes per coordinate, and exact — see ``src/models/prepared_geo.py``),
      or an ``array("d")`` for a segment prepared in memory (16). Against 128
      for a ``[lon, lat]`` list, because this is the one part large enough for
      the difference to decide whether caching a trip is affordable at all:
      7.9 MB rather than 112 MB for the 219-activity trip issue #276 measured.
    * ``levels`` — one byte per point, the lowest zoom level that keeps it
      (:func:`vertex_levels`). Serving a level is then a filter over the
      working set rather than a Ramer-Douglas-Peucker pass over it: 35–56 ms
      for a whole 219-activity trip against 1–2.5 s, identical output
      (issue #369).
    * ``bbox`` — of the working set, which is exactly the geometry that can be
      served, so "can this box show it" is O(1) instead of a walk over every
      coordinate (0.24 s to 0.34 s per whole-trip request, measured).
    * ``floor`` — what a line the viewport cannot show is served at, and what
      a level that keeps too few points falls back to. Precomputed because it
      is the answer for most lines of a long trip at most zooms.

    ``points`` is a plain list rather than an array for a line whose positions
    carry a third element, which simplification preserves and packing to pairs
    would drop, and None for a line nothing can simplify — one shorter than
    three points (a two-point GPX fallback) or one whose coordinates are not
    numbers. ``floor`` then holds that line verbatim and is always what is
    served, which is what it got before anything simplified it.
    """

    __slots__ = ("properties", "bbox", "points", "levels", "floor")

    def __init__(self, properties: Dict[str, Any], bbox: tuple | None,
                 points, levels: bytes | None, floor: list) -> None:
        self.properties = properties
        self.bbox = bbox
        self.points = points
        self.levels = levels
        self.floor = floor

    def at_level(self, level: int) -> list:
        """The line at *level*: :func:`filter_to_level` over the packed form.

        The same rule as the library function — every point whose level is at
        most *level*, or the floor when those are too few to read as a shape
        — so this is ``simplify_for_zoom(working_set, level)`` for the line,
        without unpacking the points that are not kept.
        """
        pts = self.points
        levels = self.levels
        if isinstance(pts, list):
            return filter_to_level(pts, levels, level)
        indices = range(0, len(pts), 2)
        if pts.typecode == "i":
            kept = [[pts[i] / COORD_SCALE, pts[i + 1] / COORD_SCALE]
                    for i, lowest in zip(indices, levels) if lowest <= level]
        else:
            kept = [[pts[i], pts[i + 1]]
                    for i, lowest in zip(indices, levels) if lowest <= level]
        return kept if len(kept) >= MIN_POINTS else self.floor

    def nbytes(self) -> int:
        pts = self.points
        if pts is None:
            return len(self.floor) * _LIST_COORD_BYTES
        if isinstance(pts, array):
            base = len(pts) * pts.itemsize + len(self.levels)
        else:
            base = len(pts) * _LIST_COORD_BYTES + len(self.levels)
        if self.floor is pts:
            # A line short enough that striding returned its argument.
            return base
        return base + len(self.floor) * _LIST_COORD_BYTES


class _PreparedTrack:
    """A whole trip prepared once, ready for any level and any box.

    The unit of caching is the *line*, not the level (issue #338). #325 put the
    viewport box in the cache key: each build was cheap, because it skipped the
    Ramer-Douglas-Peucker pass for lines the box could not show, but no build
    was reusable and every pan paid a fresh one. #331 took the box out again:
    one level then served every viewport, but a box-free build cannot skip
    anything, so every cold level ran RDP over the whole trip — measured 2.8 s
    to 4.8 s per level here, six times over in a zoom-heavy session.

    Both cached the wrong thing. A *line* prepared for every level is
    box-independent, so it is shareable like a level; and which lines to serve
    above their floor is exactly what the box decides, so a build is cheap
    like a per-box one.

    It also means the decode — 1.83 s for a 219-activity trip, once a fixed
    floor on every cold build — happens once per trip rather than once per
    level, and since issue #369 usually not at all: an activity's line is
    unpacked from its ``activity_geo_prepared`` row, so a cold build is a read
    of those rows plus the segments, which are prepared in memory.
    """

    __slots__ = ("lines", "nbytes")

    def __init__(self, lines: List[_PreparedLine]) -> None:
        self.lines = lines
        self.nbytes = sum(line.nbytes() for line in lines)


def _line_from_feature(feature: Dict[str, Any]) -> _PreparedLine:
    """Prepare a built feature in memory: segments, and anything not persisted."""
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    properties = feature.get("properties") or {}
    if not isinstance(coords, list) or len(coords) < 3:
        return _PreparedLine(
            properties, None, None, None, coords if isinstance(coords, list) else [])
    try:
        work = working_set(coords)
        flat = array("d", chain.from_iterable(work))
        widths = set(map(len, work))
        box = line_bbox(work)
        floor = floor_line(work)
        levels = vertex_levels(work)
    except (TypeError, ValueError, IndexError):
        # Malformed geometry is data, not a bug in the caller: one bad
        # point must not 500 a whole project's map. Serve it verbatim,
        # which is also what it got before anything simplified it.
        return _PreparedLine(properties, None, None, None, coords)
    # A GeoJSON position legally carries a third element, and
    # simplification preserves it, so a line with one stays a list of its
    # original positions rather than quietly losing elevation to the
    # packing. A line already at the floor is its own floor, and packing it
    # would hold the same points twice for no saving.
    if floor is work:
        packed = work
    else:
        packed = flat if widths == {2} else work
    return _PreparedLine(properties, box, packed, levels, floor)


def _line_from_blob(properties: Dict[str, Any], blob: bytes) -> _PreparedLine:
    """An activity's line from its ``activity_geo_prepared`` row: no decode."""
    flat, levels, box, _version = unpack_prepared_line(blob)
    n = len(flat) // 2
    # floor_line over the indices, so the floor is strided by the same rule
    # the library applies to the points themselves.
    floor = [[flat[2 * i] / COORD_SCALE, flat[2 * i + 1] / COORD_SCALE]
             for i in floor_line(range(n))]
    if n < 3:
        # Served verbatim at every level, as before it was prepared.
        return _PreparedLine(properties, None, None, None, floor)
    return _PreparedLine(properties, box, flat, levels, floor)


def _prepare_track(features: List[Dict[str, Any]]) -> _PreparedTrack:
    """Turn built features into lines that can be served at any zoom."""
    return _PreparedTrack([_line_from_feature(feature) for feature in features])


def _features_for(track: _PreparedTrack, level: int, box: tuple | None):
    """Features for *level* scoped to *box*.

    Features are never dropped. `geo` is read as a description of the whole
    trip by the segment-overlay reconciliation, by fit-to-bounds and by the
    export path, and a missing feature would silently break all three. A line
    the box cannot show is served at its floor, which keeps its shape and both
    its endpoints — and skips the filter, so a request only pays for what it
    can show.
    """
    out: List[Dict[str, Any]] = []
    for line in track.lines:
        if line.points is None or (
                box is not None and not bboxes_intersect(line.bbox, box)):
            coords = line.floor
        else:
            coords = line.at_level(level)
        out.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": line.properties,
        })
    return out


_track_cache: Dict[tuple, tuple] = {}

# One in-flight preparation per trip — see the use site in serve_simplified_geo.
# Separate from _geo_cache_lock, which is held only for dict access: this one is
# held across the whole build, and sharing them would serialise every cache read
# behind one trip's decode.
_track_build_locks: Dict[tuple, Lock] = {}
_track_build_locks_guard = Lock()

# A lock is tiny, but a dict keyed by every project an API process ever serves
# grows without bound, which is the shape of failure that OOM-killed this
# container on ordinary traffic before (#209). Unlocked entries are not in use,
# so they are dropped when the dict gets large; recreating one costs an insert.
_TRACK_BUILD_LOCKS_MAX = 256


def _track_build_lock(key: tuple) -> Lock:
    """The build lock for *key*, created on first use.

    A pruned entry can in principle be replaced while another thread still holds
    a reference to the old object, in which case both build — exactly the
    behaviour that existed before this lock, for one request, and never
    incorrect. Not worth refcounting to avoid.
    """
    with _track_build_locks_guard:
        lock = _track_build_locks.get(key)
        if lock is None:
            if len(_track_build_locks) >= _TRACK_BUILD_LOCKS_MAX:
                for idle in [k for k, v in _track_build_locks.items() if not v.locked()]:
                    del _track_build_locks[idle]
            lock = _track_build_locks[key] = Lock()
        return lock


def _track_cache_bytes() -> int:
    """Total bytes currently held. Callers must hold ``_geo_cache_lock``."""
    return sum(t.nbytes for t, _, _ in _track_cache.values())


def _track_cache_get(key: tuple) -> "_PreparedTrack | None":
    """The prepared trip for *key*, or None when absent, expired or stale.

    Mirrors :func:`_geo_cache_get`, including doing the generation check
    outside the lock — it may hit Redis.
    """
    with _geo_cache_lock:
        entry = _track_cache.get(key)
        if entry is None:
            return None
        track, deadline, gen = entry
        if monotonic() >= deadline:
            _track_cache.pop(key, None)
            return None
        # Refresh on read, so eviction is least-recently-*used* rather than
        # oldest-inserted: every entry shares one TTL, so without this
        # "closest to expiry" means "stored first", and the trip being looked
        # at right now loses to a cold one on age alone.
        _track_cache[key] = (track, monotonic() + _TRACK_CACHE_TTL_S, gen)
    if gen != _geo_generation(key[0], key[1]):
        with _geo_cache_lock:
            _track_cache.pop(key, None)
        return None
    return track


def _evict_tracks(protect: tuple) -> None:
    """Bring the track cache inside both bounds. Callers must hold the lock."""
    while _track_cache and (
        _track_cache_bytes() > _TRACK_CACHE_MAX_BYTES
        or len(_track_cache) > _TRACK_CACHE_MAX_ENTRIES
    ):
        soonest = min(_track_cache, key=lambda k: _track_cache[k][1])
        if soonest == protect and len(_track_cache) == 1:
            break  # never evict the entry we were asked to store, alone
        _track_cache.pop(soonest, None)


def _track_cache_store(key: tuple, track: _PreparedTrack, gen: int) -> None:
    """Persist *track* unless the project was busted since generation *gen*."""
    if _geo_generation(key[0], key[1]) != gen:
        return
    now = monotonic()
    with _geo_cache_lock:
        for k in [k for k, v in list(_track_cache.items()) if v[1] <= now]:
            _track_cache.pop(k, None)
        if track.nbytes > _TRACK_CACHE_MAX_ENTRY_BYTES:
            # Logged, not silent: this is the difference between "the cache is
            # working" and "every request on this trip rebuilds forever", and
            # it is invisible from the outside — X-Cache reads MISS either way.
            # warning, not info: every request on this trip now repeats the
            # load, holding the GIL, for as long as it is being viewed.
            _log.warning(
                "geo track too large to cache: %.1f MB for %r (cap %.0f MB)",
                track.nbytes / 1e6, key[1], _TRACK_CACHE_MAX_ENTRY_BYTES / 1e6)
            _track_cache.pop(key, None)
            return
        _track_cache[key] = (track, now + _TRACK_CACHE_TTL_S, gen)
        _evict_tracks(key)


# Public names for the three primitives above, used by the other per-project
# payload caches (currently /meta, see api.project_shared). They deliberately
# share this module's state: one generation counter and one bust per project
# covers every payload kind, and the TTL + generation guards (issues #132, #173)
# exist once rather than being re-derived per endpoint.
project_cache_get = _geo_cache_get
project_cache_store = _geo_cache_store
project_cache_generation = _geo_generation
bust_project_cache = bust_geo_cache


def _linestring(coords: List[List[float]], properties: Dict[str, Any]) -> Dict[str, Any]:
    """Build a GeoJSON Feature with a LineString geometry."""
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": coords,  # [[lon, lat], ...]
        },
        "properties": properties,
    }


# ── City autocomplete (issue #49) ──────────────────────────────────────────────
# Proxies OpenStreetMap Nominatim so the Flutter web client isn't blocked by CORS
# and the shared usage policy (a descriptive User-Agent, modest volume — the
# client debounces) stays on the server. We store only the display string.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_PLACES_UA = USER_AGENT


def _nominatim_search(q: str) -> List[Dict[str, Any]]:
    """Raw Nominatim search for *q* (extracted so tests can stub the upstream)."""
    with track_external("nominatim", "/search"):
        resp = requests.get(
            _NOMINATIM_URL,
            params={"q": q, "format": "jsonv2", "addressdetails": 1,
                    "limit": 8, "accept-language": "en"},
            headers={"User-Agent": _PLACES_UA},
            timeout=6,
        )
        resp.raise_for_status()
        return resp.json()


def _place_label(result: Dict[str, Any]) -> str | None:
    """Reduce a Nominatim result to a 'City, Country' label, or None.

    Requires a settlement-level field in the address (city/town/village/…); a
    result carrying only a country or a non-settlement name is dropped, so the
    suggestions stay cities rather than arbitrary places.
    """
    addr = result.get("address") or {}
    city = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("hamlet"))
    if not city:
        return None
    country = addr.get("country")
    return f"{city}, {country}" if country else city


@router.get("/places", summary="City autocomplete for a person's residence")
def places(
    q: str,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> List[str]:
    """Return up to a handful of distinct 'City, Country' suggestions for *q*."""
    q = q.strip()
    if len(q) < 2:
        return []
    try:
        raw = _nominatim_search(q)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    seen: set[str] = set()
    out: List[str] = []
    for result in raw:
        label = _place_label(result)
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


@router.get("/project/low-res", summary="Low-res GeoJSON for fast map render")
def project_geo_low_res(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Return low-res GeoJSON — straight lines per activity, arcs per segment.

    Always computed from the live project (no cached ``low_res_geo_json``
    column) so segment arcs are always present regardless of when the DB row
    was last saved.  No GPS polyline decoding occurs here — activities use
    two-point straight lines — so a MISS is cheap on its own; it still goes
    through the same ``_geo_cache``/generation machinery as ``/meta`` and the
    full-res endpoint because this is one of exactly two calls
    fired in parallel on *every* project open, and recomputing from scratch on
    every single request — cold or warm alike — added up on its own.

    include_heavy=False because ``_compute_low_res_geo`` reads only
    start_latlng/end_latlng and segment geometry: loading every activity's
    summary_polyline and elevation_profile_json meant paying for their overflow
    pages to build a payload that never looks at them, which on a cold cache put
    this endpoint (issue #178) at 13 s — over the client's whole load budget.
    """
    user_info_id = int(current_user["sub"])
    t0 = time.time()
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        owner_id = row.user_info_id

        cache_key = (owner_id, name, "low-res")
        cached_bytes = _geo_cache_get(cache_key)
        if cached_bytes is not None:
            return Response(
                content=cached_bytes,
                media_type="application/json",
                headers={"Content-Encoding": "gzip", "X-Cache": "HIT"},
            )

        gen = _geo_generation(owner_id, name)  # before the read, so a bust wins
        project = _repo.get_project(
            sess, owner_id, name,
            include_heavy=False,
        )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    t1 = time.time()
    gz_bytes = gzip_lib.compress(_compute_low_res_geo(project).encode(), compresslevel=6)
    _geo_cache_store(cache_key, gz_bytes, gen)
    _log.info("geo_low_res name=%s load=%.3fs build=%.3fs cache=MISS",
              name, t1 - t0, time.time() - t1)
    return Response(
        content=gz_bytes,
        media_type="application/json",
        headers={"Content-Encoding": "gzip", "X-Cache": "MISS"},
    )


def _activity_properties(activity) -> Dict[str, Any]:
    return {
        "type": "activity",
        "activity_id": activity.id,
        "name": activity.name,
        "sport_type": activity.type,
    }


def _activity_feature(activity, summary_polyline: str | None,
                      encoded: bool) -> Dict[str, Any] | None:
    """*activity*'s full-resolution feature, or None when there is nothing to draw.

    *summary_polyline* is passed rather than read off *activity* because the
    simplified path loads activities light and reads the polyline only for
    the rows that still need one — see :func:`_prepared_lines`.
    """
    if is_encrypted_envelope(summary_polyline):
        # Encrypted geometry (issue #29) — the server can't decode this;
        # skip it entirely. The client builds this activity's track
        # itself, from its own decrypted copy, once unlocked.
        return None

    if summary_polyline and encoded:
        # Pass the Google-encoded polyline through untouched; the client
        # decodes it. No server-side decode, tiny payload.
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": []},
            "properties": {**_activity_properties(activity), "polyline": summary_polyline},
        }
    if summary_polyline:
        # Expanded form — decode server-side so any client renders it.
        decoded = polyline_lib.decode(summary_polyline)
        coords = [[lon, lat] for lat, lon in decoded]
        if len(coords) < 2:
            return None
        return _linestring(coords, _activity_properties(activity))
    if activity.start_latlng and activity.end_latlng:
        # No polyline (GPX import / private activity) — straight line fallback
        coords = [
            [activity.start_latlng[1], activity.start_latlng[0]],
            [activity.end_latlng[1],   activity.end_latlng[0]],
        ]
        return _linestring(coords, _activity_properties(activity))
    return None  # no coordinates at all


def _segment_feature(seg) -> Dict[str, Any] | None:
    """*seg*'s feature: its resolved route, else a great-circle arc."""
    if seg.route_mode in ("rail", "ferry", "bus") and seg.route_polyline:
        coords = json.loads(seg.route_polyline)
    else:
        # great_circle_points returns [(lat, lon), ...]
        pts = great_circle_points(
            seg.start.lat, seg.start.lon,
            seg.end.lat, seg.end.lon,
            n_points=50,
        )
        coords = [[lon, lat] for lat, lon in pts]
    if len(coords) < 2:
        return None
    return _linestring(coords, {
        "type": "segment",
        "segment_id": seg.id,
        "segment_type": seg.segment_type,
        "label": seg.label,
        "route_mode": seg.route_mode,
    })


def _build_full_geo_features(project: Project, encoded: bool = False) -> List[Dict[str, Any]]:
    """Build the full-resolution GeoJSON features for *project*.

    When ``encoded`` is True, activities with a GPS track carry their
    Google-encoded ``summary_polyline`` verbatim in ``properties.polyline`` with
    an empty ``coordinates`` array; the client decodes it back to
    ``[[lon, lat], …]``. This keeps the payload an order of magnitude smaller
    than expanding every point server-side (a 120-activity trip drops from
    ~17.7 MB to a couple of MB) and skips the server-side decode.

    When ``encoded`` is False (the default), activity polylines are expanded to
    full ``coordinates`` server-side. This is the backward-compatible format any
    client renders directly; a client that doesn't decode encoded polylines (an
    older build) would otherwise show nothing for those activities. Only clients
    that opt in via ``?encoded=1`` receive the compact form.

    Activities without a polyline (GPX/private) fall back to a two-point
    straight line. Segments always use expanded coordinates (already short).
    """
    features: List[Dict[str, Any]] = []
    for item in project.items:
        if item.item_type == "activity":
            activity = project.activity_by_id(item.activity_id)
            if activity is None:
                continue
            feature = _activity_feature(activity, activity.summary_polyline, encoded)
        elif item.item_type == "segment" and item.segment is not None:
            feature = _segment_feature(item.segment)
        else:
            continue
        if feature is not None:
            features.append(feature)
    return features


def _prepared_lines(project: Project) -> List[_PreparedLine]:
    """Every line of *project*, prepared, with as little work as the DB allows.

    An activity with a current ``activity_geo_prepared`` row is unpacked from
    it: no decode, no simplification. One without — never prepared, or
    prepared under an older ``PREPARED_GEO_VERSION`` — is prepared here from
    its polyline, exactly as the write path would have, and the row is written
    back so the next cold open of this trip finds it. That is what makes the
    first open after an upgrade correct and the second fast (issue #369).

    *project* is a light load (``include_heavy=False``): the polylines of the
    rows that need one are read here, for those rows only. Segments are few
    and short (a resolved rail route is ~2,000 points) and are prepared in
    memory every time, as they always were.
    """
    ids = [item.activity_id for item in project.items
           if item.item_type == "activity" and item.activity_id is not None]
    blobs: Dict[int, bytes] = {}
    polylines: Dict[int, str | None] = {}
    if ids:
        with get_session() as sess:
            blobs = dict(sess.exec(
                select(DBActivityGeoPrepared.activity_id, DBActivityGeoPrepared.blob)
                .where(DBActivityGeoPrepared.activity_id.in_(ids),
                       DBActivityGeoPrepared.version == PREPARED_GEO_VERSION)
            ).all())
            missing = [i for i in ids if i not in blobs]
            if missing:
                polylines = dict(sess.exec(
                    select(DBActivity.id, DBActivity.summary_polyline)
                    .where(DBActivity.id.in_(missing))
                ).all())

    # Preparing is seconds of CPU for a whole trip, and deliberately outside the
    # session above: holding a connection across it serialises other writers
    # against a read, and on SQLite that is how a cold open starts timing out
    # unrelated requests.
    prepared: Dict[int, bytes] = {}
    for activity_id, poly in polylines.items():
        blob = prepare_polyline(poly)
        if blob is not None:
            prepared[activity_id] = blob
    blobs.update(prepared)

    if prepared:
        with get_session() as sess:
            try:
                for activity_id, blob in prepared.items():
                    # Guarded on the polyline this blob was built from: a
                    # writer can land during the seconds of preparation above.
                    # See store_prepared_if_unchanged for what goes wrong
                    # without it.
                    store_prepared_if_unchanged(
                        sess, activity_id, polylines[activity_id], blob)
                sess.commit()
            except Exception:  # noqa: BLE001
                # Two cold requests preparing the same rows at once, or a
                # locked database. The trip is served from memory either
                # way; the rows are simply prepared again on the next miss.
                sess.rollback()
                _log.warning("could not write back prepared geometry for %r", project.name)

    lines: List[_PreparedLine] = []
    for item in project.items:
        if item.item_type == "activity":
            activity = project.activity_by_id(item.activity_id)
            if activity is None:
                continue
            blob = blobs.get(activity.id)
            if blob is not None:
                lines.append(_line_from_blob(_activity_properties(activity), blob))
                continue
            feature = _activity_feature(activity, polylines.get(activity.id), encoded=False)
        elif item.item_type == "segment" and item.segment is not None:
            feature = _segment_feature(item.segment)
        else:
            continue
        if feature is not None:
            lines.append(_line_from_feature(feature))
    return lines


def _gzip_geo(features: List[Dict[str, Any]]) -> bytes:
    json_bytes = json.dumps({"type": "FeatureCollection", "features": features}).encode()
    return gzip_lib.compress(json_bytes, compresslevel=6)


def warm_geo_cache(user_info_id: int, name: str) -> None:
    """Recompute and cache both full-res GeoJSON variants for a project.

    Called from background tasks right after ``bust_geo_cache`` so that the next
    edit-mode load is a fast cache HIT instead of a cold recompute (which, on a
    spinning-disk NAS, can exceed the client timeout and leave activities as
    low-res straight lines). Warms both the encoded and expanded payloads so a
    client on either format gets a HIT. Best-effort: any failure is swallowed
    since the endpoint will simply recompute on demand.
    """
    try:
        gen = _geo_generation(user_info_id, name)
        with get_session() as sess:
            project = _repo.get_project(sess, user_info_id, name, include_elevation=False)
        if project is None:
            return
        for enc in (True, False):
            _geo_cache_store(
                (user_info_id, name, enc),
                _gzip_geo(_build_full_geo_features(project, encoded=enc)),
                gen,
            )
    except Exception:
        pass


def _parse_bbox(raw: str) -> tuple:
    """``"minLon,minLat,maxLon,maxLat"`` to a validated tuple, or 400.

    Deliberately strict. A malformed box that fell through as "no box" would
    silently serve the whole trip, and the client would then believe it holds
    viewport-scoped geometry it does not have — the failure would show up as a
    mysterious payload size, not as an error.

    An antimeridian-crossing box (min_lon >= max_lon) is rejected rather than
    split. The client omits the parameter in that case, which serves the whole
    trip at the requested zoom — correct, just not scoped.
    """
    parts = raw.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bbox must be minLon,minLat,maxLon,maxLat",
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bbox must be four numbers",
        )
    if not (-180.0 <= min_lon < max_lon <= 180.0
            and -90.0 <= min_lat < max_lat <= 90.0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bbox is out of range or inverted",
        )
    return (min_lon, min_lat, max_lon, max_lat)


def serve_simplified_geo(
    owner_id: int,
    name: str,
    zoom: float,
    bbox: str | None,
    load_project: Callable[[], Project | None],
) -> Response:
    """Serve *name*'s geometry simplified to roughly one pixel at *zoom*.

    The body of :func:`project_geo_simplified` — see there for what the answer
    means — shared with the token-scoped share route (issue #321) so a public
    viewer gets the same payload, and the same cache entries, an owner does.
    The caller decides *who* is asking and hands over the owner's id and the
    project name; everything from there on is identity-independent, which is
    also why nothing caller-specific may reach the cache keys.

    ``load_project`` is called only on a track-cache miss, and must read the
    project **fresh from the DB** — a light load is enough, the geometry comes
    from ``activity_geo_prepared`` (see :func:`_prepared_lines`). What it
    returns is prepared into an entry held for 15 minutes, so a caller's own
    short-lived project cache must not be its source: that would stretch that
    cache's staleness window to the track cache's (issue #321).

    Cached in two layers, because the two costs are different sizes.

    The *trip* — each line as the working set, per-vertex levels, bounding box
    and coarseness floor that every zoom is served from — is read from the
    rows the write path keeps, and prepared here only for rows that have none
    (issue #369; before that, preparing one decoded every activity polyline,
    1.83 s measured for a 219-activity trip, and every level then ran its own
    Ramer-Douglas-Peucker pass, 1–2.5 s more). It is both zoom- and
    box-independent, so one entry serves every level and every viewport.

    Serving from it is a filter per line — the points whose level is at most
    the one asked for — and the box decides which lines are worth filtering
    at all, so a request only pays for what it can show (issue #338). Neither
    #325's per-box builds (cheap but unshareable) nor #331's box-free levels
    (shareable but unskippable) had both.

    The *bytes* — this level restricted to this box and gzipped — are cached in
    front of that, keyed by the tile-snapped box as well, because serialising
    is not free either and a repeat request should cost nothing. A miss there
    falls through to the prepared trip, never to a rebuild.

    The box is snapped server-side (:func:`snap_bbox_to_tiles`) so an unsnapped
    client cannot mint an entry per pan pixel. Both layers are generation
    checked, so one bust per mutation still covers every level and every box —
    and, since the keys are the owner's, one bust covers the share route too.
    """
    if not (0 <= zoom <= 22):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Zoom must be between 0 and 22",
        )
    # Quantised to whole levels: a continuous camera zoom would otherwise mint
    # a distinct cache entry per pixel of pinch.
    #
    # Rounded UP, not down. Flooring served zoom 11.9 the zoom-11 tolerance —
    # 54 m instead of 29 m, a 1.87x over-simplification and about two pixels
    # of visible drift. Ceiling errs towards more detail than asked for, which
    # is invisible, and costs no extra cache entries.
    level = math.ceil(zoom)
    # Snapped here, not trusted from the client: the snapped box is both what
    # gets filtered against and what keys the cache, so an unsnapped one would
    # mint an entry per pan pixel.
    box = None
    box_key = "all"
    if bbox is not None:
        box, tiles = snap_bbox_to_tiles(_parse_bbox(bbox), level)
        box_key = "{}.{}.{}.{}".format(*tiles)
    # Cheapest path first: the exact bytes this caller asked for.
    byte_key = (owner_id, name, f"simplified-{level}-{box_key}")
    cached_bytes = _geo_cache_get(byte_key)
    if cached_bytes is not None:
        return Response(
            content=cached_bytes,
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "X-Cache": "HIT"},
        )
    gen_for_bytes = _geo_generation(owner_id, name)
    track_key = (owner_id, name)
    track = _track_cache_get(track_key)

    cache_state = "HIT"
    t0 = time.time()
    if track is None:
        # Reported from the state the request *arrived* in, so a waiter still
        # reads MISS: the trip was not prepared when it asked, which is what
        # this header has always meant.
        cache_state = "MISS"
        # One preparation per trip at a time. Two cold requests for the same
        # trip each used to build it in full — measured 3,474 ms each, for one
        # answer. The loser now waits for the winner's entry instead.
        #
        # The wait happens on a threadpool thread (these endpoints are sync
        # `def`, so Starlette runs them there) and costs no CPU, which is the
        # whole point: N concurrent cold requests used to mean N times the
        # decode contending for one GIL, and now mean one build and N-1 threads
        # parked on a lock.
        with _track_build_lock(track_key):
            # The winner may have finished while this request queued.
            track = _track_cache_get(track_key)
            if track is None:
                gen = _geo_generation(owner_id, name)  # before the read, so a bust wins
                project = load_project()
                if project is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
                track = _PreparedTrack(_prepared_lines(project))
                _track_cache_store(track_key, track, gen)

    t1 = time.time()
    features = _features_for(track, level, box)
    t2 = time.time()
    gz_bytes = _gzip_geo(features)
    # Serialising is not free, and reusing a prepared trip does not avoid it:
    # gzipping a large level measured ~0.5 s, which on a single-process server
    # is still enough GIL-holding CPU to time out someone else's request. So
    # the bytes are cached per (level, box) in front of it.
    #
    # This is not the cache #325 had. A miss here falls through to the
    # *prepared trip*, not to a rebuild, so a pan to a new box costs the lines
    # it newly revealed plus one gzip, rather than decoding every polyline in
    # the trip again. Keeping the short TTL for the reason the original comment
    # gave: there are many of these, they share a cache with /project,
    # /low-res and /meta, and they are by far the cheapest thing in it to
    # rebuild.
    _geo_cache_store(byte_key, gz_bytes, gen_for_bytes, ttl_s=_SIMPLIFIED_CACHE_TTL_S)
    # Same shape as the geo_low_res line, so the cold build is measurable on
    # the VPS without a harness: load is the trip (zero on a track HIT), build
    # the filter, gzip the serialisation.
    _log.info("geo_simplified name=%s level=%d box=%s load=%.3fs build=%.3fs gzip=%.3fs cache=%s",
              name, level, box_key, t1 - t0, t2 - t1, time.time() - t2, cache_state)
    return Response(
        content=gz_bytes,
        media_type="application/json",
        headers={"Content-Encoding": "gzip", "X-Cache": cache_state},
    )


def load_project_for_geo(owner_id: int, name: str) -> Project | None:
    """A project's activities and segments, loaded fresh from the DB in its own session.

    The ``load_project`` :func:`serve_simplified_geo` wants: it reads through
    to the database every time, and holds no session open across the
    simplification that follows. Light — no polylines — because the geometry
    comes from ``activity_geo_prepared`` (issue #369).
    """
    with get_session() as sess:
        return _repo.get_project(
            sess, owner_id, name,
            include_heavy=False,
        )


@router.get("/project/simplified", summary="Zoom-appropriate GeoJSON (gzip)")
def project_geo_simplified(
    name: str,
    zoom: float,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
    bbox: str | None = None,
):
    """Full-res geometry simplified to roughly one pixel at *zoom*.

    The client holds whatever this returns, so the size of that is the size of
    its map geometry. At the zoom that shows a whole trip, a pixel covers
    hundreds of metres and the geometry collapses to a fraction of its full
    resolution — which is all the screen can show anyway. Zooming in asks for
    more, and gets it.

    An additional endpoint: ``/project`` is unchanged and still serves full
    resolution, for shipped clients and for anything that genuinely needs
    every point (track editing, export).

    ``bbox`` — ``minLon,minLat,maxLon,maxLat`` — additionally scopes it to
    what is on screen (issue #324). Zoom bounds the detail, not the extent, so
    without it a deep zoom still returns the whole trip at that detail and
    simplifies all of it: measured at 6.98 s of server CPU for a 219-activity
    trip at zoom 15, against 0.19 s at zoom 9 — the cost RISES with zoom while
    the saving falls (issue #324). A line outside the box is reduced to the
    floor a whole-trip zoom would have given it and skips simplification
    entirely; no feature is ever dropped, because `geo` is read as a
    description of the whole trip elsewhere in the client.

    Optional, and ignorable. An older client sends no box and gets exactly
    what it did before; an older server ignores the parameter and serves the
    whole trip, which is a superset of what was asked for.

    Caching — two layers, both keyed on the *owner*, so this route and the
    token-scoped share one (issue #321) share every entry — is described on
    :func:`serve_simplified_geo`, which is where the work happens.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        owner_id = resolve_project(sess, user_info_id, name, owner).user_info_id
    return serve_simplified_geo(
        owner_id, name, zoom, bbox, lambda: load_project_for_geo(owner_id, name))


@router.get("/project", summary="Full-resolution GeoJSON (gzip)")
def project_geo(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    encoded: bool = False,
    owner: OwnerParam = None,
):
    """Return a GeoJSON FeatureCollection for *name*.

    Pass ``encoded=1`` to receive activity tracks as Google-encoded ``polyline``
    properties (empty ``coordinates``) for a much smaller payload — the client
    decodes them. The default (``encoded=0``) expands every activity polyline to
    full ``coordinates`` server-side so any client renders it directly. GPX/
    private activities always use a two-point ``coordinates`` line; segments
    always use expanded ``coordinates``. GeoJSON coordinates are
    [longitude, latitude] as per the spec.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        owner_id = row.user_info_id

        cache_key = (owner_id, name, encoded)
        cached_bytes = _geo_cache_get(cache_key)
        if cached_bytes is not None:
            return Response(
                content=cached_bytes,
                media_type="application/json",
                headers={"Content-Encoding": "gzip", "X-Cache": "HIT"},
            )

        gen = _geo_generation(owner_id, name)  # before the read, so a bust wins
        project = _repo.get_project(
            sess, owner_id, name,
            include_elevation=False,
        )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    gz_bytes = _gzip_geo(_build_full_geo_features(project, encoded=encoded))
    _geo_cache_store(cache_key, gz_bytes, gen)
    return Response(
        content=gz_bytes,
        media_type="application/json",
        headers={"Content-Encoding": "gzip", "X-Cache": "MISS"},
    )

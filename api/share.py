"""Public share endpoints — no authentication required.

Routes:
    GET /api/share/{token}                                     — project details for a shared link
    GET /api/share/{token}/meta                                — lightweight details (no elevation/polylines)
    GET /api/share/{token}/geo                                 — GeoJSON for a shared project
    GET /api/share/{token}/geo/simplified                      — GeoJSON simplified to a zoom (and bbox)
    GET /api/share/{token}/stats                               — project statistics (no auth required)
    GET /api/share/{token}/tiles/{z}/{x}/{y}.png               — raster track tile (cached)
    GET /api/share/{token}/photos/{memory_id}/{uuid}/thumb     — memory photo thumbnail (no auth)
    GET /api/share/{token}/photos/{memory_id}/{uuid}           — memory photo full-res (no auth)

Both the full-project token and the no-memories token are accepted by every
endpoint.  Visit events are recorded in DBShareVisit for the project owner.
"""
from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional


class _TTLCache:
    """Minimal thread-safe in-process TTL cache — no external dependencies."""

    def __init__(self, ttl: float = 60.0) -> None:
        self._ttl = ttl
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
        if entry is None:
            return None
        value, exp = entry
        if time.monotonic() > exp:
            with self._lock:
                self._data.pop(key, None)
            return None
        return value

    def set(self, key, value) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic() + self._ttl)

    def delete(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)


# Keyed by share token.  TTL is intentionally short so edits propagate quickly.
_project_cache: _TTLCache = _TTLCache(ttl=60.0)       # full project object
_project_meta_cache: _TTLCache = _TTLCache(ttl=60.0)  # lightweight project object (no polyline/elevation)
_details_cache: _TTLCache = _TTLCache(ttl=60.0)        # serialised full-details dict
_meta_cache: _TTLCache = _TTLCache(ttl=60.0)           # serialised stripped-details dict


def invalidate_share_cache(token: str) -> None:
    """Evict all cached entries for *token*. Call after any project mutation."""
    _project_cache.delete(token)
    _project_meta_cache.delete(token)
    _details_cache.delete(token)
    _meta_cache.delete(token)

from models.db import get_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from sqlmodel import select

from api.deps import get_optional_current_user
from api.geo import _build_full_geo_features, serve_simplified_geo
from models.project_db import (
    DBMemory, DBMemoryComment, DBMemoryLike, DBProject, DBShareMemoryContent, DBShareVisit,
)
from models.user import UserInfo
from src.project.repo_core import _parse_day_meta_json
from src.utils.encryption_check import is_encrypted_envelope

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
from src.models.great_circle import great_circle_points
from src.models.project import tag_options_with_untagged
from src.project.project_repo import ProjectRepo, _compute_stats
from src.tile_renderer import get_cached_tile, get_or_build_features, get_or_create_tile
from src.utils.encryption_check import is_encrypted_envelope

from src.utils.photo_paths import photo_file, photo_folder

router = APIRouter(prefix="/api/share", tags=["share"])

_repo = ProjectRepo()


def _get_project_and_type(token: str):
    """Look up a project by either share token.

    Returns (project, token_type, project_id, owner_user_info_id).
    token_type is "full" or "no_memories".  Raises 404 if neither matches.
    Result is cached for 60 s to avoid repeated DB + repo work on the same token.
    """
    cached = _project_cache.get(token)
    if cached is not None:
        return cached

    with get_session() as sess:
        row = sess.exec(
            select(DBProject).where(DBProject.share_token == token)
        ).first()
        if row is not None:
            token_type = "full"
        else:
            row = sess.exec(
                select(DBProject).where(
                    DBProject.share_token_no_memories == token
                )
            ).first()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Shared project not found",
                )
            token_type = "no_memories"
        project = _repo.get_project_by_id(sess, row.id)
        project_id = row.id
        owner_user_info_id = row.user_info_id
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shared project not found",
        )
    result = (project, token_type, project_id, owner_user_info_id)
    _project_cache.set(token, result)
    return result


def _get_meta_project_and_type(token: str):
    """Lightweight variant of _get_project_and_type for the /meta endpoint.

    Uses get_project_by_id_meta() which defers summary_polyline and
    elevation_profile_json from the SQL SELECT.  On spinning-disk NAS storage
    this avoids hundreds of SQLite overflow-page reads, dropping cold meta load
    from ~19 s to under 1 s.

    Falls back to the full _project_cache if already warm so we never load the
    project twice when the full endpoint was called first.
    """
    # If the full project is already cached, it has everything meta needs too.
    full = _project_cache.get(token)
    if full is not None:
        return full

    # Check the lightweight meta-project cache.
    cached = _project_meta_cache.get(token)
    if cached is not None:
        return cached

    with get_session() as sess:
        row = sess.exec(
            select(DBProject).where(DBProject.share_token == token)
        ).first()
        if row is not None:
            token_type = "full"
        else:
            row = sess.exec(
                select(DBProject).where(
                    DBProject.share_token_no_memories == token
                )
            ).first()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Shared project not found",
                )
            token_type = "no_memories"
        project = _repo.get_project_by_id_meta(sess, row.id)
        project_id = row.id
        owner_user_info_id = row.user_info_id

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shared project not found",
        )
    result = (project, token_type, project_id, owner_user_info_id)
    _project_meta_cache.set(token, result)
    return result


def _record_visit(
    project_id: int,
    token_type: str,
    aid: Optional[str],
    current_user: Optional[dict],
) -> None:
    """Upsert a visit record.  Errors are swallowed so they never affect the response."""
    try:
        now = time.time()
        with get_session() as sess:
            if current_user is not None:
                user_info_id = int(current_user["sub"])
                existing = sess.exec(
                    select(DBShareVisit).where(
                        DBShareVisit.project_id == project_id,
                        DBShareVisit.token_type == token_type,
                        DBShareVisit.user_info_id == user_info_id,
                    )
                ).first()
                if existing:
                    existing.last_seen_at = now
                    sess.add(existing)
                else:
                    sess.add(DBShareVisit(
                        project_id=project_id,
                        token_type=token_type,
                        visitor_type="registered",
                        user_info_id=user_info_id,
                        first_seen_at=now,
                        last_seen_at=now,
                    ))
            elif aid:
                existing = sess.exec(
                    select(DBShareVisit).where(
                        DBShareVisit.project_id == project_id,
                        DBShareVisit.token_type == token_type,
                        DBShareVisit.anonymous_id == aid,
                    )
                ).first()
                if existing:
                    existing.last_seen_at = now
                    sess.add(existing)
                else:
                    sess.add(DBShareVisit(
                        project_id=project_id,
                        token_type=token_type,
                        visitor_type="anonymous",
                        anonymous_id=aid,
                        first_seen_at=now,
                        last_seen_at=now,
                    ))
            sess.commit()
    except Exception:
        pass


def _strip_encrypted_memory_fields(
    items: List[Dict[str, Any]],
    share_content: Optional[Dict[int, DBShareMemoryContent]] = None,
) -> None:
    """Never leak raw E2EE ciphertext to an anonymous share viewer.

    Memory items whose `name`/`description` are client-side ciphertext
    envelopes (issue #26) have those fields replaced with None in place —
    otherwise the raw `v1.<b64>.<b64>` envelope would be rendered as if it
    were the actual title/notes (issue #28 Part A).

    `share_content` (issue #28 Part B) maps memory_id -> DBShareMemoryContent
    row. When a stripped field has a matching row, its `share_name_ciphertext`/
    `share_description_ciphertext` is populated alongside the None so a viewer
    holding the share-link's URL-fragment key can still decrypt it — these are
    new keys, distinct from `name`/`description`, so the client can tell
    "share-encrypted, decrypt with the fragment key" apart from anything else.

    Also sets `name_encrypted`/`description_encrypted` booleans whenever a
    field is stripped (regardless of whether share content exists for it) —
    a client-side `null` alone is ambiguous between "genuinely empty" and
    "encrypted", and the anonymous viewer UI needs to tell those apart to
    show an explicit "unavailable" state instead of a blank title/notes.
    """
    share_content = share_content or {}
    for item in items:
        if item.get("item_type") != "memory":
            continue
        mem = item.get("memory")
        if not mem:
            continue
        name_enc = is_encrypted_envelope(mem.get("name"))
        desc_enc = is_encrypted_envelope(mem.get("description"))
        if not (name_enc or desc_enc):
            continue
        content_row = share_content.get(mem.get("id"))
        if name_enc:
            mem["name"] = None
            mem["name_encrypted"] = True
            if content_row is not None and content_row.name_ciphertext:
                mem["share_name_ciphertext"] = content_row.name_ciphertext
        if desc_enc:
            mem["description"] = None
            mem["description_encrypted"] = True
            if content_row is not None and content_row.description_ciphertext:
                mem["share_description_ciphertext"] = content_row.description_ciphertext


def _fetch_share_content(memory_ids: List[int]) -> Dict[int, DBShareMemoryContent]:
    """Load DBShareMemoryContent rows (token_type="full") for these memory ids."""
    if not memory_ids:
        return {}
    with get_session() as sess:
        rows = sess.exec(
            select(DBShareMemoryContent).where(
                DBShareMemoryContent.memory_id.in_(memory_ids),
                DBShareMemoryContent.token_type == "full",
            )
        ).all()
    return {row.memory_id: row for row in rows}


@router.get("/{token}", summary="Get shared project details")
def shared_project(
    token: str,
    aid: Optional[str] = Query(default=None),
    current_user: Annotated[Optional[dict], Depends(get_optional_current_user)] = None,
):
    """Return project details (same shape as GET /api/projects/{name}).

    Memory items are stripped from the response when the no-memories token is used.
    """
    project, token_type, project_id, owner_uid = _get_project_and_type(token)
    _record_visit(project_id, token_type, aid, current_user)
    cached_result = _details_cache.get(token)
    if cached_result is None:
        result = _repo.to_dict(project)
        # Journal entries, encounters and people are always private (third-party
        # PII) — strip from all shared views.
        result.pop("people", None)
        result.pop("groups", None)
        result["items"] = [
            item for item in (result.get("items") or [])
            if item.get("item_type") not in ("journal", "encounter")
        ]
        if token_type == "no_memories":
            result["items"] = [
                item for item in result["items"]
                if item.get("item_type") != "memory"
            ]
        mem_ids = [
            item["memory"]["id"] for item in result["items"]
            if item.get("item_type") == "memory" and item.get("memory")
        ]
        _strip_encrypted_memory_fields(result["items"], _fetch_share_content(mem_ids))
        with get_session() as sess:
            owner = sess.exec(
                select(UserInfo).where(UserInfo.id == owner_uid)
            ).first()
        result["owner_name"] = owner.display_name if owner else ""
        _details_cache.set(token, result)
        cached_result = result
    return cached_result


@router.get("/{token}/meta", summary="Get shared project metadata (lightweight)")
def shared_project_meta(
    token: str,
    aid: Optional[str] = Query(default=None),
    current_user: Annotated[Optional[dict], Depends(get_optional_current_user)] = None,
):
    """Same shape as GET /{token} but with elevation_profile and map.summary_polyline
    absent from each activity.  Uses a lightweight project load that defers those
    two heavy TEXT columns from SQLite, keeping cold-cache response time under 1 s.
    """
    project, token_type, project_id, owner_uid = _get_meta_project_and_type(token)
    _record_visit(project_id, token_type, aid, current_user)

    cached = _meta_cache.get(token)
    if cached is not None:
        return cached

    # Build the response dict.  The lightweight project already has
    # elevation_profile=None and summary_polyline=None on all activities,
    # so to_dict() produces the stripped payload without extra work.
    result = _repo.to_dict(project)
    # Journal entries, encounters and people are always private (third-party PII)
    # — strip from all shared views.
    result.pop("people", None)
    result.pop("groups", None)
    result["items"] = [
        item for item in (result.get("items") or [])
        if item.get("item_type") not in ("journal", "encounter")
    ]
    if token_type == "no_memories":
        result["items"] = [
            item for item in result["items"]
            if item.get("item_type") != "memory"
        ]
    mem_ids = [
        item["memory"]["id"] for item in result["items"]
        if item.get("item_type") == "memory" and item.get("memory")
    ]
    _strip_encrypted_memory_fields(result["items"], _fetch_share_content(mem_ids))
    with get_session() as sess:
        owner = sess.exec(
            select(UserInfo).where(UserInfo.id == owner_uid)
        ).first()
    result["owner_name"] = owner.display_name if owner else ""
    _meta_cache.set(token, result)
    return result


def _build_features(project) -> List[Dict[str, Any]]:
    """Build GeoJSON-style feature dicts for all activities and segments.

    The owner-side builder, unchanged — the two had drifted apart (issue #321).
    This one used a segment's stored ``route_polyline`` for ``rail`` only, so a
    ferry or bus segment whose route had been resolved was drawn along that
    route for the owner and as a straight great-circle arc for anyone holding a
    share link, in the payload *and* in the pre-rendered raster tiles. A share
    link is a view of the same trip, so it has to be built by the same code —
    and now that both go through the zoom-simplified path, they also share the
    prepared-track cache, which one builder per key could not survive.
    """
    return _build_full_geo_features(project, encoded=False)


@router.get("/{token}/geo/low-res", summary="Low-res GeoJSON for shared project")
def shared_project_geo_low_res(token: str):
    """Return low-res GeoJSON for fast initial map render.

    Activities: straight lines (start→end, no polyline decoding).
    Segments: 50-point great-circle arcs (or stored rail polylines) — same as
    the full-res endpoint.  Both appear immediately so there are no holes
    in the route during the low-res phase.
    """
    project, _token_type, _project_id, _owner_uid = _get_project_and_type(token)
    features: List[Dict[str, Any]] = []
    for item in project.items:
        if item.item_type == "activity":
            activity = project.activity_by_id(item.activity_id)
            if activity is None:
                continue
            if activity.start_latlng and activity.end_latlng:
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [activity.start_latlng[1], activity.start_latlng[0]],
                            [activity.end_latlng[1],   activity.end_latlng[0]],
                        ],
                    },
                    "properties": {
                        "type": "activity",
                        "activity_id": activity.id,
                    },
                })
        elif item.item_type == "segment" and item.segment is not None:
            seg = item.segment
            if seg.route_mode == "rail" and seg.route_polyline:
                coords = json.loads(seg.route_polyline)
            else:
                pts = great_circle_points(
                    seg.start.lat, seg.start.lon,
                    seg.end.lat,   seg.end.lon,
                    n_points=50,
                )
                coords = [[lon, lat] for lat, lon in pts]
            if len(coords) >= 2:
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {
                        "type": "segment",
                        "segment_id": seg.id,
                        "segment_type": seg.segment_type,
                        "label": seg.label,
                        "route_mode": seg.route_mode,
                    },
                })
    return {"type": "FeatureCollection", "features": features}


@router.get("/{token}/geo", summary="Full GeoJSON for shared project")
def shared_project_geo(
    token: str,
    aid: Optional[str] = Query(default=None),
    current_user: Annotated[Optional[dict], Depends(get_optional_current_user)] = None,
):
    """Return GeoJSON FeatureCollection for a shared project."""
    project, token_type, project_id, _owner_uid = _get_project_and_type(token)
    _record_visit(project_id, token_type, aid, current_user)
    features = get_or_build_features(token, lambda: _build_features(project))
    return {"type": "FeatureCollection", "features": features}


def _load_shared_project(owner_uid: int, name: str):
    """The shared project's geometry, read fresh from the DB.

    Deliberately not the project :func:`_get_project_and_type` hands back: that
    one is cached for 60 s per token and invalidated from a worker process, so
    in this one it just ages out. What this returns is prepared into a track
    held for 15 minutes — seeding that from a 60-second-stale copy would
    stretch a one-minute staleness window into a quarter-hour one (issue #321).

    Light — the geometry comes from ``activity_geo_prepared`` (issue #369).
    """
    with get_session() as sess:
        return _repo.get_project(sess, owner_uid, name, include_heavy=False)


@router.get("/{token}/geo/simplified",
            summary="Zoom-appropriate GeoJSON for a shared project (gzip)")
def shared_project_geo_simplified(
    token: str,
    zoom: float,
    bbox: Optional[str] = Query(default=None),
):
    """The share-link counterpart of ``GET /api/geo/project/simplified``.

    Same answer, same two caches, same keys — the owner's id and project name —
    so a public viewer and the owner share every entry and one mutation busts
    both. See :func:`api.geo.serve_simplified_geo` for what those layers are.

    Everyone opening a public link used to get the full-resolution geometry:
    1,465,345 coordinates and a 4.5 MB payload on the trip this was measured
    against, about 180 MB of client heap, on the device least likely to have
    room for it. The zoom level of detail (issue #295) had landed for the owner
    path only (issue #321).

    Three things it deliberately does *not* do:

    - it takes no ``aid``. That is the anonymous visitor id, not a filter;
      keying a cache on it would mint an entry per visitor, which is the very
      failure this endpoint exists to avoid;
    - it does not record a visit. ``/{token}/meta`` already does that on every
      shared load, and this route is called again on every zoom-bucket change
      and every pan — a DB write on each would be pure cost;
    - it resolves the token before reading any cache, so a revoked link stops
      working immediately rather than for as long as some level stays warm.
    """
    # First, and before any cache is consulted: no token, no answer.
    project, _token_type, _project_id, owner_uid = _get_meta_project_and_type(token)
    # The lightweight variant is enough — only the owner's id and the project
    # name are wanted here, and it reuses the full cache when that is already
    # warm. Loading every polyline just to read a name would undo the saving.
    name = project.name
    return serve_simplified_geo(
        owner_uid, name, zoom, bbox, lambda: _load_shared_project(owner_uid, name))


@router.get("/{token}/stats", summary="Get shared project statistics")
def shared_project_stats(
    token: str,
    tags: Optional[List[str]] = Query(default=None),
) -> dict:
    """Return project statistics for a shared link (no auth required)."""
    project, _token_type, project_id, _owner_uid = _get_project_and_type(token)

    if tags:
        return _compute_stats(project, tag_filter=tags)

    with get_session() as sess:
        row = sess.exec(select(DBProject).where(DBProject.id == project_id)).first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if row.stats_json is None:
            _repo.compute_and_cache_stats(sess, row.user_info_id, row.name)
            row = sess.exec(select(DBProject).where(DBProject.id == project_id)).first()
        stats = json.loads(row.stats_json or "{}")
        stats["tag_options"] = tag_options_with_untagged(
            dm.get("tags") for dm in _parse_day_meta_json(row.day_meta_json)[0].values()
        )
    return stats


@router.get("/{token}/tiles/{z}/{x}/{y}.png", summary="Raster track tile (PNG)")
async def shared_project_tile(request: Request, token: str, z: int, x: int, y: int):
    """Return a cached raster tile PNG for the shared project's track layer.

    Zoom 0–10 are pre-rendered in the background on first access so panning
    and zooming are served instantly from disk.  Zoom 11–15 are rendered on
    demand but the endpoint checks for client disconnect before starting work,
    so stale requests from a previous zoom level are discarded immediately.
    """
    if not (0 <= z <= 15):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Zoom level must be between 0 and 15",
        )
    # Fast path: already on disk — no DB, no render, no disconnect check needed.
    cached = get_cached_tile(token, z, x, y)
    if cached is not None:
        return Response(cached, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    # If the client already zoomed away, skip the expensive work entirely.
    if await request.is_disconnected():
        return Response(b"", status_code=204)

    def _compute() -> bytes:
        def _build():
            project, _tt, _pid, _uid = _get_project_and_type(token)
            return _build_features(project)
        features = get_or_build_features(token, _build)
        return get_or_create_tile(token, features, z, x, y)

    png = await run_in_threadpool(_compute)
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


def _shared_photo_path(token: str, memory_id: int, photo_uuid: str, thumb: bool):
    """Resolve and validate a photo path via share token. Returns (Path, owner_uid) or raises."""
    project, token_type, _project_id, owner_uid = _get_project_and_type(token)
    if token_type == "no_memories":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    with get_session() as sess:
        mem_row = sess.get(DBMemory, memory_id)
        if mem_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        proj_row = sess.get(DBProject, mem_row.project_id)
        if proj_row is None or proj_row.user_info_id != owner_uid:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        photos = json.loads(mem_row.photos_json or "[]")
        if photo_uuid not in photos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    base = photo_folder(_DATA_DIR, owner_uid, "memories", memory_id)
    path = photo_file(base, photo_uuid, "_thumb" if thumb else "")
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return path


@router.get("/{token}/photos/{memory_id}/{photo_uuid}/thumb", summary="Serve memory photo thumbnail")
def shared_photo_thumb(token: str, memory_id: int, photo_uuid: str):
    """Serve a memory photo thumbnail without requiring user authentication."""
    path = _shared_photo_path(token, memory_id, photo_uuid, thumb=True)
    if not path.exists():
        # Fall back to full-res
        full = photo_file(path.parent, photo_uuid)
        if full is not None and full.exists():
            return FileResponse(str(full), media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400"})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(str(path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{token}/photos/{memory_id}/{photo_uuid}", summary="Serve full-resolution memory photo")
def shared_photo_full(token: str, memory_id: int, photo_uuid: str):
    """Serve a full-resolution memory photo without requiring user authentication."""
    path = _shared_photo_path(token, memory_id, photo_uuid, thumb=False)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(str(path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# ── Share: comments & likes ───────────────────────────────────────────────────

from pydantic import BaseModel as _BaseModel, Field as _Field
from api.memories import (
    _build_comment_tree, _delete_comment_subtree, _utc_now,
    IDOut as _IDOut, CommentOut as _CommentOut,
    LikesOut as _LikesOut, TranslationOut as _TranslationOut,
)
from api.translations import translate_text
from models.project_db import DBMemoryTranslation


def _resolve_shared_memory(token: str, memory_id: int):
    """Validate share token and confirm memory belongs to the shared project.

    Returns (project_id, owner_user_info_id).  Raises 404 on any mismatch.
    """
    _project, token_type, project_id, owner_uid = _get_project_and_type(token)
    if token_type == "no_memories":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    with get_session() as sess:
        mem_row = sess.get(DBMemory, memory_id)
        if mem_row is None or mem_row.project_id != project_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    return project_id, owner_uid


@router.get("/{token}/memories/{memory_id}/comments", response_model=list[_CommentOut],
            summary="List comments on a shared memory")
def shared_list_comments(token: str, memory_id: int):
    """Return all comments as a recursive tree. No authentication required."""
    _resolve_shared_memory(token, memory_id)
    with get_session() as sess:
        rows = sess.exec(
            select(DBMemoryComment)
            .where(DBMemoryComment.memory_id == memory_id)
            .order_by(DBMemoryComment.created_at)
        ).all()
        return _build_comment_tree(list(rows))


class _SharedCommentBody(_BaseModel):
    text: str
    parent_comment_id: Optional[int] = None


@router.post("/{token}/memories/{memory_id}/comments", status_code=201,
             response_model=_IDOut, summary="Add a comment to a shared memory")
def shared_add_comment(
    token: str,
    memory_id: int,
    body: _SharedCommentBody,
    current_user: Annotated[Optional[dict], Depends(get_optional_current_user)] = None,
):
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    _resolve_shared_memory(token, memory_id)
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        user_row = sess.get(UserInfo, user_info_id)
        commenter_name = user_row.display_name if user_row else ""

        if body.parent_comment_id is not None:
            parent = sess.get(DBMemoryComment, body.parent_comment_id)
            if parent is None or parent.memory_id != memory_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid parent comment")

        row = DBMemoryComment(
            memory_id=memory_id,
            parent_comment_id=body.parent_comment_id,
            user_info_id=user_info_id,
            commenter_name=commenter_name,
            text=body.text,
            created_at=_utc_now(),
        )
        sess.add(row)
        sess.commit()
        return {"id": row.id}


@router.delete("/{token}/memories/{memory_id}/comments/{comment_id}", status_code=204,
               summary="Delete a comment on a shared memory")
def shared_delete_comment(
    token: str,
    memory_id: int,
    comment_id: int,
    current_user: Annotated[Optional[dict], Depends(get_optional_current_user)] = None,
):
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    _project_id, owner_uid = _resolve_shared_memory(token, memory_id)
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        comment_row = sess.get(DBMemoryComment, comment_id)
        if comment_row is None or comment_row.memory_id != memory_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
        is_owner = user_info_id == owner_uid
        is_author = comment_row.user_info_id == user_info_id
        if not (is_owner or is_author):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        _delete_comment_subtree(sess, comment_id)
        sess.commit()


@router.get("/{token}/memories/{memory_id}/likes", response_model=_LikesOut,
            summary="Get likes for a shared memory")
def shared_get_likes(
    token: str,
    memory_id: int,
    current_user: Annotated[Optional[dict], Depends(get_optional_current_user)] = None,
):
    _resolve_shared_memory(token, memory_id)
    user_info_id = int(current_user["sub"]) if current_user else None
    with get_session() as sess:
        like_rows = sess.exec(
            select(DBMemoryLike).where(DBMemoryLike.memory_id == memory_id)
        ).all()
        liked_by_me = any(r.user_info_id == user_info_id for r in like_rows) if user_info_id else False
        return {
            "count": len(like_rows),
            "liked_by_me": liked_by_me,
            "likers": [{"name": r.liker_name, "user_info_id": r.user_info_id} for r in like_rows],
        }


@router.post("/{token}/memories/{memory_id}/like", status_code=204,
             summary="Like a shared memory")
def shared_like_memory(
    token: str,
    memory_id: int,
    current_user: Annotated[Optional[dict], Depends(get_optional_current_user)] = None,
):
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    _resolve_shared_memory(token, memory_id)
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        existing = sess.exec(
            select(DBMemoryLike).where(
                DBMemoryLike.memory_id == memory_id,
                DBMemoryLike.user_info_id == user_info_id,
            )
        ).first()
        if existing:
            return
        user_row = sess.get(UserInfo, user_info_id)
        liker_name = user_row.display_name if user_row else ""
        sess.add(DBMemoryLike(
            memory_id=memory_id,
            user_info_id=user_info_id,
            liker_name=liker_name,
            created_at=_utc_now(),
        ))
        sess.commit()


@router.delete("/{token}/memories/{memory_id}/like", status_code=204,
               summary="Unlike a shared memory")
def shared_unlike_memory(
    token: str,
    memory_id: int,
    current_user: Annotated[Optional[dict], Depends(get_optional_current_user)] = None,
):
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    _resolve_shared_memory(token, memory_id)
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        existing = sess.exec(
            select(DBMemoryLike).where(
                DBMemoryLike.memory_id == memory_id,
                DBMemoryLike.user_info_id == user_info_id,
            )
        ).first()
        if existing:
            sess.delete(existing)
            sess.commit()


# ── Share: translations ───────────────────────────────────────────────────────

@router.get("/{token}/memories/{memory_id}/translations/{lang_code}",
            response_model=_TranslationOut, summary="Get translation for a shared memory")
async def shared_get_translation(token: str, memory_id: int, lang_code: str):
    """Return a cached or freshly generated translation for a shared memory.

    No authentication required — any share-token holder can fetch translations.
    The lang_code must be in the project's configured languages list.
    """
    _project_id, owner_uid = _resolve_shared_memory(token, memory_id)

    with get_session() as sess:
        proj_row = sess.exec(
            select(DBProject).where(DBProject.id == _project_id)
        ).first()
        allowed_langs = json.loads(getattr(proj_row, 'languages_json', None) or "[]")
        if lang_code not in allowed_langs:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Language not enabled for this project")

        cached = sess.exec(
            select(DBMemoryTranslation).where(
                DBMemoryTranslation.memory_id == memory_id,
                DBMemoryTranslation.lang_code == lang_code,
            )
        ).first()
        if cached:
            return {"lang_code": lang_code, "name": cached.name, "description": cached.description}

        mem_row = sess.get(DBMemory, memory_id)
        if is_encrypted_envelope(mem_row.name) or is_encrypted_envelope(mem_row.description):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot translate an encrypted memory",
            )

    try:
        translated_name = await translate_text(mem_row.name, lang_code) if mem_row.name else None
        translated_desc = await translate_text(mem_row.description, lang_code) if mem_row.description else None
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Translation service error: {exc}") from exc

    with get_session() as sess:
        row = DBMemoryTranslation(
            memory_id=memory_id,
            lang_code=lang_code,
            name=translated_name,
            description=translated_desc,
            created_at=_utc_now(),
        )
        sess.add(row)
        sess.commit()

    return {"lang_code": lang_code, "name": translated_name, "description": translated_desc}

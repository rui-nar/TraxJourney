"""REST projects endpoints — list, create, open, update, and delete projects.

Routes:
    GET    /api/projects/                — list saved projects for current user
    POST   /api/projects/                — create new project
    GET    /api/projects/{name}          — get project data (GeoJSON + metadata)
    GET    /api/projects/{name}/meta     — get project metadata (lightweight)
    GET    /api/projects/{name}/elevation  — compact per-activity elevation profiles
    GET    /api/projects/{name}/memory-photos — photo uuids per memory (poll-sized)
    GET    /api/projects/{name}/content-days  — day keys holding content for any member
    GET    /api/projects/{name}/stats    — get project statistics
    PUT    /api/projects/{name}          — update project name or dates
    DELETE /api/projects/{name}          — delete a project
    PUT    /api/projects/{name}/day-meta      — update day metadata
    GET    /api/projects/{name}/sync-meta     — get sync configuration
    PUT    /api/projects/{name}/sync-meta     — update sync configuration
    PUT    /api/projects/{name}/track-style   — update track style
    PUT    /api/projects/{name}/languages     — update translation languages
    GET    /api/projects/{name}/sync/check    — check for new activities to sync

Import/export, activities, item ordering, segments, and sharing endpoints live in
their own sibling modules (api.project_transfer, api.activities, api.project_items,
api.segments, api.project_shares) — see api/project_shared.py for the infra they
all share.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional

from models.db import get_session
from sqlmodel import select

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.deps import get_current_user
from api.geo import (
    bust_project_cache,
    project_cache_generation,
    project_cache_get,
    project_cache_store,
)
from api.project_access import OwnerParam, require_role, resolve_project
from src.project.elevation_codec import build_elevation_payload
from api.project_shared import (
    _refresh_stats_background,
    _repo,
    build_details_payload,
    build_meta_payload,
    details_cache_key,
    gzip_json,
    meta_cache_key,
    queue_share_tiles_refresh,
    queue_stats_refresh,
)
from models.project_db import (
    DBActivity,
    DBEncounter,
    DBJournalEntry,
    DBMemory,
    DBProject,
    DBProjectItem,
    DBProjectMember,
    DBProjectSyncMeta,
)
from models.user import UserInfo, PolarstepsToken, StravaToken
from src.api.polarsteps_client import PolarstepsClient, format_step
from src.billing.entitlements import ensure_project_quota, ensure_trip_days_quota
from src.models.activity import parse_activities_or_log
from src.models.project import DEFAULT_SLEEPING_GROUPS, tag_options_with_untagged
from src.project.project_io import ProjectIO
from src.project.repo_core import _parse_day_meta_json, bump_lock_version
from src.project.project_repo import _compute_stats
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/projects", tags=["projects"])

_log = get_logger(__name__)


def _gzip_response(gz_bytes: bytes, cache_status: str) -> Response:
    """Serve pre-gzipped JSON bytes straight from the payload cache."""
    return Response(
        content=gz_bytes,
        media_type="application/json",
        headers={"Content-Encoding": "gzip", "X-Cache": cache_status},
    )


# ── Response schemas ──────────────────────────────────────────────────────────

class ProjectCreatedOut(BaseModel):
    name: str = Field(description="Final project name (may differ if trimmed)")
    filename: str = Field(description="Backing filename for legacy file-based access")

class ProjectUpdatedOut(BaseModel):
    name: str = Field(description="Project name after update (reflects any rename)")
    trip_start: Optional[str] = Field(None, description="Trip start date (YYYY-MM-DD) or null")

class SyncMetaOut(BaseModel):
    auto_sync_enabled: bool = Field(description="Whether auto-sync is active for this project")
    linked_ps_trip_id: Optional[int] = Field(None, description="Linked Polarsteps trip ID, or null")
    last_strava_sync_at: Optional[float] = Field(None, description="Unix timestamp of last Strava sync")
    last_ps_sync_at: Optional[float] = Field(None, description="Unix timestamp of last Polarsteps sync")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", summary="List projects")
def list_projects(current_user: Annotated[dict, Depends(get_current_user)]):
    """Return the current user's own projects plus those shared with them.

    Each entry carries ``owner_id``/``owner_name``/``role`` ("owner" |
    "editor"); shared entries are addressed by passing ``?owner=owner_id`` on
    the project routes (issue #106).
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        own = _repo.list_projects(sess, user_info_id)
        for entry in own:
            entry.update(owner_id=user_info_id, owner_name="", role="owner")

        shared = []
        memberships = sess.exec(
            select(DBProjectMember).where(DBProjectMember.user_info_id == user_info_id)
        ).all()

        # Batch-load the member projects and their owners in two queries total
        # instead of two per membership (was an N+1).
        project_ids = [m.project_id for m in memberships]
        projects_by_id: Dict[int, DBProject] = {}
        owners_by_id: Dict[int, UserInfo] = {}
        if project_ids:
            projects_by_id = {
                p.id: p for p in sess.exec(
                    select(DBProject).where(DBProject.id.in_(project_ids))
                ).all()
            }
            owner_ids = {p.user_info_id for p in projects_by_id.values()}
            if owner_ids:
                owners_by_id = {
                    u.id: u for u in sess.exec(
                        select(UserInfo).where(UserInfo.id.in_(owner_ids))
                    ).all()
                }

        for m in memberships:
            proj = projects_by_id.get(m.project_id)
            if proj is None:
                continue
            owner_user = owners_by_id.get(proj.user_info_id)
            shared.append({
                "name": proj.name,
                "filename": proj.name + ProjectIO.EXTENSION,
                "owner_id": proj.user_info_id,
                "owner_name": (owner_user.display_name or owner_user.email) if owner_user else "",
                "role": m.role,
            })
        shared.sort(key=lambda e: e["name"])
        return own + shared


class CreateProjectRequest(BaseModel):
    name: str


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=ProjectCreatedOut,
             summary="Create a project")
def create_project(
    body: CreateProjectRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    user_info_id = int(current_user["sub"])
    name = body.name.strip() or "My Trip"
    with get_session() as sess:
        if _repo.project_exists(sess, user_info_id, name):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Project '{name}' already exists",
            )
        # Plan limit (issue #121) — raises QuotaExceeded → 402. No-op unless the
        # deployment sells plans *and* has quota enforcement switched on.
        ensure_project_quota(sess, user_info_id)
        _repo.create_project(sess, user_info_id, name)
    return {"name": name, "filename": name + ProjectIO.EXTENSION}


@router.get("/{name}", summary="Get full project data")
def get_project(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Full project data — the same shape as ``/meta`` but with elevation_profile
    and map.summary_polyline present on each activity.

    Served from the per-project payload cache when warm (issue #178 follow-up):
    this endpoint had the identical cold-load cost as ``/meta`` — 12-13 s on a
    real 219-activity project, with zero improvement on repeat calls since it
    had no cache at all. Rides the same cache infra as ``/meta`` (see
    api.project_shared), keyed per caller for the same reason: journal entries
    are filtered per-author and ``caller_role`` is baked into the body.
    """
    user_info_id = int(current_user["sub"])
    t0 = time.time()
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        owner_id = row.user_info_id
        cache_key = details_cache_key(owner_id, name, user_info_id)
        cached = project_cache_get(cache_key)
        if cached is not None:
            return _gzip_response(cached, "HIT")

        gen = project_cache_generation(owner_id, name)  # before the read, so a bust wins
        data = build_details_payload(sess, row, name, user_info_id)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    t1 = time.time()

    gz_bytes = gzip_json(data)
    project_cache_store(cache_key, gz_bytes, gen)
    _log.info("project_details name=%s load=%.3fs gzip=%.3fs cache=MISS",
              name, t1 - t0, time.time() - t1)
    return _gzip_response(gz_bytes, "MISS")


@router.get("/{name}/elevation", summary="Elevation profiles (compact)")
def get_project_elevation(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Per-activity elevation profiles, delta+varint encoded (issue #295).

    An *additional* endpoint, not a change to ``GET /{name}``: Android and iOS
    builds in the wild expect elevation_profile to arrive inside the full
    details payload, and they keep being served it.

    That payload measures 33 MB on a 180-day trip — ~9 s to fetch and ~5 s to
    decode on a real device — and elevation_profile is nearly all of it, as
    JSON text spending ~20 bytes per [distance, elevation] pair. The encoding
    here is lossless at the precision the data actually has (1 m distance,
    0.1 m elevation; see src/project/elevation_codec.py) and typically 5-10x
    smaller. Track geometry is untouched by any of this — it lives in
    /api/geo/project and stays full resolution.

    Rides the same payload cache and generation counter as every other
    per-project response, so one bust per mutation still covers it.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        owner_id = row.user_info_id

        cache_key = (owner_id, name, "elevation")
        cached = project_cache_get(cache_key)
        if cached is not None:
            return _gzip_response(cached, "HIT")

        gen = project_cache_generation(owner_id, name)
        # journal_user_id scopes journal items to the caller, as every other
        # read-only serialising path does. Without it this materialises every
        # member's journal to serialise elevation — needless work on an
        # endpoint whose whole purpose is spending less memory (#209, #276).
        project = _repo.get_project(
            sess, owner_id, name,
            journal_user_id=user_info_id,
        )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    gz_bytes = gzip_json(build_elevation_payload(project.activities))
    project_cache_store(cache_key, gz_bytes, gen)
    return _gzip_response(gz_bytes, "MISS")


@router.get("/{name}/meta", summary="Get project metadata (lightweight)")
def get_project_meta(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Same shape as GET /{name} but with elevation_profile and map.summary_polyline
    absent from each activity.  Uses lightweight loading (deferred heavy columns).

    Served from the per-project payload cache when warm (issue #178): the client
    blocks its whole activity panel on this call and polls it every few seconds
    during a re-fetch or route resolve, while a cold load on a large project ran
    to 11-13 s. Cached bytes are pre-gzipped, so this returns a Response rather
    than a dict — same shape as the geo endpoints, and GZipMiddleware leaves an
    already-encoded body alone.
    """
    user_info_id = int(current_user["sub"])
    t0 = time.time()
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        owner_id = row.user_info_id
        cache_key = meta_cache_key(owner_id, name, user_info_id)
        cached = project_cache_get(cache_key)
        if cached is not None:
            return _gzip_response(cached, "HIT")

        gen = project_cache_generation(owner_id, name)  # before the read, so a bust wins
        data = build_meta_payload(sess, row, name, user_info_id)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    t1 = time.time()

    gz_bytes = gzip_json(data)
    project_cache_store(cache_key, gz_bytes, gen)
    _log.info("project_meta name=%s load=%.3fs gzip=%.3fs cache=MISS",
              name, t1 - t0, time.time() - t1)
    return _gzip_response(gz_bytes, "MISS")


@router.get("/{name}/memory-photos", summary="Photo uuids per memory (poll-sized)")
def get_project_memory_photos(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """``{"photos": {memory_id: [uuid, ...]}}`` and nothing else (issue #308).

    The client polls this every few seconds for three minutes after a
    Polarsteps import, waiting for background photo downloads to land. It used
    to poll ``GET /{name}`` with the cache bypassed: 36 MB on a 180-day trip,
    up to 60 times, while the server was busy fetching those very photos.

    ``/meta`` also carries these uuids — memory photos are hydrated whatever
    ``include_heavy`` says — and it is 73x smaller than the details payload, so
    polling it was the obvious alternative. Measured on the 219-activity /
    600-memory shape it is still the wrong instrument: ``/meta`` is 492 KB
    (76 KB gzipped) and 92 ms to rebuild against 75 KB (42 KB gzipped) and 7 ms
    here. And that cost is paid on *every* tick rather than absorbed by the
    payload cache, because ``_write_memory_photo`` busts the project's cached
    payloads as each photo lands: during the polling window a ``/meta`` poll is
    a MISS almost every time.

    Uncached for the same reason — an entry stored here would be invalidated
    before the next tick read it, so it could only ever cost a gzip and a
    store. And unkeyed by caller, unlike ``/meta``: ``photos_json`` holds no
    per-author content and is never encrypted (a memory's E2EE marker covers
    its name and description), so every member gets the same body.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        rows = sess.exec(
            select(DBMemory.id, DBMemory.photos_json)
            .where(DBMemory.project_id == row.id)
        ).all()
    # Falsy entries are unfilled placeholders for a download that hasn't landed
    # yet — filtered out exactly as _row_to_memory does (see api/photo_locks.py).
    return {
        "photos": {
            str(mem_id): [p for p in json.loads(photos_json or "[]") if p]
            for mem_id, photos_json in rows
        }
    }


def _day_key(raw: Optional[str]) -> str:
    """The "YYYY-MM-DD" part of a date or datetime string; "" when absent."""
    return (raw or "").split("T")[0]


def _content_day_keys(sess, project, visible_to: int | None = None) -> set[str]:
    """Day keys this project holds content on: activities, memories, journal
    entries, encounters and dated segments.

    ``visible_to`` None counts *every* member's content. An id counts only what
    that user can actually see, which differs for journals alone — they are
    per-user (issue #106), and a NULL author is a legacy row belonging to the
    project owner. The difference between the two answers is exactly the set of
    days a caller cannot account for, which is what #372 turns on.
    """
    days: set[str] = set()
    for model in (DBMemory, DBEncounter):
        days.update(
            _day_key(d) for d in sess.exec(
                select(model.date).where(model.project_id == project.id)
            ).all()
        )
    if visible_to is not None:
        # Only this user's own journals, plus legacy NULL-author rows when they
        # are the project owner.
        authors = (DBJournalEntry.user_info_id == visible_to) | (
            (DBJournalEntry.user_info_id.is_(None))
            & (project.user_info_id == visible_to)
        )
    else:
        # Every journal that somebody can still *see*. A member who leaves
        # keeps their rows (remove_member drops only the membership), and an
        # orphan like that is visible to nobody: counting it would pin its day
        # for everyone, forever, with no one able to clear it (issue #387
        # review). Legacy NULL authors belong to the owner.
        member_ids = select(DBProjectMember.user_info_id).where(
            DBProjectMember.project_id == project.id
        )
        authors = (
            DBJournalEntry.user_info_id.is_(None)
            | (DBJournalEntry.user_info_id == project.user_info_id)
            | DBJournalEntry.user_info_id.in_(member_ids)
        )
    days.update(
        _day_key(d) for d in sess.exec(
            select(DBJournalEntry.date).where(
                DBJournalEntry.project_id == project.id, authors
            )
        ).all()
    )
    days.update(
        _day_key(d) for d in sess.exec(
            select(DBActivity.start_date_local)
            .join(DBProjectItem, DBProjectItem.activity_id == DBActivity.id)
            .where(DBProjectItem.project_id == project.id)
        ).all()
    )
    # Segments carry their date inside the serialised ConnectingSegment, and an
    # undated one is placed by its neighbours rather than by a day of its own —
    # _day_key drops those to "".
    for segment_json in sess.exec(
        select(DBProjectItem.segment_json).where(
            DBProjectItem.project_id == project.id,
            DBProjectItem.item_type == "segment",
        )
    ).all():
        if not segment_json:
            continue
        try:
            days.add(_day_key((json.loads(segment_json) or {}).get("date")))
        except (ValueError, TypeError, AttributeError):
            # A blob that isn't a JSON object can't name a day. Skipping it
            # pins nothing extra and keeps one malformed row from 500-ing the
            # check, which the client reads as "can't prune at all".
            # Mirrors src/billing/trip_days.py:project_day_bounds.
            continue
    days.discard("")
    return days


@router.get("/{name}/content-days", summary="Days holding content for any member")
def get_project_content_days(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """``{"days": ["YYYY-MM-DD", ...]}`` — every day this trip holds content on
    for *any* member: activities, memories, journal entries, encounters and
    dated segments (issue #372).

    A client cannot answer this for itself. Journal entries are per-user, so
    every item list the API serves hides other members' ones (see
    ``journal_visible_positions``); a day whose only content is another
    member's journal therefore looks empty to the caller. The settings screen
    needs the unfiltered answer before it prunes the days past a new trip end
    date, or it deletes the *shared* day-meta of a day that still renders for
    the member who owns that journal.

    Deliberately not filtered by caller — that is the whole point — and it
    leaks nothing beyond the dates themselves: no journal text, no author, not
    even a count.

    Editor tier, not viewer: a journal-only day is precisely the day a member
    does *not* see in their own trip view, so answering for every member does
    disclose that *somebody* wrote something that day. Only the day-meta prune
    consumes this, and that write is editor-gated already, so nothing is lost
    by matching it.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        return {"days": sorted(_content_day_keys(sess, row))}


@router.get("/{name}/stats", summary="Get project statistics")
def get_project_stats(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    tags: Optional[List[str]] = Query(default=None),
    owner: OwnerParam = None,
):
    """Return project statistics, optionally filtered to days with matching tags."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        owner_id = row.user_info_id

        if tags:
            # Tag-filtered request: compute on-demand (bypass cache)
            project = _repo.get_project(sess, owner_id, name)
            if project is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            return _compute_stats(project, tag_filter=tags)

        # Unfiltered: use cached stats
        if row.stats_json is None:
            _repo.compute_and_cache_stats(sess, owner_id, name)
            row = resolve_project(sess, user_info_id, name, owner)
        stats = json.loads(row.stats_json or "{}")

        # Always derive tag_options live from day_meta_json so they are never
        # stale (cached stats pre-date the tag-save fix and stored [] here).
        stats["tag_options"] = tag_options_with_untagged(
            dm.get("tags") for dm in _parse_day_meta_json(row.day_meta_json)[0].values()
        )
    return stats


class ProjectUpdateRequest(BaseModel):
    new_name: Optional[str] = None
    trip_start: Optional[str] = None  # "YYYY-MM-DD" or None to clear
    trip_end: Optional[str] = None    # "YYYY-MM-DD" or None to clear


@router.put("/{name}", response_model=ProjectUpdatedOut, summary="Update project name or dates")
def update_project(
    name: str,
    body: ProjectUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id

        # Rename if requested — co-owner+; editors may still update trip dates
        if body.new_name is not None:
            require_role(sess, row, user_info_id, "co-owner")
            new_name = body.new_name.strip()
            if not new_name:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Name cannot be empty")
            if new_name != name and _repo.project_exists(sess, owner_id, new_name):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Project '{new_name}' already exists")
            row.name = new_name

        # Plan limit on trip length (issue #121). Checked before the dates are
        # applied: declaring a range wider than the plan allows is refused, and
        # clearing a date can only ever shrink the span, so it is always fine.
        widening = [
            body.trip_start if 'trip_start' in body.model_fields_set else None,
            body.trip_end if 'trip_end' in body.model_fields_set else None,
        ]
        ensure_trip_days_quota(sess, row.id, owner_id, *widening)

        # Update trip_start if key is present in body (None = clear)
        if 'trip_start' in body.model_fields_set:
            row.trip_start = body.trip_start or None

        # Update trip_end if key is present in body (None = clear)
        if 'trip_end' in body.model_fields_set:
            row.trip_end = body.trip_end or None

        bump_lock_version(sess, row.id)
        sess.add(row)
        sess.commit()
        result_name = row.name
        result_trip_start = row.trip_start
        # Read inside the session, used after it: the share module caches the
        # project object per token, and that copy carries the old name.
        share_tokens = [t for t in (row.share_token, row.share_token_no_memories) if t]
    # Both names: cache entries are keyed by project name, so a rename would
    # otherwise strand the old name's payload for whoever still asks for it.
    bust_project_cache(owner_id, name)
    if result_name != name:
        bust_project_cache(owner_id, result_name)
        # The share module's per-token project cache still holds the old name
        # for up to 60s, and the share geo routes resolve a project by
        # (owner, name) — so a shared link would 404 on the zoom-simplified
        # endpoint until that expired, and any level it did build would be
        # keyed under a name no later bust will ever target (issue #321).
        # Mirrors what the revoke paths in api/project_shares.py already do.
        from api.share import invalidate_share_cache
        for token in share_tokens:
            invalidate_share_cache(token)
    return {"name": result_name, "trip_start": result_trip_start}


class DayMetaUpdateRequest(BaseModel):
    day_meta: Dict[str, Dict[str, Any]]
    sleeping_options: Optional[List[str]] = None
    sleeping_option_groups: Optional[Dict[str, str]] = None  # name → "Outdoors"|"Indoors"|"Other"
    counters: Optional[List[Dict[str, Any]]] = None  # [{name, start}]


def _stored_day_meta(existing_json: str | None) -> dict:
    """The stored day-meta map, or ``{}`` for anything that is not one.

    ``day_meta_json`` is expected to hold a JSON object, but a hand-edited row,
    a bad migration or an older bug can leave ``"null"``, a list, or a broken
    blob there. Reading it with a bare ``json.loads(...).items()`` turns that
    into a 500 on every settings save for that trip, which the client reads as
    "cannot save at all" — and, since #387, also as "cannot prune days". An
    unreadable map means nothing to preserve and nothing to protect, so the
    write proceeds on the caller's data rather than failing outright.
    """
    if not existing_json:
        return {}
    try:
        parsed = json.loads(existing_json)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _day_meta_has_content(meta) -> bool:
    """True when a day-meta entry holds anything a user would miss.

    Not just ``bool(meta)``: ``_fill_day_gaps`` (src/project/repo_core.py)
    writes ``{"difficulty": None, "sleeping": None, ..., "counters": []}`` for
    every gap day, which is a truthy dict carrying nothing at all. Treating
    those as "something to lose" would pin empty days in place (issue #387
    review).
    """
    if not isinstance(meta, dict):
        # A corrupt entry is not something a user would miss, and protecting it
        # would pin an unreadable day in place permanently.
        return False
    return any(value not in (None, "", [], {}) for value in meta.values())


def _keep_days_the_caller_cannot_see(
    sess, project, incoming: dict, existing_json: str | None, caller_id: int
) -> dict:
    """Put back any non-empty day-meta entry the caller dropped for a day whose
    content they cannot see (issue #387).

    ``PUT /day-meta`` replaces the whole map, so "the user cleared this day's
    notes" and "the trip-end prune dropped a day it should not have" arrive as
    the same payload: a key that is simply absent. Client-side, #372 settled
    which days may go — but only a current client asks. A stale build, another
    client, or a future bug computing the pruned map wrongly would still wipe
    the *shared* notes of a day that only another member's journal keeps on
    screen, and the owner of that journal would watch a day's notes vanish.

    The narrow rule that separates the two cases without blocking real edits:
    refuse the drop only when the caller demonstrably could not have known what
    they were dropping — the day has content, none of it visible to them, and
    there was something to lose. Clearing a day you can see still works, an
    empty entry still goes, and a day with no content at all still prunes.
    """
    dropped = {
        key: meta for key, meta in _stored_day_meta(existing_json).items()
        if key not in incoming and _day_meta_has_content(meta)
    }
    if not dropped:
        return incoming
    # Only pay for the scan when something non-empty is actually being removed.
    invisible = (
        _content_day_keys(sess, project)
        - _content_day_keys(sess, project, visible_to=caller_id)
    )
    protected = invisible.intersection(dropped)
    if not protected:
        return incoming
    return {**incoming, **{key: dropped[key] for key in protected}}


def _merge_day_meta_preserve_counters(incoming: dict, existing_json: str | None) -> dict:
    """Return incoming day_meta with existing per-day counter values preserved.

    If a day in the stored row has counters but the incoming dict for that day
    omits the "counters" key entirely, the stored counters are copied across.
    Sending "counters": {} explicitly clears them (caller's intent wins).

    This protects against a Flutter app saving settings from a session that
    started before an enrichment script added counter values.
    """
    merged = dict(incoming)
    for date_key, existing_day in _stored_day_meta(existing_json).items():
        if not isinstance(existing_day, dict):
            continue
        existing_counters = existing_day.get("counters")
        if existing_counters:
            incoming_day = merged.get(date_key)
            if incoming_day is not None and "counters" not in incoming_day:
                incoming_day["counters"] = existing_counters
    return merged


@router.put("/{name}/day-meta", status_code=status.HTTP_204_NO_CONTENT,
            summary="Update day metadata")
def update_day_meta(
    name: str,
    body: DayMetaUpdateRequest,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Replace day metadata (and optionally sleeping options) for a project."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        row.day_meta_json = json.dumps(
            _merge_day_meta_preserve_counters(
                _keep_days_the_caller_cannot_see(
                    sess, row, body.day_meta, row.day_meta_json, user_info_id
                ),
                row.day_meta_json,
            )
        )
        if body.sleeping_options:  # ignore empty list — never wipe sleeping options
            groups = body.sleeping_option_groups or {}
            row.sleeping_options_json = json.dumps([
                {"name": n, "group": groups.get(n, DEFAULT_SLEEPING_GROUPS.get(n, 'Other'))}
                for n in body.sleeping_options
            ])
        if body.counters is not None:
            row.counters_json = json.dumps([
                {"name": c["name"], "start": float(c.get("start", 0))}
                for c in body.counters
            ])
        row.updated_at = time.time()
        bump_lock_version(sess, row.id)
        sess.add(row)
        sess.commit()
    bust_project_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)


# ── Sync-meta (auto-sync config per project) ─────────────────────────────────

def _get_sync_meta(sess, project_id: int) -> DBProjectSyncMeta:
    """Return the sync-meta row for a project, creating a default if absent."""
    row = sess.get(DBProjectSyncMeta, project_id)
    if row is None:
        row = DBProjectSyncMeta(project_id=project_id)
    return row


@router.get("/{name}/sync-meta", response_model=SyncMetaOut, summary="Get sync configuration")
def get_sync_meta(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
) -> dict:
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        proj = resolve_project(sess, user_info_id, name, owner)
        meta = _get_sync_meta(sess, proj.id)
    return {
        "auto_sync_enabled": meta.auto_sync_enabled,
        "linked_ps_trip_id": meta.linked_ps_trip_id,
        "last_strava_sync_at": meta.last_strava_sync_at,
        "last_ps_sync_at": meta.last_ps_sync_at,
    }


class TrackStyleUpdateRequest(BaseModel):
    track_color: Optional[str] = None           # "#RRGGBB" hex
    track_secondary_color: Optional[str] = None  # "#RRGGBB" hex; null = auto-derive
    track_width: Optional[float] = None
    alternating_track_colors: Optional[bool] = None
    elevation_chart_color: Optional[str] = None  # "#RRGGBB" hex; null = use default
    elevation_chart_show_line: Optional[bool] = None
    color_by_type: Optional[bool] = None
    # Keyed by activity bucket ("ride"/"run"/"hike"/"other") or segment type
    # ("flight"/"train"/"bus"/"boat"); each value e.g. {"color": "#RRGGBB",
    # "style": "solid"|"dashed"|"dotted"}.
    type_styles: Optional[Dict[str, Dict[str, Any]]] = None


@router.put("/{name}/track-style", status_code=status.HTTP_204_NO_CONTENT,
            summary="Update track style")
def update_track_style(
    name: str,
    body: TrackStyleUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
) -> None:
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        if body.track_color is not None:
            row.track_color = body.track_color
        if 'track_secondary_color' in body.model_fields_set:
            row.track_secondary_color = body.track_secondary_color
        if body.track_width is not None:
            row.track_width = body.track_width
        if body.alternating_track_colors is not None:
            row.alternating_track_colors = body.alternating_track_colors
        if 'elevation_chart_color' in body.model_fields_set:
            row.elevation_chart_color = body.elevation_chart_color
        if body.elevation_chart_show_line is not None:
            row.elevation_chart_show_line = body.elevation_chart_show_line
        if body.color_by_type is not None:
            row.color_by_type = body.color_by_type
        if body.type_styles is not None:
            row.type_styles_json = json.dumps(body.type_styles)
        row.updated_at = time.time()
        bump_lock_version(sess, row.id)
        sess.add(row)
        owner_id = row.user_info_id
        sess.commit()
    bust_project_cache(owner_id, name)


class LanguagesUpdateRequest(BaseModel):
    languages: List[str]  # ISO 639-1 codes, e.g. ["fr", "de"]


@router.put("/{name}/languages", status_code=status.HTTP_204_NO_CONTENT,
            summary="Update translation languages")
def update_languages(
    name: str,
    body: LanguagesUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
) -> None:
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        row.languages_json = json.dumps(body.languages)
        row.updated_at = time.time()
        bump_lock_version(sess, row.id)
        sess.add(row)
        owner_id = row.user_info_id
        sess.commit()
    bust_project_cache(owner_id, name)


class SyncMetaUpdateRequest(BaseModel):
    auto_sync_enabled: Optional[bool] = None
    linked_ps_trip_id: Optional[int] = None
    last_strava_sync_at: Optional[float] = None
    last_ps_sync_at: Optional[float] = None


@router.put("/{name}/sync-meta", response_model=SyncMetaOut, summary="Update sync configuration")
def update_sync_meta(
    name: str,
    body: SyncMetaUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
) -> dict:
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        proj = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        meta = _get_sync_meta(sess, proj.id)
        if meta.project_id != proj.id:
            meta.project_id = proj.id
        if body.auto_sync_enabled is not None:
            meta.auto_sync_enabled = body.auto_sync_enabled
        if 'linked_ps_trip_id' in body.model_fields_set:
            meta.linked_ps_trip_id = body.linked_ps_trip_id
        if body.last_strava_sync_at is not None:
            meta.last_strava_sync_at = body.last_strava_sync_at
        if body.last_ps_sync_at is not None:
            meta.last_ps_sync_at = body.last_ps_sync_at
        sess.add(meta)
        sess.commit()
        # Read attributes BEFORE the session closes. After commit the instance is
        # expired (expire_on_commit=True), so touching meta.* outside the `with`
        # block raises DetachedInstanceError.
        result = {
            "auto_sync_enabled": meta.auto_sync_enabled,
            "linked_ps_trip_id": meta.linked_ps_trip_id,
            "last_strava_sync_at": meta.last_strava_sync_at,
            "last_ps_sync_at": meta.last_ps_sync_at,
        }
    return result


@router.get("/{name}/sync/check", summary="Check for new activities to sync")
def sync_check(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
) -> dict:
    """Return new Strava activities and Polarsteps steps not yet in this project."""
    user_info_id = int(current_user["sub"])

    with get_session() as sess:
        proj = resolve_project(sess, user_info_id, name, owner)
        meta = _get_sync_meta(sess, proj.id)

        if not meta.auto_sync_enabled:
            return {"strava": [], "polarsteps": []}

        # ── Strava ────────────────────────────────────────────────────────────
        strava_results: List[Dict[str, Any]] = []
        strava_token = sess.exec(
            select(StravaToken).where(StravaToken.user_info_id == user_info_id)
        ).first()

        if strava_token and strava_token.access_token:
            from api.strava import _load_cache, _strava_client_for_token, _fetch_all_strava, _save_cache, _save_refreshed_token

            raw_list: Optional[List[Dict[str, Any]]] = None
            cache_data = _load_cache(user_info_id)
            if cache_data is not None:
                raw_list = cache_data["activities"]
            else:
                try:
                    client = _strava_client_for_token(strava_token)
                    raw_list = _fetch_all_strava(client)
                    _save_cache(user_info_id, raw_list)
                    _save_refreshed_token(sess, strava_token, client)
                except Exception:
                    raw_list = []

            if raw_list:
                # Parse + filter by last_strava_sync_at and trip date range
                cutoff = meta.last_strava_sync_at or 0.0
                trip_start_date = (
                    datetime.strptime(proj.trip_start, "%Y-%m-%d").date()
                    if proj.trip_start else None
                )
                trip_end_date = (
                    datetime.strptime(proj.trip_end, "%Y-%m-%d").date()
                    if proj.trip_end else None
                )
                # Get IDs already in project
                item_rows = sess.exec(
                    select(DBProjectItem).where(
                        DBProjectItem.project_id == proj.id,
                        DBProjectItem.item_type == "activity",
                    )
                ).all()
                in_project_ids = {r.activity_id for r in item_rows if r.activity_id is not None}

                for act in parse_activities_or_log(raw_list, "projects_unimported"):
                    if act.id in in_project_ids:
                        continue
                    act_ts = act.start_date.timestamp() if act.start_date else 0.0
                    if act_ts <= cutoff:
                        continue
                    act_date = act.start_date.date() if act.start_date else None
                    if act_date is None:
                        continue
                    if trip_start_date and act_date < trip_start_date:
                        continue
                    if trip_end_date and act_date > trip_end_date:
                        continue
                    d = act.to_strava_dict()
                    d["in_project"] = False
                    strava_results.append(d)

        # ── Polarsteps ────────────────────────────────────────────────────────
        ps_results: List[Dict[str, Any]] = []
        ps_token = sess.exec(
            select(PolarstepsToken).where(PolarstepsToken.user_info_id == user_info_id)
        ).first()

        if ps_token and meta.linked_ps_trip_id:
            try:
                from datetime import datetime as _dt
                ps_client = PolarstepsClient(ps_token.remember_token)
                raw_steps = ps_client.get_trip_steps(meta.linked_ps_trip_id)
                cutoff = meta.last_ps_sync_at or 0.0
                for raw_step in raw_steps:
                    ct = raw_step.get("creation_time")
                    try:
                        step_ts = _dt.fromisoformat(ct).timestamp() if isinstance(ct, str) else float(ct or 0)
                    except (ValueError, TypeError):
                        step_ts = 0.0
                    if step_ts <= cutoff:
                        continue
                    ps_results.append(format_step(raw_step))
            except Exception:
                pass  # PS errors are non-fatal

    return {"strava": strava_results, "polarsteps": ps_results}


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a project")
def delete_project(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="owner")
        owner_id = row.user_info_id
        found = _repo.delete_project(sess, owner_id, name)
    bust_project_cache(owner_id, name)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

"""REST activity endpoints — add/refresh/edit/split activities within a project.

Routes:
    POST   /api/projects/{name}/activities                          — add activities to project
    POST   /api/projects/{name}/activities/gpx/inspect               — read a GPX file without importing it
    POST   /api/projects/{name}/activities/import-gpx                — import a single activity from a GPX file
    POST   /api/projects/{name}/activities/{activity_id}/refresh    — trigger async activity refresh from Strava
    GET    /api/projects/{name}/activities/{activity_id}/track      — get editable track geometry
    PUT    /api/projects/{name}/activities/{activity_id}/track      — replace track geometry
    POST   /api/projects/{name}/activities/{activity_id}/reset      — reset edited track to original
    POST   /api/projects/{name}/activities/{activity_id}/split      — split into head + local tail
    DELETE /api/projects/{name}/activities/{activity_id}/local      — delete a local (split-tail) activity
    PUT    /api/activities/{activity_id}                            — update an activity's E2EE-in-scope fields
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Optional

import polyline as polyline_lib
from models.db import get_session
from sqlmodel import select

from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from api.deps import get_current_user
from api.geo import bust_geo_cache, warm_geo_cache
from api.project_access import OwnerParam, resolve_project
from api.project_shared import _refresh_share_tiles, _refresh_stats_background, _repo, queue_share_tiles_refresh, queue_stats_refresh, warm_meta_cache
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import StravaToken
from src.api.strava_client import RateLimiter, StravaAPI
from src.billing.entitlements import ensure_trip_days_quota
from src.config.settings import Config
from src.exceptions.errors import RateLimitError
from src.gpx.importer import (
    GPXImportError,
    candidates as gpx_candidates,
    guard_declared_size,
    guard_upload_size,
    gpx_track_to_points,
    parse_gpx_bytes,
    suggested_name as gpx_suggested_name,
    validate_candidate,
    validate_for_import,
)
from src.models.activity import Activity, parse_activities_or_log
from src.models.track_edit import points_to_elevation_profile, points_to_polyline, recompute_track_metrics
from src.project.local_ids import LocalIdExhausted, allocate_local_activity_id, track_fingerprint
from src.project.project_repo import bump_lock_version
from src.project.repo_activities import store_prepared_geometry
from src.utils.logging import get_logger

_log = get_logger(__name__)
_cfg = Config("config/config.json")
if os.environ.get("STRAVA_CLIENT_ID"):
    _cfg.set("strava.client_id", os.environ["STRAVA_CLIENT_ID"])
if os.environ.get("STRAVA_CLIENT_SECRET"):
    _cfg.set("strava.client_secret", os.environ["STRAVA_CLIENT_SECRET"])

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Response schemas ──────────────────────────────────────────────────────────

class ActivitiesAddedOut(BaseModel):
    added: int = Field(description="Number of new activities added")
    total: int = Field(description="Total activities in the project after add")
    pending_enrichment: int = Field(description="Activities queued for GPS stream enrichment in background")


#: Points kept in a preview outline. A thumbnail a few centimetres across
#: cannot show more, and the whole payload has to survive being held in a
#: dialog's state on a phone: a 50k-point track encodes to about 250 KB, this
#: to about 1.
PREVIEW_POINTS = 200


class GPXCandidateOut(BaseModel):
    """One importable thing in an inspected file, and what it would become."""
    index: int = Field(description="Position to pass back as track_index")
    name: Optional[str] = Field(description="The track's own name, if it has one")
    activity_type: Optional[str] = Field(
        description="Mapped from the file's <type>; null when unrecognised")
    point_count: int
    distance_m: float
    is_route: bool = Field(
        description="True for a planned <rte> rather than a recorded <trk>")
    has_times: bool = Field(description="False for a route, which has no clock")
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    elapsed_seconds: Optional[int] = None
    moving_seconds: Optional[int] = None
    elevation_gain_m: Optional[float] = Field(
        default=None,
        description="Derived from the file's elevations, and an ESTIMATE: the "
                    "app measures it itself rather than being told, so it is "
                    "labelled as such wherever it is shown")
    elevation_gain_estimated: bool = True
    polyline: Optional[str] = Field(
        default=None,
        description="Encoded outline of the track, thinned to at most "
                    "PREVIEW_POINTS points. For drawing a thumbnail so the "
                    "user can see what they picked before committing to it — "
                    "not geometry of record, which the import derives from the "
                    "file itself")
    errors: List[str] = Field(
        default_factory=list,
        description="Why this one cannot be imported; empty means it can")


class GPXDuplicateOut(BaseModel):
    activity_id: int
    name: str


class GPXInspectOut(BaseModel):
    """What a file holds, without importing any of it."""
    candidates: List[GPXCandidateOut]
    suggested_name: Optional[str] = None
    duplicate_of: Optional[GPXDuplicateOut] = Field(
        default=None,
        description="Set when this trip already holds this track, so the "
                    "client can offer to open it instead of importing again")
    errors: List[str] = Field(
        default_factory=list,
        description="Why the file as a whole is unusable; empty means it is not")


class GPXImportOut(BaseModel):
    activity_id: int = Field(description="ID assigned to the newly imported activity")
    total: int = Field(description="Total activities in the project after import")


# ── Strava stream enrichment ───────────────────────────────────────────────────

def _strava_client_for_user(user_info_id: int) -> Optional[StravaAPI]:
    """Return a StravaAPI instance for the given user, or None if not connected."""
    with get_session() as sess:
        token_row = sess.exec(
            select(StravaToken).where(StravaToken.user_info_id == user_info_id)
        ).first()
        if not token_row:
            return None
    client = StravaAPI(_cfg)
    client.token_data = {
        "access_token":  token_row.access_token,
        "refresh_token": token_row.refresh_token,
        "expires_at":    token_row.expires_at,
    }
    return client


def _enrich_activities(
    activities: List[Activity],
    client: StravaAPI,
) -> List[Activity]:
    """Fetch streams for each activity, enriching summary_polyline and elevation_profile in-place.

    Returns any activities that could not be enriched due to rate limiting.
    """
    pending: List[Activity] = []
    for index, act in enumerate(activities):
        if act.id is None:
            continue
        if act.is_edited:
            continue  # locally edited track — never overwrite from Strava
        if client.remaining_requests <= 2:
            pending.append(act)
            continue
        try:
            streams  = client.get_activity_streams(act.id)
            latlng   = streams.get("latlng",   {}).get("data") or []
            altitude = streams.get("altitude", {}).get("data") or []
            distance = streams.get("distance", {}).get("data") or []

            if latlng:
                act.summary_polyline = polyline_lib.encode(
                    [(pt[0], pt[1]) for pt in latlng]
                )
                if not act.start_latlng:
                    act.start_latlng = [latlng[0][0], latlng[0][1]]
                if not act.end_latlng:
                    act.end_latlng = [latlng[-1][0], latlng[-1][1]]
            n = min(len(altitude), len(distance))
            if n >= 2:
                act.elevation_profile = (
                    [distance[i] / 1000 for i in range(n)],
                    [altitude[i]        for i in range(n)],
                )
        except RateLimitError:
            # The quota window filled between the check above and the call
            # (another request got there first — the limiter is process-wide
            # since issue #130). Defer this one and stop: every remaining
            # activity would hit the same wall.
            pending.append(act)
            pending.extend(a for a in activities[index + 1:]
                           if a.id is not None and not a.is_edited)
            break
        except Exception:
            pass  # private activity or network error — skip silently
    return pending


def _enrich_activities_background(
    activity_ids: List[int],
    user_info_id: int,
    owner_id: int,
    project_name: str,
) -> None:
    """Enrich GPS streams for newly imported activities in the background.

    Starts immediately (no sleep) so the response is never blocked.  Each
    activity is written to the DB as it completes so partial progress is
    preserved on interruption.  Strava 429 responses are handled by the
    StravaAPI client (sleeps Retry-After then continues).

    If the application's own 15-min/daily quota (shared process-wide, see
    :class:`RateLimiter`) runs low mid-batch, the remaining activities are
    *not* pushed through the limiter's up-to-60s wait one at a time — that
    would burn minutes for no benefit when every one of them would hit the
    same wall. Instead they're handed to :func:`_enrich_pending_background`,
    which sleeps until the window resets and retries them.

    ``user_info_id`` is the IMPORTER whose Strava token fetches the streams;
    ``owner_id`` is the PROJECT OWNER whose geo cache is keyed — the two differ
    when a companion imports into a shared trip (issue #106).
    """
    client = _strava_client_for_user(user_info_id)
    if client is None:
        return

    any_enriched = False
    pending: List[int] = []
    for index, activity_id in enumerate(activity_ids):
        if _repo.activity_is_edited(activity_id):
            continue  # locally edited track — never overwrite from Strava
        if client.remaining_requests <= 2:
            # Quota window is nearly exhausted — every remaining activity
            # would hit the same wall. Defer this one and the rest instead of
            # blocking on the limiter for up to 60s each.
            pending.append(activity_id)
            pending.extend(
                a for a in activity_ids[index + 1:]
                if not _repo.activity_is_edited(a)
            )
            break
        try:
            streams  = client.get_activity_streams(activity_id)
            latlng   = streams.get("latlng",   {}).get("data") or []
            altitude = streams.get("altitude", {}).get("data") or []
            distance = streams.get("distance", {}).get("data") or []

            polyline_str: Optional[str] = None
            ep_json: Optional[str] = None

            if latlng:
                polyline_str = polyline_lib.encode([(pt[0], pt[1]) for pt in latlng])
            n = min(len(altitude), len(distance))
            if n >= 2:
                ep_json = json.dumps({
                    "distances_km": [distance[i] / 1000 for i in range(n)],
                    "elevations_m": [altitude[i]        for i in range(n)],
                })

            if polyline_str or ep_json:
                with get_session() as sess:
                    _repo.update_activity_enrichment(
                        sess, activity_id, polyline_str, ep_json
                    )
                any_enriched = True
        except RateLimitError:
            # The quota window filled between the check above and the call
            # (another request got there first — the limiter is process-wide,
            # issue #130). Defer this one and the rest of the batch.
            _log.warning(
                "enrich activity=%s: rate limit hit mid-batch, deferring %d "
                "remaining activities", activity_id, len(activity_ids) - index,
            )
            pending.append(activity_id)
            pending.extend(
                a for a in activity_ids[index + 1:]
                if not _repo.activity_is_edited(a)
            )
            break
        except Exception as exc:  # noqa: BLE001 — private activity, network error, or revoked auth
            _log.warning(
                "enrich activity=%s failed: %s: %s", activity_id, type(exc).__name__, exc,
            )

    if any_enriched:
        with get_session() as sess:
            # Advance the project's lock_version (issue #173) so a native
            # client's on-disk cache — which only ever checks that counter —
            # notices the newly enriched polyline/elevation instead of serving
            # pre-enrichment data from disk indefinitely.
            project_id = _repo.project_id_for(sess, owner_id, project_name)
            if project_id is not None:
                bump_lock_version(sess, project_id)
                sess.commit()
        bust_geo_cache(owner_id, project_name)
        # Recompute now (still in the background task) so the user's next geo
        # load is a fast cache HIT rather than a cold recompute.
        warm_geo_cache(owner_id, project_name)
        warm_meta_cache(owner_id, project_name)

    if pending:
        _enrich_pending_background(pending, user_info_id, owner_id, project_name)


def _enrich_pending_background(
    pending_ids: List[int],
    user_info_id: int,
    owner_id: int,
    project_name: str,
) -> None:
    """Sleep until the Strava rate-limit window resets, then enrich remaining activities."""
    time.sleep(RateLimiter.WINDOW_SECONDS + 5)
    _enrich_activities_background(pending_ids, user_info_id, owner_id, project_name)


# ── Activity management ────────────────────────────────────────────────────────

class AddActivitiesRequest(BaseModel):
    activities: List[Dict[str, Any]]


@router.post("/{name}/activities", response_model=ActivitiesAddedOut,
             summary="Add activities to project")
def add_activities(
    name: str,
    body: AddActivitiesRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    """Add activities to a project, enriching GPS streams from Strava.

    If the rate limit is approached, remaining activities are queued for
    enrichment after the 15-min window resets.
    """
    user_info_id = int(current_user["sub"])

    activities: List[Activity] = parse_activities_or_log(body.activities, "activities_add")

    # Permission/ownership check runs once, outside the retry loop below: the
    # caller and the project's ownership/membership can't change mid-request,
    # so — unlike the quota check below — re-running this on every retry
    # attempt would just repeat the same answer (see resolve_project's
    # docstring; same pattern as delete_item/reorder_items in
    # api/project_items.py).
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        project_id = row.id

    added_holder: Dict[str, int] = {}

    def _add(project) -> None:
        # Plan limit on trip length (issue #121) — an import that reaches
        # outside the trip's current span stretches it. Re-checked from
        # scratch on EVERY retry attempt, in its own short-lived read-only
        # session (separate from save_project_with_retry's per-attempt write
        # session — this only reads, so it doesn't need to share it), against
        # the *current* DB state rather than the stale snapshot from a
        # previous failed attempt: a concurrent writer may have lengthened or
        # shortened the trip since this request started, and quota must
        # reflect reality at save time, not at request-start time. Reusing
        # the result from attempt 1 on a later retry could let an import
        # through that a concurrent change should have blocked, or the
        # reverse.
        with get_session() as qsess:
            ensure_trip_days_quota(
                qsess, project_id, owner_id,
                *[a.start_date_local for a in activities],
            )
        added_holder["added"] = project.add_activities(activities)

    # New activity rows record the IMPORTER (the caller), not the project
    # owner — a companion's imports must stay tied to their Strava account.
    # save_project_with_retry (src/project/repo_retry.py) reloads the project
    # fresh on each attempt and retries under check_version=True on a 409
    # conflict, instead of the blind check_version=False overwrite this used
    # to do — which could silently clobber a concurrent writer's already-
    # committed changes with no error at all.
    project = _repo.save_project_with_retry(
        owner_id, name, _add,
        activity_user_id=user_info_id,
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    added = added_holder["added"]

    bust_geo_cache(owner_id, name)

    # Enrich GPS streams in the background immediately — never blocks this response.
    # Uses the importer's (caller's) Strava token, not the project owner's.
    activity_ids = [a.id for a in activities if a.id is not None]
    if activity_ids:
        background_tasks.add_task(
            _enrich_activities_background, activity_ids, user_info_id, owner_id, name
        )

    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)

    return {
        "added": added,
        "total": len(project.activities),
        "pending_enrichment": len(activity_ids),
    }


async def _read_gpx_upload(file: UploadFile):
    """Size-guard, read, parse, and list what the file holds — or raise 422.

    Three things are load-bearing about the order here.

    The guard consults the upload's DECLARED size first, so an oversized body
    is refused without being pulled into memory at all. Reading it first and
    measuring afterwards — which is what this did — grew the process by the
    whole file before deciding it was too big.

    The bytes are re-checked after reading, because a declared size is a
    claim and some clients do not send one.

    And the parse runs in a worker thread. gpxpy is pure Python and entirely
    synchronous: a 40k-point ride costs 1.25 s and a file at the size limit
    4.5 s, and on the event loop that is 4.5 s in which this instance serves
    nobody. Both GPX routes were the only ``async def`` handlers in this
    module doing their own CPU work; the upload paths in journal.py and
    memories.py already hand off the same way.
    """
    try:
        if file.size is not None:
            guard_declared_size(file.size)
        contents = await file.read()
        guard_upload_size(contents)
        gpx, found = await run_in_threadpool(_parse_and_list, contents)
    except GPXImportError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                             detail={"errors": exc.errors})
    return gpx, found


def _parse_and_list(contents: bytes):
    """The synchronous half, for :func:`run_in_threadpool`."""
    gpx = parse_gpx_bytes(contents)
    return gpx, gpx_candidates(gpx)


def _import_fingerprint(candidate, start_dt):
    """The value that decides whether this track is already in the trip.

    The file's OWN start time is used whenever it has one, never the time the
    request carried. The form only carries HH:MM, and devices start recording
    mid-minute, so fingerprinting the supplied value gave the same file two
    identities: import it once from the preview (07:33) and once with the form
    left alone (07:33:12) and the second copy sailed past the duplicate check.

    Falls back to the supplied start for a file with no clock — a planned route
    — because there the date IS the distinguishing fact: the same route ridden
    on two days is two activities, which is the whole reason time is in the
    fingerprint at all. Returns None when neither is available, meaning "cannot
    be judged" rather than "no duplicate".
    """
    span = candidate.time_span
    basis = span[0] if span else start_dt
    if basis is None:
        return None
    return track_fingerprint(((p.lat, p.lng) for p in candidate.points),
                             basis.isoformat())


def _resolve_times(candidate, date, start_time, end_time):
    """The activity's start and end, from the form where given, else the file.

    A form value always wins: the file may be wrong, and the user is the one
    looking at it. What changed in unit 4 is that omitting them is allowed —
    before, a recorded track that knew exactly when it happened still made the
    user type it in.
    """
    supplied = (date, start_time, end_time)
    span = candidate.time_span
    if all(v is None for v in supplied):
        if span is not None:
            # Same accessor the preview reported from, so a file the preview
            # said had a clock cannot be refused here for not having one.
            return span
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["This file has no timestamps, so it needs a "
                               "date, a start time and an end time."]})

    if any(v is None for v in supplied):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["A date, a start time and an end time go "
                               "together — supply all three, or none to "
                               "take them from the file."]})

    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
        start_clock = datetime.strptime(start_time, "%H:%M").time()
        end_clock = datetime.strptime(end_time, "%H:%M").time()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["date must be YYYY-MM-DD and start_time/"
                               "end_time must be HH:MM."]},
        )
    start_dt = datetime.combine(day, start_clock, tzinfo=timezone.utc)
    end_dt = datetime.combine(day, end_clock, tzinfo=timezone.utc)
    if end_dt < start_dt:
        # An end before the start means the activity ran past midnight: a
        # night ride leaving at 23:30 and back at 00:30. Refusing it made
        # every such ride unimportable, since the form carries one date.
        end_dt += timedelta(days=1)
    if end_dt == start_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["Start and end cannot be the same time."]})
    return start_dt, end_dt


def _existing_import(sess, project_row_id: int, fingerprint: str):
    """The activity in this trip already holding *fingerprint*, if any."""
    return sess.exec(
        select(DBActivity)
        .join(DBProjectItem, DBProjectItem.activity_id == DBActivity.id)
        .where(DBProjectItem.project_id == project_row_id)
        .where(DBActivity.source_id == fingerprint)
    ).first()


@router.post("/{name}/activities/gpx/inspect", response_model=GPXInspectOut,
             summary="Read a GPX file without importing it")
async def inspect_gpx_file(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
    owner: OwnerParam = None,
):
    """Report what a GPX file contains, so the user can confirm before committing.

    A dry run. It writes nothing — which is what lets the client show a preview,
    prefill every field from the file, offer a choice between several tracks, and
    warn that a track is already in the trip, all before anything is created.
    The first version of this import had none of that: it asked for a date and a
    time up front and only then said whether the file was acceptable at all.

    Editor role, like the import itself: reading someone else's file into a
    trip you cannot add to has no purpose.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        project_row_id = row.id

    gpx, found = await _read_gpx_upload(file)

    if not found:
        return {"candidates": [], "errors": validate_for_import(gpx)}

    out = await run_in_threadpool(_describe_candidates, found)

    duplicate = None
    if len(found) == 1 and not out[0]["errors"]:
        fingerprint = _import_fingerprint(found[0], None)
        if fingerprint is not None:
            with get_session() as sess:
                existing = _existing_import(sess, project_row_id, fingerprint)
            if existing is not None:
                duplicate = {"activity_id": existing.id,
                             "name": existing.name}

    return {
        "candidates": out,
        "suggested_name": gpx_suggested_name(gpx, found[0], file.filename),
        "duplicate_of": duplicate,
    }


def _describe_candidates(found):
    """Summarise each candidate. Synchronous and O(points), so off the loop.

    A candidate that cannot be imported is not measured: distance and moving
    time are full passes over the points, and spending 4.5 s computing them
    for a track the answer will reject anyway is work nobody asked for.
    """
    out = []
    for candidate in found:
        errors = validate_candidate(candidate)
        metrics = (recompute_track_metrics(candidate.points) if not errors
                   else None)
        span = candidate.time_span
        out.append({
            "index": candidate.index,
            "name": candidate.name,
            "activity_type": candidate.activity_type,
            "point_count": candidate.point_count,
            "distance_m": metrics.distance if metrics else 0.0,
            "is_route": candidate.is_route,
            "has_times": candidate.has_times,
            "started_at": (span[0].isoformat() if span else None),
            "ended_at": (span[1].isoformat() if span else None),
            "elapsed_seconds": candidate.elapsed_seconds,
            "moving_seconds": (candidate.moving_seconds if not errors
                               else None),
            "elevation_gain_m": (metrics.total_elevation_gain if metrics
                                 else None),
            "elevation_gain_estimated": True,
            "polyline": _preview_polyline(candidate.points) if not errors else None,
            "errors": errors,
        })
    return out


def _preview_polyline(points) -> Optional[str]:
    """An outline of the track, thinned to at most :data:`PREVIEW_POINTS`.

    Thinned by stride rather than by Douglas-Peucker: this is a thumbnail, so
    what matters is a predictable point count and one pass over the list, not
    the minimal set of points within a tolerance. The first and last points are
    always kept, because a preview that does not start and end where the track
    does looks wrong in a way a user notices.
    """
    if len(points) < 2:
        return None
    # Ceiling division: floor let 399 points through a cap of 200, because
    # 399 // 200 is a stride of 1.
    stride = -(-len(points) // PREVIEW_POINTS)
    kept = points[::stride]
    if len(points) % stride != 1:
        # The stride missed the final point, and a preview that does not end
        # where the track does looks wrong in a way users notice.
        kept.append(points[-1])
    return polyline_lib.encode([(p.lat, p.lng) for p in kept])



@router.post("/{name}/activities/import-gpx", response_model=GPXImportOut,
             summary="Import a single activity from a GPX file")
async def import_gpx_activity(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    date: Annotated[Optional[str], Form()] = None,
    start_time: Annotated[Optional[str], Form()] = None,
    end_time: Annotated[Optional[str], Form()] = None,
    activity_type: Annotated[Optional[str], Form()] = None,
    track_index: Annotated[Optional[int], Form()] = None,
    activity_name: Annotated[Optional[str], Form()] = None,
    owner: OwnerParam = None,
):
    """Import a GPX track as a new local activity — no Strava involved.

    Unlike ``add_activities``, the geometry is already final (it comes straight
    off the uploaded track), so nothing is queued for background enrichment.

    Every field is optional now, and what is omitted is taken from the file: the
    date and times from its ``<time>`` stamps, the name from its ``<name>``, the
    type from its ``<type>``. They remain accepted because the file does not
    always have them — a planned route carries no clock — and because the user
    is entitled to correct what it does say. ``track_index`` chooses between
    several tracks in one file; the positions are the ones ``inspect`` returned.
    """
    user_info_id = int(current_user["sub"])

    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        project_row_id = row.id

    gpx, found = await _read_gpx_upload(file)

    # track_index stays None unless the caller chose one, so a file holding
    # several tracks is still refused rather than quietly importing the first.
    # Silently taking part of a file is the outcome issue #260 ruled out.
    problems = validate_for_import(gpx, track_index=track_index)
    if problems:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                             detail={"errors": problems})
    candidate = found[track_index or 0]

    start_dt, end_dt = _resolve_times(candidate, date, start_time, end_time)
    # start_date is documented as ISO-8601 UTC, and a file may carry any
    # offset it likes. Normalising here keeps the column honest and keeps two
    # exports of one ride — 05:33Z and 07:33+02:00 — the same instant.
    start_dt = start_dt.astimezone(timezone.utc)
    end_dt = end_dt.astimezone(timezone.utc)
    elapsed_time = int((end_dt - start_dt).total_seconds())
    moving_time = candidate.moving_seconds
    if moving_time is None:
        # No clock in the file — a planned route — so there is nothing to
        # distinguish moving from stopped and the whole window is moving time.
        moving_time = elapsed_time
    else:
        # Clamped, not replaced. A track that genuinely never moved has zero
        # moving time and should say so; replacing a zero with the elapsed time
        # is the very thing unit 3 stopped doing.
        moving_time = min(moving_time, elapsed_time)

    points = candidate.points
    metrics = recompute_track_metrics(points)
    fingerprint = _import_fingerprint(candidate, start_dt)

    resolved_name = (activity_name
                     or gpx_suggested_name(gpx, candidate, file.filename)
                     or "GPX Import")
    resolved_type = activity_type or candidate.activity_type or "Workout"

    activity = Activity(
        id=None,
        name=resolved_name,
        type=resolved_type,
        distance=metrics.distance,
        moving_time=moving_time,
        elapsed_time=elapsed_time,
        total_elevation_gain=metrics.total_elevation_gain,
        start_date=start_dt,
        start_date_local=start_dt,
        timezone="UTC",
        achievement_count=0,
        kudos_count=0,
        comment_count=0,
        athlete_count=0,
        photo_count=0,
        trainer=False,
        commute=False,
        manual=True,
        private=False,
        flagged=False,
        average_speed=metrics.distance / moving_time if moving_time > 0 else 0.0,
        max_speed=0.0,
        has_heartrate=False,
        pr_count=0,
        total_photo_count=0,
        has_kudoed=False,
        elev_high=metrics.elev_high,
        elev_low=metrics.elev_low,
        start_latlng=metrics.start_latlng,
        end_latlng=metrics.end_latlng,
        summary_polyline=points_to_polyline(points),
        elevation_profile=points_to_elevation_profile(points),
        source="gpx",
        source_id=fingerprint,
    )

    with get_session() as sess:
        # Refuse a file this trip already holds. The same track legitimately
        # belongs to two different trips, so the check is scoped to this one's
        # timeline rather than to the global activity table.
        duplicate = (_existing_import(sess, project_row_id, fingerprint)
                     if fingerprint is not None else None)
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "errors": [
                        f'This trip already has "{duplicate.name}" from the same '
                        f"track."
                    ],
                    "activity_id": duplicate.id,
                },
            )

        try:
            activity.id = allocate_local_activity_id(sess)
        except LocalIdExhausted:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                 detail="Could not allocate a unique activity id.")

    def _add(project) -> None:
        # Re-checked from scratch on every retry attempt, in its own
        # short-lived read-only session, against current DB state rather than
        # a stale snapshot — same reasoning as the bulk Strava import above.
        with get_session() as qsess:
            ensure_trip_days_quota(
                qsess, project_row_id, owner_id, activity.start_date_local)
        project.add_activities([activity])

    # New activity rows record the IMPORTER (the caller), not the project
    # owner — see add_activities. Goes through save_project_with_retry rather
    # than a blind save_project: this is a load-mutate-save, and the blind
    # variant rewrites every field of the row from the snapshot loaded before
    # the mutation — so a PUT /day-meta (or any other write) committing in
    # that window was silently overwritten with pre-request values.
    # In a threadpool: this handler is `async def`, and save_project_with_retry
    # sleeps between attempts (src/project/repo_retry.py). Up to ~0.3 s of
    # time.sleep on the event loop under contention would stall every other
    # request on the worker. The bulk import above is a sync `def`, so Starlette
    # already gives it a thread; this one awaits the upload, so it hands off
    # just the blocking part.
    project = await run_in_threadpool(
        _repo.save_project_with_retry,
        owner_id, name, _add,
        activity_user_id=user_info_id,
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)

    return {"activity_id": activity.id, "total": len(project.activities)}


# ── Single-activity refresh ────────────────────────────────────────────────────

def _set_refresh_state(
    activity_id: int,
    refresh_status: Optional[str],
    *,
    started_at: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Write only the refresh bookkeeping columns on an activity row.

    Deliberately separate from :meth:`force_update_activity`, which overwrites
    the activity's *data* columns and does not touch these — so the job can
    persist a freshly fetched activity without clobbering its own status.
    """
    with get_session() as sess:
        row = sess.get(DBActivity, activity_id)
        if row is None:
            return
        row.refresh_status = refresh_status
        row.refresh_started_at = started_at
        row.refresh_error = error
        sess.add(row)
        sess.commit()


def _refresh_activity_job(
    user_info_id: int, owner_id: int, name: str, activity_id: int
) -> None:
    """Background task: re-fetch one activity from Strava and persist it.

    Runs off the request path because the two Strava calls below can take
    minutes end to end — a 60 s rate-limiter wait per attempt plus a >=60 s
    sleep per 429, times three attempts, times two calls. Held open as a
    request that reliably blew past the client's 30 s HTTP timeout, so the user
    saw "re-fetch failed: timeout" for work the server usually finished
    (issue #148).

    The row was marked ``pending`` synchronously by the trigger; every exit path
    here writes a terminal ``resolved``/``failed``. Mirrors
    :func:`api.segments._resolve_route_job`.
    """
    try:
        client = _strava_client_for_user(user_info_id)
        if client is None:
            _set_refresh_state(activity_id, "failed", error="Strava not connected")
            return

        # 1. Fetch fresh activity metadata from Strava
        try:
            raw = client.get_activity(activity_id)
        except Exception as exc:  # noqa: BLE001 — any failure marks the re-fetch failed
            _log.warning("refresh activity=%s status=failed: %s", activity_id, exc)
            _set_refresh_state(
                activity_id, "failed",
                error=f"Strava fetch failed: {exc}"[:200],
            )
            return

        act = Activity.from_strava_api(raw)

        # 2. Enrich with full GPS streams (single call — check rate limit first)
        if client.remaining_requests > 2:
            try:
                streams  = client.get_activity_streams(activity_id)
                latlng   = streams.get("latlng",   {}).get("data") or []
                altitude = streams.get("altitude", {}).get("data") or []
                distance = streams.get("distance", {}).get("data") or []
                if latlng:
                    act.summary_polyline = polyline_lib.encode(
                        [(pt[0], pt[1]) for pt in latlng]
                    )
                    # Derive start/end from stream if metadata didn't provide them
                    if not act.start_latlng:
                        act.start_latlng = [latlng[0][0], latlng[0][1]]
                    if not act.end_latlng:
                        act.end_latlng = [latlng[-1][0], latlng[-1][1]]
                n = min(len(altitude), len(distance))
                if n >= 2:
                    act.elevation_profile = (
                        [distance[i] / 1000 for i in range(n)],
                        [altitude[i]        for i in range(n)],
                    )
            except Exception:
                pass  # streams failed — still save the refreshed metadata

        # 3. Overwrite the DB row (all columns, including enrichment)
        with get_session() as sess:
            # Advance the project's lock_version (issue #173) so a native
            # client's on-disk cache — which only ever checks that counter —
            # notices the re-fetched polyline/elevation instead of serving
            # the pre-refresh data from disk indefinitely.
            project_id = _repo.project_id_for(sess, owner_id, name)
            _repo.force_update_activity(sess, user_info_id, act, project_id)
        _set_refresh_state(activity_id, "resolved")
        _log.info("refresh activity=%s status=resolved", activity_id)
    except Exception as exc:  # noqa: BLE001
        # The row was marked "pending" synchronously by the trigger. If the job
        # crashes anywhere above, nothing writes a terminal status and the tile
        # spins forever. Best-effort flip it to "failed" and log the cause.
        # (Unlike api.segments._resolve_route_job, this refresh job has no RQ
        # retry to preserve, so it still recovers locally rather than raising.)
        _log.exception("refresh activity=%s crashed before persisting a verdict", activity_id)
        try:
            _set_refresh_state(activity_id, "failed", error=str(exc)[:200] or "Re-fetch failed")
        except Exception:  # noqa: BLE001
            _log.exception("could not mark activity=%s failed after a crashed refresh", activity_id)
    finally:
        bust_geo_cache(owner_id, name)
        # Warm while still off the request path so reopening the project is a
        # fast cache HIT rather than a cold recompute.
        warm_geo_cache(owner_id, name)
        warm_meta_cache(owner_id, name)


@router.post("/{name}/activities/{activity_id}/refresh",
             status_code=status.HTTP_202_ACCEPTED,
             summary="Trigger async activity refresh from Strava")
def refresh_activity(
    name: str,
    activity_id: int,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Schedule a re-fetch of one activity from Strava.

    Fetches fresh metadata (name, distance, kudos, etc.) plus full GPS streams
    (polyline + elevation).  Useful when the user has edited the activity on
    Strava and wants the local copy to reflect those changes.

    The Strava calls can take minutes (rate limiting, 429 backoff), so they run
    as a background task rather than blocking the request — holding the request
    open is what made this fail with a client-side timeout (issue #148). Every
    check that can be answered without calling Strava still happens
    synchronously, so a permission/edit/connection problem is still an immediate
    error rather than a job that fails a poll later.

    The activity is marked ``refresh_status="pending"`` synchronously and a 202
    is returned. The client polls ``/meta`` until it flips to ``resolved`` or
    ``failed``. See :func:`_refresh_activity_job`.
    """
    user_info_id = int(current_user["sub"])

    # Check project access first, then restrict the refresh to the activity's
    # IMPORTER (issue #106): a re-fetch talks to Strava with the caller's token,
    # and another editor's account can't see this activity — worse, the
    # overwrite would re-attribute the row to the wrong user.
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        act_row = sess.get(DBActivity, activity_id)
    if act_row is not None and act_row.user_info_id != user_info_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the user who imported this activity can refresh it from Strava",
        )

    # Locally edited tracks must never be overwritten by a Strava re-fetch.
    # Surface a clear message so the client can prompt the user to reset first.
    if _repo.activity_is_edited(activity_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This activity has a locally edited track. "
                   "Reset it to Strava before refreshing.",
        )

    # Checked here as well as in the job: a missing Strava connection is knowable
    # without any network call, so the user gets it as an error on the button
    # press rather than as a failed poll seconds later.
    if _strava_client_for_user(user_info_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Strava not connected",
        )

    _set_refresh_state(
        activity_id, "pending",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    # The client polls /meta for this very flag, so the cached payload — which
    # still says "not pending" — has to go before the first poll lands (#178).
    bust_geo_cache(owner_id, name)
    background_tasks.add_task(
        _refresh_activity_job, user_info_id, owner_id, name, activity_id
    )
    return {"status": "pending", "refresh_status": "pending"}


# ── Activity geometry editing (issue #31) ─────────────────────────────────────

class TrackPointIn(BaseModel):
    lat: float
    lng: float
    elev: Optional[float] = None

    # Raises HTTPException directly rather than the usual ValueError: a
    # ValueError becomes a pydantic ValidationError, and FastAPI's default 422
    # handler echoes the rejected value back as `input` in the response body —
    # Starlette's JSONResponse renders with allow_nan=False, so a NaN/Infinity
    # `input` would blow up turning this into a 500 instead of the clean 422
    # this validation exists to produce. An HTTPException skips that path
    # entirely (matches the plain-string 422s raised elsewhere in this file,
    # e.g. edit_activity_track's "A track needs at least 2 points").
    @field_validator("lat")
    @classmethod
    def _lat_in_range(cls, v: float) -> float:
        if not math.isfinite(v) or not (-90.0 <= v <= 90.0):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="lat must be finite and within -90..90",
            )
        return v

    @field_validator("lng")
    @classmethod
    def _lng_in_range(cls, v: float) -> float:
        if not math.isfinite(v) or not (-180.0 <= v <= 180.0):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="lng must be finite and within -180..180",
            )
        return v


class TrackEditRequest(BaseModel):
    points: List[TrackPointIn] = Field(
        description="Full edited track as an ordered list of {lat, lng, elev?} points")
    lock_version: Optional[int] = Field(
        default=None,
        description="The project's lock_version last seen by the editor (from "
                    "GET .../track). When given, the save is rejected with 409 "
                    "if the project has changed since — e.g. the same activity "
                    "edited from a second tab. Omit to save unconditionally.")


def _project_contains_activity(project, activity_id: int) -> bool:
    return any(
        it.item_type == "activity" and it.activity_id == activity_id
        for it in project.items
    )


@router.get("/{name}/activities/{activity_id}/track",
            summary="Get a single activity's editable geometry")
def get_activity_track(
    name: str,
    activity_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Return one activity's editor payload (map.summary_polyline + elevation_profile
    pairs), so the track editor doesn't download the whole project just to edit a
    single activity — the full GET /{name} payload is 10-15x larger. Same per-activity
    shape as GET /{name}.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        project = _repo.get_project(
            sess, row.user_info_id, name,
        )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    activity = next((a for a in project.activities if a.id == activity_id), None)
    if activity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not in project")
    d = activity.to_strava_dict()
    ep = activity.elevation_profile or getattr(activity, "elevation_profile_low_res", None)
    d["elevation_profile"] = [list(pair) for pair in zip(ep[0], ep[1])] if ep else None
    # So the editor can send it back on save/split — see TrackEditRequest.lock_version.
    d["lock_version"] = project.lock_version
    return d


@router.put("/{name}/activities/{activity_id}/track",
            summary="Replace an activity's track geometry")
def edit_activity_track(
    name: str,
    activity_id: int,
    body: TrackEditRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    """Overwrite an activity's track with an edited point list (trim/add/remove).

    Snapshots the original geometry on the first edit, marks the activity edited
    (so Strava sync skips it), recomputes distance / elevation / times, and
    returns the updated project.
    """
    from src.models.track_edit import TrackPoint

    user_info_id = int(current_user["sub"])
    if len(body.points) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A track needs at least 2 points",
        )
    points = [TrackPoint(lat=p.lat, lng=p.lng, elev=p.elev) for p in body.points]

    # Phase-timed (issue #45 follow-up): the align_points O(N*M) fix cut most
    # of the hang, but split/edit-track were still blowing past the client's
    # timeout on some real-world tracks — this pins down which phase (DB
    # load+commit vs. response serialisation) the remaining time is in, on the
    # next repro, instead of inferring it from scheduler-jitter side effects.
    t0 = time.time()
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        # include_heavy=False: this load is only used for the existence/
        # containment check below, never the track geometry — no reason to pull
        # every activity's summary_polyline/elevation_profile_json off disk just
        # to check membership (issue #45 follow-up: this alone measured 2.2s on
        # a project with a large activity).
        project = _repo.get_project(
            sess, owner_id, name,
            include_heavy=False,
        )
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        if not _project_contains_activity(project, activity_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not in project")
        if not _repo.edit_activity_track(
            sess, row.id, activity_id, points, expected_version=body.lock_version
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
        # include_elevation=False: the client (see project_notifier.dart
        # saveActivityTrack) discards this response and immediately re-fetches
        # via /meta + /geo, so there's no reason to pay for serialising every
        # activity's full elevation_profile (~12 MB on a large trip) into a
        # response nobody reads.
        project = _repo.get_project(
            sess, owner_id, name,
            include_elevation=False,
        )
    t1 = time.time()

    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)
    result = _repo.to_dict(project)
    t2 = time.time()
    _log.info(
        "edit_activity_track name=%s activity_id=%s db=%.3fs serialize=%.3fs total=%.3fs",
        name, activity_id, t1 - t0, t2 - t1, t2 - t0,
    )
    return result


@router.post("/{name}/activities/{activity_id}/reset",
             summary="Reset an edited activity's track to the original")
def reset_activity_track(
    name: str,
    activity_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    """Restore an edited activity's geometry from its snapshot and clear is_edited.

    On the root of a split family this also undoes the split — the pieces cut out
    of it would otherwise duplicate the restored full track (#141). The editor
    confirms before calling this; see reset_activity_track in the repo.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        # include_heavy=False: only used for the containment check below — see
        # edit_activity_track for why.
        project = _repo.get_project(
            sess, owner_id, name,
            include_heavy=False,
        )
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        if not _project_contains_activity(project, activity_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not in project")
        if not _repo.reset_activity_track(sess, row.id, activity_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Activity has no edit to reset",
            )
        # include_elevation=False: see edit_activity_track for why — the
        # client discards this response and immediately re-fetches via
        # /meta + /geo.
        project = _repo.get_project(
            sess, owner_id, name,
            include_elevation=False,
        )

    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)
    return _repo.to_dict(project)


class SplitRequest(BaseModel):
    split_index: int = Field(
        description="0-based point index at which to split; the point is shared "
                    "as the last point of the head and the first of the tail")
    drop_boundary: bool = Field(
        default=False,
        description="If true, exclude the boundary point from the tail instead "
                    "of sharing it — used when a transportation segment will "
                    "bridge the gap at the cut (issue #104)")
    points: Optional[List[TrackPointIn]] = Field(
        default=None,
        description="The client's current (possibly unsaved) edited track. When "
                    "given, the split is taken from these points and split_index "
                    "indexes into them; when omitted the stored geometry is used. "
                    "Issue #127: without this the editor's pending trims/deletes "
                    "were discarded by a split and split_index was applied to a "
                    "different point list than the one the user was looking at.")
    lock_version: Optional[int] = Field(
        default=None,
        description="The project's lock_version last seen by the editor (from "
                    "GET .../track). When given, the split is rejected with 409 "
                    "if the project has changed since — e.g. the same activity "
                    "edited from a second tab. Omit to split unconditionally.")


@router.post("/{name}/activities/{activity_id}/split",
             summary="Split an activity into a head and a local tail")
def split_activity(
    name: str,
    activity_id: int,
    body: SplitRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    """Split an activity at *split_index*: the head keeps its Strava id, the tail
    becomes a new LOCAL activity (negative id, manual, "<name> (2)") inserted
    right after the head. Both pieces are marked edited. Returns the updated project.

    When *points* is supplied the split is taken from that (edited) track rather
    than the stored one, so unsaved editor changes compound with the cut instead
    of being discarded (issue #127).
    """
    from src.models.track_edit import TrackPoint

    user_info_id = int(current_user["sub"])
    edited_points = (
        [TrackPoint(lat=p.lat, lng=p.lng, elev=p.elev) for p in body.points]
        if body.points is not None else None
    )
    if edited_points is not None and len(edited_points) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A track needs at least 2 points",
        )
    # Phase-timed (issue #45 follow-up) — see edit_activity_track for why.
    t0 = time.time()
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        # include_heavy=False: only used for the containment check below — see
        # edit_activity_track for why.
        project = _repo.get_project(
            sess, owner_id, name,
            include_heavy=False,
        )
        if project is None or not _project_contains_activity(project, activity_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not in project")
        t1 = time.time()
        try:
            tail_id = _repo.split_activity(
                sess, owner_id, row.id, activity_id, body.split_index,
                drop_boundary=body.drop_boundary, points=edited_points,
                expected_version=body.lock_version)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
        if tail_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
        t2 = time.time()
        # include_elevation=False: the client (see project_notifier.dart
        # splitActivity) discards this response and immediately re-fetches via
        # /meta + /geo, so there's no reason to pay for serialising every
        # activity's full elevation_profile (~12 MB on a large trip) into a
        # response nobody reads.
        project = _repo.get_project(
            sess, owner_id, name,
            include_elevation=False,
        )
    t3 = time.time()

    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)
    result = _repo.to_dict(project)
    t4 = time.time()
    _log.info(
        "split_activity name=%s activity_id=%s load=%.3fs split_commit=%.3fs "
        "reload=%.3fs serialize=%.3fs total=%.3fs",
        name, activity_id, t1 - t0, t2 - t1, t3 - t2, t4 - t3, t4 - t0,
    )
    return result


@router.delete("/{name}/activities/{activity_id}/local",
               status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a local (split-tail) activity")
def delete_local_activity(
    name: str,
    activity_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    """Delete a local (negative-id) activity row and unlink it from the project.

    Only local activities may be deleted (Strava activities are shared). This is
    the undo path for a split — deleting the tail leaves the head in place.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        if not _repo.delete_local_activity(sess, row.id, activity_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Local activity not found",
            )
    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)


# ── Activity field update (issue #29 — client-side E2EE migration) ────────────
#
# A separate, project-agnostic router: activities are rows shared across every
# project that references them (see DBActivity's docstring), so — unlike the
# routes above — this doesn't hang off /api/projects/{name}. Deliberately
# narrow: it only ever writes the six DB columns EncryptionMigration.run()
# (flutter_client/lib/src/crypto/encryption_migration.dart) needs to migrate an
# activity from plaintext to ciphertext, plus the two original_* edit-undo
# snapshot columns (issue #31) it may also need to scrub — nothing else. This
# is not a general-purpose activity editor; every other activity field is
# updated exclusively via the Strava-sync / track-edit paths above.
activity_fields_router = APIRouter(prefix="/api/activities", tags=["activities"])


class ActivityFieldsUpdate(BaseModel):
    name: Optional[str] = None
    summary_polyline: Optional[str] = None
    start_latlng_json: Optional[str] = None
    end_latlng_json: Optional[str] = None
    elevation_profile_json: Optional[str] = None
    elevation_profile_low_res_json: Optional[str] = None
    original_polyline: Optional[str] = None
    original_elevation_profile_json: Optional[str] = None


@activity_fields_router.put("/{activity_id}", summary="Update an activity's E2EE-in-scope fields")
def update_activity_fields(
    activity_id: int,
    body: ActivityFieldsUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
):
    """Write only the fields present in the request body (unset fields are left
    untouched — this is a partial update, not a replace) directly onto the
    activity row. Used by the client's encryption-enable migration to swap a
    still-plaintext field for its encrypted envelope, and safe to call
    repeatedly (idempotent: re-sending the same ciphertext is a no-op).

    The server does not interpret these values — once encrypted they're opaque
    ciphertext envelopes — so there is intentionally no JSON/polyline
    validation here, unlike the track-edit endpoints.
    """
    user_info_id = int(current_user["sub"])
    data = body.model_dump(exclude_unset=True)
    with get_session() as sess:
        row = sess.get(DBActivity, activity_id)
        if row is None or row.user_info_id != user_info_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")

        # Every project this activity appears in — needed both to bust the geo
        # cache below and to advance each one's lock_version (issue #173) so a
        # native client's on-disk cache notices the ciphertext swap.
        project_ids = sess.exec(
            select(DBProjectItem.project_id).where(
                DBProjectItem.item_type == "activity",
                DBProjectItem.activity_id == activity_id,
            ).distinct()
        ).all()
        for project_id in project_ids:
            bump_lock_version(sess, project_id)

        for field, value in data.items():
            setattr(row, field, value)
        sess.add(row)
        if "summary_polyline" in data:
            # Once the polyline is ciphertext the prepared row derived from its
            # plaintext must go too, or the simplified geo endpoints would keep
            # serving the track the user just encrypted (issue #369).
            store_prepared_geometry(sess, row)
        sess.commit()

        # Bust the full-res geo cache for every project this activity appears
        # in — same as every other activity-mutating endpoint above — so a
        # subsequent (non-E2EE-client) geo load doesn't serve a stale cached
        # response built from the pre-migration plaintext.
        project_names = sess.exec(
            select(DBProject.name).where(DBProject.id.in_(project_ids))
        ).all() if project_ids else []

    for pname in project_names:
        bust_geo_cache(user_info_id, pname)
        queue_stats_refresh(background_tasks, user_info_id, pname)

    return {"id": activity_id}

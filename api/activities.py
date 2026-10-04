"""REST activity endpoints — add/refresh/edit/split activities within a project.

Routes:
    POST   /api/projects/{name}/activities                          — add activities to project
    POST   /api/projects/{name}/activities/gpx/inspect               — read a GPX file without importing it
    POST   /api/projects/{name}/activities/import-gpx                — import a single activity from a GPX file
    POST   /api/projects/{name}/activities/import-gpx-tracks         — import every track of a GPX file
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
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Path, UploadFile, status
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
from src.gpx.timezone import local_to_utc, to_local, zone_at
from src.models.activity import (
    ACTIVITY_ID_MAX, ACTIVITY_ID_MIN, Activity, parse_activities_or_log,
)
from src.models.value_bounds import DURATION_MAX_S
from src.project.traxj_schema import activity_fault, stored_json_fault
from src.utils.encryption_check import is_encrypted_envelope
from src.models.track_edit import (
    elevation_profile_from_streams, implausible_track, repair_elapsed,
    points_to_elevation_profile, points_to_polyline, recompute_track_metrics,
)
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


#: An activity id in a URL: bounded to the database's 64-bit INTEGER, so a
#: larger one is a 422 rather than an OverflowError on the first lookup.
ActivityIdPath = Annotated[int, Path(ge=ACTIVITY_ID_MIN, le=ACTIVITY_ID_MAX)]

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
        description="Mapped from the file's <type>, in the types installed "
                    "clients offer (run, ride, hike, walk, Workout): any other "
                    "type is Workout here. Null when unrecognised")
    activity_type_exact: Optional[str] = Field(
        default=None,
        description="The type the file's <type> maps to, which may be one "
                    "installed clients do not offer, such as Kayaking")
    is_connection: bool = Field(
        default=False,
        description="True for a track a TraxJourney export drew for a "
                    "connecting segment, which import-gpx-tracks leaves out")
    point_count: int
    distance_m: float
    is_route: bool = Field(
        description="True for a planned <rte> rather than a recorded <trk>")
    has_times: bool = Field(description="False for a route, which has no clock")
    started_at: Optional[str] = Field(
        default=None, description="The file's first stamp, a UTC instant")
    ended_at: Optional[str] = Field(
        default=None, description="The file's last stamp, a UTC instant")
    timezone: str = Field(
        default="Etc/UTC",
        description="IANA zone at the track's first point; the zone the import "
                    "stores and reads typed times in")
    start_local: Optional[str] = Field(
        default=None,
        description="started_at as a naive ISO-8601 wall clock in `timezone`")
    end_local: Optional[str] = Field(
        default=None,
        description="ended_at as the naive wall clock in `timezone`")
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
    warnings: List[str] = Field(
        default_factory=list,
        description="What the user should know before importing, such as "
                    "a clock that looks wrong; never a reason not to import")


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


class GPXImportedTrackOut(BaseModel):
    activity_id: int
    name: str


class GPXSkippedTrackOut(BaseModel):
    track_index: int = Field(description="The track's position, as inspect gives it")
    name: str
    duplicate_of: GPXDuplicateOut = Field(
        description="The activity already holding this track: in the trip, "
                    "or imported from an earlier track of the same file")


class GPXImportTracksOut(BaseModel):
    imported: List[GPXImportedTrackOut]
    skipped: List[GPXSkippedTrackOut] = Field(
        description="Tracks left out because the trip already holds them")


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
            profile = elevation_profile_from_streams(distance, altitude)
            if profile is not None:
                act.elevation_profile = profile
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
            profile = elevation_profile_from_streams(distance, altitude)
            if profile is not None:
                ep_json = json.dumps({
                    "distances_km": profile[0],
                    "elevations_m": profile[1],
                })

            if polyline_str or ep_json:
                with get_session() as sess:
                    _repo.update_activity_enrichment(
                        sess, activity_id, polyline_str, ep_json,
                        owner_id=user_info_id,
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
    # An activity the trip-file import would refuse is dropped as a malformed
    # one is (#205): the trip it joined could not be exported and imported
    # back (issue #462).
    kept = []
    for n, act in enumerate(activities):
        fault = activity_fault(act.to_strava_dict(), n)
        if fault is None:
            kept.append(act)
        else:
            _log.warning("activities_add: dropped activity %s, which a trip file "
                         "could not hold: %s", act.id, fault)
    activities = kept

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
        # Only the caller's own activities: an id another account already
        # holds is theirs. save_project enforces the same within its own
        # transaction; filtering here keeps the counts and the enrichment
        # below to what can actually be added.
        activities = _repo.own_activities_only(sess, user_info_id, activities)

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


def _resolve_times(candidate, date, start_time, end_time, zone, times_local):
    """The activity's start and end, from the form where given, else the file.

    A form value always wins: the file may be wrong, and the user is the one
    looking at it. What changed in unit 4 is that omitting them is allowed —
    before, a recorded track that knew exactly when it happened still made the
    user type it in.

    Returns ``(start, end, typed_start)``: start and end as instants, and the
    typed start as a naive wall clock in *zone* when the track has no clock of
    its own, else None (issue #365). Typed times are read:

    - for an untimed track, as wall clock in *zone*, whatever the client says:
      installed clients prefill nothing there and send the clock the user
      picked, the same bytes the new client sends;
    - for a stamped track, as wall clock in *zone* only with *times_local*,
      and as UTC without it, because that is what installed clients show and
      send.

    A typed time equal to the file's own, to the minute, keeps the file's
    exact instant, so a start in a repeated DST hour is not re-read as its
    other occurrence.
    """
    supplied = (date, start_time, end_time)
    span = candidate.time_span
    if all(v is None for v in supplied):
        if span is not None:
            # Same accessor the preview reported from, so a file the preview
            # said had a clock cannot be refused here for not having one.
            return span[0], span[1], None
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
    start_wall = datetime.combine(day, start_clock)
    end_wall = datetime.combine(day, end_clock)
    if end_wall < start_wall:
        # An end before the start means the activity ran past midnight: a
        # night ride leaving at 23:30 and back at 00:30. Refusing it made
        # every such ride unimportable, since the form carries one date.
        end_wall += timedelta(days=1)
    if end_wall == start_wall:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["Start and end cannot be the same time."]})

    # The frame the typed clocks are in: the track's zone, or UTC for an
    # installed client typing over a stamped track.
    frame = zone if (span is None or times_local) else "Etc/UTC"

    def own_wall(own):
        # A stamp at the calendar's edge has no wall clock: the user is
        # typing times precisely to replace it, so it matches nothing.
        try:
            return to_local(own, frame).replace(second=0, microsecond=0)
        except (OverflowError, ValueError):
            return None

    def instant(wall, own, fold=0):
        if own is not None and own_wall(own) == wall:
            # A stamp without an offset is UTC, as to_local reads it.
            return own if own.tzinfo else own.replace(tzinfo=timezone.utc)
        return local_to_utc(wall, frame, fold=fold)

    try:
        start_dt = instant(start_wall, span[0] if span else None)
        end_dt = instant(end_wall, span[1] if span else None)
        if end_dt <= start_dt:
            # Clocks went back inside the activity: the end is the repeated
            # hour's second occurrence.
            end_dt = instant(end_wall, None, fold=1)
    except (OverflowError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["This date is outside the calendar the app can "
                               "store."]})
    if end_dt <= start_dt:
        # Only a start and end either side of a clock change get here.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["The end must come after the start."]})
    return start_dt, end_dt, (start_wall if span is None else None)


#: The types installed clients' type dropdown offers (Decision 3, E5). They
#: show nothing for a suggested type outside it.
_INSTALLED_CLIENT_TYPES = frozenset({"run", "ride", "hike", "walk", "Workout"})


def _installed_client_type(exact: Optional[str]) -> Optional[str]:
    """*exact* in the vocabulary installed clients know: any type beyond it
    is ``Workout``, their "Other". None stays None, so they still ask."""
    if exact is None or exact in _INSTALLED_CLIENT_TYPES:
        return exact
    return "Workout"


def _gpx_distance(candidate, metrics) -> float:
    """The distance a TraxJourney export carried, else the track's measured
    one: a Strava polyline is simplified, so its length falls short (#367)."""
    if candidate.carried_distance_m is not None:
        return candidate.carried_distance_m
    return metrics.distance


def _gpx_moving_seconds(candidate) -> Optional[int]:
    """The moving time a TraxJourney export carried, else the one measured
    from the file's clock; None when there is neither."""
    if candidate.carried_moving_seconds is not None:
        return candidate.carried_moving_seconds
    return candidate.moving_seconds


def _inspected_moving_seconds(candidate) -> Optional[int]:
    """What the import stores from the file's own times, for the preview:
    never longer than the track's span, as the import clamps it."""
    moving = _gpx_moving_seconds(candidate)
    elapsed = candidate.elapsed_seconds
    if moving is not None and elapsed is not None:
        moving = min(moving, elapsed)
    return moving


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
    for summary, candidate in zip(out, found):
        # The zone at the track's start, and its span as wall clocks there
        # (issue #365). started_at/ended_at stay UTC instants: installed
        # clients read them so. The server converts because Dart has no zone
        # database. A candidate that failed validation is not looked up: its
        # coordinates are not trusted, and it cannot be imported anyway.
        if summary["errors"]:
            continue
        first = candidate.points[0] if candidate.points else None
        zone = zone_at(first.lat if first else None,
                       first.lng if first else None)
        summary["timezone"] = zone
        span = candidate.time_span
        if span is not None:
            try:
                summary["start_local"] = to_local(span[0], zone).isoformat()
                summary["end_local"] = to_local(span[1], zone).isoformat()
            except (OverflowError, ValueError):
                # A clock at the calendar's edge; the review step asks for
                # times, as for any clock it flags as wrong.
                summary["start_local"] = summary["end_local"] = None

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
        implausible = implausible_track(metrics) if metrics else None
        if implausible is not None:
            errors = [*errors, implausible]
            metrics = None
        span = candidate.time_span
        out.append({
            "index": candidate.index,
            "name": candidate.name,
            "activity_type": _installed_client_type(candidate.activity_type),
            "activity_type_exact": candidate.activity_type,
            "is_connection": candidate.is_connection,
            "point_count": candidate.point_count,
            # The figures the import stores: carried by a TraxJourney export
            # when present (#367), else measured from the track.
            "distance_m": (_gpx_distance(candidate, metrics) if metrics
                           else 0.0),
            "is_route": candidate.is_route,
            "has_times": candidate.has_times,
            "started_at": (span[0].isoformat() if span else None),
            "ended_at": (span[1].isoformat() if span else None),
            "elapsed_seconds": candidate.elapsed_seconds,
            "moving_seconds": (_inspected_moving_seconds(candidate)
                               if not errors else None),
            "elevation_gain_m": (metrics.total_elevation_gain if metrics
                                 else None),
            "elevation_gain_estimated": True,
            "polyline": _preview_polyline(candidate.points) if not errors else None,
            "errors": errors,
            "warnings": [w for w in (_clock_warning(candidate),) if w],
        })
    return out


#: A stamp before this is a clock error more likely than a track: a device
#: stamps a point 1970-01-01, or 1980-01-06 (the GPS epoch), before its clock
#: syncs (issue #462).
_CLOCK_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _clock_warning(candidate) -> Optional[str]:
    """What the preview says of a clock that looks wrong (issue #462): stamps
    before :data:`_CLOCK_FLOOR` or more than a day ahead, or a span past the
    31-year bound. Only said, never refused, and no stamp is left out of
    anything: the user can set the date and times in review, and a span past
    the bound is repaired at import (repair_elapsed)."""
    ceiling = datetime.now(timezone.utc) + timedelta(days=1)

    def utc(t):
        return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)

    odd = sorted((t for t in candidate.times
                  if t is not None and not _CLOCK_FLOOR <= utc(t) <= ceiling), key=utc)
    too_long = (candidate.elapsed_seconds or 0) > DURATION_MAX_S
    if not odd and not too_long:
        return None
    parts = []
    if odd:
        first, last = odd[0], odd[-1]
        dates = (f"{first:%Y-%m-%d}" if first.date() == last.date()
                 else f"{first:%Y-%m-%d} to {last:%Y-%m-%d}")
        parts.append(f"1 timestamp is dated {dates}" if len(odd) == 1
                     else f"{len(odd)} timestamps are dated {dates}")
    if too_long:
        parts.append("the track spans more than 31 years")
    return ("This file's clock looks wrong: " + ", and ".join(parts)
            + ". Check the date and times before importing.")


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



def _gpx_activity(gpx, candidate, filename, *, date=None, start_time=None,
                  end_time=None, activity_type=None, activity_name=None,
                  times_local=False, activity_type_is_exact=False,
                  metrics=None):
    """The activity one candidate becomes, and its fingerprint, unsaved and
    without an id: what ``import-gpx`` and ``import-gpx-tracks`` both store.

    Raises a 422 for times or geometry the app cannot store. *metrics* are
    the track's, when the caller has already measured them.
    *activity_type_is_exact* says *activity_type* is the user's own pick,
    stored as sent (PIR1-1).
    """
    first = candidate.points[0] if candidate.points else None
    zone = zone_at(first.lat if first else None, first.lng if first else None)
    start_dt, end_dt, typed_start = _resolve_times(
        candidate, date, start_time, end_time, zone, times_local)
    # start_date is documented as ISO-8601 UTC, and a file may carry any
    # offset it likes. Normalising here keeps the column honest and keeps two
    # exports of one ride — 05:33Z and 07:33+02:00 — the same instant.
    try:
        start_dt = start_dt.astimezone(timezone.utc)
        end_dt = end_dt.astimezone(timezone.utc)
        # start_date_local is stored as Strava sync stores it: the wall clock
        # labelled UTC. An untimed track keeps the clock the user typed.
        start_local = (typed_start if typed_start is not None
                       else to_local(start_dt, zone)).replace(
                           tzinfo=timezone.utc)
    except (OverflowError, ValueError):
        # An offset can push a stamp near year 1 or 9999 past what a date holds.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["This track's clock is outside the calendar the app can "
                               "store. Set the date and times in review."]},
        )
    elapsed_time = int((end_dt - start_dt).total_seconds())
    moving_time = _gpx_moving_seconds(candidate)
    if moving_time is None:
        # No clock in the file — a planned route — so there is nothing to
        # distinguish moving from stopped and the whole window is moving time.
        moving_time = elapsed_time
    else:
        # Clamped, not replaced. A track that genuinely never moved has zero
        # moving time and should say so; replacing a zero with the elapsed time
        # is the very thing unit 3 stopped doing.
        moving_time = min(moving_time, elapsed_time)

    if all(v is None for v in (date, start_time, end_time)):
        # The file's own clock: one wrong stamp can make it decades long.
        # Repaired as a stored or imported one is; times the user set win.
        elapsed_time = repair_elapsed(elapsed_time, moving_time)

    points = candidate.points
    if metrics is None:
        metrics = recompute_track_metrics(points)
    implausible = implausible_track(metrics)
    if implausible is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail={"errors": [implausible]})
    distance = _gpx_distance(candidate, metrics)
    # Not start_dt: an untimed track has been fingerprinted on its typed wall
    # clock labelled UTC since before #365, which start_local still is, so a
    # re-import across the release is still recognised.
    fingerprint = _import_fingerprint(candidate, start_local)

    resolved_name = (activity_name
                     or gpx_suggested_name(gpx, candidate, filename)
                     or "GPX Import")
    if (activity_type is not None and not activity_type_is_exact
            and activity_type == _installed_client_type(candidate.activity_type)):
        # Sent back as inspect suggested it: an installed client offers no
        # Kayaking, so it suggests and sends Workout for one (Decision 3, E5).
        # The new client marks its pick as exact, so its "Other" on a kayak
        # stays Workout (PIR1-1).
        activity_type = candidate.activity_type
    resolved_type = activity_type or candidate.activity_type or "Workout"

    activity = Activity(
        id=None,
        name=resolved_name,
        type=resolved_type,
        distance=distance,
        moving_time=moving_time,
        elapsed_time=elapsed_time,
        total_elevation_gain=metrics.total_elevation_gain,
        start_date=start_dt,
        start_date_local=start_local,
        timezone=zone,
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
        average_speed=distance / moving_time if moving_time > 0 else 0.0,
        max_speed=0.0,
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
    return activity, fingerprint


#: Why import-gpx refuses a connection track (Q2).
_CONNECTION_REFUSAL = ("This track is a connecting segment between activities, "
                       "not an activity.")


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
    times_local: Annotated[bool, Form()] = False,
    activity_type_is_exact: Annotated[bool, Form()] = False,
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

    The activity's zone is the one at the track's first point (issue #365):
    ``start_date`` is the true UTC instant, ``start_date_local`` the wall
    clock there. ``times_local=true`` says typed times are wall clock in that
    zone; see :func:`_resolve_times` for how they are read without it.

    ``activity_type_is_exact=true`` stores ``activity_type`` as sent. Without
    it, the type inspect suggested to installed clients, sent back unchanged,
    is stored as the file's precise type (Decision 3, PIR1-1).

    A connecting segment's track is refused with a 400: it is a train or a
    flight drawn as an arc, not an activity, and installed clients do not
    read ``is_connection`` to leave it out (Q2, PIR1-3).
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
    if candidate.is_connection:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"errors": [_CONNECTION_REFUSAL]})

    activity, fingerprint = _gpx_activity(
        gpx, candidate, file.filename, date=date, start_time=start_time,
        end_time=end_time, activity_type=activity_type,
        activity_name=activity_name, times_local=times_local,
        activity_type_is_exact=activity_type_is_exact)

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


def _track_label(candidate) -> str:
    """How a refusal names a track: its own name, else its position."""
    return (f'"{candidate.name}"' if candidate.name
            else f"track {candidate.index + 1}")


def _importable_tracks(found):
    """``(candidate, metrics)`` for each track import-all takes, in file order.

    Connection tracks are left out, as are tracks inspect reports errors for
    (too few points, coordinates off the globe, an implausible track): the
    user saw those refused in review. The metrics are kept for the import.
    """
    kept = []
    for candidate in found:
        if candidate.is_connection or validate_candidate(candidate):
            continue
        metrics = recompute_track_metrics(candidate.points)
        if implausible_track(metrics) is None:
            kept.append((candidate, metrics))
    return kept


def _prepare_tracks(gpx, found, filename):
    """Each importable track as an unsaved activity, with its fingerprint.

    Synchronous and O(points), so the route hands it to a worker thread.
    Every field comes from the file. Raises a 400 naming the tracks without a
    clock, which need their times typed one at a time; and a 422 naming the
    track for anything else the app cannot store.
    """
    tracks = _importable_tracks(found)
    if not tracks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"errors": ["This file has no track that can be imported."]})
    untimed = [c for c, _ in tracks if c.time_span is None]
    if untimed:
        names = ", ".join(_track_label(c) for c in untimed)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"errors": [
                f"These tracks have no times: {names}. Import them one at a "
                f"time to set their date and times."
                if len(untimed) > 1 else
                f"This track has no times: {names}. Import it on its own to "
                f"set its date and times."]})
    prepared = []
    for candidate, metrics in tracks:
        try:
            activity, fingerprint = _gpx_activity(
                gpx, candidate, filename, metrics=metrics)
        except HTTPException as exc:
            errors = (exc.detail or {}).get("errors", [])
            raise HTTPException(
                status_code=exc.status_code,
                detail={"errors": [f"{_track_label(candidate)}: {e}"
                                   for e in errors]})
        prepared.append((candidate, activity, fingerprint))
    return prepared


@router.post("/{name}/activities/import-gpx-tracks",
             response_model=GPXImportTracksOut,
             summary="Import every track of a GPX file")
async def import_gpx_tracks(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    owner: OwnerParam = None,
):
    """Import every importable track of a GPX file as its own activity (#367).

    A TraxJourney GPX export holds one track per activity, so a trip exported
    and imported back comes back as its activities. Connection tracks are
    left out, and so are tracks review shows as refused.

    Names, types and times all come from the file, as ``import-gpx`` takes
    them when no field is sent: there is no form to correct them. A track
    without times would need them typed, so a file holding one is refused
    with a 400 naming it, for the user to import it alone.

    All or nothing: every new activity is written in one transaction, after
    one trip-days check over all their dates. A track this trip already holds
    (or an earlier track of the same file) is skipped and listed, not refused.
    """
    user_info_id = int(current_user["sub"])

    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        project_row_id = row.id

    gpx, found = await _read_gpx_upload(file)
    if not found:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail={"errors": validate_for_import(gpx)})
    prepared = await run_in_threadpool(_prepare_tracks, gpx, found, file.filename)

    with get_session() as sess:
        allocated = set()
        for _, activity, _ in prepared:
            try:
                while activity.id is None or activity.id in allocated:
                    activity.id = allocate_local_activity_id(sess)
            except LocalIdExhausted:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not allocate a unique activity id.")
            allocated.add(activity.id)

    imported: List[dict] = []
    skipped: List[dict] = []

    def _sort(qsess):
        # Which tracks are new, against the trip as it is now. An earlier
        # track of the same file counts too: a file holding one track twice
        # imports it once.
        imported.clear()
        skipped.clear()
        new = []
        seen: Dict[str, Activity] = {}
        for candidate, activity, fingerprint in prepared:
            existing = (seen.get(fingerprint)
                        or _existing_import(qsess, project_row_id, fingerprint))
            if existing is not None:
                skipped.append({
                    "track_index": candidate.index, "name": activity.name,
                    "duplicate_of": {"activity_id": existing.id,
                                     "name": existing.name}})
                continue
            seen[fingerprint] = activity
            new.append(activity)
            imported.append({"activity_id": activity.id, "name": activity.name})
        return new

    with get_session() as sess:
        if not _sort(sess):
            # Every track is already in the trip: nothing to write.
            return {"imported": imported, "skipped": skipped}

    def _add(project) -> None:
        # Re-sorted and re-checked on every retry attempt, against current DB
        # state, as import-gpx does: one trip-days check over every date.
        with get_session() as qsess:
            new = _sort(qsess)
            if new:
                ensure_trip_days_quota(
                    qsess, project_row_id, owner_id,
                    *(a.start_date_local for a in new))
        project.add_activities(new)

    # One save, so one transaction: all of the tracks or none. In a
    # threadpool for the same reason as import-gpx.
    project = await run_in_threadpool(
        _repo.save_project_with_retry,
        owner_id, name, _add,
        activity_user_id=user_info_id,
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    if imported:
        bust_geo_cache(owner_id, name)
        queue_stats_refresh(background_tasks, owner_id, name)
        queue_share_tiles_refresh(background_tasks, owner_id, name)

    return {"imported": imported, "skipped": skipped}


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
                profile = elevation_profile_from_streams(distance, altitude)
                if profile is not None:
                    act.elevation_profile = profile
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
    activity_id: ActivityIdPath,
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


def _require_rewritable_by_trip(sess, project_row: DBProject, activity_id: int) -> None:
    """404 unless the trip may rewrite this activity's geometry — see
    ``activity_rewritable_by_trip``. A missing row is left to the caller's
    own 404."""
    if not _repo.activity_rewritable_by_trip(sess, project_row.id, activity_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not in project")


def _project_contains_activity(project, activity_id: int) -> bool:
    return any(
        it.item_type == "activity" and it.activity_id == activity_id
        for it in project.items
    )


@router.get("/{name}/activities/{activity_id}/track",
            summary="Get a single activity's editable geometry")
def get_activity_track(
    name: str,
    activity_id: ActivityIdPath,
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
    activity_id: ActivityIdPath,
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
    # Checked on the new track alone: an edit only ever apportions the times
    # down, so the span cannot grow.
    implausible = implausible_track(recompute_track_metrics(points))
    if implausible is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail=implausible)

    # Phase-timed (issue #45 follow-up): the align_points O(N*M) fix cut most
    # of the hang, but split/edit-track were still blowing past the client's
    # timeout on some real-world tracks — this pins down which phase (DB
    # load+commit vs. response serialisation) the remaining time is in, on the
    # next repro, instead of inferring it from scheduler-jitter side effects.
    t0 = time.time()
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        _require_rewritable_by_trip(sess, row, activity_id)
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
    activity_id: ActivityIdPath,
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
        _require_rewritable_by_trip(sess, row, activity_id)
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
    activity_id: ActivityIdPath,
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
        _require_rewritable_by_trip(sess, row, activity_id)
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
                sess, row.id, activity_id, body.split_index,
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
    activity_id: ActivityIdPath,
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
        _require_rewritable_by_trip(sess, row, activity_id)
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

    # A value that is not a ciphertext envelope is plaintext the export parses
    # and writes as the activity's start, end or profile, so it must be one
    # the trip-file import takes (issue #462). HTTPException rather than
    # ValueError, for the reason TrackPointIn gives.
    @field_validator("start_latlng_json", "end_latlng_json", "elevation_profile_json",
                     "elevation_profile_low_res_json", "original_elevation_profile_json")
    @classmethod
    def _exportable(cls, v: Optional[str], info) -> Optional[str]:
        if v is not None and not is_encrypted_envelope(v):
            fault = stored_json_fault(info.field_name, v)
            if fault is not None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                    detail=fault)
        return v


@activity_fields_router.put("/{activity_id}", summary="Update an activity's E2EE-in-scope fields")
def update_activity_fields(
    activity_id: ActivityIdPath,
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
    ciphertext envelopes — so an envelope is taken as it is. A plaintext
    start, end or profile must be one the trip-file import takes (#462).
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

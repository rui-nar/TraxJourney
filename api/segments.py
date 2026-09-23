"""REST transport-segment endpoints — create/update/delete + async route resolution.

Routes:
    POST   /api/projects/{name}/segments                       — create a connecting segment
    PUT    /api/projects/{name}/segments/{id}                   — update a segment
    PUT    /api/projects/{name}/segments/{id}/track             — replace route with a manual edit
    DELETE /api/projects/{name}/segments/{id}                   — delete a segment
    POST   /api/projects/{name}/segments/{id}/resolve-route     — trigger async route resolution
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional

from models.db import get_session

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.deps import get_current_user
from api.geo import bust_geo_cache, warm_geo_cache
from api.project_access import (
    OwnerParam,
    journal_visible_positions,
    resolve_project,
    translate_insert_after,
)
from api.project_shared import _refresh_share_tiles, _refresh_stats_background, _repo, queue_share_tiles_refresh, queue_stats_refresh, warm_meta_cache
from src.billing.entitlements import ensure_trip_days_quota
from src.jobs.queue import QUEUE_RESOLVE, enqueue
from src.jobs.route_jobs import RESOLVER_VERSION, create_job, mark_done, mark_running
from src.models.project import ConnectingSegment, ProjectItem, SegmentEndpoint
from src.utils.logging import get_logger

_log = get_logger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Response schemas ──────────────────────────────────────────────────────────

class SegmentIDOut(BaseModel):
    id: str = Field(description="UUID of the newly created segment")


class RouteResolvedOut(BaseModel):
    polyline: List[List[float]] = Field(description="Resolved route as [[lon, lat], …] coordinates")
    stop_count: int = Field(description="Number of intermediate stops on the route")


class RouteResolveTriggered(BaseModel):
    """Returned by the async resolve-route trigger (HTTP 202)."""
    status: str = Field(description="Always 'pending' — resolution runs in the background")
    route_status: str = Field(description="Segment route_status after scheduling: 'pending'")


def _compute_segment_geometry(
    seg: ConnectingSegment, params: Dict[str, Any]
) -> tuple[list, int, bool, str]:
    """Run the (slow) HAFAS + Overpass lookups for a segment.

    Returns ``(polyline, stop_count, degraded, strategy)``.  ``degraded`` is True
    only for rail when every Overpass strategy failed and the result is a straight
    endpoint chord — the line is approximate, not real track.  Ferry/bus raise
    ``OverpassError`` on failure (never degrade), so their ``degraded`` is always
    False.  ``strategy`` names how the geometry was obtained (for logging).

    Side effect: also sets ``seg.route_hafas_failed`` — True when a train's HAFAS
    stop lookup failed and resolution fell through to the generic two-point OSM
    strategy instead. This is independent of ``degraded`` (OSM can still find a
    real track between the two plain endpoints), so it isn't part of the return
    tuple; the caller reads it off *seg* after this returns. Always reset here
    (not just on failure) so a segment's stale flag from an earlier attempt
    doesn't leak into a fresh, fully-successful resolve.

    Same for ``seg.route_error``: on a HAFAS failure it carries the provider's
    own message, so the caller can persist *why* the train lookup failed rather
    than only that it did (issue #277).
    """
    from src.services.overpass_service import (
        OverpassError,  # noqa: F401 — re-exported for callers' except clauses
        get_bus_geometry,
        get_ferry_geometry,
        get_rail_geometry,
    )

    seg.route_hafas_failed = False
    seg.route_error = None

    if seg.segment_type == "train":
        from src.services.hafas_service import HafasError, get_train_route

        use_date = params.get("date") or seg.date
        stops: list[dict] = [
            {"lat": seg.start.lat, "lon": seg.start.lon},
            {"lat": seg.end.lat,   "lon": seg.end.lon},
        ]
        # ``hafas_provider`` is vestigial (issue #277): MOTIS/Transitous indexes
        # every country the old per-operator backends covered, so the lookup no
        # longer needs — or reads — the operator the user picked. It is still
        # persisted on the segment, so nothing stored breaks.
        if params.get("train_number"):
            try:
                route = get_train_route(
                    train_number=params["train_number"],
                    date=use_date or "",
                    start_lat=seg.start.lat, start_lon=seg.start.lon,
                    end_lat=seg.end.lat,     end_lon=seg.end.lon,
                )
                stops = route.stops
                # The trip carries its own real track, so Overpass is skipped
                # entirely for a matched train — the whole point of the move.
                if len(route.polyline) >= 2:
                    return route.polyline, len(stops), False, "motis_trip"
            except HafasError as exc:
                _log.warning(
                    "seg=%s train lookup failed (train=%r): %s — "
                    "degrading to straight-line geometry",
                    seg.id, params.get("train_number"), exc)
                # fall back to two-point geometry
                seg.route_hafas_failed = True
                seg.route_error = f"Train lookup failed: {exc}"[:200]
        rail = get_rail_geometry(stops)
        return rail.polyline, len(stops), rail.degraded, rail.strategy

    if seg.segment_type == "boat":
        polyline = get_ferry_geometry(
            seg.start.lat, seg.start.lon, seg.end.lat, seg.end.lon)
        return polyline, 2, False, "ferry"

    if seg.segment_type == "bus":
        polyline = get_bus_geometry(
            seg.start.lat, seg.start.lon, seg.end.lat, seg.end.lon)
        return polyline, 2, False, "bus"

    raise ValueError("Route resolution only supported for train, boat, and bus segments")


def _find_segment(project, seg_id: str):
    return next(
        (i.segment for i in project.items
         if i.item_type == "segment" and i.segment and i.segment.id == seg_id),
        None,
    )


def _token_guard(started_at: Optional[str]) -> Dict[str, Any]:
    """Compare-and-set kwargs for a verdict write, when a token is available.

    A caller with no ``route_started_at`` to match on (an older enqueued job, or
    a direct call in a test) writes unconditionally — the same behaviour as
    before the token existed. Passing ``expect_started_at=None`` would instead
    mean "only write if the segment has no timestamp", which silently drops
    every verdict for a segment the trigger just stamped.
    """
    return {"expect_started_at": started_at} if started_at else {}


def _resolve_route_job(
    user_info_id: int, name: str, seg_id: str, params: Dict[str, Any],
    started_at: Optional[str] = None, job_id: Optional[int] = None,
) -> None:
    """Background task: resolve a segment's real-world route geometry.

    Runs the long HAFAS + Overpass lookups off the request path (holding no DB
    session during the slow work), then persists the verdict as a *payload*
    write — one row, no optimistic lock (issue #173). Two segments resolving at
    once therefore do not contend with each other, and neither blocks an
    unrelated edit; the old whole-project save collided on both counts and gave
    up after two attempts, leaving a segment stuck "pending".

    ``started_at`` is the ``route_started_at`` stamped by the trigger. It is
    carried through and compared before writing, so a job superseded by a newer
    trigger cannot overwrite the fresh attempt's result.
    Mirrors the fire-and-forget pattern of :func:`api.project_shared._refresh_share_tiles`.
    """
    _mode_for_type = {"train": "rail", "boat": "ferry", "bus": "bus"}
    mark_running(job_id)
    try:
        # 1. Load the segment (cheap) and compute geometry with no session held.
        with get_session() as sess:
            project = _repo.get_project(sess, user_info_id, name)
            project_id = _repo.project_id_for(sess, user_info_id, name)
        if project is None or project_id is None:
            mark_done(job_id)  # nothing to resolve — not a failure to retry
            return
        seg = _find_segment(project, seg_id)
        if seg is None:
            mark_done(job_id)  # deleted before we got to it
            return
        from src.services.hafas_service import HafasError
        from src.services.overpass_service import OverpassError

        try:
            polyline, _stops, degraded, strategy = _compute_segment_geometry(seg, params)
            fields = {
                "route_status": "resolved",
                "route_polyline": json.dumps(polyline),
                "route_mode": _mode_for_type.get(seg.segment_type, "great_circle"),
                # Normally None. On a HAFAS fallback it carries the provider's
                # own reason so the UI can say *why* the train lookup failed
                # (issue #277) — a resolved segment, but not as asked for.
                "route_error": seg.route_error,
                "route_degraded": degraded,
                # The specific train HAFAS lookup failed and this resolved via the
                # generic two-point OSM fallback instead — distinct from `degraded`,
                # which means OSM itself found no usable track.
                "route_hafas_failed": seg.route_hafas_failed,
                # A fresh auto-resolve is authoritative again — clears the
                # guard a prior manual edit (issue #150) set.
                "route_edited": False,
                # Provenance (issue #364). Stamped on every resolved verdict,
                # degraded ones included: the stamp answers "which generation of
                # the resolver produced this", not "was the answer any good" —
                # route_degraded already says that, and a degraded result really
                # was produced by this generation. Stamping it unconditionally is
                # also what guarantees sweep_stale_resolver_segments terminates:
                # a segment it queues leaves the stale set on the first attempt,
                # whatever comes back.
                "route_resolver_version": RESOLVER_VERSION,
                # Already computed and, until now, only logged. Persisting it is
                # what turns "which trips did the buggy strategy draw" from
                # unanswerable into one query over segment_json.
                "route_strategy": strategy,
            }
            if not degraded and not seg.route_hafas_failed:
                # Only a resolve that produced a *real* route restarts the
                # automatic-retry budget (issue #207).
                #
                # This used to be unconditional, on the reasoning that "any
                # successful resolve" should reset it — but a degraded or
                # HAFAS-fallback result is `route_status="resolved"` too, and it
                # is precisely what sweep_degraded_segments is retrying. So the
                # sweep incremented the counter, the retry it queued wrote it
                # straight back to 0, and MAX_DEGRADE_RETRIES became unreachable:
                # every provisionally-resolved segment was re-resolved every hour
                # forever, each pass flipping a usable approximate route back to
                # a spinner for the 2-5 minutes it took to come back degraded
                # again, and consuming a resolve worker to do it.
                #
                # Omitted rather than carried forward on the degraded path: the
                # payload write only touches the keys it is given, so leaving it
                # out preserves whatever the sweep set.
                fields["route_degrade_retries"] = 0
            if params.get("train_number"):
                fields["train_number"] = params["train_number"]
            if params.get("hafas_provider"):
                fields["hafas_provider"] = params["hafas_provider"]
            _log.info(
                "resolve seg=%s type=%s strategy=%s points=%d degraded=%s "
                "hafas_failed=%s status=resolved",
                seg_id, seg.segment_type, strategy, len(polyline), degraded,
                seg.route_hafas_failed)
        except (HafasError, OverpassError) as exc:
            # "We tried, no route exists" — a clean, expected verdict, not a
            # crash. Leave route_mode/route_polyline so geo still renders the
            # great-circle arc; surface a short error for the UI. Anything
            # else (a DB hiccup, a network timeout unrelated to route lookup)
            # is NOT caught here — it propagates below so RQ's retry/backoff
            # (src/jobs/queue.py) gets a real chance at a transient failure
            # instead of it being written as a permanent "failed" on the spot.
            fields = {
                "route_status": "failed",
                "route_error": str(exc)[:200] or "Route resolution failed",
                "route_degraded": False,
                "route_hafas_failed": False,
            }
            _log.warning("resolve seg=%s type=%s status=failed: %s",
                         seg_id, seg.segment_type, exc)

        # 2. Persist. A single-row update under no lock — nothing to retry, and
        #    nothing another writer can make us lose.
        fields["route_started_at"] = None
        with get_session() as sess:
            written = _repo.update_segment_fields(
                sess, project_id, seg_id, fields, **_token_guard(started_at))
            sess.commit()
        if not written:
            _log.info("resolve seg=%s verdict discarded — segment gone or superseded", seg_id)
        # Terminal either way: the job ran to completion. A discarded verdict
        # means someone else owns the outcome, not that this job must be retried.
        mark_done(job_id)
    except Exception:  # noqa: BLE001
        # Everything reaching here is an unanticipated failure (a load/save
        # error, a bug) — not the expected "no route exists" outcome, which
        # the inner except above already resolved cleanly and returned from.
        # Re-raise rather than writing a "failed" verdict here: this leaves
        # the segment "pending" and the job row "running" so RQ's retry/backoff
        # (src/jobs/queue.py) actually gets to retry a transient failure,
        # instead of it being permanently — and prematurely — marked failed on
        # the first attempt. If every retry is exhausted (or there is no
        # broker), the job stays non-terminal and sweep_orphaned_jobs
        # (src/jobs/route_jobs.py, run at API startup) is the backstop that
        # eventually re-queues or gives up on it — same as any other crash it
        # recovers from.
        _log.exception("resolve seg=%s crashed before persisting a verdict", seg_id)
        raise
    finally:
        bust_geo_cache(user_info_id, name)
        # Warm the cache while still off the request path so returning to the
        # project after a resolve is a fast HIT, not a cold recompute that can
        # time out and leave activities as low-res straight lines. The /meta
        # payload matters just as much here: it is what the client polls while
        # the resolve runs, and what the activity panel blocks on (issue #178).
        warm_geo_cache(user_info_id, name)
        warm_meta_cache(user_info_id, name)


def _mark_segment_failed(
    user_info_id: int, name: str, seg_id: str, err: str,
    started_at: Optional[str] = None,
) -> None:
    """Best-effort: flip a still-``pending`` segment to ``failed`` after a crash.

    Wrapped so it can never raise out of the job's except handler. If the segment
    is gone or superseded by a newer trigger, it's a no-op; if the write itself
    fails (e.g. the very DB error that crashed the job), we log and give up —
    the client's stale-pending recovery remains the last line of defence.

    This used to take the project CAS with no retry, so the crash safety net
    could itself lose a race and leave the segment pending (issue #173). It is
    now the same single-row payload write as the success path.
    """
    try:
        with get_session() as sess:
            project_id = _repo.project_id_for(sess, user_info_id, name)
            if project_id is None:
                return
            _repo.update_segment_fields(
                sess, project_id, seg_id,
                {
                    "route_status": "failed",
                    "route_error": err or "Route resolution failed",
                    "route_started_at": None,
                    "route_degraded": False,
                },
                # Only a still-pending segment may be failed: a late crash after
                # a successful persist must not clobber the resolved result.
                expect_status="pending",
                **_token_guard(started_at),
            )
            sess.commit()
    except Exception:  # noqa: BLE001
        _log.exception("could not mark seg=%s failed after a crashed resolve", seg_id)


# ── Segment CRUD ───────────────────────────────────────────────────────────────

class SegmentBody(BaseModel):
    segment_type: str = "flight"
    label: str = ""
    start_lat: float = 0.0
    start_lon: float = 0.0
    end_lat: float = 0.0
    end_lon: float = 0.0
    insert_after_index: Optional[int] = None  # POST only
    date: Optional[str] = None  # ISO date "YYYY-MM-DD"
    train_number: Optional[str] = None
    hafas_provider: Optional[str] = None
    route_mode: Optional[str] = None  # "great_circle" | "rail"; None = preserve


@router.post("/{name}/segments", status_code=status.HTTP_201_CREATED,
             response_model=SegmentIDOut, summary="Add a transport segment")
def create_segment(
    name: str,
    body: SegmentBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    user_info_id = int(current_user["sub"])
    seg = ConnectingSegment(
        id=str(uuid.uuid4()),
        segment_type=body.segment_type,
        label=body.label,
        date=body.date,
        start=SegmentEndpoint(lat=body.start_lat, lon=body.start_lon),
        end=SegmentEndpoint(lat=body.end_lat, lon=body.end_lon),
        train_number=body.train_number,
        hafas_provider=body.hafas_provider,
    )
    item = ProjectItem(item_type="segment", segment=seg)

    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        project_row_id = row.id

    def _insert(project) -> None:
        # Plan limit on trip length (issue #121) — a segment dated outside the
        # trip's current span stretches it. Re-checked per attempt, in its own
        # read-only session, against current state rather than a stale snapshot.
        with get_session() as qsess:
            ensure_trip_days_quota(qsess, project_row_id, owner_id, body.date)
        # insert_after_index is an index into the caller's *visible* item list
        # (other users' journal items are hidden) — translate it (issue #106).
        # Recomputed per attempt: a retry reloads the item list, and an index
        # translated against the previous attempt's list would land elsewhere.
        visible = journal_visible_positions(project.items, user_info_id, owner_id)
        insert_at = translate_insert_after(visible, body.insert_after_index, len(project.items))
        project.items.insert(insert_at, item)

    # save_project_with_retry, not a bare check_version=True save: this is a
    # load-mutate-save, and a single attempt turns any bump landing inside the
    # request — the user's own resolve job finishing, a companion adding a
    # memory — into a 409 the client shows as "this trip changed elsewhere".
    # repo_retry owns the policy for exactly this (issues #172/#173).
    if _repo.save_project_with_retry(
        owner_id, name, _insert,
    ) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)
    return {"id": seg.id}


@router.put("/{name}/segments/{seg_id}", status_code=status.HTTP_204_NO_CONTENT,
            summary="Update a transport segment")
def update_segment(
    name: str,
    seg_id: str,
    body: SegmentBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        project_row_id = row.id

    def _update(project) -> None:
        for item in project.items:
            if item.item_type == "segment" and item.segment and item.segment.id == seg_id:
                # Re-checked per attempt against current state, in its own
                # read-only session, rather than a stale snapshot.
                with get_session() as qsess:
                    ensure_trip_days_quota(qsess, project_row_id, owner_id, body.date)
                seg = item.segment
                coords_changed = (
                    seg.start.lat != body.start_lat or seg.start.lon != body.start_lon or
                    seg.end.lat != body.end_lat     or seg.end.lon != body.end_lon
                )
                seg.segment_type  = body.segment_type
                seg.label         = body.label
                seg.date          = body.date
                seg.start         = SegmentEndpoint(lat=body.start_lat, lon=body.start_lon)
                seg.end           = SegmentEndpoint(lat=body.end_lat,   lon=body.end_lon)
                seg.train_number  = body.train_number
                seg.hafas_provider = body.hafas_provider
                if coords_changed or body.route_mode == "great_circle":
                    seg.route_mode     = "great_circle"
                    seg.route_polyline = None
                elif body.route_mode == "rail":
                    seg.route_mode = "rail"
                return
        # Raise from inside the callback, not through a flag checked after the
        # wrapper returns. save_project_with_retry only swallows
        # StaleWriteError, so this escapes *before* the save — which matters
        # twice: a PUT for an already-deleted segment no longer performs a full
        # project rewrite (bumping the lock and forcing every concurrent
        # structural writer into a needless retry) before answering 404, and a
        # retry whose reloaded list no longer holds the segment answers 404
        # instead of reporting success for an edit it did not apply. A flag set
        # on the first attempt cannot express that: it survives the retry.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Segment not found")

    # save_project_with_retry rather than a single check_version=True save —
    # see create_segment above. The mutation re-runs against the reloaded item
    # list on each attempt, which is also why the segment is looked up inside
    # the callback rather than once outside it.
    if _repo.save_project_with_retry(
        owner_id, name, _update,
    ) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)
    return


# Ceiling on the point count of a manually edited segment track.
#
# Derived from what the server itself can produce, not picked for roundness:
# a route the app resolved must always survive a round-trip through the
# editor, so the bound sits above the largest geometry our own resolver can
# emit. Overpass rail geometry is stored at full OSM-node resolution, which
# measures ~14 points/km (9,700 points for the ~700 km Hamburg-Offenburg
# route). The longest path the rail resolver will accept is pinned by two
# guards in src/services/overpass_service.py: _RAIL_BBOX_MAX_SPAN = 12
# degrees bounds the query box at roughly 1,600 km corner-to-corner at
# European latitudes, and _MAX_RAIL_DETOUR_RATIO = 4.0 rejects any polyline
# longer than 4x the crow-flight baseline through its stops -- so ~6,400 km,
# i.e. ~90,000 points at that density. 100,000 clears that worst case with
# margin while capping the persisted JSON at roughly 3 MB.
#
# Above the limit we reject rather than truncate: silently dropping points
# would destroy a user's manual edit without telling them.
MAX_TRACK_POINTS = 100_000


class SegmentTrackPointIn(BaseModel):
    lat: float = Field(description="Latitude, decimal degrees")
    lng: float = Field(description="Longitude, decimal degrees")


class SegmentTrackEditRequest(BaseModel):
    points: List[SegmentTrackPointIn] = Field(
        description="Full edited route as an ordered list of {lat, lng} points")


class SegmentTrackOut(BaseModel):
    route_polyline: str = Field(description="JSON-encoded [[lon,lat],…] coordinates")
    route_mode: str = Field(
        description="The segment's resolved mode after the edit — always "
                     "'rail', 'ferry', or 'bus' (matching the segment's type)")
    route_status: str = Field(description="Always 'resolved' after a successful manual edit")
    route_edited: bool = Field(
        description="Always true — a manual edit always sets this flag, guarding "
                     "a later auto-resolve (see ResolveRouteRequest.force)")


@router.put("/{name}/segments/{seg_id}/track", response_model=SegmentTrackOut,
            summary="Replace a segment's route with a manually edited track")
def edit_segment_track(
    name: str,
    seg_id: str,
    body: SegmentTrackEditRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    """Overwrite a segment's route geometry with a hand-edited point list (issue #150).

    Sometimes the auto-resolved OSM route is missing or malformed; this lets the
    user fix it directly rather than delete and recreate the segment. Only the
    route geometry changes here — start/end anchors, label, and date are
    untouched (those stay under ``update_segment``/the segment form).

    Marks the segment ``route_edited=True`` so a later auto-resolve
    (:func:`resolve_segment_route`) refuses to silently discard the edit
    unless the caller passes ``force``.

    Uses the same single-row payload write as the resolve job
    (``ItemOrderingMixin.update_segment_fields``, issue #173) rather than a
    whole-project CAS — editing a track never contends with an unrelated
    concurrent edit.
    """
    user_info_id = int(current_user["sub"])
    if len(body.points) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A route needs at least 2 points",
        )
    if len(body.points) > MAX_TRACK_POINTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"A route may not exceed {MAX_TRACK_POINTS} points "
                f"(got {len(body.points)}). Simplify the track before saving."
            ),
        )
    mode_for_type = {"train": "rail", "boat": "ferry", "bus": "bus"}
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        # include_heavy=False: this load is only used to find the segment and
        # check its type below — see edit_activity_track for why.
        project = _repo.get_project(
            sess, owner_id, name,
            include_heavy=False,
        )
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        seg = _find_segment(project, seg_id)
        if seg is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
        route_mode = mode_for_type.get(seg.segment_type)
        if route_mode is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Track editing only supported for train, boat, and bus segments",
            )
        polyline = json.dumps([[p.lng, p.lat] for p in body.points])
        fields: Dict[str, Any] = {
            "route_polyline": polyline,
            "route_mode": route_mode,
            "route_status": "resolved",
            "route_error": None,
            "route_degraded": False,
            "route_hafas_failed": False,
            "route_edited": True,
            "route_started_at": None,
            # Provenance (issue #364): no resolver produced this geometry, a
            # person did. Leaving the previous strategy in place would have the
            # "which segments did strategy X draw" query count a hand-drawn
            # track as the resolver's work. route_resolver_version is
            # deliberately *not* touched — route_edited is the guard that keeps
            # the stale-stamp sweep away from this segment, and inventing a
            # version for geometry no resolver produced would only hide that.
            "route_strategy": "manual",
        }
        if not _repo.update_segment_fields(sess, row.id, seg_id, fields):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
        sess.commit()

    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)
    return {
        "route_polyline": polyline,
        "route_mode": route_mode,
        "route_status": "resolved",
        "route_edited": True,
    }


@router.delete("/{name}/segments/{seg_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a transport segment")
def delete_segment(
    name: str,
    seg_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    """Delete one transport segment.

    A single-row delete, not a whole-project save (issue #173): deleting several
    segments at once — which the client does routinely, one request per segment
    as each undo window expires — used to have them all take the same
    project-wide optimistic lock, so every request but one 409'd. The client had
    already dropped those segments locally and never surfaced the error, so they
    silently reappeared at the next full reload.

    ``delete_segment_row`` returning False means no such segment row — the 404
    the length comparison used to detect.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        if not _repo.delete_segment_row(sess, row.id, seg_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
        sess.commit()
    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)


class ResolveRouteRequest(BaseModel):
    # Kept for backwards compatibility and still persisted on the segment, but
    # no longer read when resolving (issue #277) — MOTIS covers every operator
    # in one index. ``train_number`` alone is what enables the train lookup.
    hafas_provider: Optional[str] = None
    train_number: Optional[str] = None
    date: Optional[str] = None             # ISO "YYYY-MM-DD"; defaults to segment.date
    force: bool = Field(
        default=False,
        description="Must be true when the segment's route_edited flag is set "
                     "(issue #150) — otherwise the request 409s rather than "
                     "silently discarding a manually edited route. The client "
                     "should confirm with the user before setting this.",
    )


@router.post("/{name}/segments/{seg_id}/resolve-route", response_model=RouteResolveTriggered,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Trigger async route resolution for a train, ferry, or bus segment")
def resolve_segment_route(
    name: str,
    seg_id: str,
    body: ResolveRouteRequest,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """
    Schedule OSM-based route resolution for a train, boat, or bus segment.

    Resolution (HAFAS stop sequence + Overpass track geometry) can take tens of
    seconds, so it runs as a background task rather than blocking the request —
    this is what previously caused proxy 504s on long routes.

    The segment is marked ``route_status="pending"`` synchronously and a 202 is
    returned immediately.  The client polls ``/meta`` until the segment flips to
    ``resolved`` or ``failed``.  See :func:`_resolve_route_job`.

    Marking pending is a payload write on one row, not a whole-project save
    (issue #173): triggering two resolves at once — or triggering one while
    another finishes — used to lose the optimistic lock and return a 409 the
    client never retried, so the second segment simply never resolved.

    Returns **409** instead if the segment has a manually edited route
    (``route_edited=True``, issue #150) and ``force`` is not set — an
    auto-resolve would otherwise silently overwrite the user's hand-drawn
    track. Pass ``force=True`` to proceed anyway; the client should confirm
    with the user first. A successful resolve clears ``route_edited``.
    """
    user_info_id = int(current_user["sub"])
    started_at = datetime.now(timezone.utc).isoformat()
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        project = _repo.get_project(
            sess, owner_id, name,
        )
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

        seg = next(
            (i.segment for i in project.items
             if i.item_type == "segment" and i.segment and i.segment.id == seg_id),
            None,
        )
        if seg is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
        if seg.segment_type not in ("train", "boat", "bus"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Route resolution only supported for train, boat, and bus segments",
            )
        # A manually edited route (issue #150) is not silently overwritten by
        # an auto-resolve — the caller must confirm and pass force=True.
        if seg.route_edited and not body.force:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This route has a manually edited track. "
                       "Resolving again will discard it.",
            )

        fields: Dict[str, Any] = {
            "route_status": "pending",
            "route_error": None,
            "route_degraded": False,
            "route_hafas_failed": False,
            "route_started_at": started_at,
            # A person asking for this again re-arms the automatic retry budget
            # (issue #207). The *job* only resets it on a genuinely good result,
            # because resetting it on a degraded one is what stopped
            # sweep_degraded_segments ever terminating — but that reasoning does
            # not apply here: a deliberate tap is not a loop, and without this a
            # segment whose budget the sweep had spent would never be picked up
            # automatically again no matter how often the user retried it.
            "route_degrade_retries": 0,
        }
        if body.train_number:
            fields["train_number"] = body.train_number
        if body.hafas_provider:
            fields["hafas_provider"] = body.hafas_provider
        _repo.update_segment_fields(sess, row.id, seg_id, fields)
        sess.commit()
        project_id = row.id
    bust_geo_cache(owner_id, name)

    # Record the job before queueing it: a row with no queue entry is recoverable
    # (the startup sweep re-queues it); a queue entry with no row is not.
    job_id = create_job(owner_id, project_id, name, seg_id, started_at, body.model_dump())
    enqueue(
        QUEUE_RESOLVE, _resolve_route_job,
        owner_id, name, seg_id, body.model_dump(), started_at, job_id,
        background_tasks=background_tasks,
    )
    return {"status": "pending", "route_status": "pending"}

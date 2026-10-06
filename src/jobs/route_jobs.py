"""Durable lifecycle for route-resolution jobs (issue #173, phase D).

A ``DBRouteJob`` row is created when a resolve is requested and advanced to a
terminal status when it finishes. The row — not the queue entry, and not the
segment's ``route_status`` — is the record that work is owed.

That matters because every layer above it can lose the job: RQ without
``appendonly`` loses the queue on a broker restart, a worker killed mid-run
loses whatever it held, and an enqueue that fell back to ``BackgroundTasks``
dies with the API process. Before this, the only component that ever noticed was
the Flutter client's stale-pending recovery — five minutes late, and only if
someone reopened the project.

:func:`sweep_orphaned_jobs` runs at API startup and re-queues anything left
non-terminal. :func:`warn_stuck_route_jobs` runs hourly and logs any job left
unfinished for too long while the API stays up.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlmodel import select

from models.db import get_session
from models.project_db import DBProject, DBProjectItem, DBRouteJob
from src.models.project import ConnectingSegment
from src.project.project_repo import ProjectRepo
from src.utils.logging import get_logger

_log = get_logger(__name__)
_repo = ProjectRepo()

# A job that dies mid-run is re-queued by the next sweep. One that dies *because
# of its own inputs* would be re-queued forever, taking a worker with it every
# boot — so the sweep gives up and fails it loudly instead.
MAX_ATTEMPTS = 3

# A degraded resolve (route_degraded=True) is a straight endpoint chord used
# because Overpass failed every mirror/strategy at request time — not a
# permanent verdict; the same mirror flakiness is often transient (issue
# #207). A HAFAS-fallback resolve (route_hafas_failed=True) is the same kind
# of provisional verdict: the train's own schedule lookup failed and the
# geometry shown is a generic two-point route, not the train the user picked
# (issue #277). sweep_degraded_segments retries either up to this many times
# before leaving it alone — still manually retryable from the tile, same as
# any other resolve.
MAX_DEGRADE_RETRIES = 5

# The generation of the rail resolver that produced a segment's geometry
# (issue #364), stamped on every resolve as ``route_resolver_version``.
# :func:`sweep_stale_resolver_segments` re-resolves anything below it, which is
# what lets a resolver fix reach the geometry it already got wrong — nothing
# else does, because nothing re-resolves a segment that did not fail.
#
# ── BUMP THIS ONLY WHEN A CHANGE ALTERS THE GEOMETRY A RESOLVE PRODUCES ──
# The test is one question: run the resolver again on an unchanged segment —
# would it now return a *different* polyline? A new or reordered strategy, a
# change to how the routing graph is built, a fixed member filter (#359/#361):
# yes, bump. Logging, a refactor, a new mirror in the endpoint list, a timeout
# tweak, a release that happens to ship on the same day: no, leave it.
#
# Getting that wrong is not free. A bump makes every rail segment in the
# deployment a candidate, and each one costs a full re-resolve to arrive at a
# byte-identical answer. A stamp that moves every release is a permanent
# re-resolve treadmill against a rate-limited public API.
#
# 1 — first stamped generation. Includes #361 (station platforms kept out of
#     the routing graph); a segment stamped 0 predates the stamp entirely.
RESOLVER_VERSION = 1

# How many stale-stamp segments one sweep may queue.
#
# Deliberately not MAX_DEGRADE_RETRIES, which bounds a *per-segment* budget: a
# version bump makes every rail segment stale simultaneously, so what needs
# bounding here is the per-run count.
#
# The scarce resource is Overpass slot-seconds, not our CPU or wall clock:
# ``_OVERPASS_CONCURRENCY`` is 1 (src/services/overpass_service.py), one rail
# resolve issues four or five queries, and the endpoint measurements there put
# a healthy query at 8.4s and an unreachable host at the full ``_TIMEOUT_HTTP``
# of 60s. One re-resolve is therefore ~40s of the hour at best, ~5 minutes at
# worst.
#
# 3 per hourly run is ~2 minutes of a good hour (3%) and ~15 minutes of a bad
# one (25%), leaving the rest of the single slot to user-triggered resolves and
# to sweep_degraded_segments, which is uncapped per run. It also drains a
# few-hundred-segment deployment over a couple of days rather than in one pass
# — slow is the point, not a compromise: a version bumped by mistake is noticed
# and reverted long before it has re-resolved much, and this deployment's IPv4
# was hard-banned by overpass-api.de in September 2026 for precisely this shape
# of traffic. Under ``RAIL_SOURCE=local`` the cost is disk instead of quota and
# the cap is merely conservative, which is the right way round.
MAX_STALE_RESOLVES_PER_SWEEP = 3

TERMINAL = ("done", "failed")

# How long a route job may stay pending/running before warn_stuck_route_jobs
# reports it. A resolve normally finishes in well under a minute and its worst
# case — every Overpass query timing out — is a few minutes, so half an hour
# means the job is lost, not slow.
STUCK_JOB_AFTER_S = 30 * 60


def create_job(
    user_info_id: int,
    project_id: int,
    project_name: str,
    segment_id: str,
    started_at: str,
    params: Dict[str, Any],
) -> int:
    """Record that a resolve is owed. Returns the job id."""
    with get_session() as sess:
        # Supersede any earlier attempt for this segment: the newer trigger owns
        # the outcome, and leaving the old row pending would have the sweep
        # re-queue a job whose verdict the token guard now discards anyway.
        for stale in sess.exec(
            select(DBRouteJob).where(
                DBRouteJob.project_id == project_id,
                DBRouteJob.segment_id == segment_id,
                DBRouteJob.status.notin_(TERMINAL),
            )
        ).all():
            stale.status = "failed"
            stale.error_message = "superseded by a newer resolve request"
            stale.completed_at = time.time()
            sess.add(stale)

        job = DBRouteJob(
            project_id=project_id,
            user_info_id=user_info_id,
            project_name=project_name,
            segment_id=segment_id,
            started_at=started_at,
            params_json=json.dumps(params or {}),
        )
        sess.add(job)
        sess.commit()
        sess.refresh(job)
        return job.id


def mark_running(job_id: Optional[int]) -> None:
    _set_status(job_id, "running")


def mark_done(job_id: Optional[int]) -> None:
    _set_status(job_id, "done", completed=True)


def mark_failed(job_id: Optional[int], error: str) -> None:
    _set_status(job_id, "failed", completed=True, error=error)


def _set_status(
    job_id: Optional[int], status: str, *, completed: bool = False,
    error: Optional[str] = None,
) -> None:
    """Advance a job row. Never raises — bookkeeping must not sink the job."""
    if job_id is None:
        return
    try:
        with get_session() as sess:
            job = sess.get(DBRouteJob, job_id)
            if job is None:
                return
            job.status = status
            if error is not None:
                job.error_message = error[:500]
            if completed:
                job.completed_at = time.time()
            sess.add(job)
            sess.commit()
    except Exception:  # noqa: BLE001
        _log.exception("could not set route job %s to %s", job_id, status)


def sweep_orphaned_jobs() -> int:
    """Re-queue every non-terminal route job. Returns how many were re-queued.

    Called once at API startup. Anything still ``pending``/``running`` when the
    process starts is by definition orphaned — nothing was executing it a moment
    ago, because nothing was running at all.

    Deliberately not scoped by age: at startup there is no in-flight work to
    accidentally duplicate, which is exactly what makes startup the right moment
    to do this. (The client's five-minute staleness window existed only because
    it *could not* know whether a job was still running.)
    """
    from api.segments import _resolve_route_job
    from src.jobs.queue import QUEUE_RESOLVE, enqueue

    requeued = 0
    try:
        with get_session() as sess:
            orphans = sess.exec(
                select(DBRouteJob).where(DBRouteJob.status.notin_(TERMINAL))
            ).all()
            pending = [
                (j.id, j.user_info_id, j.project_name, j.segment_id,
                 json.loads(j.params_json or "{}"), j.started_at, j.attempts)
                for j in orphans
            ]
            for job in orphans:
                if job.attempts + 1 >= MAX_ATTEMPTS:
                    job.status = "failed"
                    job.error_message = (
                        f"abandoned after {MAX_ATTEMPTS} attempts — the job did not "
                        "survive repeated restarts")
                    job.completed_at = time.time()
                else:
                    job.attempts += 1
                sess.add(job)
            sess.commit()
    except Exception:  # noqa: BLE001 — a broken sweep must not stop the app booting
        _log.exception("route job sweep failed to read orphans")
        return 0

    for job_id, user_id, name, seg_id, params, started_at, attempts in pending:
        if attempts + 1 >= MAX_ATTEMPTS:
            _log.warning("route job %s abandoned after %d attempts", job_id, attempts)
            _fail_segment_for(user_id, name, seg_id, started_at)
            continue
        try:
            enqueue(QUEUE_RESOLVE, _resolve_route_job,
                    user_id, name, seg_id, params, started_at, job_id)
            requeued += 1
        except Exception:  # noqa: BLE001
            _log.exception("could not re-queue route job %s", job_id)

    if requeued:
        _log.info("re-queued %d orphaned route job(s) at startup", requeued)
    return requeued


def warn_stuck_route_jobs() -> int:
    """Log one WARNING per route job still unfinished after
    :data:`STUCK_JOB_AFTER_S`. Returns how many. Changes nothing.

    :func:`sweep_orphaned_jobs` only runs at startup, so a worker killed
    mid-resolve while the API stays up leaves its job — and the segment's
    "pending" — in place until the next restart, with every open session
    polling ``/meta`` for it meanwhile. Nothing re-queues it here (re-queueing a
    job that may still be running would duplicate it); this only makes the stall
    visible in the logs.

    Age is counted from ``started_at`` (the attempt's token, stamped when the
    resolve was requested), or from ``created_at`` when that is missing or
    unparsable.
    """
    now = time.time()
    stuck: list = []
    try:
        with get_session() as sess:
            rows = sess.exec(
                select(DBRouteJob).where(DBRouteJob.status.notin_(TERMINAL))
            ).all()
            for job in rows:
                since = _started_epoch(job.started_at)
                if since is None:
                    since = job.created_at
                age = now - since
                if age > STUCK_JOB_AFTER_S:
                    stuck.append((job.id, job.status, job.project_id,
                                  job.segment_id, age))
    except Exception:  # noqa: BLE001 — a broken check must not take the scheduler down
        _log.exception("stuck route job check failed to read jobs")
        return 0

    for job_id, status, project_id, seg_id, age in stuck:
        _log.warning("route job %s still %s after %d min (project=%s seg=%s)",
                     job_id, status, age // 60, project_id, seg_id)
    return len(stuck)


def _started_epoch(started_at: Optional[str]) -> Optional[float]:
    """A job's ISO-8601 ``started_at`` as epoch seconds; naive means UTC."""
    if not started_at:
        return None
    try:
        dt = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fail_segment_for(
    user_info_id: int, name: str, seg_id: str, started_at: Optional[str]
) -> None:
    """Flip an abandoned job's segment out of "pending" so the UI stops spinning."""
    from api.segments import _mark_segment_failed

    _mark_segment_failed(
        user_info_id, name, seg_id,
        "Route resolution did not survive a server restart — please try again.",
        started_at,
    )


def sweep_degraded_segments() -> int:
    """Re-attempt every provisionally-resolved segment. Returns how many.

    "Provisional" means either ``route_degraded`` (Overpass found no usable
    track, so the line is a straight endpoint chord — issue #207) or
    ``route_hafas_failed`` (the selected train's schedule lookup failed and the
    geometry is a generic two-point route — issue #277). Both are resolved
    *around* a failure rather than because of a real answer, and both are
    routinely transient, so neither should be frozen in for good.

    Called on a schedule (hourly, api/router.py) — unlike :func:`sweep_orphaned_jobs`
    there is no startup/crash urgency here, just giving a flaky Overpass mirror
    or train-schedule provider room to recover between attempts.

    Reads candidates directly off ``DBProjectItem`` rows rather than loading
    whole projects via :class:`ProjectRepo` — the same reason
    :func:`sweep_orphaned_jobs` reads ``DBRouteJob`` rows directly instead of
    going through a heavier path.
    """
    candidates: list = []
    try:
        with get_session() as sess:
            rows = sess.exec(
                select(DBProjectItem, DBProject.user_info_id, DBProject.name)
                .join(DBProject, DBProject.id == DBProjectItem.project_id)
                .where(DBProjectItem.item_type == "segment")
            ).all()
            for row, user_info_id, name in rows:
                seg = ConnectingSegment.from_dict(json.loads(row.segment_json or "{}"))
                if seg.route_status != "resolved":
                    continue
                if not (seg.route_degraded or seg.route_hafas_failed):
                    continue
                if seg.route_degrade_retries >= MAX_DEGRADE_RETRIES:
                    continue
                candidates.append((
                    row.project_id, user_info_id, name, seg.id,
                    seg.route_degrade_retries,
                    {
                        "hafas_provider": seg.hafas_provider,
                        "train_number": seg.train_number,
                        "date": seg.date,
                    },
                ))
    except Exception:  # noqa: BLE001 — a broken sweep must not take the scheduler down
        _log.exception("degraded-segment sweep failed to read candidates")
        return 0

    retried = 0
    for project_id, user_info_id, name, seg_id, retries, params in candidates:
        if _requeue_resolve(
            project_id, user_info_id, name, seg_id, params,
            extra_fields={"route_degrade_retries": retries + 1},
            reason="degraded retry",
        ):
            retried += 1

    if retried:
        _log.info("retried %d provisionally-resolved segment(s)", retried)
    return retried


def _requeue_resolve(
    project_id: int, user_info_id: int, name: str, seg_id: str,
    params: Dict[str, Any], *, extra_fields: Dict[str, Any], reason: str,
) -> bool:
    """Flip one resolved segment back to pending and queue a fresh resolve.

    Shared by both sweeps: the compare-and-set, the cache bust and the
    job-row-before-queue ordering are the parts that are easy to get subtly
    wrong, and two sweeps disagreeing about any of them would be worse than the
    indirection. The only thing they differ on is *extra_fields* — the degraded
    sweep spends a retry from the per-segment budget, the stale-stamp sweep has
    no counter to spend (see :func:`sweep_stale_resolver_segments`).

    Returns True when a resolve was actually queued. Never raises: a sweep runs
    on the scheduler, and one bad segment must not cost the rest of the batch.
    """
    from api.geo import bust_geo_cache
    from api.segments import _resolve_route_job
    from src.jobs.queue import QUEUE_RESOLVE, enqueue

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_session() as sess:
            # Only if still resolved: a manual trigger or edit racing this
            # sweep owns the outcome instead. This is also what stops the two
            # sweeps double-queueing a segment that is both degraded and stale
            # — whichever ran first has already taken it out of "resolved".
            written = _repo.update_segment_fields(
                sess, project_id, seg_id,
                {
                    "route_status": "pending",
                    "route_started_at": started_at,
                    **extra_fields,
                },
                expect_status="resolved",
            )
            sess.commit()
    except Exception:  # noqa: BLE001
        _log.exception("could not mark seg=%s pending for a %s", seg_id, reason)
        return False
    if not written:
        return False
    # Mirrors resolve_segment_route: a cached /meta must not keep serving
    # the pre-retry "resolved+degraded" state, including to a client's own
    # periodic degraded-route-upgrade check (project_notifier.dart).
    bust_geo_cache(user_info_id, name)

    job_id = create_job(user_info_id, project_id, name, seg_id, started_at, params)
    try:
        enqueue(QUEUE_RESOLVE, _resolve_route_job,
                user_info_id, name, seg_id, params, started_at, job_id)
        return True
    except Exception:  # noqa: BLE001
        _log.exception("could not enqueue %s for seg=%s", reason, seg_id)
        return False


def sweep_stale_resolver_segments() -> int:
    """Re-resolve rail segments produced by an older resolver. Returns how many.

    The gap this closes (issue #364): every flag a segment carries records
    whether a resolve *failed*, so a resolve that succeeded and was wrong is
    invisible to every mechanism we have. #359 came back
    ``strategy=relation_endpoints``, ``degraded=False``, 6567 points, a
    plausible 492 km — and started 13.9 km from the station it claimed to leave.
    :func:`sweep_degraded_segments` will never look at that, so #361 fixed the
    resolver and changed nothing for the trips already drawn. Comparing the
    segment's stamp against :data:`RESOLVER_VERSION` is what makes a resolver
    fix reach stored geometry at all.

    Scope and guards, each load bearing:

    * **Rail only.** The stamp tracks the rail resolver's generation, and
      ``api.segments._compute_segment_geometry`` raises ``ValueError`` for a
      flight segment — queueing one would kill a worker, not resolve anything.
    * **No stored geometry, nothing to redo.** ``route_status`` is not enough on
      its own: moving a segment's endpoints (``update_segment``) drops the
      polyline and puts the route back to ``great_circle`` while leaving the
      status at "resolved", so without the ``route_polyline`` check this sweep
      would draw rail track on a segment the user had just reset to a plain arc.
    * **``route_edited`` is skipped, explicitly.** That guard is *not* inherited
      from :func:`sweep_degraded_segments`: the track-edit endpoint clears
      ``route_degraded`` and ``route_hafas_failed``, so a hand-drawn track is
      invisible to that sweep by accident of the flags rather than by a check.
      A stale stamp has no such accident — an edited segment keeps whatever
      version it was last resolved at, normally 0 — so without this line the
      first version bump would silently discard every hand-drawn track in the
      deployment (issue #150).
    * **Already-provisional segments are skipped.** A degraded or HAFAS-fallback
      segment belongs to the other sweep, which will re-resolve it with the new
      resolver and stamp the version as a side effect. Disjoint candidate sets
      mean one segment cannot consume both budgets.
    * **At most :data:`MAX_STALE_RESOLVES_PER_SWEEP` per run** — the arithmetic
      behind the number is with the constant.

    One attempt per segment per version bump, deliberately: the resolve stamps
    the current version on *any* resolved verdict, degraded included, so a
    segment cannot come back for a second try. That choice has a real cost worth
    naming — a re-resolve attempted while every Overpass mirror is unreachable
    replaces good track with a straight chord. It is bounded rather than avoided:
    the result is flagged degraded, shown as such in the UI, and
    :func:`sweep_degraded_segments` then owns it with a fresh
    ``MAX_DEGRADE_RETRIES`` attempts to get real track back. Refusing to write a
    degraded verdict here instead would leave the segment stale forever, re-read
    every hour, and starve the cap with segments that cannot succeed.
    """
    candidates: list = []
    try:
        with get_session() as sess:
            rows = sess.exec(
                select(DBProjectItem, DBProject.user_info_id, DBProject.name)
                .join(DBProject, DBProject.id == DBProjectItem.project_id)
                .where(DBProjectItem.item_type == "segment")
            ).all()
            for row, user_info_id, name in rows:
                seg = ConnectingSegment.from_dict(json.loads(row.segment_json or "{}"))
                if seg.segment_type != "train":
                    continue
                if seg.route_status != "resolved":
                    continue
                if not seg.route_polyline:
                    continue
                if seg.route_edited:
                    continue
                if seg.route_degraded or seg.route_hafas_failed:
                    continue
                if seg.route_resolver_version >= RESOLVER_VERSION:
                    continue
                candidates.append((
                    row.project_id, user_info_id, name, seg.id,
                    {
                        "hafas_provider": seg.hafas_provider,
                        "train_number": seg.train_number,
                        "date": seg.date,
                    },
                ))
                if len(candidates) >= MAX_STALE_RESOLVES_PER_SWEEP:
                    # Stop reading, not merely stop enqueueing: the rows past the
                    # cap cost a JSON parse each and will still be here next hour.
                    # Taking them in row order drains the backlog deterministically
                    # and cannot starve on a bad segment, because a swept segment
                    # leaves the candidate set whatever happens — a resolved
                    # verdict stamps the current version, and every other outcome
                    # leaves route_status something other than "resolved".
                    break
    except Exception:  # noqa: BLE001 — a broken sweep must not take the scheduler down
        _log.exception("stale-resolver sweep failed to read candidates")
        return 0

    retried = 0
    for project_id, user_info_id, name, seg_id, params in candidates:
        if _requeue_resolve(
            project_id, user_info_id, name, seg_id, params,
            # No counter to bump: the resolve stamps RESOLVER_VERSION on its
            # verdict, and that is what takes the segment out of this candidate
            # set. route_degrade_retries belongs to the other sweep and is left
            # exactly as it was.
            extra_fields={},
            reason="stale-resolver re-resolve",
        ):
            retried += 1

    if retried:
        _log.info("re-resolving %d segment(s) stamped below resolver v%d",
                  retried, RESOLVER_VERSION)
    return retried

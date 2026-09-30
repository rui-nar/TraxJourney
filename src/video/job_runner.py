"""Lifecycle of a trip-video render job (docs/TRIP_VIDEO_PLAN.md, U6).

``run_video_job`` runs on the ``video`` RQ worker, never in the API process
(D7). It owns the job row's status, calls the renderer (Convention 5), and
sends the ready/failed email. The poster runner is the model for emails and
files, but not for status changes: every transition here is a
compare-and-set (Convention 6), because a video job can be failed by someone
else while it waits or renders —

* the killed-work-horse handler (``mark_video_job_interrupted``, wired in
  ``src.jobs.worker``),
* the ``video`` worker's own startup sweep (``sweep_stale_running_video_jobs``),
* the API's hourly sweep (``sweep_video_jobs``).

A runner that finds its row no longer where it left it stops, deletes what it
wrote and sends nothing, so a job failed by a sweep never later reports done.

A preview (``kind='preview'``, docs/VIDEO_PREVIEW_PLAN.md D2) goes through the
same lifecycle, started by ``run_video_preview_job`` on the ``default`` queue
(D5). It is disposable (D7): no email on any path, a WebP kept for an hour,
and it is failed only by age in the hourly sweep — never by the ``video``
worker's startup sweep, since ``default`` has more than one consumer.

Consent geometry (D2) is plaintext for an encrypted trip. It exists only as
the job's ``geometry.json`` and is deleted on every terminal path. It never
goes into the job row, a log line, an exception message or an email — which
is why ``error_message`` only ever holds one of the fixed reasons below and
failures are logged by exception type and traceback, never by message.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from typing import Iterable, Optional

from sqlalchemy import and_, or_, update
from sqlmodel import select

from models.db import get_session
from models.project_db import DBVideoJob
from models.user import UserInfo
from src.email.service import EmailMessage, get_email_service
from src.email.templates import render_video_failed_email, render_video_ready_email
from src.project.project_repo import ProjectRepo
from src.utils.logging import get_logger
from src.video import paths
from src.video.timeline import NothingToAnimate

_log = get_logger(__name__)

_repo = ProjectRepo()

#: RQ's limit for one render (POST /video enqueues with it).
JOB_TIMEOUT_S = 1800
#: A running row older than this was killed by RQ's timeout (plus slack for
#: the killed-horse handler that should have failed it already).
STALE_RUNNING_S = JOB_TIMEOUT_S + 5 * 60
#: A pending row older than this lost its queue entry.
STALE_PENDING_S = 24 * 3600
#: How long a finished video is kept (D8).
RETENTION_DAYS = 30
RETENTION_S = RETENTION_DAYS * 24 * 3600

NON_TERMINAL = ("pending", "running")

#: RQ's limit for one preview render (POST /video/preview enqueues with it; D5).
PREVIEW_JOB_TIMEOUT_S = 300
#: A running preview older than this was killed by RQ's timeout.
PREVIEW_STALE_RUNNING_S = PREVIEW_JOB_TIMEOUT_S + 5 * 60
#: A pending preview older than this lost its queue entry.
PREVIEW_STALE_PENDING_S = 3600
#: How long a finished preview is kept (D7).
PREVIEW_RETENTION_S = 3600
#: The preview's file, in the job's directory beside where a video's MP4 goes.
PREVIEW_FILE = "preview.webp"

# The only strings ever stored in ``error_message``: user-facing, and free of
# anything a render or a request produced (Convention 2).
REASON_OUT_OF_MEMORY = ("The render ran out of memory — try a shorter video "
                        "or a lower resolution.")
REASON_NOTHING_TO_ANIMATE = "Nothing in this trip could be drawn."
REASON_RENDER_FAILED = "The video could not be rendered. Please try again."
REASON_WORKER_RESTARTED = ("The video service restarted during the render. "
                           "Please try again.")
REASON_TIMED_OUT = "The render took too long and was stopped. Please try again."
REASON_NEVER_STARTED = ("The render never started — the video service may be "
                        "busy or down. Please try again.")
REASON_UNAVAILABLE = "The video service is unavailable. Please try again later."


def _frontend_origin() -> str:
    """Base URL of the email's download link, read at call time (see the
    poster runner)."""
    return os.environ.get("FRONTEND_ORIGIN", "http://localhost:5500")


def _describe(exc: BaseException) -> str:
    """*exc*'s type and traceback, without its message: a message can quote
    the data being processed, which here may be consent geometry."""
    return type(exc).__qualname__ + "\n" + "".join(traceback.format_tb(exc.__traceback__))


def _reason_for(exc: BaseException) -> str:
    if isinstance(exc, MemoryError):
        return REASON_OUT_OF_MEMORY
    if isinstance(exc, NothingToAnimate):
        return REASON_NOTHING_TO_ANIMATE
    if type(exc).__name__ == "JobTimeoutException":  # RQ's job_timeout
        return REASON_TIMED_OUT
    return REASON_RENDER_FAILED


def _cas(job_id: int, from_states: Iterable[str], **values) -> bool:
    """Move the row to *values* only if its status is one of *from_states*.
    True when this call made the change."""
    with get_session() as sess:
        result = sess.execute(
            update(DBVideoJob)
            .where(DBVideoJob.id == job_id, DBVideoJob.status.in_(tuple(from_states)))
            .values(**values)
        )
        sess.commit()
        return result.rowcount == 1


def _renderer():
    """``render_video`` (Convention 5), imported only when a job runs: it pulls
    in Pillow and ffmpeg plumbing the API process never needs. Tests replace
    this function."""
    from src.video.renderer import render_video

    return render_video


def _preview_renderer():
    """``render_preview``, imported only when a preview runs, like
    :func:`_renderer`. Tests replace this function."""
    from src.video.renderer import render_preview

    return render_preview


def preview_result_path(user_info_id: int, job_id: int):
    """Where a preview's WebP is written: in the job's own directory, so
    every path that deletes the job's files deletes it too."""
    return paths.video_dir(user_info_id, job_id) / PREVIEW_FILE


# ── Emails ────────────────────────────────────────────────────────────────────

def _project_name(project_id: int) -> str:
    with get_session() as sess:
        project = _repo.get_project_by_id_meta(sess, project_id)
    return project.name if project is not None else "your trip"


def _notify_ready(job_id: int, user_info_id: int, project_id: int,
                  download_token: Optional[str]) -> None:
    """Best-effort: the video is ready. Never raises — a mail failure must not
    undo a finished render."""
    if not download_token:
        _log.warning("Video job %s done with no download_token; no ready email", job_id)
        return
    try:
        with get_session() as sess:
            user = sess.get(UserInfo, user_info_id)
        if user is None or not user.email:
            return
        name = _project_name(project_id)
        text_body, html_body = render_video_ready_email(
            project_name=name,
            download_url=f"{_frontend_origin()}/video/{download_token}",
            retention_days=RETENTION_DAYS,
        )
        asyncio.run(get_email_service().send(EmailMessage(
            to=user.email, subject=f"Your video of {name} is ready",
            text_body=text_body, html_body=html_body)))
    except Exception:
        _log.exception("Failed to send video-ready email for job %s", job_id)


def _notify_failed(job_id: int, user_info_id: int, project_id: int) -> None:
    """Best-effort: the video failed. Never raises, and carries no reason."""
    try:
        with get_session() as sess:
            user = sess.get(UserInfo, user_info_id)
        if user is None or not user.email:
            return
        name = _project_name(project_id)
        text_body, html_body = render_video_failed_email(project_name=name)
        asyncio.run(get_email_service().send(EmailMessage(
            to=user.email, subject=f"Your video of {name} could not be made",
            text_body=text_body, html_body=html_body)))
    except Exception:
        _log.exception("Failed to send video-failed email for job %s", job_id)


# ── The job ───────────────────────────────────────────────────────────────────

def run_video_job(job_id: int) -> None:
    """Render *job_id*: ``pending → running → done | failed``, each step a
    compare-and-set. Never raises."""
    try:
        _run(job_id)
    finally:
        paths.delete_job_geometry(job_id)


def run_video_preview_job(job_id: int) -> None:
    """Render preview *job_id*, the same way as :func:`run_video_job`. A
    separate entry point so the killed-horse handler can name it
    (``src.jobs.worker``); what is rendered, kept and emailed follows the
    row's ``kind``, as in every other path that touches a job."""
    try:
        _run(job_id)
    finally:
        paths.delete_job_geometry(job_id)


def _run(job_id: int) -> None:
    now = time.time()
    if not _cas(job_id, ("pending",), status="running", started_at=now,
                stage="starting", progress=0.0):
        with get_session() as sess:
            job = sess.get(DBVideoJob, job_id)
            status = job.status if job is not None else None
            uid = job.user_info_id if job is not None else None
        _log.warning("Video job %s not started: its row is %s, not pending",
                     job_id, status or "missing")
        if uid is not None and status != "done":
            paths.delete_job_files(uid, job_id)
        return

    with get_session() as sess:
        job = sess.get(DBVideoJob, job_id)
        uid, project_id = job.user_info_id, job.project_id
        download_token = job.download_token
        request = json.loads(job.request_json or "{}")
        preview = job.kind == "preview"

    def _progress(fraction: float, stage: str) -> None:
        """Persist progress for polling clients, only while still running."""
        _cas(job_id, ("running",), progress=max(0.0, min(1.0, float(fraction))),
             stage=stage)

    out_path = (preview_result_path if preview else paths.result_path)(uid, job_id)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        geometry = paths.read_job_geometry(uid, job_id)
        written = (_preview_renderer if preview else _renderer)()(
            job_id=job_id, user_info_id=uid, project_id=project_id,
            request=request, out_path=out_path, geometry=geometry,
            progress=_progress,
        )
    except BaseException as exc:  # noqa: BLE001 — every failure ends the job
        _log.error("Video job %s failed: %s", job_id, _describe(exc))
        paths.delete_job_files(uid, job_id)
        if _cas(job_id, ("running",), status="failed", error_message=_reason_for(exc),
                completed_at=time.time()) and not preview:
            _notify_failed(job_id, uid, project_id)
        if not isinstance(exc, Exception):
            raise  # KeyboardInterrupt / SystemExit: let the process go
        return

    completed = time.time()
    size = written.stat().st_size if written.exists() else None
    if not _cas(job_id, ("running",), status="done", stage="complete", progress=1.0,
                completed_at=completed, result_path=str(written), size_bytes=size,
                expires_at=completed + (PREVIEW_RETENTION_S if preview else RETENTION_S)):
        # Failed by a sweep while rendering: that outcome stands, and the
        # user already has its email (a video's; a preview has none).
        _log.warning("Video job %s finished after its row was failed; "
                     "discarding the render", job_id)
        paths.delete_job_files(uid, job_id)
        return
    if not preview:
        _notify_ready(job_id, uid, project_id, download_token)


def mark_video_job_interrupted(job_id: int, reason: str) -> None:
    """Fail a pending or running job whose render died where ``run_video_job``
    could not see it (killed work-horse, dead worker, lost queue entry):
    delete its files and send the failed email — for a video only, never for
    a preview (D7). No-op on any other state, so a race with the job's own
    ending never sends a second email."""
    with get_session() as sess:
        job = sess.get(DBVideoJob, job_id)
        if job is None:
            return
        uid, project_id, kind = job.user_info_id, job.project_id, job.kind
    if not _cas(job_id, NON_TERMINAL, status="failed", error_message=reason,
                completed_at=time.time()):
        return
    paths.delete_job_files(uid, job_id)
    if kind != "preview":
        _notify_failed(job_id, uid, project_id)


def fail_unqueued_video_job(job_id: int) -> None:
    """Fail a job that could not be queued, and delete its geometry. No email:
    the request that created it answers 503 right away."""
    _cas(job_id, ("pending",), status="failed", error_message=REASON_UNAVAILABLE,
         completed_at=time.time())
    paths.delete_job_geometry(job_id)


def _fail_each(ids: Iterable[int], reason: str) -> int:
    failed = 0
    for job_id in ids:
        try:
            mark_video_job_interrupted(job_id, reason)
            failed += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
            _log.exception("Could not fail stale video job %s", job_id)
    return failed


def sweep_stale_running_video_jobs() -> int:
    """Fail every ``running`` full-video job. Called by the ``video`` worker as
    it starts, before it takes a job.

    That worker is the queue's only consumer (QUEUE_MAX_CONCURRENCY), so at
    that moment nothing is rendering: a ``running`` video row belongs to a
    work-horse that died with the previous worker. ``pending`` rows are left
    alone — they may still be waiting in Redis, and this worker is about to
    run them. Previews are left alone too: they run on ``default``, whose
    other consumer may be rendering one right now (D5); the hourly sweep
    fails them by age. Never raises.
    """
    try:
        with get_session() as sess:
            ids = list(sess.exec(
                select(DBVideoJob.id).where(DBVideoJob.status == "running",
                                            DBVideoJob.kind == "video")).all())
    except Exception:  # noqa: BLE001 — a broken sweep must not stop the worker
        _log.exception("Video startup sweep could not read running jobs")
        return 0
    failed = _fail_each(ids, REASON_WORKER_RESTARTED)
    if failed:
        _log.info("Failed %d video job(s) left running by a previous worker", failed)
    return failed


def sweep_video_jobs(now: Optional[float] = None) -> dict:
    """The API's hourly video sweep. Never raises. Returns what it did.

    1. Fails a ``running`` job started more than ``STALE_RUNNING_S`` ago (RQ
       has killed it by then) and a ``pending`` job created more than
       ``STALE_PENDING_S`` ago (its queue entry is gone). A preview's limits
       are ``PREVIEW_STALE_RUNNING_S`` and ``PREVIEW_STALE_PENDING_S`` (D5),
       and failing one sends no email (D7).
    2. Expires finished videos and previews past ``expires_at``: row →
       ``expired``, file deleted.
    3. Deletes a ``geometry.json`` whose job is terminal or gone — never one
       whose job is ``pending`` or ``running``, however old: a waiting job
       still needs it, and a stale one was failed in step 1, which deleted it.
    """
    now = time.time() if now is None else now
    done = {"failed": 0, "expired": 0, "geometry_deleted": 0}
    try:
        with get_session() as sess:
            stale_running = list(sess.exec(select(DBVideoJob.id).where(
                DBVideoJob.status == "running",
                or_(and_(DBVideoJob.kind == "video",
                         DBVideoJob.started_at < now - STALE_RUNNING_S),
                    and_(DBVideoJob.kind == "preview",
                         DBVideoJob.started_at < now - PREVIEW_STALE_RUNNING_S)))).all())
            stale_pending = list(sess.exec(select(DBVideoJob.id).where(
                DBVideoJob.status == "pending",
                or_(and_(DBVideoJob.kind == "video",
                         DBVideoJob.created_at < now - STALE_PENDING_S),
                    and_(DBVideoJob.kind == "preview",
                         DBVideoJob.created_at < now - PREVIEW_STALE_PENDING_S)))).all())
            expiring = [(j.id, j.user_info_id) for j in sess.exec(select(DBVideoJob).where(
                DBVideoJob.status == "done",
                DBVideoJob.expires_at < now)).all()]
        done["failed"] += _fail_each(stale_running, REASON_TIMED_OUT)
        done["failed"] += _fail_each(stale_pending, REASON_NEVER_STARTED)

        for job_id, uid in expiring:
            if _cas(job_id, ("done",), status="expired", result_path=None):
                paths.delete_job_files(uid, job_id)
                done["expired"] += 1

        strays = list(paths.stray_geometry_files())
        if strays:
            with get_session() as sess:
                live = set(sess.exec(select(DBVideoJob.id).where(
                    DBVideoJob.id.in_([job_id for job_id, _ in strays]),
                    DBVideoJob.status.in_(NON_TERMINAL))).all())
            for job_id, path in strays:
                if job_id not in live:
                    path.unlink(missing_ok=True)
                    done["geometry_deleted"] += 1
    except Exception:  # noqa: BLE001 — a scheduled job must not die loudly
        _log.exception("Video sweep failed")
    if any(done.values()):
        _log.info("Video sweep: %s", done)
    return done

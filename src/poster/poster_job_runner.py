"""Background runner for async A0 poster-generation jobs (issue #14).

``run_poster_job`` is invoked via ``BackgroundTasks.add_task`` from
``api/poster.py`` right after a ``DBPosterJob`` row is created with
status="pending". The actual rendering (basemap + route + memory pins/cards)
lives in ``src/poster/poster_renderer.py`` (Unit E) — this module owns only
the job-row lifecycle: marking it "running", invoking the renderer with a
``progress`` callback that updates ``job.stage`` between steps, and marking it
"done"/"failed" with the resulting file paths or error. Once a terminal state
is committed it also sends a best-effort notification email (issue #14
follow-up) with a download link — the Flutter client no longer polls for
completion, so email is the only thing that tells the user their poster is
ready (or failed).

``mark_job_interrupted``/``sweep_orphaned_poster_jobs`` cover the case
``run_poster_job``'s own try/except cannot: a poster render is memory-heavy
enough that the RQ work-horse running it can be OOM-killed outright (SIGKILL)
rather than raising a catchable Python exception — the process is just gone,
mid-render, with nothing left to run the ``except`` block or send the email.
Left alone the job row stays "running" forever and the user — who was
explicitly told "we'll email you when it's ready" — never hears anything at
all. Unlike a route job's transient network hiccup, a poster OOM is a
deterministic property of that job's own inputs (bounds size, photo count),
so neither path retries; both go straight to "failed" plus the same
notification email a normal failure would send.

A job that ends (done or failed, by any path) keeps only a scrubbed copy of
its request: no memory text, no coordinates (``scrub_request``). Nothing reads
the request after the render, and an encrypted trip's text must not outlive
it. ``sweep_poster_jobs`` deletes finished jobs' rows and files after
``RETENTION_S``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import delete
from sqlmodel import select

from models.db import get_session
from models.project_db import DBPosterJob
from models.user import UserInfo
from src.email.service import EmailMessage, get_email_service
from src.email.templates import render_poster_failed_email, render_poster_ready_email
from src.poster.poster_renderer import render_poster
from src.project.project_repo import ProjectRepo

_log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

_repo = ProjectRepo()

TERMINAL = ("done", "failed")

# Finished poster jobs (row and files) are deleted this long after
# ``completed_at`` — 30 days, as for trip videos.
RETENTION_S = 30 * 24 * 3600

# What a finished job's ``request_json`` keeps: the layout choices, never the
# region, the title or a memory's text, date or position.
_KEPT_REQUEST_KEYS = ("orientation", "paper_size", "config", "title_position",
                      "title_scale", "plaintext_consent")
_KEPT_MEMORY_KEYS = ("id", "photo_uuids")


def scrub_request(request_json: str | None) -> str:
    """The copy of a job's request a finished job keeps (see module docstring).

    An allow-list, so a field added to the request later is dropped unless
    someone decides it is safe to keep. An unreadable request becomes ``{}``.
    """
    try:
        request = json.loads(request_json or "{}")
    except ValueError:
        return "{}"
    if not isinstance(request, dict):
        return "{}"
    kept = {k: request[k] for k in _KEPT_REQUEST_KEYS if k in request}
    memories = request.get("memories")
    if isinstance(memories, list):
        kept["memories"] = [
            {k: m[k] for k in _KEPT_MEMORY_KEYS if k in m}
            for m in memories if isinstance(m, dict)
        ]
    return json.dumps(kept)


def _poster_dir_path(user_info_id: int, job_id: int) -> Path:
    """The job's file directory, without creating it (unlike ``_poster_dir``)."""
    return _DATA_DIR / "users" / str(user_info_id) / "posters" / str(job_id)


def _frontend_origin() -> str:
    """Base URL for the download link in the notification email.

    Read at call time, not import time, so tests and ops can set it without
    reimporting the module — same as ``_frontend_origin`` in
    ``src/auth/email_verification.py``.
    """
    return os.environ.get("FRONTEND_ORIGIN", "http://localhost:5500")


def _poster_dir(user_id: str, job_id: int) -> Path:
    p = _DATA_DIR / "users" / user_id / "posters" / str(job_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _project_name(project_id: int) -> str:
    """Best-effort display name for the notification email subject/body."""
    with get_session() as sess:
        project = _repo.get_project_by_id(sess, project_id)
    return project.name if project is not None else "your trip"


def _notify_poster_ready(job_id: int, user_info_id: int, project_id: int,
                          download_token: str | None) -> None:
    """Best-effort email: the poster is ready to download (issue #14).

    Called after the job row's "done" commit, so a slow or failing send never
    delays the status a polling client — or the token page — would otherwise
    see. Never raises: an email failure must not turn a successful poster
    render into a failed job, same rationale as the comment on
    ``send_verification_email`` in ``src/auth/email_verification.py``.
    """
    if not download_token:
        _log.warning("Poster job %s done with no download_token; skipping "
                      "ready email", job_id)
        return
    try:
        with get_session() as sess:
            user = sess.get(UserInfo, user_info_id)
        if user is None or not user.email:
            return
        project_name = _project_name(project_id)
        text_body, html_body = render_poster_ready_email(
            project_name=project_name,
            download_url=f"{_frontend_origin()}/poster/{download_token}",
        )
        asyncio.run(get_email_service().send(EmailMessage(
            to=user.email,
            subject=f"Your poster for {project_name} is ready",
            text_body=text_body,
            html_body=html_body,
        )))
    except Exception:
        _log.exception("Failed to send poster-ready email for job %s", job_id)


def _notify_poster_failed(job_id: int, user_info_id: int, project_id: int) -> None:
    """Best-effort email: poster generation failed (issue #14). See
    ``_notify_poster_ready`` for why this never raises.

    Deliberately does not forward ``job.error_message`` — it can carry
    internal detail (e.g. a Mapbox error derived from a stack trace) — the
    email uses generic "try again" copy instead (see
    ``render_poster_failed_email``).
    """
    try:
        with get_session() as sess:
            user = sess.get(UserInfo, user_info_id)
        if user is None or not user.email:
            return
        project_name = _project_name(project_id)
        text_body, html_body = render_poster_failed_email(project_name=project_name)
        asyncio.run(get_email_service().send(EmailMessage(
            to=user.email,
            subject=f"Your poster for {project_name} could not be generated",
            text_body=text_body,
            html_body=html_body,
        )))
    except Exception:
        _log.exception("Failed to send poster-failed email for job %s", job_id)


def run_poster_job(job_id: int) -> None:
    """Render the poster for *job_id*, updating its status as it progresses.

    Looks up the job row, marks it "running", renders the real poster (real
    basemap + pins/cards/route), and marks it "done" with the resulting file
    paths. Any exception is caught and recorded as a "failed" status with
    ``error_message`` rather than propagating (this runs detached, as a
    FastAPI background task).
    """
    with get_session() as sess:
        job = sess.get(DBPosterJob, job_id)
        if job is None:
            return
        job.status = "running"
        job.stage = "starting"
        sess.add(job)
        sess.commit()
        user_info_id = job.user_info_id
        project_id = job.project_id
        download_token = job.download_token
        request = json.loads(job.request_json or "{}")

    def _progress(stage: str) -> None:
        """Persist a progress label onto the job row for polling clients."""
        with get_session() as s:
            j = s.get(DBPosterJob, job_id)
            if j is not None:
                j.stage = stage
                s.add(j)
                s.commit()

    try:
        poster_dir = _poster_dir(str(user_info_id), job_id)
        png_path, pdf_path = render_poster(
            job_id=job_id,
            user_info_id=user_info_id,
            project_id=project_id,
            request=request,
            poster_dir=poster_dir,
            progress=_progress,
        )

        with get_session() as sess:
            job = sess.get(DBPosterJob, job_id)
            job.status = "done"
            job.stage = "complete"
            job.result_png_path = str(png_path)
            job.result_pdf_path = str(pdf_path)
            job.completed_at = time.time()
            job.request_json = scrub_request(job.request_json)
            sess.add(job)
            sess.commit()
        _notify_poster_ready(job_id, user_info_id, project_id, download_token)
    except Exception as exc:
        _log.exception("Poster job %s failed", job_id)
        with get_session() as sess:
            job = sess.get(DBPosterJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error_message = str(exc)
                job.completed_at = time.time()
                job.request_json = scrub_request(job.request_json)
                sess.add(job)
                sess.commit()
        _notify_poster_failed(job_id, user_info_id, project_id)


def mark_job_interrupted(job_id: int, reason: str) -> None:
    """Force a non-terminal job straight to "failed" and send the failure
    email — for a job whose worker died without ever reaching
    ``run_poster_job``'s own except block (see module docstring).

    No-ops on an already-terminal job (a race against the job actually
    finishing/failing on its own must never overwrite that outcome, and must
    never send a second email for the same job).
    """
    with get_session() as sess:
        job = sess.get(DBPosterJob, job_id)
        if job is None or job.status in TERMINAL:
            return
        job.status = "failed"
        job.error_message = reason
        job.completed_at = time.time()
        job.request_json = scrub_request(job.request_json)
        sess.add(job)
        sess.commit()
        user_info_id = job.user_info_id
        project_id = job.project_id
    _notify_poster_failed(job_id, user_info_id, project_id)


def sweep_orphaned_poster_jobs() -> int:
    """Fail every non-terminal poster job. Returns how many were failed.

    Called once at API startup (backstop for a worker *container* dying
    outright, not just its work-horse — see ``mark_job_interrupted`` for the
    more immediate path that covers a lone work-horse OOM-kill). Anything
    still "pending"/"running" when the process starts is by definition
    orphaned: nothing was executing it a moment ago, because nothing was
    running at all — same reasoning as ``route_jobs.sweep_orphaned_jobs``.

    Unlike that route-job sweep, this never re-queues: a poster job that died
    mid-render almost always did so because of its own inputs (bounds size,
    photo count), so re-running the identical render would just repeat the
    failure. Telling the user plainly and letting them retry — possibly with
    a smaller region — is both simpler and more honest.
    """
    try:
        with get_session() as sess:
            orphan_ids = [
                j.id for j in sess.exec(
                    select(DBPosterJob).where(DBPosterJob.status.notin_(TERMINAL))
                ).all()
            ]
    except Exception:  # noqa: BLE001 — a broken sweep must not stop the app booting
        _log.exception("poster job sweep failed to read orphans")
        return 0

    failed = 0
    for job_id in orphan_ids:
        try:
            mark_job_interrupted(
                job_id,
                "The poster generation process did not survive a server "
                "restart — please try again.",
            )
            failed += 1
        except Exception:  # noqa: BLE001
            _log.exception("could not fail orphaned poster job %s", job_id)

    if failed:
        _log.info("failed %d orphaned poster job(s) at startup", failed)
    return failed


def sweep_poster_jobs(now: float | None = None) -> int:
    """The API's hourly poster sweep: deletes the files, then the row, of every
    finished job whose ``completed_at`` is more than ``RETENTION_S`` ago.
    Returns how many rows it deleted. Never raises.

    Only ``done``/``failed`` jobs are candidates, and the row delete re-checks
    that, so a pending or running job's files are never touched. Files go
    before the row: a sweep interrupted between the two leaves a row the next
    run deletes again, never files nothing points at.
    """
    cutoff = (time.time() if now is None else now) - RETENTION_S
    deleted = 0
    try:
        with get_session() as sess:
            expired = [(j.id, j.user_info_id) for j in sess.exec(
                select(DBPosterJob).where(
                    DBPosterJob.status.in_(TERMINAL),
                    DBPosterJob.completed_at < cutoff,
                )).all()]
        for job_id, user_info_id in expired:
            shutil.rmtree(_poster_dir_path(user_info_id, job_id), ignore_errors=True)
            with get_session() as sess:
                result = sess.execute(delete(DBPosterJob).where(
                    DBPosterJob.id == job_id,
                    DBPosterJob.status.in_(TERMINAL),
                ))
                sess.commit()
                deleted += result.rowcount
    except Exception:  # noqa: BLE001 — a scheduled job must not die loudly
        _log.exception("Poster sweep failed")
    if deleted:
        _log.info("Poster sweep: deleted %d job(s) older than the retention", deleted)
    return deleted

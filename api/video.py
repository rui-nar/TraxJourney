"""REST trip-video endpoints (docs/TRIP_VIDEO_PLAN.md, U6).

Routes:
    POST /api/projects/{name}/video/plan               — what a video of this trip would be
    POST /api/projects/{name}/video                    — start a render job
    GET  /api/projects/{name}/video/{job_id}           — poll job status
    GET  /api/projects/{name}/video/{job_id}/download  — the MP4 (range requests)

    GET  /api/video/{token}                            — poll job status (unauthenticated)
    GET  /api/video/{token}/download                   — the MP4 (unauthenticated)

Shaped like ``api/poster.py`` (job row, token routes, 404 semantics), with
three differences that matter:

* **The API never renders (D7).** No broker ⇒ 503 before anything is
  written; a failed enqueue fails the job and deletes its geometry.
* **The requester pays (D12).** Quota and resolution are always the caller's,
  also on a shared trip reached with ``?owner=``; the job, its emails and its
  download belong to the caller.
* **Encrypted trips need consent (D2).** The server can't read an encrypted
  activity, so the client sends its decrypted line in ``decrypted_geometry``
  for this one render. That plaintext is checked here, written only to the
  job's ``geometry.json`` after the row commits, and never logged, stored in
  the row or echoed in an error (Convention 2).
"""
from __future__ import annotations

import json
import math
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Dict, List, Literal, Optional

import polyline as polyline_lib
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from api.deps import get_current_user
from api.project_access import OwnerParam, resolve_project
from models.db import get_session
from models.project_db import DBVideoJob
from src.billing.entitlements import (
    ensure_video_quota,
    limits_for,
    plan_for,
    quotas_enforced,
    videos_this_month,
)
from src.billing.plans import FULL_HD_HEIGHT
from src.billing.subscriptions import lock_account
from src.exceptions.errors import QuotaExceeded
from src.jobs.queue import QUEUE_VIDEO, enqueue, queue_available
from src.models.project import Project
from src.project.project_repo import ProjectRepo
from src.utils.logging import get_logger
from src.video import paths
from src.video.job_runner import JOB_TIMEOUT_S, fail_unqueued_video_job, run_video_job
from src.video.legs import is_encrypted_activity
from src.video.pacing import group_clips, max_clips
from src.video.timeline import NothingToAnimate, Timeline, timeline_for_project

router = APIRouter(prefix="/api/projects", tags=["video"])
video_public_router = APIRouter(prefix="/api/video", tags=["video"])

_log = get_logger(__name__)
_repo = ProjectRepo()

LENGTHS = (30, 60, 90)                      # D11
HEIGHTS = (720, 1080)                       # D10
WIDTH_FOR_HEIGHT = {720: 1280, 1080: 1920}  # 16:9 (D8)

VideoLength = Literal[30, 60, 90]
VideoHeight = Literal[720, 1080]


# ── Request/response schemas ──────────────────────────────────────────────────

class VideoPlanRequest(BaseModel):
    length_s: VideoLength = 60
    height: Optional[VideoHeight] = None
    # Activity id → Google-encoded polyline, decrypted on the client, for
    # encrypted activities only (D2). A trackless one sends its 2-point line.
    decrypted_geometry: Optional[Dict[int, str]] = None


class VideoRequest(BaseModel):
    length_s: VideoLength
    height: VideoHeight = 720
    decrypted_geometry: Optional[Dict[int, str]] = None


class ClipOut(BaseModel):
    index: int
    mode: str
    start_s: float
    duration_s: float
    legs: int
    date: Optional[str]


class SkippedOut(BaseModel):
    kind: str
    ref: str
    reason: str


class QuotaOut(BaseModel):
    limit: Optional[int] = Field(None, description="Videos per UTC month; null = unlimited")
    used: int
    remaining: Optional[int] = Field(None, description="null = unlimited")


class VideoPlanOut(BaseModel):
    available: bool = Field(description="A broker and ffmpeg are there to render it")
    length_s: int
    legs: int
    clips: List[ClipOut]
    clip_counts: Dict[str, int] = Field(description="Clip count per offered length")
    skipped: List[SkippedOut]
    consent_required: List[int] = Field(
        description="Encrypted activities still without decrypted_geometry "
                    "(always empty here: a missing one is a 409)")
    quota: QuotaOut
    resolutions: List[int] = Field(description="Heights the requester's plan allows")


class JobIdOut(BaseModel):
    job_id: int = Field(description="ID of the created video job")


class JobStatusOut(BaseModel):
    status: str = Field(description="'pending' | 'running' | 'done' | 'failed' | 'expired'")
    stage: Optional[str] = Field(None, description="Human-readable progress label")
    progress: float = Field(0.0, description="0.0 – 1.0")
    error_message: Optional[str] = Field(None, description="Set when status='failed'")
    expires_at: Optional[float] = Field(None, description="When a done video is deleted")


# ── Helpers ───────────────────────────────────────────────────────────────────

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _max_height(sess, requester: int) -> int:
    """Tallest video *requester* may render. Plan limits apply only while
    quotas are enforced, like every other limit."""
    if not quotas_enforced():
        return FULL_HD_HEIGHT
    return limits_for(plan_for(sess, requester)).max_video_height or FULL_HD_HEIGHT


def _ensure_height(sess, requester: int, height: Optional[int]) -> None:
    """402 when *height* is above the requester's plan (D10)."""
    allowed = _max_height(sess, requester)
    if height is not None and height > allowed:
        raise QuotaExceeded(
            f"Your plan makes videos up to {allowed}p. Upgrade for {height}p.",
            plan=plan_for(sess, requester), limit=allowed, used=allowed,
            needed=height, resource="video_height")


def _quota(sess, requester: int) -> QuotaOut:
    used = videos_this_month(sess, requester)
    limit = (limits_for(plan_for(sess, requester)).max_videos_per_month
             if quotas_enforced() else None)
    return QuotaOut(limit=limit, used=used,
                    remaining=None if limit is None else max(0, limit - used))


def _load(sess, requester: int, name: str, owner: Optional[int]):
    """(project row, domain project) for the requester, 404 as the poster's."""
    row = resolve_project(sess, requester, name, owner)
    project = _repo.get_project(sess, row.user_info_id, name, include_elevation=False,
                                journal_user_id=requester)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return row, project


def _valid_line(encoded: str) -> bool:
    """A decodable polyline of at least two real coordinates."""
    try:
        points = polyline_lib.decode(encoded)
    except Exception:  # noqa: BLE001 — any undecodable string is just invalid
        return False
    return len(points) >= 2 and all(
        math.isfinite(lat) and math.isfinite(lon)
        and -90 <= lat <= 90 and -180 <= lon <= 180
        for lat, lon in points)


def _consent(project: Project, geometry: Optional[Dict[int, str]]) -> Dict[int, str]:
    """The consent geometry to render with, checked before any timeline.

    Accepted only for activities that are in this trip *and* encrypted (422
    otherwise); 409 ``consent_required`` while any encrypted activity has
    none. Error details name activity ids only, never the geometry.
    """
    geometry = geometry or {}
    encrypted = []
    for item in project.items:
        if item.item_type == "activity" and item.activity_id is not None:
            activity = project.activity_by_id(item.activity_id)
            if activity is not None and is_encrypted_activity(activity):
                encrypted.append(activity.id)

    refused = sorted(set(geometry) - set(encrypted))
    if refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"decrypted_geometry given for activities that are not encrypted "
                   f"activities of this trip: {refused}")
    invalid = sorted(k for k, v in geometry.items() if not _valid_line(v))
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"decrypted_geometry is not a valid line for activities: {invalid}")

    missing = [i for i in encrypted if i not in geometry]
    if missing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "code": "consent_required",
            "message": "This trip is encrypted. Its tracks can be drawn only if "
                       "you send them decrypted for this one video.",
            "consent_required": missing,
        })
    return geometry


def _timeline(project: Project, length_s: int, geometry: Dict[int, str]) -> Timeline:
    try:
        return timeline_for_project(project, float(length_s), geometry=geometry)
    except NothingToAnimate:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Nothing in this trip can be animated")


def _get_owned_job(sess, job_id: int, requester: int, project_id: int) -> DBVideoJob:
    job = sess.get(DBVideoJob, job_id)
    if job is None or job.user_info_id != requester or job.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video job not found")
    return job


def _get_job_by_token(sess, token: str) -> DBVideoJob:
    """Same 404 as an unknown job id, whether the token is wrong or missing."""
    job = sess.exec(select(DBVideoJob).where(DBVideoJob.download_token == token)).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video job not found")
    return job


def _status(job: DBVideoJob) -> dict:
    return {"status": job.status, "stage": job.stage, "progress": job.progress,
            "error_message": job.error_message, "expires_at": job.expires_at}


def _file_response(job: DBVideoJob) -> FileResponse:
    """The MP4 of a done job; Starlette's FileResponse answers Range requests,
    so a player can seek and a download can resume."""
    if job.status != "done":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not ready")
    if not job.result_path or not Path(job.result_path).exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(job.result_path, media_type="video/mp4")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/{name}/video/plan", response_model=VideoPlanOut,
             summary="Plan a trip video: clips, quota, resolutions")
def plan_video(
    name: str,
    body: VideoPlanRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """What a video of this trip would be, for the dialog. Writes nothing.

    Consent comes before the timeline, as in ``POST /video``: a trip whose
    only legs are encrypted activities answers 409, not 422.
    """
    requester = int(current_user["sub"])
    with get_session() as sess:
        _row, project = _load(sess, requester, name, owner)
        _ensure_height(sess, requester, body.height)
        geometry = _consent(project, body.decrypted_geometry)
        timeline = _timeline(project, body.length_s, geometry)
        quota = _quota(sess, requester)
        allowed = _max_height(sess, requester)

    return VideoPlanOut(
        available=queue_available() and ffmpeg_available(),
        length_s=body.length_s,
        legs=len(timeline.legs),
        clips=[ClipOut(index=c.index, mode=c.clip.mode, start_s=c.start_s,
                       duration_s=c.duration_s, legs=len(c.clip.legs),
                       date=c.subs[0].leg.date.isoformat() if c.subs[0].leg.date else None)
               for c in timeline.clips],
        clip_counts={str(n): len(group_clips(timeline.legs, max_clips(float(n))))
                     for n in LENGTHS},
        skipped=[SkippedOut(kind=s.kind, ref=str(s.ref), reason=s.reason)
                 for s in timeline.skipped],
        consent_required=[],
        quota=quota,
        resolutions=[h for h in HEIGHTS if h <= allowed],
    )


@router.post("/{name}/video", status_code=status.HTTP_201_CREATED,
             response_model=JobIdOut, summary="Start a trip video render")
def create_video_job(
    name: str,
    body: VideoRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Create a video job and queue it on the ``video`` worker.

    In this order, so nothing is written unless the job can run:
    broker (503) → resolution (402) → consent (409) → timeline (422) →
    one transaction whose first write locks the requester's account, then
    the monthly quota (402) and the row → consent geometry file → enqueue
    (a failure fails the row, deletes the geometry, 503).
    """
    requester = int(current_user["sub"])
    if not queue_available():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Video rendering is not available right now")

    with get_session() as sess:
        row, project = _load(sess, requester, name, owner)
        project_id = row.id
        _ensure_height(sess, requester, body.height)
        geometry = _consent(project, body.decrypted_geometry)
        _timeline(project, body.length_s, geometry)

    request = {"length_s": body.length_s, "height": body.height,
               "width": WIDTH_FOR_HEIGHT[body.height]}
    with get_session() as sess:
        # The lock serialises this requester's concurrent POSTs: the quota
        # count below sees every job a racing request committed.
        lock_account(sess, requester)
        ensure_video_quota(sess, requester)
        job = DBVideoJob(project_id=project_id, user_info_id=requester, status="pending",
                         request_json=json.dumps(request),
                         download_token=str(uuid.uuid4()))
        sess.add(job)
        sess.commit()
        job_id = job.id

    try:
        if geometry:
            paths.write_job_geometry(requester, job_id, geometry)
        queued = enqueue(QUEUE_VIDEO, run_video_job, job_id, max_retries=0,
                         allow_inline=False, job_timeout=JOB_TIMEOUT_S)
    except BaseException:
        fail_unqueued_video_job(job_id)
        raise
    if not queued:
        fail_unqueued_video_job(job_id)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Video rendering is not available right now")
    return {"job_id": job_id}


@router.get("/{name}/video/{job_id}", response_model=JobStatusOut,
            summary="Get video job status")
def get_video_job_status(
    name: str,
    job_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    requester = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, requester, name, owner)
        return _status(_get_owned_job(sess, job_id, requester, row.id))


@router.get("/{name}/video/{job_id}/download", summary="Download the trip video")
def download_video(
    name: str,
    job_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """The MP4 once done. 404 when the job is unknown, someone else's, not
    done (or expired), or its file is gone."""
    requester = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, requester, name, owner)
        job = _get_owned_job(sess, job_id, requester, row.id)
    return _file_response(job)


# ── Public (token-based) routes ──────────────────────────────────────────────
# Reached from the ready email, possibly with no session: the job's
# download_token is the only credential, as for the poster.

@video_public_router.get("/{token}", response_model=JobStatusOut,
                         summary="Get video job status (unauthenticated, by download token)")
def get_video_job_status_by_token(token: str):
    with get_session() as sess:
        return _status(_get_job_by_token(sess, token))


@video_public_router.get("/{token}/download",
                         summary="Download the trip video (unauthenticated, by download token)")
def download_video_by_token(token: str):
    with get_session() as sess:
        job = _get_job_by_token(sess, token)
    return _file_response(job)

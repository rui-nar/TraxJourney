"""REST poster endpoints — async server-side A0 poster generation (issue #14).

Routes:
    POST /api/projects/{name}/poster                    — start a poster job
    GET  /api/projects/{name}/poster/{job_id}            — poll job status
    GET  /api/projects/{name}/poster/{job_id}/download   — download the rendered file
    POST /api/projects/{name}/poster/preview             — fast low-res layout preview

    GET  /api/poster/{token}                             — poll job status (unauthenticated)
    GET  /api/poster/{token}/download                     — download the rendered file (unauthenticated)

This is Unit A of the poster feature: it owns the job row, the API contract,
and a placeholder renderer (src/poster/poster_job_runner.py). Later units
replace what happens inside ``run_poster_job`` (real tile-fetching, card
placement, day metrics, rendering) without changing this contract.

The two ``/api/poster/{token}`` routes are a follow-up (issue #14, email
notification): the client no longer polls for completion, instead the
finished job's owner gets an email with a download link, and that link is
opened from an inbox — possibly on a different device/browser with no active
session. They live on a separate router (``poster_public_router``, mounted at
``/api/poster`` rather than under ``/api/projects/{name}``) precisely so they
can skip ``Depends(get_current_user)``; the token itself (a UUID stored on
the job row, see ``DBPosterJob.download_token``) is the only credential,
mirroring the existing share-link pattern in ``api/project_shares.py``.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Annotated, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from models.db import get_session
from pydantic import BaseModel, Field, field_validator
from sqlmodel import select

from api.deps import get_current_user
from api.project_access import OwnerParam, resolve_project
from models.project_db import DBPosterJob
from models.user import UserInfo
from src.poster.poster_job_runner import run_poster_job
from src.jobs.queue import QUEUE_POSTER, enqueue
from src.poster.poster_renderer import (
    _TITLE_SCALE_MAX,
    _TITLE_SCALE_MIN,
    render_poster_preview,
)

router = APIRouter(prefix="/api/projects", tags=["poster"])
poster_public_router = APIRouter(prefix="/api/poster", tags=["poster"])

_log = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


# ── Request/response schemas ──────────────────────────────────────────────────

class BoundsIn(BaseModel):
    north: float
    south: float
    east: float
    west: float


class TitlePositionIn(BaseModel):
    """Normalized (0-1) position of the title/trip-summary card's top-left
    corner within the poster's drawable area. ``(0, 0)`` is the pre-feature
    hardcoded top-left corner, so an omitted position preserves today's
    placement exactly."""
    x: float = 0.0
    y: float = 0.0


class PosterConfigIn(BaseModel):
    distance: bool = False
    elevation: bool = False
    hero_photo: bool = False
    all_photos: bool = False
    memory_text: bool = False
    counters: bool = False
    tag_pie: bool = False
    encounters: bool = False
    # One card for the whole trip (title, period, total distance, total climb),
    # pinned to the poster's top-left corner. On by default: it is what makes a
    # poster readable as a trip rather than as a set of unrelated pins.
    trip_summary: bool = True
    # Colour scheme for the poster's cards, legend and stat panels
    # (src/poster/theme.py). Dark by default: a poster is composed over
    # satellite imagery, which dark cards sit on far more comfortably.
    theme: Literal["light", "dark"] = "dark"
    # Card placement algorithm. "radial" (src/poster/card_placement.py) searches
    # outwards from each pin and is the default; "perimeter"
    # (src/poster/perimeter_placement.py) is an opt-in prototype that lays every
    # card along the page border instead, leaving the map area to the track.
    layout: Literal["radial", "perimeter"] = "radial"


class PosterMemoryIn(BaseModel):
    id: int
    lat: float
    lon: float
    date: str
    name: Optional[str] = None
    description: Optional[str] = None
    photo_uuids: List[str] = Field(default_factory=list)


class PosterRequest(BaseModel):
    bounds: BoundsIn
    orientation: str = Field(description="'landscape' or 'portrait'")
    paper_size: Literal["A0", "A1", "A2", "A3", "A4"] = "A0"
    config: PosterConfigIn
    memories: List[PosterMemoryIn] = Field(default_factory=list)
    # Trip-summary/title card placement (poster_title_dialog.dart, between the
    # config dialog and the layout preview). Defaults reproduce the
    # pre-feature hardcoded top-left corner, the project's own name, and the
    # unscaled "hero_title" size exactly, so a request that omits them renders
    # byte-for-byte the same as before this feature existed.
    title_position: TitlePositionIn = Field(default_factory=TitlePositionIn)
    title_text: Optional[str] = None
    title_scale: float = 1.0
    # The user agreed to send this trip's memory text in plaintext for this one
    # poster. Needed only when the trip owner is encrypted and a memory carries
    # text (``_require_consent``); the preview never asks for it.
    plaintext_consent: bool = False

    @field_validator("title_scale")
    @classmethod
    def _clamp_title_scale(cls, v: float) -> float:
        """Clamp rather than reject: the client-side slider already limits to
        this same range, but the API is the trust boundary, not the Flutter
        widget. Reuses poster_renderer's bounds so the two can't drift apart."""
        return max(_TITLE_SCALE_MIN, min(_TITLE_SCALE_MAX, v))


class JobIdOut(BaseModel):
    job_id: int = Field(description="ID of the created poster job")


class JobStatusOut(BaseModel):
    status: str = Field(description="'pending' | 'running' | 'done' | 'failed'")
    stage: Optional[str] = Field(None, description="Human-readable progress label")
    error_message: Optional[str] = Field(None, description="Set when status='failed'")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _poster_dir(user_id: str, job_id: int) -> Path:
    p = Path(_DATA_DIR) / "users" / user_id / "posters" / str(job_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_owned_job(sess, job_id: int, user_info_id: int) -> DBPosterJob:
    """Return the DBPosterJob row, 404ing unless it exists and belongs to the caller."""
    job = sess.get(DBPosterJob, job_id)
    if job is None or job.user_info_id != user_info_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poster job not found")
    return job


def _get_job_by_token(sess, token: str) -> DBPosterJob:
    """Return the DBPosterJob row for the unauthenticated email download link.

    A wrong or missing token 404s with the exact same message as an unknown
    job id — never leaking whether a token merely doesn't exist versus
    belonging to someone else, mirroring ``_get_owned_job`` above.
    """
    job = sess.exec(
        select(DBPosterJob).where(DBPosterJob.download_token == token)
    ).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poster job not found")
    return job


def _require_consent(sess, owner_id: int, body: PosterRequest) -> None:
    """409 ``consent_required`` when an encrypted owner's trip would send memory
    text without the user having agreed to it.

    The job renders that text into files kept on the server, so an encrypted
    trip's memory text may reach it only with consent. The detail names memory
    ids, never their text (same shape as the video consent 409 in
    ``api/video.py``). The preview does not call this: it stores nothing.
    """
    if body.plaintext_consent:
        return
    with_text = [m.id for m in body.memories if m.name or m.description]
    if not with_text:
        return
    owner_user = sess.get(UserInfo, owner_id)
    if owner_user is None or not owner_user.encryption_enabled:
        return
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
        "code": "consent_required",
        "message": "This trip is encrypted. Its memory text can be printed only "
                   "if you send it decrypted for this one poster.",
        "consent_required": with_text,
    })


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/{name}/poster", status_code=status.HTTP_201_CREATED,
             response_model=JobIdOut, summary="Start a poster generation job")
def create_poster_job(
    name: str,
    body: PosterRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    """Create a pending poster job for a project and render it in the background."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        project = resolve_project(sess, user_info_id, name, owner)
        _require_consent(sess, project.user_info_id, body)
        job = DBPosterJob(
            project_id=project.id,
            user_info_id=user_info_id,
            status="pending",
            request_json=json.dumps(body.model_dump()),
            download_token=str(uuid.uuid4()),
        )
        sess.add(job)
        sess.commit()
        job_id = job.id

    enqueue(QUEUE_POSTER, run_poster_job, job_id,
            background_tasks=background_tasks)
    return {"job_id": job_id}


@router.get("/{name}/poster/{job_id}", response_model=JobStatusOut,
            summary="Get poster job status")
def get_poster_job_status(
    name: str,
    job_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Poll the status of a poster job."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        resolve_project(sess, user_info_id, name, owner)
        job = _get_owned_job(sess, job_id, user_info_id)
        return {
            "status": job.status,
            "stage": job.stage,
            "error_message": job.error_message,
        }


@router.get("/{name}/poster/{job_id}/download", summary="Download the rendered poster")
def download_poster(
    name: str,
    job_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    format: str = Query("png", pattern="^(png|pdf)$"),
    owner: OwnerParam = None,
):
    """Return the rendered poster file once the job is done.

    404s if the job doesn't exist, isn't owned by the caller, isn't done yet, or
    the requested file is missing on disk — paths are always re-derived from the
    job row server-side, never trusted from the client.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        resolve_project(sess, user_info_id, name, owner)
        job = _get_owned_job(sess, job_id, user_info_id)
        if job.status != "done":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poster not ready")
        path_str = job.result_png_path if format == "png" else job.result_pdf_path

    if not path_str or not Path(path_str).exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    media_type = "image/png" if format == "png" else "application/pdf"
    return FileResponse(path_str, media_type=media_type)


# ── Public (token-based) routes ──────────────────────────────────────────────
# Reached from the "your poster is ready/failed" email (issue #14) — no
# active session is assumed, so these use the job's ``download_token`` as the
# sole credential instead of ``Depends(get_current_user)``. Logic mirrors
# ``get_poster_job_status``/``download_poster`` above exactly, just looked up
# by token instead of by (job_id, JWT, project ownership).

@poster_public_router.get("/{token}", response_model=JobStatusOut,
                           summary="Get poster job status (unauthenticated, by download token)")
def get_poster_job_status_by_token(token: str):
    """Poll the status of a poster job via its unguessable email download token."""
    with get_session() as sess:
        job = _get_job_by_token(sess, token)
        return {
            "status": job.status,
            "stage": job.stage,
            "error_message": job.error_message,
        }


@poster_public_router.get("/{token}/download",
                           summary="Download the rendered poster (unauthenticated, by download token)")
def download_poster_by_token(token: str, format: str = Query("png", pattern="^(png|pdf)$")):
    """Return the rendered poster file once the job is done.

    404s if the token doesn't match any job, the job isn't done yet, or the
    requested file is missing on disk — same behavior as ``download_poster``,
    just reached without a JWT.
    """
    with get_session() as sess:
        job = _get_job_by_token(sess, token)
        if job.status != "done":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poster not ready")
        path_str = job.result_png_path if format == "png" else job.result_pdf_path

    if not path_str or not Path(path_str).exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    media_type = "image/png" if format == "png" else "application/pdf"
    return FileResponse(path_str, media_type=media_type)


@router.post("/{name}/poster/preview", summary="Fast low-res poster layout preview")
def preview_poster(
    name: str,
    body: PosterRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Return a small PNG preview of the real poster for the given request,
    synchronously — no job row, no background task.

    The preview renders the same basemap, cards and placement as the full job,
    just small, so it shows what the poster will actually look like. If the
    basemap alone is unavailable (missing ``MAPBOX_TOKEN``, Mapbox
    unreachable), the preview still renders on flat grey and says so in the
    ``X-Poster-Warning`` response header rather than silently implying the
    finished poster will be grey too.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        project_id = row.id
        owner_id = row.user_info_id

    try:
        png_bytes, warning = render_poster_preview(project_id, owner_id, body.model_dump())
    except Exception as exc:
        # Unlike the async job (whose failure lands in job.error_message), a
        # preview failure would otherwise surface as a bare 500 with no
        # detail — issue #14 feedback point 9 ("preview was not available")
        # was undiagnosable for exactly that reason.
        _log.exception("Poster preview render failed for project %s", name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Preview render failed: {exc}",
        )

    headers = {}
    if warning:
        # Header rather than a JSON envelope so the body stays a plain PNG the
        # client can hand straight to Image.memory.
        headers["X-Poster-Warning"] = warning
        headers["Access-Control-Expose-Headers"] = "X-Poster-Warning"
    return Response(content=png_bytes, media_type="image/png", headers=headers)

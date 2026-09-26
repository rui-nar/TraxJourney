"""REST journal endpoints — CRUD and photo management for project journal entries.

Journal entries are private and per-user (issue #106): every project editor —
owner or travel companion — keeps their own entries, visible and editable only
by their author. They are never exposed in shared views.

Routes:
    POST   /api/journal                            — create a journal entry
    PUT    /api/journal/{id}                       — update a journal entry
    DELETE /api/journal/{id}                       — delete a journal entry + its photos
    POST   /api/journal/{id}/photos                — upload a photo
    POST   /api/journal/{id}/photos/from-url       — queue a photo download from URL
    DELETE /api/journal/{id}/photos/{uuid}         — delete a specific photo
    PUT    /api/journal/{id}/photos/{uuid}/replace — replace a photo's bytes in place
    GET    /api/journal/{id}/photos/{uuid}         — serve full-res photo
    GET    /api/journal/{id}/photos/{uuid}/thumb   — serve thumbnail
"""
from __future__ import annotations

import io
import json
import os
import uuid as uuid_lib
from pathlib import Path
from typing import Annotated, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from models.db import get_session
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from api.deps import get_current_user
from api.project_access import (
    OwnerParam,
    assert_project_access,
    journal_visible_row_positions,
    row_position_for_index,
    resolve_project,
    translate_insert_after,
)
from api.photo_locks import photo_lock
from api.project_shared import bust_project_payloads, project_cache_ref
from models.project_db import DBJournalEntry, DBProject, DBProjectItem
from src.project.project_repo import bump_lock_version
from src.billing.entitlements import ensure_storage_quota, ensure_trip_days_quota
from src.billing.usage import record_written, unlink_and_record
from src.utils.photo_paths import photo_file, photo_files, photo_folder
from src.utils.safe_fetch import fetch_bytes
from src.exceptions.errors import QuotaExceeded
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/journal", tags=["journal"])

_log = get_logger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_THUMB_SIZE = (400, 400)

# Per-file cap on a photo upload, checked before the (CPU-bound) decode/resize
# work — see api/memories.py's _MAX_PHOTO_UPLOAD_BYTES for why 25MB.
_MAX_PHOTO_UPLOAD_BYTES = 25 * 1024 * 1024
# A photo fetched from a URL is held to the same limit as an upload.
_MAX_PHOTO_FETCH_BYTES = _MAX_PHOTO_UPLOAD_BYTES


# ── Response schemas ──────────────────────────────────────────────────────────

class IDOut(BaseModel):
    id: int = Field(description="ID of the newly created resource")


class UUIDOut(BaseModel):
    uuid: str = Field(description="UUID of the uploaded photo")


class QueuedOut(BaseModel):
    queued: bool = Field(description="True when the background download was scheduled")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _photo_dir(user_id: str, journal_id: int) -> Path:
    p = Path(_DATA_DIR) / "users" / user_id / "journal" / str(journal_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_owned_journal(
    sess, journal_id: int, user_info_id: int, min_role: str = "editor"
) -> DBJournalEntry:
    """Return the entry iff the caller is its author (issue #106).

    Order matters: 404 for an unknown entry, 403 when the caller's tier on
    the parent project doesn't satisfy *min_role* (default "editor" — a
    viewer could never be an author anyway, since authoring requires editor+;
    read routes pass "viewer" for consistency), and 403 again when the entry
    belongs to another author — only the author may read/update/delete an
    entry or manage its photos. A NULL author is a legacy row owned by the
    project owner.
    """
    row = sess.get(DBJournalEntry, journal_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Journal entry not found")
    project_row = assert_project_access(sess, user_info_id, row.project_id, min_role=min_role)
    author = row.user_info_id if row.user_info_id is not None else project_row.user_info_id
    if author != user_info_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return row


def _find_by_client_token(sess, project_id: int, client_token: str) -> Optional[DBJournalEntry]:
    """Existing entry already carrying this idempotency token, if any."""
    return sess.exec(
        select(DBJournalEntry).where(
            DBJournalEntry.project_id == project_id,
            DBJournalEntry.client_token == client_token,
        )
    ).first()


def _resolve_geo(sess, project_id: int, date: str, geo_mode: str):
    if geo_mode == "custom":
        return None, None
    from models.project_db import DBActivity
    items = sess.exec(
        select(DBProjectItem).where(
            DBProjectItem.project_id == project_id,
            DBProjectItem.item_type == "activity",
        )
    ).all()
    if not items:
        return None, None
    activity_ids = [i.activity_id for i in items if i.activity_id is not None]
    if not activity_ids:
        return None, None
    act_rows = sess.exec(select(DBActivity).where(DBActivity.id.in_(activity_ids))).all()
    day_acts = [r for r in act_rows if (r.start_date_local or "")[:10] == date]
    if not day_acts:
        return None, None
    day_acts.sort(key=lambda r: r.start_date_local or "")
    if geo_mode == "start_of_day":
        row = day_acts[0]
        latlng = json.loads(row.start_latlng_json) if row.start_latlng_json else None
        if latlng and len(latlng) >= 2:
            return float(latlng[0]), float(latlng[1])
    elif geo_mode == "end_of_day":
        row = day_acts[-1]
        latlng = json.loads(row.end_latlng_json) if row.end_latlng_json else None
        if latlng and len(latlng) >= 2:
            return float(latlng[0]), float(latlng[1])
    return None, None


def _save_photo_files(user_id: str, journal_id: int, uuid_str: str, raw: bytes) -> None:
    # Decode before writing anything to disk: a corrupt/non-image upload must
    # not leave an orphaned full-res file behind with no valid thumbnail.
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid image file",
        ) from exc
    photo_path = _photo_dir(user_id, journal_id)
    full = photo_path / f"{uuid_str}.jpg"
    thumb = photo_path / f"{uuid_str}_thumb.jpg"
    full.write_bytes(raw)
    img.thumbnail(_THUMB_SIZE, Image.LANCZOS)
    img.save(str(thumb), "JPEG", quality=85)
    # Storage accounting for quota checks (issue #121) — see api/memories.py.
    # Journals are per-user, so the bytes are the writing user's own.
    record_written(user_id, full, thumb)


def _write_journal_photo(journal_id: int, uuid_str: str, order: Optional[int] = None) -> None:
    """Add *uuid_str* to a journal entry's photo list, at *order* if given, else appended.

    Guarded by a per-entry lock (issue #237) — see api/memories.py's
    _write_memory_photo for why this read-modify-write of photos_json can't
    be left unsynchronized.
    """
    with photo_lock("journal", journal_id):
        with get_session() as sess:
            row = sess.get(DBJournalEntry, journal_id)
            if row is None:
                return
            photos: List[Optional[str]] = json.loads(row.photos_json or "[]")
            if order is None:
                photos.append(uuid_str)
            else:
                if len(photos) <= order:
                    photos.extend([None] * (order + 1 - len(photos)))
                photos[order] = uuid_str
            row.photos_json = json.dumps(photos)
            sess.add(row)
            cache_ref = project_cache_ref(sess, row.project_id)
            sess.commit()
            bust_project_payloads(cache_ref)


def _download_photo_from_url(
    journal_id: int, url: str, user_id: str, project_id: Optional[int] = None, order: Optional[int] = None,
) -> None:
    try:
        # The client picks this URL: fetch it only from a public address
        # (src/utils/safe_fetch), capped like a direct upload.
        content = fetch_bytes(url, max_bytes=_MAX_PHOTO_FETCH_BYTES, total_timeout=60)
    except Exception:
        _log.exception(
            "Photo download failed for journal entry: journal_id=%s project_id=%s user_id=%s url=%s",
            journal_id, project_id, user_id, url,
        )
        return
    # Background task — no request left to answer 402 on, so it declines to store.
    try:
        with get_session() as sess:
            ensure_storage_quota(sess, int(user_id), len(content))
    except QuotaExceeded:
        _log.info("Skipped photo download for user %s: storage quota reached", user_id)
        return
    uuid_str = str(uuid_lib.uuid4())
    _save_photo_files(user_id, journal_id, uuid_str, content)
    _write_journal_photo(journal_id, uuid_str, order)


# ── Schemas ───────────────────────────────────────────────────────────────────

class JournalBody(BaseModel):
    project_name: str = Field(description="Project the journal entry belongs to")
    date: str = Field(description="Date of the entry (YYYY-MM-DD)")
    geo_mode: str = Field("start_of_day", description="How to resolve coordinates: 'start_of_day', 'end_of_day', or 'custom'")
    time: Optional[str] = Field(None, description="Optional time of day (HH:MM)")
    description: Optional[str] = Field(None, description="Journal entry text")
    lat: Optional[float] = Field(None, description="Latitude (required when geo_mode='custom')")
    lon: Optional[float] = Field(None, description="Longitude (required when geo_mode='custom')")
    insert_after_index: Optional[int] = Field(None, description="Position in the project item list to insert after")
    client_token: Optional[str] = Field(
        None, description="Idempotency token: retrying a save with the same token "
                           "returns the entry already created for it instead of a duplicate",
    )


class JournalUpdateBody(BaseModel):
    date: str = Field(description="Date of the entry (YYYY-MM-DD)")
    geo_mode: str = Field("start_of_day", description="How to resolve coordinates")
    time: Optional[str] = Field(None, description="Optional time of day (HH:MM)")
    description: Optional[str] = Field(None, description="Journal entry text")
    lat: Optional[float] = Field(None, description="Latitude (required when geo_mode='custom')")
    lon: Optional[float] = Field(None, description="Longitude (required when geo_mode='custom')")


class PhotoFromUrlIn(BaseModel):
    url: str = Field(description="Public URL of the image to download")
    order: Optional[int] = Field(
        None, description="Intended position of this photo within the entry's photo list; "
                           "preserved even if concurrent downloads complete out of order. Omit to append.",
    )


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=IDOut,
             summary="Create a journal entry")
def create_journal(
    body: JournalBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Create a new private journal entry and insert it at the requested position."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        project_row = resolve_project(sess, user_info_id, body.project_name, owner, min_role="editor")
        project_id = project_row.id
        owner_id = project_row.user_info_id

        if body.client_token is not None:
            # A retry of the same save action (client timeout, user tapping
            # Save again) resends the same token: return the entry already
            # created for it instead of a duplicate.
            existing = _find_by_client_token(sess, project_id, body.client_token)
            if existing is not None:
                return {"id": existing.id}

        # Plan limit on trip length (issue #121) — an entry dated outside the
        # trip's current span stretches it.
        ensure_trip_days_quota(sess, project_id, owner_id, body.date)

        lat, lon = body.lat, body.lon
        if body.geo_mode != "custom":
            lat, lon = _resolve_geo(sess, project_id, body.date, body.geo_mode)

        row = DBJournalEntry(
            project_id=project_id,
            user_info_id=user_info_id,
            date=body.date,
            time=body.time,
            description=body.description,
            photos_json="[]",
            geo_mode=body.geo_mode,
            lat=lat,
            lon=lon,
            client_token=body.client_token,
        )
        sess.add(row)
        try:
            # This flush is where the partial unique index actually fires the
            # INSERT (needed early to get row.id for the project item below)
            # — not the later sess.commit(). Two concurrent requests carrying
            # the same client_token can both pass the _find_by_client_token
            # check above; the loser lands here. Recover the same "already
            # created" response the check would have given had it run a
            # moment later (mirrors api/memories.py's Polarsteps race fix).
            # Any other integrity error is a real bug and must still surface.
            sess.flush()
        except IntegrityError as exc:
            sess.rollback()
            if body.client_token is None or "client_token" not in str(exc.orig):
                raise
            winner = _find_by_client_token(sess, project_id, body.client_token)
            if winner is None:
                raise
            return {"id": winner.id}

        existing_items = sess.exec(
            select(DBProjectItem)
            .where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.position)
        ).all()
        # insert_after_index is an index into the caller's *visible* item list
        # (other users' journal items are hidden) — translate it (issue #106).
        visible = journal_visible_row_positions(sess, existing_items, user_info_id, owner_id)
        insert_at = translate_insert_after(visible, body.insert_after_index, len(existing_items))
        # It lands a *list index*, and positions are not dense — a segment
        # delete removes one row without renumbering — so translate it into a
        # position value before comparing it against one.
        insert_pos = row_position_for_index(existing_items, insert_at)

        for item in existing_items:
            if item.position >= insert_pos:
                item.position += 1
                sess.add(item)

        db_item = DBProjectItem(
            project_id=project_id,
            position=insert_pos,
            uid=uuid_lib.uuid4().hex,
            item_type="journal",
            journal_id=row.id,
        )
        sess.add(db_item)
        # Make this insert visible to the optimistic lock, so a structural
        # rewrite loaded before it cannot erase the row (issue #173).
        bump_lock_version(sess, project_id)
        cache_ref = project_cache_ref(sess, project_id)
        sess.commit()
        journal_id = row.id
        bust_project_payloads(cache_ref)

    return {"id": journal_id}


@router.put("/{journal_id}", status_code=status.HTTP_204_NO_CONTENT,
            summary="Update a journal entry")
def update_journal(
    journal_id: int,
    body: JournalUpdateBody,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Update the text and metadata of an existing journal entry."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_journal(sess, journal_id, user_info_id)
        project = sess.get(DBProject, row.project_id)
        ensure_trip_days_quota(
            sess, row.project_id, project.user_info_id if project else 0, body.date
        )

        lat, lon = body.lat, body.lon
        if body.geo_mode != "custom":
            lat, lon = _resolve_geo(sess, row.project_id, body.date, body.geo_mode)

        row.date = body.date
        row.time = body.time
        row.description = body.description
        row.geo_mode = body.geo_mode
        row.lat = lat
        row.lon = lon
        sess.add(row)
        cache_ref = project_cache_ref(sess, row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)


@router.delete("/{journal_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a journal entry")
def delete_journal(
    journal_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Delete a journal entry and all its photos from disk."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_journal(sess, journal_id, user_info_id)

        photos: List[str] = json.loads(row.photos_json or "[]")
        photo_path = photo_folder(_DATA_DIR, current_user["sub"], "journal", journal_id)
        unlink_and_record(current_user["sub"], photo_files(photo_path, photos))
        if photo_path.exists():
            try:
                photo_path.rmdir()
            except OSError:
                pass

        item_row = sess.exec(
            select(DBProjectItem).where(DBProjectItem.journal_id == journal_id)
        ).first()
        if item_row:
            sess.delete(item_row)
        # Make this delete visible to the optimistic lock too: a structural
        # rewrite that loaded before it would otherwise pass the CAS and
        # re-insert the item row from its snapshot, leaving a row pointing at
        # content that no longer exists (issue #173; foreign keys are off).
        bump_lock_version(sess, row.project_id)

        cache_ref = project_cache_ref(sess, row.project_id)
        sess.delete(row)
        sess.commit()
        bust_project_payloads(cache_ref)


# ── Photos ────────────────────────────────────────────────────────────────────

@router.post("/{journal_id}/photos", status_code=status.HTTP_201_CREATED,
             response_model=UUIDOut, summary="Upload a photo")
async def upload_photo(
    journal_id: int,
    file: Annotated[UploadFile, File()],
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Upload a JPEG photo to a journal entry; a 400×400 thumbnail is generated automatically."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        _get_owned_journal(sess, journal_id, user_info_id)
    raw = await file.read()
    if len(raw) > _MAX_PHOTO_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Photo exceeds the {_MAX_PHOTO_UPLOAD_BYTES // (1024 * 1024)}MB upload limit",
        )
    with get_session() as sess:
        ensure_storage_quota(sess, user_info_id, len(raw))
    photo_uuid = str(uuid_lib.uuid4())
    # JPEG decode + LANCZOS thumbnail resize is CPU-bound; run it off the event
    # loop so one upload doesn't stall every other in-flight request (mirrors
    # the read-path thumbnail server's own move to a thread — see the
    # semaphore comment in api/memories.py — but here via FastAPI's thread
    # pool instead of a sync route, since the rest of this handler stays async).
    await run_in_threadpool(_save_photo_files, current_user["sub"], journal_id, photo_uuid, raw)
    _write_journal_photo(journal_id, photo_uuid)
    return {"uuid": photo_uuid}


@router.post("/{journal_id}/photos/from-url", status_code=202,
             response_model=QueuedOut, summary="Queue a photo download from URL")
async def queue_photo_from_url(
    journal_id: int,
    body: PhotoFromUrlIn,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Enqueue a background download of a photo from a public URL."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_journal(sess, journal_id, user_info_id)
        project_id = row.project_id
    background_tasks.add_task(
        _download_photo_from_url, journal_id, body.url, current_user["sub"], project_id, body.order,
    )
    return {"queued": True}


@router.delete("/{journal_id}/photos/{photo_uuid}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a photo")
def delete_photo(
    journal_id: int,
    photo_uuid: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Remove a photo from a journal entry and delete its files from disk."""
    user_info_id = int(current_user["sub"])
    with photo_lock("journal", journal_id), get_session() as sess:
        row = _get_owned_journal(sess, journal_id, user_info_id)
        photos: List[str] = json.loads(row.photos_json or "[]")
        if photo_uuid not in photos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

        unlink_and_record(current_user["sub"], photo_files(
            photo_folder(_DATA_DIR, current_user["sub"], "journal", journal_id), [photo_uuid]))

        photos.remove(photo_uuid)
        row.photos_json = json.dumps(photos)
        sess.add(row)
        cache_ref = project_cache_ref(sess, row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)


@router.put("/{journal_id}/photos/{old_uuid}/replace", status_code=status.HTTP_200_OK,
            response_model=UUIDOut, summary="Replace a photo")
async def replace_photo(
    journal_id: int,
    old_uuid: str,
    file: Annotated[UploadFile, File()],
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Replace a photo's bytes with a higher-quality upload, keeping its position in photos_json."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_journal(sess, journal_id, user_info_id)
        photos: List[str] = json.loads(row.photos_json or "[]")
        if old_uuid not in photos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

    raw = await file.read()
    if len(raw) > _MAX_PHOTO_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Photo exceeds the {_MAX_PHOTO_UPLOAD_BYTES // (1024 * 1024)}MB upload limit",
        )
    with get_session() as sess:
        ensure_storage_quota(sess, user_info_id, len(raw))
    new_uuid = str(uuid_lib.uuid4())
    await run_in_threadpool(_save_photo_files, current_user["sub"], journal_id, new_uuid, raw)

    with photo_lock("journal", journal_id), get_session() as sess:
        row = sess.get(DBJournalEntry, journal_id)
        photos: List[str] = json.loads(row.photos_json or "[]")
        # In-place index replacement, not remove+append: photos_json order is display order.
        photos[photos.index(old_uuid)] = new_uuid
        row.photos_json = json.dumps(photos)
        sess.add(row)
        cache_ref = project_cache_ref(sess, row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)

    unlink_and_record(current_user["sub"], photo_files(
        photo_folder(_DATA_DIR, current_user["sub"], "journal", journal_id), [old_uuid]))

    return {"uuid": new_uuid}


@router.get("/{journal_id}/photos/{photo_uuid}", summary="Serve full-resolution photo")
def serve_photo(
    journal_id: int,
    photo_uuid: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the full-resolution JPEG for a journal photo."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_journal(sess, journal_id, user_info_id, min_role="viewer")
        photos: List[str] = json.loads(row.photos_json or "[]")
        if photo_uuid not in photos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

    full_path = photo_file(
        photo_folder(_DATA_DIR, current_user["sub"], "journal", journal_id), photo_uuid)
    if full_path is None or not full_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(str(full_path), media_type="image/jpeg")


@router.get("/{journal_id}/photos/{photo_uuid}/thumb", summary="Serve photo thumbnail")
def serve_photo_thumb(
    journal_id: int,
    photo_uuid: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the 400×400 thumbnail JPEG; falls back to full-res if thumb is missing."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_journal(sess, journal_id, user_info_id, min_role="viewer")
        photos: List[str] = json.loads(row.photos_json or "[]")
        if photo_uuid not in photos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

    photo_path = photo_folder(_DATA_DIR, current_user["sub"], "journal", journal_id)
    thumb_path = photo_file(photo_path, photo_uuid, "_thumb")
    if thumb_path is None or not thumb_path.exists():
        full_path = photo_file(photo_path, photo_uuid)
        if full_path is not None and full_path.exists():
            return FileResponse(str(full_path), media_type="image/jpeg")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(str(thumb_path), media_type="image/jpeg")

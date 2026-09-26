"""REST memories endpoints — CRUD and photo management for project memories.

Routes:
    POST   /api/memories                            — create a memory
    PUT    /api/memories/{id}                       — update a memory
    DELETE /api/memories/{id}                       — delete a memory + its photos
    POST   /api/memories/{id}/photos                — upload a photo
    DELETE /api/memories/{id}/photos/{uuid}         — delete a specific photo
    PUT    /api/memories/{id}/photos/{uuid}/replace — replace a photo's bytes in place
    GET    /api/memories/{id}/photos/{uuid}         — serve full-res photo
    GET    /api/memories/{id}/photos/{uuid}/thumb   — serve thumbnail
    GET    /api/memories/{id}/comments              — list comments (threaded)
    POST   /api/memories/{id}/comments              — add a comment
    DELETE /api/memories/{id}/comments/{cid}        — delete a comment
    GET    /api/memories/{id}/likes                 — get like count and likers
    POST   /api/memories/{id}/like                  — like a memory
    DELETE /api/memories/{id}/like                  — unlike a memory
    GET    /api/memories/{id}/translations/{lang}   — get translated memory text
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import uuid as uuid_lib
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Dict, List, Optional

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
from api.translations import translate_text
from models.project_db import DBMemory, DBMemoryComment, DBMemoryLike, DBMemoryTranslation, DBProject, DBProjectItem
from models.user import UserInfo
from src.billing.entitlements import ensure_storage_quota, ensure_trip_days_quota
from src.billing.usage import record_written, unlink_and_record
from src.utils.photo_paths import photo_file, photo_files, photo_folder
from src.utils.safe_fetch import fetch_bytes
from src.exceptions.errors import QuotaExceeded
from src.models.memory import Memory
from src.project.memory_match import step_key
from src.project.project_repo import bump_lock_version
from src.utils.encryption_check import is_encrypted_envelope as _is_encrypted_envelope

router = APIRouter(prefix="/api/memories", tags=["memories"])

_log = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_THUMB_SIZE = (400, 400)

# Per-file cap on a photo upload, checked before the (CPU-bound) decode/resize
# work. The flutter_client photo picker (flutter_client/lib/src/photos) applies
# no size limit of its own, so this is the only backstop against an
# arbitrarily large upload; 25MB is generous for a phone camera JPEG (typically
# a few MB) while still bounding the memory/CPU one request can consume.
_MAX_PHOTO_UPLOAD_BYTES = 25 * 1024 * 1024
# A photo fetched from a URL is held to the same limit as an upload.
_MAX_PHOTO_FETCH_BYTES = _MAX_PHOTO_UPLOAD_BYTES

# A burst of concurrent thumbnail requests — e.g. opening the trip map for a
# photo-heavy project, which fires one request per marker with no throttling
# on the client — repeatedly OOM-killed the production API container. Sync
# routes run in FastAPI's thread pool (default capacity 40), so an unbounded
# burst piles up threads faster than the container's memory limit can
# absorb. Capping in-flight requests here trades a fast, cheap 503 for that
# crash; the client-side fix (map_panel.dart's _MarkerThumbImage) is the
# real throttle — this is a safety net for any other caller.
_THUMB_MAX_CONCURRENT = 24
_thumb_semaphore = threading.BoundedSemaphore(_THUMB_MAX_CONCURRENT)


# ── Response schemas ──────────────────────────────────────────────────────────

class IDOut(BaseModel):
    id: int = Field(description="ID of the newly created resource")


class UUIDOut(BaseModel):
    uuid: str = Field(description="UUID of the uploaded photo")


class QueuedOut(BaseModel):
    queued: bool = Field(description="True when the background download was scheduled")


class LikerOut(BaseModel):
    name: str = Field(description="Display name of the user who liked this memory")
    user_info_id: int = Field(description="Internal user ID")


class LikesOut(BaseModel):
    count: int = Field(description="Total number of likes")
    liked_by_me: bool = Field(description="True if the authenticated user has liked this memory")
    likers: List[LikerOut] = Field(description="Users who liked this memory")


class CommentOut(BaseModel):
    id: int = Field(description="Comment ID")
    user_info_id: int = Field(description="Author's internal user ID")
    commenter_name: str = Field(description="Display name of the commenter")
    text: str = Field(description="Comment body")
    created_at: str = Field(description="ISO-8601 UTC timestamp")
    replies: List["CommentOut"] = Field(default_factory=list, description="Nested replies")


class TranslationOut(BaseModel):
    lang_code: str = Field(description="BCP-47 language code, e.g. 'fr' or 'de'")
    name: Optional[str] = Field(None, description="Translated memory name")
    description: Optional[str] = Field(None, description="Translated memory description")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _photo_dir(user_id: str, memory_id: int) -> Path:
    p = Path(_DATA_DIR) / "users" / user_id / "memories" / str(memory_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _owner_id_of(sess, project_id: int) -> int:
    """The trip owner's user id — whose plan the trip's limits come from."""
    project = sess.get(DBProject, project_id)
    return project.user_info_id if project else 0


def _owner_dir_id(sess, mem_row: DBMemory) -> str:
    """Canonical photo-dir key for a memory: the project OWNER's user id.

    With travel companions (issue #106) the caller may not be the owner, but
    all editors and viewers must resolve the same directory — keying on the
    caller would scatter one memory's photos across several users' dirs.
    ``sess.get`` hits the identity map, so this is free after
    ``assert_project_access``/``resolve_project`` already loaded the project.
    """
    return str(sess.get(DBProject, mem_row.project_id).user_info_id)


def _resolve_geo(
    sess, project_id: int, date: str, geo_mode: str
) -> tuple[Optional[float], Optional[float]]:
    """Resolve (lat, lon) from activities on *date* according to *geo_mode*."""
    if geo_mode == "custom":
        return None, None

    items = sess.exec(
        select(DBProjectItem).where(
            DBProjectItem.project_id == project_id,
            DBProjectItem.item_type == "activity",
        )
    ).all()
    if not items:
        return None, None

    from models.project_db import DBActivity
    activity_ids = [i.activity_id for i in items if i.activity_id is not None]
    if not activity_ids:
        return None, None

    act_rows = sess.exec(
        select(DBActivity).where(DBActivity.id.in_(activity_ids))
    ).all()

    def _date_of(row) -> str:
        return (row.start_date_local or "")[:10]

    day_acts = [r for r in act_rows if _date_of(r) == date]
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


def _row_to_memory(row: DBMemory) -> Memory:
    return Memory(
        id=row.id,
        public_id=row.public_id,
        project_id=row.project_id,
        name=row.name,
        date=row.date,
        time=row.time,
        description=row.description,
        # Falsy entries are unfilled placeholders for a from-url download that
        # hasn't landed yet (or never will) — see _write_memory_photo.
        photos=[p for p in json.loads(row.photos_json or "[]") if p],
        geo_mode=row.geo_mode,
        lat=row.lat,
        lon=row.lon,
    )


def _get_owned_memory(sess, memory_id: int, user_info_id: int, min_role: str = "editor") -> DBMemory:
    """Return the DBMemory row, verifying the caller's tier on the parent
    project satisfies *min_role* (default "editor" — most callers mutate;
    read routes pass "viewer")."""
    mem_row = sess.get(DBMemory, memory_id)
    if mem_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    assert_project_access(sess, user_info_id, mem_row.project_id, min_role=min_role)
    return mem_row


# ── CRUD ─────────────────────────────────────────────────────────────────────

def _resolve_body_geo(sess, project_id: int, body: "MemoryBody") -> tuple[Optional[float], Optional[float]]:
    """(lat, lon) for a memory body: explicit coords for 'custom', else derived
    from the day's activities — the same resolution the insert path uses."""
    if body.geo_mode == "custom":
        return body.lat, body.lon
    return _resolve_geo(sess, project_id, body.date, body.geo_mode)


def _find_by_step_id(sess, project_id: int, step_id: int) -> Optional[DBMemory]:
    """Existing memory already carrying this Polarsteps step id, if any."""
    return sess.exec(
        select(DBMemory).where(
            DBMemory.project_id == project_id,
            DBMemory.polarsteps_step_id == step_id,
        )
    ).first()


def _find_namedate_match(sess, project_id: int, body: "MemoryBody") -> Optional[DBMemory]:
    """A pre-step-id memory (``polarsteps_step_id`` NULL) whose normalized
    name+date equals this step's — the duplicate the old write path couldn't see.
    Filtered in Python so it shares ``step_key`` normalization with the read path
    (trailing-space / empty-vs-NULL name); per-project memory counts are small."""
    target = step_key(body.name, body.date)
    candidates = sess.exec(
        select(DBMemory).where(
            DBMemory.project_id == project_id,
            DBMemory.polarsteps_step_id.is_(None),
        )
    ).all()
    for row in candidates:
        if step_key(row.name, row.date) == target:
            return row
    return None


def _adopt_and_refresh(sess, user_id: str, mem_row: DBMemory, body: "MemoryBody") -> int:
    """Adopt an existing memory for a Polarsteps re-import: backfill the step id,
    overwrite scalar fields from the step (Polarsteps is source of truth), and
    clear photos so the client's re-upload repopulates a fresh set. Returns the
    memory id; never creates a second row or project item."""
    lat, lon = _resolve_body_geo(sess, mem_row.project_id, body)
    mem_row.name = body.name
    mem_row.date = body.date
    mem_row.time = body.time
    mem_row.description = body.description
    mem_row.geo_mode = body.geo_mode
    mem_row.lat = lat
    mem_row.lon = lon
    mem_row.polarsteps_step_id = body.polarsteps_step_id
    _clear_memory_photos(sess, user_id, mem_row)
    sess.add(mem_row)
    cache_ref = project_cache_ref(sess, mem_row.project_id)
    sess.commit()
    bust_project_payloads(cache_ref)
    return mem_row.id


class MemoryBody(BaseModel):
    project_name: str = Field(description="Project the memory belongs to")
    date: str = Field(description="Date of the memory (YYYY-MM-DD)")
    geo_mode: str = Field("start_of_day", description="How to resolve coordinates: 'start_of_day', 'end_of_day', or 'custom'")
    name: Optional[str] = Field(None, description="Optional memory title")
    time: Optional[str] = Field(None, description="Optional time of day (HH:MM)")
    description: Optional[str] = Field(None, description="Free-text notes")
    lat: Optional[float] = Field(None, description="Latitude (required when geo_mode='custom')")
    lon: Optional[float] = Field(None, description="Longitude (required when geo_mode='custom')")
    insert_after_index: Optional[int] = Field(None, description="Position in the project item list to insert after")
    polarsteps_step_id: Optional[int] = Field(None, description="Polarsteps step ID for deduplication during import")


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=IDOut,
             summary="Create a memory")
def create_memory(
    body: MemoryBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Create a new memory in a project and insert it at the requested position."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        project_row = resolve_project(sess, user_info_id, body.project_name, owner, min_role="editor")
        project_id = project_row.id
        owner_id = project_row.user_info_id

        if body.polarsteps_step_id is not None:
            # Exact step-id re-import is idempotent: return the existing row
            # untouched so retries never wipe already-uploaded photos.
            by_id = _find_by_step_id(sess, project_id, body.polarsteps_step_id)
            if by_id is not None:
                return {"id": by_id.id}
            # Pre-step-id duplicate (NULL step id, same name+date): adopt it —
            # backfill the step id and refresh from the step — instead of
            # creating a second copy. This is the split-brain fix.
            adopt = _find_namedate_match(sess, project_id, body)
            if adopt is not None:
                return {"id": _adopt_and_refresh(sess, str(owner_id), adopt, body)}

        # Plan limit on trip length (issue #121) — a memory dated outside the
        # trip's current span stretches it.
        ensure_trip_days_quota(sess, project_id, owner_id, body.date)

        lat, lon = _resolve_body_geo(sess, project_id, body)

        mem_row = DBMemory(
            project_id=project_id,
            name=body.name,
            date=body.date,
            time=body.time,
            description=body.description,
            photos_json="[]",
            geo_mode=body.geo_mode,
            lat=lat,
            lon=lon,
            polarsteps_step_id=body.polarsteps_step_id,
        )
        sess.add(mem_row)
        try:
            # This flush is where the partial unique index actually fires the
            # INSERT (needed early to get mem_row.id for the project item
            # below) — not the later sess.commit(). Two concurrent requests
            # for the same Polarsteps step can both pass the _find_by_step_id
            # check above; the loser lands here. Recover the same
            # "already imported" response the check would have given had it
            # run a moment later. Any other integrity error is a real bug and
            # must still surface.
            sess.flush()
        except IntegrityError as exc:
            sess.rollback()
            if body.polarsteps_step_id is None or "polarsteps_step_id" not in str(exc.orig):
                raise
            winner = _find_by_step_id(sess, project_id, body.polarsteps_step_id)
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
            item_type="memory",
            memory_id=mem_row.id,
        )
        sess.add(db_item)
        # Make this insert visible to the optimistic lock, so a structural
        # rewrite loaded before it cannot erase the row (issue #173).
        bump_lock_version(sess, project_id)
        cache_ref = project_cache_ref(sess, project_id)
        sess.commit()
        memory_id = mem_row.id
        bust_project_payloads(cache_ref)

    return {"id": memory_id}


class MemoryUpdateBody(BaseModel):
    date: str = Field(description="Date of the memory (YYYY-MM-DD)")
    geo_mode: str = Field("start_of_day", description="How to resolve coordinates")
    name: Optional[str] = Field(None, description="Optional memory title")
    time: Optional[str] = Field(None, description="Optional time of day (HH:MM)")
    description: Optional[str] = Field(None, description="Free-text notes")
    lat: Optional[float] = Field(None, description="Latitude (required when geo_mode='custom')")
    lon: Optional[float] = Field(None, description="Longitude (required when geo_mode='custom')")


@router.put("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT,
            summary="Update a memory")
def update_memory(
    memory_id: int,
    body: MemoryUpdateBody,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Update the metadata of an existing memory."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        ensure_trip_days_quota(
            sess, mem_row.project_id, _owner_id_of(sess, mem_row.project_id), body.date
        )

        lat, lon = body.lat, body.lon
        if body.geo_mode != "custom":
            lat, lon = _resolve_geo(sess, mem_row.project_id, body.date, body.geo_mode)

        mem_row.name = body.name
        mem_row.date = body.date
        mem_row.time = body.time
        mem_row.description = body.description
        mem_row.geo_mode = body.geo_mode
        mem_row.lat = lat
        mem_row.lon = lon
        sess.add(mem_row)

        # Once a memory's content becomes ciphertext, any previously-cached
        # translation of the old plaintext is a plaintext-adjacent artifact that
        # would otherwise survive encryption unprotected — purge it (issue #27).
        if _is_encrypted_envelope(body.name) or _is_encrypted_envelope(body.description):
            for row in sess.exec(
                select(DBMemoryTranslation).where(DBMemoryTranslation.memory_id == memory_id)
            ).all():
                sess.delete(row)

        cache_ref = project_cache_ref(sess, mem_row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a memory")
def delete_memory(
    memory_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Delete a memory and all its photos from disk."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)

        photos: List[str] = json.loads(mem_row.photos_json or "[]")
        owner_dir = _owner_dir_id(sess, mem_row)
        photo_path = photo_folder(_DATA_DIR, owner_dir, "memories", memory_id)
        unlink_and_record(owner_dir, photo_files(photo_path, photos))
        if photo_path.exists():
            try:
                photo_path.rmdir()
            except OSError:
                pass

        item_rows = sess.exec(
            select(DBProjectItem).where(
                DBProjectItem.memory_id == memory_id
            )
        ).all()
        for item_row in item_rows:
            sess.delete(item_row)

        # Make this delete visible to the optimistic lock too: a structural
        # rewrite that loaded before it would otherwise pass the CAS and
        # re-insert the item row from its snapshot, leaving a row pointing at
        # content that no longer exists (issue #173; foreign keys are off).
        bump_lock_version(sess, mem_row.project_id)
        cache_ref = project_cache_ref(sess, mem_row.project_id)
        sess.delete(mem_row)
        sess.commit()
        bust_project_payloads(cache_ref)


# ── Photos ────────────────────────────────────────────────────────────────────

def _save_photo_files(user_id: str, memory_id: int, uuid_str: str, raw: bytes) -> None:
    # Decode before writing anything to disk: a corrupt/non-image upload must
    # not leave an orphaned full-res file behind with no valid thumbnail.
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid image file",
        ) from exc
    photo_path = _photo_dir(user_id, memory_id)
    full = photo_path / f"{uuid_str}.jpg"
    thumb = photo_path / f"{uuid_str}_thumb.jpg"
    full.write_bytes(raw)
    img.thumbnail(_THUMB_SIZE, Image.LANCZOS)
    img.save(str(thumb), "JPEG", quality=85)
    # Storage accounting for quota checks (issue #121). Done here rather than at
    # each call site so every path that writes a photo — upload, replace,
    # from-url, Polarsteps import — is counted by construction. Attribution
    # follows the directory: photos live under the project OWNER's tree.
    record_written(user_id, full, thumb)


def _delete_photo_files(user_id: str, memory_id: int, photo_uuids: List[str]) -> None:
    """Remove the on-disk full-res + thumbnail files for the given photo UUIDs."""
    unlink_and_record(user_id, photo_files(
        photo_folder(_DATA_DIR, user_id, "memories", memory_id), photo_uuids))


def _clear_memory_photos(sess, user_id: str, mem_row: DBMemory) -> None:
    """Drop all photos from *mem_row*: delete files and reset ``photos_json``.

    Used when a Polarsteps re-import adopts an existing memory and refreshes it,
    so the client's subsequent ``from-url`` uploads repopulate a clean set rather
    than appending duplicates onto the previously imported photos.
    """
    existing: List[str] = json.loads(mem_row.photos_json or "[]")
    if existing:
        _delete_photo_files(user_id, mem_row.id, existing)
    mem_row.photos_json = "[]"


def _write_memory_photo(memory_id: int, uuid_str: str, order: Optional[int] = None) -> None:
    """Add *uuid_str* to a memory's photo list, at *order* if given, else appended.

    Guarded by a per-memory lock (issue #237): Polarsteps import fires many
    of these concurrently as background downloads complete, so without
    synchronizing this read-modify-write of ``photos_json`` two overlapping
    calls could clobber each other's update. *order* is the photo's intended
    position (e.g. its index in the source album) — placing it there instead
    of always appending means the final list reflects that intended order
    even when downloads complete out of order. A gap left by a
    still-in-flight or failed slot is a falsy placeholder, filtered out
    wherever photos_json is read back for a client.
    """
    with photo_lock("memory", memory_id):
        with get_session() as sess:
            mem_row = sess.get(DBMemory, memory_id)
            if mem_row is None:
                return
            photos: List[Optional[str]] = json.loads(mem_row.photos_json or "[]")
            if order is None:
                photos.append(uuid_str)
            else:
                if len(photos) <= order:
                    photos.extend([None] * (order + 1 - len(photos)))
                photos[order] = uuid_str
            mem_row.photos_json = json.dumps(photos)
            sess.add(mem_row)
            cache_ref = project_cache_ref(sess, mem_row.project_id)
            sess.commit()
            bust_project_payloads(cache_ref)


def _download_photo_from_url(
    memory_id: int, url: str, user_id: str, project_id: Optional[int] = None, order: Optional[int] = None,
) -> None:
    try:
        # The client picks this URL: fetch it only from a public address
        # (src/utils/safe_fetch), capped like a direct upload.
        content = fetch_bytes(url, max_bytes=_MAX_PHOTO_FETCH_BYTES, total_timeout=60)
    except Exception:
        _log.exception(
            "Photo download failed for memory: memory_id=%s project_id=%s user_id=%s url=%s",
            memory_id, project_id, user_id, url,
        )
        return
    # Same quota rule as a direct upload, but this runs in a background task —
    # there is no request left to answer 402 on, so it just declines to store.
    try:
        with get_session() as sess:
            ensure_storage_quota(sess, int(user_id), len(content))
    except QuotaExceeded:
        _log.info("Skipped photo download for user %s: storage quota reached", user_id)
        return
    uuid_str = str(uuid_lib.uuid4())
    _save_photo_files(user_id, memory_id, uuid_str, content)
    _write_memory_photo(memory_id, uuid_str, order)


class PhotoFromUrlIn(BaseModel):
    url: str = Field(description="Public URL of the image to download")
    order: Optional[int] = Field(
        None, description="Intended position of this photo within the memory's photo list "
                           "(e.g. its index in a Polarsteps step); preserved even if concurrent "
                           "downloads complete out of order. Omit to append.",
    )


@router.post("/{memory_id}/photos", status_code=status.HTTP_201_CREATED,
             response_model=UUIDOut, summary="Upload a photo")
async def upload_photo(
    memory_id: int,
    file: Annotated[UploadFile, File()],
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Upload a JPEG photo; a 400×400 thumbnail is generated automatically."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        owner_dir = _owner_dir_id(sess, mem_row)
    raw = await file.read()
    if len(raw) > _MAX_PHOTO_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Photo exceeds the {_MAX_PHOTO_UPLOAD_BYTES // (1024 * 1024)}MB upload limit",
        )
    # The bytes land in the owner's tree, so it is the owner's quota that
    # applies — a companion uploading to a shared trip spends the owner's space.
    with get_session() as sess:
        ensure_storage_quota(sess, int(owner_dir), len(raw))
    photo_uuid = str(uuid_lib.uuid4())
    # JPEG decode + LANCZOS thumbnail resize is CPU-bound; run it off the event
    # loop so one upload doesn't stall every other in-flight request (mirrors
    # the read-path thumbnail server's own move to a thread — see the
    # semaphore comment above — but here via FastAPI's thread pool instead of
    # a sync route, since the rest of this handler needs to stay async).
    await run_in_threadpool(_save_photo_files, owner_dir, memory_id, photo_uuid, raw)
    _write_memory_photo(memory_id, photo_uuid)
    return {"uuid": photo_uuid}


@router.post("/{memory_id}/photos/from-url", status_code=202,
             response_model=QueuedOut, summary="Queue a photo download from URL")
async def queue_photo_from_url(
    memory_id: int,
    body: PhotoFromUrlIn,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Enqueue a background download of a photo from a public URL."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        owner_dir = _owner_dir_id(sess, mem_row)
        project_id = mem_row.project_id
    background_tasks.add_task(
        _download_photo_from_url, memory_id, body.url, owner_dir, project_id, body.order,
    )
    return {"queued": True}


@router.delete("/{memory_id}/photos/{photo_uuid}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a photo")
def delete_photo(
    memory_id: int,
    photo_uuid: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Remove a photo from a memory and delete its files from disk."""
    user_info_id = int(current_user["sub"])
    with photo_lock("memory", memory_id), get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        photos: List[str] = json.loads(mem_row.photos_json or "[]")
        if photo_uuid not in photos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

        _delete_photo_files(_owner_dir_id(sess, mem_row), memory_id, [photo_uuid])

        photos.remove(photo_uuid)
        mem_row.photos_json = json.dumps(photos)
        sess.add(mem_row)
        cache_ref = project_cache_ref(sess, mem_row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)


@router.put("/{memory_id}/photos/{old_uuid}/replace", status_code=status.HTTP_200_OK,
            response_model=UUIDOut, summary="Replace a photo")
async def replace_photo(
    memory_id: int,
    old_uuid: str,
    file: Annotated[UploadFile, File()],
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Replace a photo's bytes with a higher-quality upload, keeping its position in photos_json."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        owner_dir = _owner_dir_id(sess, mem_row)
        photos: List[str] = json.loads(mem_row.photos_json or "[]")
        if old_uuid not in photos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

    raw = await file.read()
    if len(raw) > _MAX_PHOTO_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Photo exceeds the {_MAX_PHOTO_UPLOAD_BYTES // (1024 * 1024)}MB upload limit",
        )
    with get_session() as sess:
        ensure_storage_quota(sess, int(owner_dir), len(raw))
    new_uuid = str(uuid_lib.uuid4())
    await run_in_threadpool(_save_photo_files, owner_dir, memory_id, new_uuid, raw)

    with photo_lock("memory", memory_id), get_session() as sess:
        mem_row = sess.get(DBMemory, memory_id)
        photos: List[str] = json.loads(mem_row.photos_json or "[]")
        # In-place index replacement, not remove+append: photos_json order is display order.
        photos[photos.index(old_uuid)] = new_uuid
        mem_row.photos_json = json.dumps(photos)
        sess.add(mem_row)
        cache_ref = project_cache_ref(sess, mem_row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)

    _delete_photo_files(owner_dir, memory_id, [old_uuid])
    return {"uuid": new_uuid}


@router.get("/{memory_id}/photos/{photo_uuid}", summary="Serve full-resolution photo")
def serve_photo(
    memory_id: int,
    photo_uuid: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the full-resolution JPEG for a memory photo."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id, min_role="viewer")
        owner_dir = _owner_dir_id(sess, mem_row)
        photos: List[str] = json.loads(mem_row.photos_json or "[]")
        if photo_uuid not in photos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

    full_path = photo_file(photo_folder(_DATA_DIR, owner_dir, "memories", memory_id), photo_uuid)
    if full_path is None or not full_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(str(full_path), media_type="image/jpeg")


@router.get("/{memory_id}/photos/{photo_uuid}/thumb", summary="Serve photo thumbnail")
def serve_photo_thumb(
    memory_id: int,
    photo_uuid: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the 400×400 thumbnail JPEG; falls back to full-res if thumb is missing."""
    if not _thumb_semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server busy, retry shortly",
        )
    try:
        user_info_id = int(current_user["sub"])
        with get_session() as sess:
            mem_row = _get_owned_memory(sess, memory_id, user_info_id, min_role="viewer")
            owner_dir = _owner_dir_id(sess, mem_row)
            photos: List[str] = json.loads(mem_row.photos_json or "[]")
            if photo_uuid not in photos:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

        photo_path = photo_folder(_DATA_DIR, owner_dir, "memories", memory_id)
        thumb_path = photo_file(photo_path, photo_uuid, "_thumb")
        if thumb_path is None or not thumb_path.exists():
            full_path = photo_file(photo_path, photo_uuid)
            if full_path is not None and full_path.exists():
                return FileResponse(str(full_path), media_type="image/jpeg")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
        return FileResponse(str(thumb_path), media_type="image/jpeg")
    finally:
        _thumb_semaphore.release()


# ── Comments ──────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_comment_tree(rows: List[DBMemoryComment]) -> List[Dict]:
    """Convert flat comment rows into a fully recursive tree."""
    by_id: Dict[int, Dict] = {}
    for r in rows:
        by_id[r.id] = {
            "id": r.id,
            "user_info_id": r.user_info_id,
            "commenter_name": r.commenter_name,
            "text": r.text,
            "created_at": r.created_at,
            "replies": [],
        }
    roots: List[Dict] = []
    for r in rows:
        node = by_id[r.id]
        if r.parent_comment_id is not None and r.parent_comment_id in by_id:
            by_id[r.parent_comment_id]["replies"].append(node)
        else:
            roots.append(node)
    return roots


class CommentBody(BaseModel):
    text: str = Field(description="Comment body text")
    parent_comment_id: Optional[int] = Field(None, description="ID of the parent comment for threaded replies")


@router.get("/{memory_id}/comments", response_model=List[CommentOut],
            summary="List comments")
def list_comments(
    memory_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return all comments on a memory as a recursive tree (replies nested under parents)."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        _get_owned_memory(sess, memory_id, user_info_id, min_role="viewer")
        rows = sess.exec(
            select(DBMemoryComment)
            .where(DBMemoryComment.memory_id == memory_id)
            .order_by(DBMemoryComment.created_at)
        ).all()
        return _build_comment_tree(list(rows))


@router.post("/{memory_id}/comments", status_code=status.HTTP_201_CREATED,
             response_model=IDOut, summary="Add a comment")
def add_comment(
    memory_id: int,
    body: CommentBody,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Add a top-level comment or a threaded reply to an existing comment."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        user_row = sess.get(UserInfo, user_info_id)
        commenter_name = user_row.display_name if user_row else ""

        if body.parent_comment_id is not None:
            parent = sess.get(DBMemoryComment, body.parent_comment_id)
            if parent is None or parent.memory_id != memory_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid parent comment")

        row = DBMemoryComment(
            memory_id=memory_id,
            parent_comment_id=body.parent_comment_id,
            user_info_id=user_info_id,
            commenter_name=commenter_name,
            text=body.text,
            created_at=_utc_now(),
        )
        sess.add(row)
        # The project payload carries each memory's comment_count (issue #178).
        cache_ref = project_cache_ref(sess, mem_row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)
        return {"id": row.id}


@router.delete("/{memory_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a comment")
def delete_comment(
    memory_id: int,
    comment_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Delete a comment and all its replies. Allowed for the comment author or project owner."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        comment_row = sess.get(DBMemoryComment, comment_id)
        if comment_row is None or comment_row.memory_id != memory_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")

        project_row = sess.get(DBProject, mem_row.project_id)
        is_owner = project_row is not None and project_row.user_info_id == user_info_id
        is_author = comment_row.user_info_id == user_info_id
        if not (is_owner or is_author):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        _delete_comment_subtree(sess, comment_id)
        cache_ref = project_cache_ref(sess, mem_row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)


def _delete_comment_subtree(sess, comment_id: int) -> None:
    """Delete a comment and all its descendants (BFS)."""
    queue = [comment_id]
    while queue:
        current = queue.pop(0)
        children = sess.exec(
            select(DBMemoryComment).where(DBMemoryComment.parent_comment_id == current)
        ).all()
        for child in children:
            queue.append(child.id)
            sess.delete(child)
        row = sess.get(DBMemoryComment, current)
        if row is not None:
            sess.delete(row)


# ── Likes ─────────────────────────────────────────────────────────────────────

@router.get("/{memory_id}/likes", response_model=LikesOut, summary="Get likes")
def get_likes(
    memory_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the like count, whether the caller has liked this memory, and the list of likers."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        _get_owned_memory(sess, memory_id, user_info_id, min_role="viewer")
        like_rows = sess.exec(
            select(DBMemoryLike).where(DBMemoryLike.memory_id == memory_id)
        ).all()
        liked_by_me = any(r.user_info_id == user_info_id for r in like_rows)
        return {
            "count": len(like_rows),
            "liked_by_me": liked_by_me,
            "likers": [{"name": r.liker_name, "user_info_id": r.user_info_id} for r in like_rows],
        }


@router.post("/{memory_id}/like", status_code=status.HTTP_204_NO_CONTENT,
             summary="Like a memory")
def like_memory(
    memory_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Like a memory. Idempotent — calling this twice has no effect."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        existing = sess.exec(
            select(DBMemoryLike).where(
                DBMemoryLike.memory_id == memory_id,
                DBMemoryLike.user_info_id == user_info_id,
            )
        ).first()
        if existing:
            return
        user_row = sess.get(UserInfo, user_info_id)
        liker_name = user_row.display_name if user_row else ""
        sess.add(DBMemoryLike(
            memory_id=memory_id,
            user_info_id=user_info_id,
            liker_name=liker_name,
            created_at=_utc_now(),
        ))
        # The project payload carries each memory's like_count (issue #178).
        cache_ref = project_cache_ref(sess, mem_row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)


@router.delete("/{memory_id}/like", status_code=status.HTTP_204_NO_CONTENT,
               summary="Unlike a memory")
def unlike_memory(
    memory_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Remove the caller's like from a memory. No-op if not liked."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id)
        existing = sess.exec(
            select(DBMemoryLike).where(
                DBMemoryLike.memory_id == memory_id,
                DBMemoryLike.user_info_id == user_info_id,
            )
        ).first()
        if existing:
            cache_ref = project_cache_ref(sess, mem_row.project_id)
            sess.delete(existing)
            sess.commit()
            bust_project_payloads(cache_ref)


# ── Translations ──────────────────────────────────────────────────────────────

import json as _json


@router.get("/{memory_id}/translations/{lang_code}", response_model=TranslationOut,
            summary="Get memory translation")
async def get_translation(
    memory_id: int,
    lang_code: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return a translated version of the memory's name and description.

    Translations are generated on first request via Google Translate and cached
    in the database. Subsequent calls for the same memory + language return
    instantly from cache. The language must be enabled in the project's settings.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        mem_row = _get_owned_memory(sess, memory_id, user_info_id, min_role="viewer")
        if _is_encrypted_envelope(mem_row.name) or _is_encrypted_envelope(mem_row.description):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot translate an encrypted memory",
            )
        project_row = sess.get(DBProject, mem_row.project_id)
        allowed_langs = _json.loads(getattr(project_row, 'languages_json', None) or "[]")
        if lang_code not in allowed_langs:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Language not enabled for this project")

        cached = sess.exec(
            select(DBMemoryTranslation).where(
                DBMemoryTranslation.memory_id == memory_id,
                DBMemoryTranslation.lang_code == lang_code,
            )
        ).first()
        if cached:
            return {"lang_code": lang_code, "name": cached.name, "description": cached.description}

    try:
        translated_name = await translate_text(mem_row.name, lang_code) if mem_row.name else None
        translated_desc = await translate_text(mem_row.description, lang_code) if mem_row.description else None
    except Exception as exc:
        _log.exception(
            "Translation failed for memory %s -> %s", memory_id, lang_code
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Translation service error: {exc}") from exc

    with get_session() as sess:
        row = DBMemoryTranslation(
            memory_id=memory_id,
            lang_code=lang_code,
            name=translated_name,
            description=translated_desc,
            created_at=_utc_now(),
        )
        sess.add(row)
        sess.commit()

    return {"lang_code": lang_code, "name": translated_name, "description": translated_desc}

"""REST project import/export endpoints — .traxj upload, GPX/.traxj/ZIP download.

Routes:
    POST   /api/projects/import              — upload a .traxj file
    POST   /api/projects/import-zip          — upload a ZIP (.traxj + photos)
    GET    /api/projects/{name}/export       — download project as GPX file
    GET    /api/projects/{name}/export-traxj — download project as .traxj JSON
    GET    /api/projects/{name}/export-zip   — download ZIP (.traxj + photos)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import IO, Annotated, Any, Dict, Iterator, List, Literal, Optional, Tuple

import anyio
import gpxpy
import gpxpy.gpx
import polyline as polyline_lib
from models.db import get_session

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, status,
)
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlmodel import select
from starlette.concurrency import run_in_threadpool

from api.deps import get_current_user
from api.geo import bust_geo_cache
from api.photo_locks import photo_lock
from api.project_access import OwnerParam, resolve_project
import api.project_shared as project_shared
from api.project_shared import (
    _DATA_DIR, _repo, bust_project_payloads, project_cache_ref,
    queue_share_tiles_refresh, queue_stats_refresh,
)
from models.project_db import DBJournalEntry, DBMemory, DBProject
from src.billing.entitlements import ensure_project_quota, ensure_storage_quota
from src.billing.usage import unlink_and_record
from src.brand import APP_NAME
from src.models.great_circle import great_circle_points
from src.models.project import Project
from src.project.photo_placement import already_present, place_photos
from src.project.project_io import InvalidProjectFile, ProjectIO
from src.project.repo_transfer import Placement, PhotoRemoval, ProjectNameTaken
from src.project.staged_photos import StagedPhotos, staged_total
from src.project.zip_import import MANIFEST_NAME, InvalidTripArchive, read_trip_zip
from src.utils.logging import get_logger, request_id_var
from src.utils.encryption_check import is_encrypted_envelope
from src.utils.photo_paths import photo_file, photo_files, photo_folder
from src.utils.photo_privacy import remove_share_copy

router = APIRouter(prefix="/api/projects", tags=["projects"])

_log = get_logger(__name__)


# ── Response schemas ──────────────────────────────────────────────────────────

class ImportedOut(BaseModel):
    name: str = Field(description="Name of the imported project")
    outcome: Literal["created", "copied", "replaced"] = Field(
        description="created: a new trip under the file's name; copied: a new "
                    "trip under a de-duplicated name, the file's name being "
                    "taken; replaced: the content of the trip of that name "
                    "was overwritten with the file's")


# ── Import ────────────────────────────────────────────────────────────────────

#: Largest project file the import accepts, whatever the plan or billing
#: settings (issue #434). An export carries every track at full resolution,
#: written compact since #454. Per GPS point (encoded polyline plus the
#: elevation profile's distance/elevation pair), measured on generated trips:
#: ~18.5 bytes for a Strava activity, ~28 for a GPX upload with 0.1 m
#: elevations, ~40 for one with unrounded or interpolated elevations (a GPX
#: profile keeps cumulative distances at full float precision). So 50 MB holds
#: roughly 2.8, 1.9 or 1.3 million points. Indented, every point took ~22
#: bytes more.
#:
#: The import holds the file and its parsed form at once. Measured with
#: tracemalloc around ProjectIO.from_bytes on a 200,000-point trip, file bytes
#: included, that is ~5.7x the file for compact JSON (it was ~3.7x indented:
#: less whitespace per value parsed), so a 50 MB file peaks near 285 MB. The
#: same trip exported compact is half the size, so any given trip now needs
#: less memory to import than before; only a file at the cap needs more. That
#: fits the API container's 768 MB limit (docker-compose.yml.example) beside
#: the running process, its geo caches and ordinary requests, but not twice
#: over: two maximum-size imports at once would leave little room. 100 MB
#: would peak near 570 MB, too close to the limit on its own.
MAX_IMPORT_BYTES = 50 * 1024 * 1024

#: Largest trip ZIP (trip file plus photos) the ZIP import accepts (#469, owner
#: decision). Unlike a ``.traxj``, the upload is never held in memory:
#: Starlette spools it to a temp file, and the archive's entries are read from
#: there one at a time (src/project/zip_import.py). Its trip file is still
#: parsed in memory, so it keeps MAX_IMPORT_BYTES as its own limit, and photos
#: are decoded one at a time. So this bounds disk, not memory: the spooled
#: upload plus the photos staged from it, about twice this at worst.
MAX_ZIP_IMPORT_BYTES = 1024 * 1024 * 1024

#: Room for the multipart envelope (boundaries, part headers) around the file.
_MULTIPART_ALLOWANCE = 64 * 1024

_ZIP_EXTENSION = ".zip"

#: Staging directories older than this were left by a crash: an import holds
#: its own for minutes at most, and removes it when it ends (#469).
_STALE_STAGING_SECONDS = 24 * 60 * 60

#: Prefix of a staging directory's name under ``<data dir>/tmp/``.
_STAGING_PREFIX = "import-"

#: One trip import at a time, ``.traxj`` or ZIP (#469). An import's peak memory
#: (a trip file of up to MAX_IMPORT_BYTES parsed, ~5.7x its size) fits the API
#: container beside the running process only once, and a ZIP import also
#: stages up to MAX_ZIP_IMPORT_BYTES on the data volume. Taken without waiting
#: by the import routes before any of the body is received, so a second import
#: is refused at once rather than queued holding its upload open. The API runs
#: as a single process, so a process-wide semaphore covers every import.
_import_guard = threading.BoundedSemaphore(1)

#: Longest wait, in seconds, for the next piece of an import's body. The
#: import guard is held while the body arrives, and neither uvicorn nor the
#: reverse proxy limits how slowly a body may be sent, so a stalled or
#: trickling upload would keep every other import refused (owner decision,
#: #469 review). A working connection sends something well within a minute.
UPLOAD_IDLE_TIMEOUT_SECONDS = 60

#: Longest time, in seconds, an import's whole body may take to arrive, for
#: the same reason: a body that keeps trickling is never idle. 30 minutes
#: fits the 1 GB ZIP cap at about 4.7 Mbit/s, and a 50 MB .traxj at well
#: under 1 Mbit/s.
UPLOAD_TOTAL_TIMEOUT_SECONDS = 30 * 60


def _size_text(limit: int) -> str:
    gb = 1024 * 1024 * 1024
    if limit >= gb and limit % gb == 0:
        return f"{limit // gb} GB"
    return f"{limit // (1024 * 1024)} MB"


def _too_large(limit: Optional[int] = None) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=(f"This file is too large to import. The limit is "
                f"{_size_text(MAX_IMPORT_BYTES if limit is None else limit)}."),
    )


def _upload_too_slow() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_408_REQUEST_TIMEOUT,
        detail="The upload was too slow or stalled, so the import was stopped. "
               "Check your connection and try again.",
    )


def _import_busy() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Another trip import is in progress. Try again in a minute.",
    )


def _authenticated(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    return user


#: ``get_current_user`` alone, as FastAPI solves it for an endpoint: through
#: this, the import routes run exactly the endpoint's own check, overrides and
#: all, without the endpoint's File/Form parameters that would read the body.
_AUTH_DEPENDANT = get_dependant(path="", call=_authenticated)


async def _no_body() -> dict:
    raise RuntimeError("authenticating an import must not read its body")


async def _authenticate(request: Request, overrides_provider: Any) -> None:
    """Raise get_current_user's own 401 for *request*, reading none of its body.

    FastAPI reads a multipart body before it runs an endpoint's dependencies,
    so the endpoint's ``get_current_user`` would only refuse an anonymous
    upload once all of it was received. ``solve_dependencies`` is not public
    FastAPI API: if it changes, this fails with a server error, never by
    letting the request through.
    """
    solved = await solve_dependencies(
        request=Request(request.scope, _no_body),
        dependant=_AUTH_DEPENDANT,
        dependency_overrides_provider=overrides_provider,
        async_exit_stack=request.scope["fastapi_inner_astack"],
        embed_body_fields=False,
    )
    if solved.errors:
        raise RuntimeError(f"authenticating an import failed: {solved.errors}")


class _CappedUploadRoute(APIRoute):
    """Refuses a request body larger than the import can accept, as it arrives.

    FastAPI parses the multipart form before the endpoint runs, and Starlette
    spools the file to a temp file as it goes, so a check in the endpoint alone
    would only run once an arbitrarily large upload had been received and
    written to disk. Here a declared ``Content-Length`` over the limit is
    refused before any of the body is read, and a body without one (chunked)
    is counted as it streams in and cut off once past the limit.

    Before that, the caller is authenticated (401) and, after it, the import
    guard is taken (503 when another import holds it), both before any of the
    body is read. A body that stalls or takes too long to arrive is cut off
    with 408, so no upload holds the guard indefinitely. The guard is
    released once the handler has returned or raised, a 413 or 408 cut-off
    included.

    :meth:`file_limit` is the largest file the route takes, read per request.
    """

    @staticmethod
    def file_limit() -> int:
        return MAX_IMPORT_BYTES

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def capped(request: Request):
            # An included route has no provider of its own; FastAPI then takes
            # the app's, as here.
            await _authenticate(request, self.dependency_overrides_provider or request.app)
            file_limit = self.file_limit()
            limit = file_limit + _MULTIPART_ALLOWANCE
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise _too_large(file_limit)
            if not _import_guard.acquire(blocking=False):
                raise _import_busy()
            try:
                receive = request.receive
                seen = 0
                deadline = time.monotonic() + UPLOAD_TOTAL_TIMEOUT_SECONDS

                async def bounded_receive():
                    nonlocal seen
                    wait = min(UPLOAD_IDLE_TIMEOUT_SECONDS, deadline - time.monotonic())
                    try:
                        with anyio.fail_after(max(wait, 0)):
                            message = await receive()
                    except TimeoutError:
                        raise _upload_too_slow() from None
                    if message["type"] == "http.request":
                        seen += len(message.get("body", b""))
                        if seen > limit:
                            raise _too_large(file_limit)
                    return message

                return await handler(Request(request.scope, bounded_receive))
            finally:
                _import_guard.release()

        return capped


class _CappedZipUploadRoute(_CappedUploadRoute):
    """:class:`_CappedUploadRoute` at the ZIP import's limit."""

    @staticmethod
    def file_limit() -> int:
        return MAX_ZIP_IMPORT_BYTES


def _name_conflict(name: str) -> JSONResponse:
    """409 for a name the user already has a trip under (issue #452).

    The name travels in its own field for the client to show; the detail
    leaves it out, since a file name may hold a double quote and the client
    reads the detail with a pattern that stops at one.
    """
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": "You already have a trip with this name.",
            "code": "name_conflict",
            "name": name,
            "request_id": request_id_var.get(),
        },
    )


def _remove_photos(removals: list[PhotoRemoval]) -> None:
    """Delete the photo files a replace dropped, once it has committed."""
    for removal in removals:
        folder = photo_folder(project_shared._DATA_DIR, removal.user_info_id,
                              removal.kind, removal.content_id)
        # Only names that stay inside this entry's own folder (photo_paths).
        unlink_and_record(removal.user_info_id, photo_files(folder, removal.uuids))
        # The share-link copies go with the photos, outside the accounting
        # (issue #430) — else they keep the folder from being removed below.
        for name in removal.uuids:
            full = photo_file(folder, name)
            if full is not None:
                remove_share_copy(full)
        if removal.remove_dir and folder.exists():
            try:
                folder.rmdir()
            except OSError:
                pass  # not empty: left for storage reconciliation


async def import_project(
    file: Annotated[UploadFile, File()],
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    on_conflict: Annotated[Optional[Literal["copy", "replace"]], Query(
        description="What to do when the name is taken. Absent: refuse with "
                    "409. copy: import under the first free \"<name> (n)\". "
                    "replace: overwrite that trip's content with the file's, "
                    "keeping the trip, its share links and companions.",
    )] = None,
):
    user_info_id = int(current_user["sub"])

    fname = os.path.basename(file.filename or "imported" + ProjectIO.EXTENSION)
    # Only the current format is accepted (issue #151). An older .viewtrip or
    # .gettracks file would otherwise parse and land under a name that still
    # carries its old suffix.
    if not fname.endswith(ProjectIO.EXTENSION) or fname == ProjectIO.EXTENSION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only {ProjectIO.EXTENSION} project files can be imported",
        )

    # Parsed straight from the upload: nothing is written under the user's
    # directory, so no copy is left behind to count against their storage,
    # whether the import succeeds or fails (issue #434). Starlette already
    # spools a large upload to a system temp file it deletes itself, and the
    # JSON parse needs the whole document in memory regardless.
    # The route already bounds the whole body; this is the exact file size.
    if file.size is not None and file.size > MAX_IMPORT_BYTES:
        raise _too_large()
    contents = await file.read()

    # Only the file's own faults are the uploader's (issue #451): anything else
    # raised while reading it, or during the ingest below, is a server bug and
    # stays a 500.
    try:
        project = ProjectIO.from_bytes(contents)
    except InvalidProjectFile as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This file isn't a valid {APP_NAME} trip: {exc}.",
        ) from None

    name = fname[: -len(ProjectIO.EXTENSION)]
    copy = on_conflict == "copy"
    removals = None
    held: List[int] = []
    with get_session() as sess:
        taken = _repo.project_exists(sess, user_info_id, name)
        # Refused before the plan limit is checked: at the limit the user must
        # still learn the name is taken and get to choose (issue #452).
        if taken and on_conflict is None:
            return _name_conflict(name)
        if taken and on_conflict == "replace":
            # The same trip, new content: no new trip, so no plan limit.
            removals = _repo.replace_project(
                sess, user_info_id, name, project, data_dir=project_shared._DATA_DIR,
                held_activities=held)
        if removals is None:
            # A copy or a new name is a new trip. The storage quota does not
            # apply: nothing lands on disk. The size is bounded by
            # MAX_IMPORT_BYTES instead, whatever the plan (issue #434).
            ensure_project_quota(sess, user_info_id)
            try:
                imported = _repo.import_project(
                    sess, user_info_id, name, project, copy=copy)
            except ProjectNameTaken:
                # A concurrent request took the name after the check above.
                return _name_conflict(name)
        else:
            imported = name
    if removals is not None:
        _remove_photos(removals)
        _free_dropped_activities(user_info_id, held)
        queue_stats_refresh(background_tasks, user_info_id, imported)
        queue_share_tiles_refresh(background_tasks, user_info_id, imported)
    # Cached payloads of this name are now wrong: the replaced trip's, or a
    # deleted trip's of the same name (issue #178).
    bust_geo_cache(user_info_id, imported)

    if removals is not None:
        outcome = "replaced"
    else:
        outcome = "created" if imported == name else "copied"
    return {"name": imported, "outcome": outcome}


router.add_api_route(
    "/import", import_project, methods=["POST"],
    status_code=status.HTTP_201_CREATED, response_model=ImportedOut,
    summary="Import a .traxj file",
    responses={
        400: {"description": "Not a .traxj file, or not a readable trip"},
        409: {"description": "The name is taken and on_conflict was not given; "
                             "the body's name field holds it"},
        413: {"description": "The file is larger than MAX_IMPORT_BYTES"},
        408: {"description": "The upload stalled, or took too long to arrive"},
        503: {"description": "Another trip import is in progress"},
    },
    route_class_override=_CappedUploadRoute,
)


# ── ZIP import ────────────────────────────────────────────────────────────────

def _sweep_stale_staging(root: Path, now: float) -> None:
    """Remove the staging directories under *root* older than a day.

    Only a crash leaves one behind. One that still holds photos may be a crash
    between an import's commit and its placement, whose rows then name photos
    that are gone, so it is logged at ERROR with what its manifest says.
    """
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return
    for folder in entries:
        if not folder.name.startswith(_STAGING_PREFIX) or not folder.is_dir():
            continue
        try:
            age = now - folder.stat().st_mtime
        except OSError:
            continue
        if age <= _STALE_STAGING_SECONDS:
            continue
        photos = [f for f in folder.rglob("*") if f.is_file() and f.name != MANIFEST_NAME]
        if photos:
            try:
                manifest = (folder / MANIFEST_NAME).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                manifest = None
            _log.error(
                "import: removing stale staging directory %s, %.1f hours old, which "
                "still holds %d photo files; manifest: %s",
                folder, age / 3600, len(photos), manifest)
        shutil.rmtree(folder, ignore_errors=True)


def _new_staging_dir() -> Path:
    """This request's staging directory, ``<data dir>/tmp/import-<uuid4>/``.

    On the data volume, so placing a photo is a rename, and outside ``users/``,
    so nothing staged is counted as anyone's storage. Stale ones are swept
    first; with one import at a time, none of them is in use.
    """
    root = Path(project_shared._DATA_DIR) / "tmp"
    _sweep_stale_staging(root, time.time())
    staging = root / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    return staging


def _ensure_room(sess, user_info_id: int, incoming: int) -> None:
    """The storage quota, for bytes the import will store. Nothing stored is
    nothing to refuse: an account over its limit is refused only what would
    take it further over."""
    if incoming:
        ensure_storage_quota(sess, user_info_id, incoming)


def _free_dropped_activities(user_info_id: int, held: List[int]) -> None:
    """Free what a Replace dropped: the Strava rows and Strava split tails the
    trip *held* before it that no trip references now (issue #509).

    As a trip deletion frees its own: a Replace is the owner's, so the rows
    go whoever imported them, and ids the file kept are referenced again and
    stay. Runs once the import and its photo moves are done, so a failure
    here cannot cost a photo. Another trip showing a surviving split root
    renumbered gets its cached payloads busted.
    """
    if not held:
        return
    with get_session() as sess:
        renamed = _repo.delete_unreferenced_strava_activities(
            sess, user_info_id,
            ids=[aid for aid in held if aid > 0],
            tail_ids=[aid for aid in held if aid < 0],
        )
        others = sess.exec(select(DBProject.user_info_id, DBProject.name).where(
            DBProject.id.in_(renamed))).all() if renamed else []
    for owner_id, trip_name in others:
        bust_geo_cache(owner_id, trip_name)


def _ingest_zip(
    user_info_id: int, name: str, project: Project, staged: StagedPhotos,
    on_conflict: Optional[str], held: Optional[List[int]] = None,
) -> Tuple[Optional[str], Optional[List[PhotoRemoval]], List[Placement]]:
    """Check the storage quota and write the trip; place no file.

    Returns the trip's name (None when the name was taken meanwhile and no
    ``on_conflict`` was given), the photo files a Replace dropped, and the
    staged photos to move into place, all as of the commit.

    The quota counts every staged photo, less, on a Replace only, those the
    trip already has in place: a copy or a new name places every one. An
    import that stores no new bytes is not checked at all, so an account
    already over its limit can still restore a trip that adds nothing.
    """
    placements: List[Placement] = []
    removals = None
    data_dir = project_shared._DATA_DIR
    with get_session() as sess:
        if on_conflict == "replace" and _repo.project_exists(sess, user_info_id, name):
            skip = already_present(sess, user_info_id, name, project, data_dir=data_dir)
            _ensure_room(sess, user_info_id, staged_total(staged, skip))
            removals = _repo.replace_project(
                sess, user_info_id, name, project, data_dir=data_dir,
                staged=staged, placements=placements, held_activities=held)
        if removals is not None:
            return name, removals, placements
        # A new trip, or the trip to replace went meanwhile.
        ensure_project_quota(sess, user_info_id)
        _ensure_room(sess, user_info_id, staged_total(staged))
        try:
            imported = _repo.import_project(
                sess, user_info_id, name, project, copy=on_conflict == "copy",
                staged=staged, placements=placements)
        except ProjectNameTaken:
            return None, None, []
    return imported, None, placements


#: photo_lock's key for each placement kind: the one that kind's uploads take.
_PHOTO_LOCK_KEYS = {"memories": "memory", "journal": "journal"}
_PHOTO_ROWS = {"memories": DBMemory, "journal": DBJournalEntry}


def _drop_unplaced(kind: str, row_id: int, photo: str) -> None:
    """Remove *photo*, which could not be placed, from its committed row.

    Under the row's photo lock, as an upload changes it, and re-read inside
    it, so a photo added to the row since the import's commit is kept.
    """
    lock_key = _PHOTO_LOCK_KEYS[kind]
    with photo_lock(lock_key, row_id):
        cache_ref = None
        with get_session() as sess:
            row = sess.get(_PHOTO_ROWS[kind], row_id)
            if row is not None:  # else deleted since: nothing names the photo
                photos = json.loads(row.photos_json or "[]")
                row.photos_json = json.dumps([p for p in photos if p != photo])
                sess.add(row)
                cache_ref = project_cache_ref(sess, row.project_id)
                sess.commit()
        bust_project_payloads(cache_ref)
    _log.error(
        "import: %s %s photo %s could not be placed after the commit; its name was "
        "removed from the row under photo_lock(%r, %s)",
        kind, row_id, photo, lock_key, row_id)


async def import_project_zip(
    file: Annotated[UploadFile, File()],
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    on_conflict: Annotated[Optional[Literal["copy", "replace"]], Query(
        description="What to do when the name is taken. Absent: refuse with "
                    "409. copy: import under the first free \"<name> (n)\". "
                    "replace: overwrite that trip's content with the file's, "
                    "keeping the trip, its share links, companions and the "
                    "photos it already has.",
    )] = None,
):
    """Import a trip ZIP, as the ZIP export writes it, with its photos (#469).

    All or nothing: the archive is read and every photo it carries decoded and
    staged before any row is written, and the photos are moved into place
    only once the trip has committed.
    """
    user_info_id = int(current_user["sub"])

    fname = os.path.basename(file.filename or "imported" + _ZIP_EXTENSION)
    if not fname.endswith(_ZIP_EXTENSION) or fname == _ZIP_EXTENSION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only {_ZIP_EXTENSION} trip archives can be imported here",
        )
    # The route already bounds the whole body; this is the exact file size.
    if file.size is not None and file.size > MAX_ZIP_IMPORT_BYTES:
        raise _too_large(MAX_ZIP_IMPORT_BYTES)
    name = fname[: -len(_ZIP_EXTENSION)]

    # Settled before the archive is read: reading it can take minutes.
    with get_session() as sess:
        taken = _repo.project_exists(sess, user_info_id, name)
        if taken and on_conflict is None:
            return _name_conflict(name)
        if not (taken and on_conflict == "replace"):
            ensure_project_quota(sess, user_info_id)

    staging = await run_in_threadpool(_new_staging_dir)
    try:
        try:
            # From the spooled upload, an entry at a time: never read whole.
            project, staged = await run_in_threadpool(
                read_trip_zip, file.file, staging, importer=user_info_id, trip_name=name)
        except InvalidTripArchive as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

        held: List[int] = []
        imported, removals, placements = await run_in_threadpool(
            _ingest_zip, user_info_id, name, project, staged, on_conflict, held)
        if imported is None:
            # A concurrent request took the name after the check above.
            return _name_conflict(name)

        # Removals first: SQLite can give a new row the id of a row the
        # Replace deleted, so a removal and a placement can name the same
        # folder and photo, and removing after placing would delete it.
        if removals is not None:
            await run_in_threadpool(_remove_photos, removals)
        failed = await run_in_threadpool(
            place_photos, project_shared._DATA_DIR, user_info_id, placements)
        for kind, row_id, photo in failed:
            await run_in_threadpool(_drop_unplaced, kind, row_id, photo)
    finally:
        # Even when cancelled: what is left here was never placed.
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(shutil.rmtree, staging, ignore_errors=True)

    if removals is not None:
        await run_in_threadpool(_free_dropped_activities, user_info_id, held)
        queue_stats_refresh(background_tasks, user_info_id, imported)
        queue_share_tiles_refresh(background_tasks, user_info_id, imported)
    # Cached payloads of this name are now wrong: the replaced trip's, or a
    # deleted trip's of the same name (issue #178).
    bust_geo_cache(user_info_id, imported)

    if removals is not None:
        outcome = "replaced"
    else:
        outcome = "created" if imported == name else "copied"
    return {"name": imported, "outcome": outcome}


router.add_api_route(
    "/import-zip", import_project_zip, methods=["POST"],
    status_code=status.HTTP_201_CREATED, response_model=ImportedOut,
    summary="Import a trip ZIP with its photos",
    responses={
        400: {"description": "Not a .zip file, or not a readable trip archive, "
                             "or a photo in it is not a readable image"},
        402: {"description": "The trip or its photos would exceed the plan's "
                             "trip count or storage"},
        409: {"description": "The name is taken and on_conflict was not given; "
                             "the body's name field holds it"},
        413: {"description": "The file is larger than MAX_ZIP_IMPORT_BYTES"},
        408: {"description": "The upload stalled, or took too long to arrive"},
        503: {"description": "Another trip import is in progress"},
    },
    route_class_override=_CappedZipUploadRoute,
)


# ── GPX export ─────────────────────────────────────────────────────────────────

_SEGMENT_GPX_POINTS = 50
_SAFE_NAME = re.compile(r"[^\w\-. ]")


@router.get("/{name}/export", summary="Export project as GPX")
def export_project_gpx(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Build and send the project as a GPX file."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        project = _repo.get_project(
            sess, row.user_info_id, name,
        )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # A GPX export can't carry an activity whose geometry is client-side E2EE
    # ciphertext (issue #29) — the server can't decode it, and silently
    # producing a file missing some tracks (or garbage) would be worse than an
    # explicit error. Check every activity actually referenced by this project
    # up front, before building anything (mirrors api/memories.py's
    # get_translation 409 for an encrypted memory, issue #27).
    for item in project.items:
        if item.item_type != "activity":
            continue
        act = project.activity_by_id(item.activity_id) if item.activity_id else None
        if act is None:
            continue
        if (is_encrypted_envelope(act.summary_polyline)
                or act.start_latlng_enc or act.end_latlng_enc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot export GPX for a project containing an encrypted activity",
            )

    gpx = gpxpy.gpx.GPX()
    gpx.name = project.name
    gpx.creator = APP_NAME

    track = gpxpy.gpx.GPXTrack(name=project.name)
    gpx.tracks.append(track)

    for item in project.items:
        if item.item_type == "activity":
            act = project.activity_by_id(item.activity_id) if item.activity_id else None
            if act is None:
                continue

            seg = gpxpy.gpx.GPXTrackSegment()

            if act.summary_polyline:
                decoded = polyline_lib.decode(act.summary_polyline)
                for idx, (lat, lon) in enumerate(decoded):
                    pt = gpxpy.gpx.GPXTrackPoint(lat, lon)
                    if idx == 0 and act.start_date_local:
                        pt.time = act.start_date_local
                    seg.points.append(pt)
            elif act.start_latlng and act.end_latlng:
                pt_start = gpxpy.gpx.GPXTrackPoint(act.start_latlng[0], act.start_latlng[1])
                if act.start_date_local:
                    pt_start.time = act.start_date_local
                pt_end = gpxpy.gpx.GPXTrackPoint(act.end_latlng[0], act.end_latlng[1])
                seg.points.append(pt_start)
                seg.points.append(pt_end)
            else:
                continue

            if seg.points:
                track.segments.append(seg)

        elif item.item_type == "segment" and item.segment:
            cs = item.segment
            arc = great_circle_points(
                cs.start.lat, cs.start.lon,
                cs.end.lat,   cs.end.lon,
                n_points=_SEGMENT_GPX_POINTS,
            )
            seg = gpxpy.gpx.GPXTrackSegment()
            for lat, lon in arc:
                seg.points.append(gpxpy.gpx.GPXTrackPoint(lat, lon))
            track.segments.append(seg)

        elif item.item_type == "memory" and item.memory:
            mem = item.memory
            if mem.lat is not None and mem.lon is not None:
                wpt = gpxpy.gpx.GPXWaypoint(mem.lat, mem.lon)
                wpt.name = mem.name or "Memory"
                wpt.description = mem.description
                if mem.date and mem.time:
                    try:
                        wpt.time = datetime.fromisoformat(f"{mem.date}T{mem.time}:00")
                    except ValueError:
                        pass
                gpx.waypoints.append(wpt)

    # Emit one <wpt> per day that has any day metadata
    if project.day_meta:
        # Build a map: date_key → first lat/lon from an activity on that day
        day_first_latlng: dict[str, tuple[float, float]] = {}
        for it in project.items:
            if it.item_type == "activity" and it.activity_id is not None:
                act = project.activity_by_id(it.activity_id)
                if act is None:
                    continue
                try:
                    date_key = act.start_date_local.date().isoformat()
                except AttributeError:
                    date_key = str(act.start_date_local)[:10]
                if date_key and date_key not in day_first_latlng and act.start_latlng:
                    day_first_latlng[date_key] = (act.start_latlng[0], act.start_latlng[1])

        for date_key, dm in project.day_meta.items():
            if not any([dm.difficulty, dm.sleeping, dm.weather, dm.journal]):
                continue
            lat, lon = day_first_latlng.get(date_key, (0.0, 0.0))
            wpt = gpxpy.gpx.GPXWaypoint(lat, lon)
            wpt.name = f"Day meta {date_key}"
            parts = [s for s in [
                dm.difficulty and f"Difficulty: {dm.difficulty}",
                dm.sleeping   and f"Sleeping: {dm.sleeping}",
                dm.weather    and f"Weather: {dm.weather}",
                dm.journal    and f"Journal: {dm.journal}",
            ] if s]
            wpt.description = " | ".join(parts)
            wpt.comment = json.dumps({
                "date": date_key,
                "difficulty": dm.difficulty,
                "sleeping": dm.sleeping,
                "weather": dm.weather,
                "journal": dm.journal,
            })
            gpx.waypoints.append(wpt)

    gpx_xml = gpx.to_xml()
    safe = _SAFE_NAME.sub("_", project.name)

    return Response(
        gpx_xml.encode("utf-8"),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{safe}.gpx"'},
    )


# ── .traxj export ─────────────────────────────────────────────────────────────

def _traxj_document(project) -> Dict[str, Any]:
    """The trip as a .traxj document: what the .traxj export writes, and the
    trip file inside the ZIP export (#469)."""
    data: Dict[str, Any] = ProjectIO.to_dict(project)
    # Override activities with the raw Strava format (no elevation_profile pairs) for the backup file
    data["activities"] = [a.to_strava_dict() for a in project.activities]
    return data


@router.get("/{name}/export-traxj", summary="Export project as .traxj file")
def export_project_traxj(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Download the project as a .traxj JSON file (no embedded photos)."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        project = _repo.get_project(
            sess, row.user_info_id, name,
            journal_user_id=user_info_id,
        )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    json_bytes = ProjectIO.dumps(_traxj_document(project))
    safe = _SAFE_NAME.sub("_", project.name)
    return Response(
        json_bytes,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe}{ProjectIO.EXTENSION}"'},
    )


# ── ZIP export (.traxj + photos) ──────────────────────────────────────────────

#: Size of each piece of a streamed ZIP export (#484). The archive is sent from
#: a temp file in pieces of this size, so no single write holds more.
_ZIP_CHUNK_BYTES = 64 * 1024

#: How much of a ZIP export is kept in memory before its temp file moves to
#: disk. A trip without photos stays in memory; one with photos, which can run
#: to hundreds of MB, goes to disk instead of the API's memory.
_ZIP_SPOOL_BYTES = 1024 * 1024


def _iter_chunks(spool: IO[bytes]) -> Iterator[bytes]:
    """*spool* from its start, in pieces of at most ``_ZIP_CHUNK_BYTES``,
    closing it when done or when the download is abandoned."""
    try:
        spool.seek(0)
        while chunk := spool.read(_ZIP_CHUNK_BYTES):
            yield chunk
    finally:
        spool.close()


@router.get("/{name}/export-zip", summary="Export project as ZIP (with photos)")
def export_project_zip(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Download a ZIP containing the .traxj file, the memory photos and the
    caller's own journal photos (#469)."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        # Memory photos live under the project OWNER's data dir (issue #106) —
        # not the caller's, who may be a companion.
        owner_dir_id = str(row.user_info_id)
        project = _repo.get_project(
            sess, row.user_info_id, name,
            journal_user_id=user_info_id,
        )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # The full .traxj document (#469), with relative photo_refs added to
    # memories and journal entries. Items serialise in project.items order.
    data = _traxj_document(project)
    # (folder, uuid, archive entry name) of every photo to add.
    photos: list[tuple[Path, str, str]] = []
    for item, d in zip(project.items, data["items"]):
        if item.item_type == "memory" and item.memory and item.memory.id:
            content, prefix = item.memory, "photos"
            folder = photo_folder(_DATA_DIR, owner_dir_id, "memories", content.id)
        elif item.item_type == "journal" and item.journal and item.journal.id:
            # The export holds only the caller's own entries (journal_user_id
            # above), and a journal photo lives under its author: the caller.
            content, prefix = item.journal, "journal"
            folder = photo_folder(_DATA_DIR, user_info_id, "journal", content.id)
        else:
            continue
        if not content.photos:
            continue
        refs = [f"{prefix}/{content.id}/{uuid}.jpg" for uuid in content.photos]
        d[item.item_type]["photo_refs"] = refs
        photos += [(folder, uuid, f"{prefix}/{int(content.id)}/{uuid}.jpg")
                   for uuid in content.photos]
    project_bytes = ProjectIO.dumps(data)

    safe = _SAFE_NAME.sub("_", project.name)
    spool = tempfile.SpooledTemporaryFile(max_size=_ZIP_SPOOL_BYTES)
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{safe}{ProjectIO.EXTENSION}", project_bytes)
            written: set[str] = set()
            for folder, photo_uuid, entry in photos:
                # photo_file only answers for an app-made name inside the
                # item's folder, which also keeps the entry name plain.
                full_path = photo_file(folder, photo_uuid)
                if (full_path is not None and entry not in written
                        and full_path.exists()):
                    zf.write(full_path, entry)
                    written.add(entry)
    except BaseException:
        spool.close()
        raise

    return StreamingResponse(
        _iter_chunks(spool),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
    )

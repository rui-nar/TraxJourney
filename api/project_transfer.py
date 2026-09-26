"""REST project import/export endpoints — .traxj upload, GPX/.traxj/ZIP download.

Routes:
    POST   /api/projects/import              — upload a .traxj file
    GET    /api/projects/{name}/export       — download project as GPX file
    GET    /api/projects/{name}/export-traxj — download project as .traxj JSON
    GET    /api/projects/{name}/export-zip   — download ZIP (.traxj + photos)
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, Literal, Optional

import gpxpy
import gpxpy.gpx
import polyline as polyline_lib
from models.db import get_session

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from api.deps import get_current_user
from api.geo import bust_geo_cache
from api.project_access import OwnerParam, resolve_project
import api.project_shared as project_shared
from api.project_shared import (
    _DATA_DIR, _repo, queue_share_tiles_refresh, queue_stats_refresh,
)
from src.billing.entitlements import ensure_project_quota
from src.billing.usage import unlink_and_record
from src.brand import APP_NAME
from src.models.great_circle import great_circle_points
from src.project.project_io import InvalidProjectFile, ProjectIO
from src.project.repo_transfer import PhotoRemoval, ProjectNameTaken
from src.utils.logging import request_id_var
from src.utils.encryption_check import is_encrypted_envelope
from src.utils.photo_paths import photo_file, photo_files, photo_folder

router = APIRouter(prefix="/api/projects", tags=["projects"])


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

#: Room for the multipart envelope (boundaries, part headers) around the file.
_MULTIPART_ALLOWANCE = 64 * 1024


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=(f"This file is too large to import. The limit is "
                f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB."),
    )


class _CappedUploadRoute(APIRoute):
    """Refuses a request body larger than the import can accept, as it arrives.

    FastAPI parses the multipart form before the endpoint runs, and Starlette
    spools the file to a temp file as it goes, so a check in the endpoint alone
    would only run once an arbitrarily large upload had been received and
    written to disk. Here a declared ``Content-Length`` over the limit is
    refused before any of the body is read, and a body without one (chunked)
    is counted as it streams in and cut off once past the limit.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def capped(request: Request):
            limit = MAX_IMPORT_BYTES + _MULTIPART_ALLOWANCE
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise _too_large()
            receive = request.receive
            seen = 0

            async def bounded_receive():
                nonlocal seen
                message = await receive()
                if message["type"] == "http.request":
                    seen += len(message.get("body", b""))
                    if seen > limit:
                        raise _too_large()
                return message

            return await handler(Request(request.scope, bounded_receive))

        return capped


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
    with get_session() as sess:
        taken = _repo.project_exists(sess, user_info_id, name)
        # Refused before the plan limit is checked: at the limit the user must
        # still learn the name is taken and get to choose (issue #452).
        if taken and on_conflict is None:
            return _name_conflict(name)
        if taken and on_conflict == "replace":
            # The same trip, new content: no new trip, so no plan limit.
            removals = _repo.replace_project(
                sess, user_info_id, name, project, data_dir=project_shared._DATA_DIR)
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
    },
    route_class_override=_CappedUploadRoute,
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
    """Build and stream the project as a GPX file."""
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

    return StreamingResponse(
        io.BytesIO(gpx_xml.encode("utf-8")),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{safe}.gpx"'},
    )


# ── .traxj export ─────────────────────────────────────────────────────────────

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

    data: Dict[str, Any] = ProjectIO.to_dict(project)
    # Override activities with the raw Strava format (no elevation_profile pairs) for the backup file
    data["activities"] = [a.to_strava_dict() for a in project.activities]
    json_bytes = ProjectIO.dumps(data)
    safe = _SAFE_NAME.sub("_", project.name)
    return StreamingResponse(
        io.BytesIO(json_bytes),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe}{ProjectIO.EXTENSION}"'},
    )


# ── ZIP export (.traxj + photos) ──────────────────────────────────────────────

@router.get("/{name}/export-zip", summary="Export project as ZIP (with photos)")
def export_project_zip(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Download a ZIP containing the .traxj file and all memory photos."""
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

    # Serialise items, adding relative photo_refs for memories.
    items_serialised = []
    for item in project.items:
        d = ProjectIO._serialise_item(item)
        if item.item_type == "memory" and item.memory and item.memory.id and item.memory.photos:
            d["memory"]["photo_refs"] = [
                f"photos/{item.memory.id}/{uuid}.jpg"
                for uuid in item.memory.photos
            ]
        items_serialised.append(d)

    data: Dict[str, Any] = {
        "version": project.version,
        "name": project.name,
        "trip_start": project.trip_start,
        "filter_state": {
            "start_date": project.filter_state.start_date,
            "end_date": project.filter_state.end_date,
            "activity_types": project.filter_state.activity_types,
        },
        "items": items_serialised,
        "activities": [a.to_strava_dict() for a in project.activities],
    }
    project_bytes = ProjectIO.dumps(data)

    zip_buffer = io.BytesIO()
    safe = _SAFE_NAME.sub("_", project.name)
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{safe}{ProjectIO.EXTENSION}", project_bytes)
        for item in project.items:
            if item.item_type != "memory" or item.memory is None or item.memory.id is None:
                continue
            mem = item.memory
            mem_dir = photo_folder(_DATA_DIR, owner_dir_id, "memories", mem.id)
            for photo_uuid in mem.photos:
                # photo_file only answers for an app-made name inside the
                # memory's folder, which also keeps the entry name plain.
                full_path = photo_file(mem_dir, photo_uuid)
                if full_path is not None and full_path.exists():
                    zf.write(full_path, f"photos/{int(mem.id)}/{photo_uuid}.jpg")

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
    )

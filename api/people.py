"""REST people endpoints — CRUD + avatar for the trip's people directory (#40).

People are private and owner-only — never exposed in shared views. Each person
belongs to one project and may be referenced by any number of encounters.

Routes:
    POST   /api/people                         — create a person
    GET    /api/people/{id}                     — person + their encounters (incl. their group's)
    PUT    /api/people/{id}                     — update a person
    DELETE /api/people/{id}                     — delete a person + their encounters
    POST   /api/people/{id}/avatar              — upload/replace the avatar photo
    DELETE /api/people/{id}/avatar              — remove the avatar photo
    GET    /api/people/{id}/avatar              — serve full-res avatar
    GET    /api/people/{id}/avatar/thumb        — serve avatar thumbnail
"""
from __future__ import annotations

import io
import json
import os
import uuid as uuid_lib
from pathlib import Path
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from models.db import get_session
from PIL import Image
from pydantic import BaseModel, Field
from sqlmodel import select

import requests

from api.deps import get_current_user
from api.polarsteps import (
    POLARSTEPS_TOKEN_EXPIRED_DETAIL,
    _persist_rotated_token,
    _require_client,
)
from api.project_access import OwnerParam, assert_project_access, resolve_project
from api.project_shared import bust_project_payloads, project_cache_ref
from src.project.project_repo import bump_lock_version
from models.project_db import (
    DBEncounter,
    DBPerson,
    DBPersonGroup,
    DBProject,
    DBProjectItem,
)
from src.api.polarsteps_client import format_step, format_trip
from src.billing.entitlements import ensure_storage_quota
from src.billing.usage import record_written, unlink_and_record
from src.utils.photo_paths import photo_file, photo_files, photo_folder
from src.models.person import polarsteps_from_socials
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/people", tags=["people"])

_log = get_logger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_THUMB_SIZE = (400, 400)


# ── Response schemas ──────────────────────────────────────────────────────────

class IDOut(BaseModel):
    id: int = Field(description="ID of the newly created person")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _avatar_dir(user_id: str, person_id: int) -> Path:
    p = Path(_DATA_DIR) / "users" / user_id / "people" / str(person_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_owned_person(sess, person_id: int, user_info_id: int, min_role: str = "editor") -> DBPerson:
    row = sess.get(DBPerson, person_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    assert_project_access(sess, user_info_id, row.project_id, min_role=min_role)
    return row


def _get_project_id(sess, user_info_id: int, project_name: str,
                    owner_id: int | None = None, min_role: str = "editor") -> int:
    return resolve_project(sess, user_info_id, project_name, owner_id, min_role=min_role).id


def _loads_list(raw: str | None) -> list:
    return json.loads(raw) if raw else []


def _person_out(row: DBPerson) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "email": row.email,
        "phone": row.phone,
        "polarsteps": row.polarsteps,
        "notes": row.notes,
        "avatar_photo": row.avatar_photo,
        "socials": _loads_list(row.socials_json),
        "nationalities": _loads_list(row.nationalities_json),
        "residence": row.residence,
        "group_id": row.group_id,
    }


def _delete_avatar_files(user_id: str, person_id: int, uuid_str: str) -> None:
    unlink_and_record(user_id, photo_files(
        photo_folder(_DATA_DIR, user_id, "people", person_id), [uuid_str]))


def _parse_ps_username(raw: str | None) -> str | None:
    """Extract a Polarsteps username from a stored handle or profile URL.

    Accepts "alice", "@alice", "polarsteps.com/alice", or a full profile URL.
    Returns None when there's nothing usable.
    """
    if not raw:
        return None
    v = raw.strip()
    if "polarsteps.com/" in v:
        v = v.split("polarsteps.com/", 1)[1]
    v = v.lstrip("@").strip("/ ")
    v = v.split("/")[0].split("?")[0].strip()
    return v or None


# ── Schemas ───────────────────────────────────────────────────────────────────

class SocialLink(BaseModel):
    network: str = Field(description="e.g. instagram, facebook, polarsteps, strava")
    handle: str = Field(description="Username or profile URL on that network")


class PersonBody(BaseModel):
    project_name: str = Field(description="Project the person belongs to")
    name: Optional[str] = Field(None, description="Display name (optional — may be a first name or 'Unknown')")
    email: Optional[str] = Field(None)
    phone: Optional[str] = Field(None)
    polarsteps: Optional[str] = Field(None, description="Legacy; superseded by a 'polarsteps' entry in socials")
    notes: Optional[str] = Field(None)
    socials: Optional[List[SocialLink]] = Field(None, description="Social links; the 'polarsteps' entry drives the shared-trip view")
    nationalities: Optional[List[str]] = Field(None, description="ISO 3166-1 alpha-2 country codes")
    residence: Optional[str] = Field(None, description="'city, country' where they live")


class PersonUpdateBody(BaseModel):
    name: Optional[str] = Field(None)
    email: Optional[str] = Field(None)
    phone: Optional[str] = Field(None)
    polarsteps: Optional[str] = Field(None)
    notes: Optional[str] = Field(None)
    socials: Optional[List[SocialLink]] = Field(None)
    nationalities: Optional[List[str]] = Field(None)
    residence: Optional[str] = Field(None)


def _apply_person_fields(row: DBPerson, body: "PersonBody | PersonUpdateBody") -> None:
    """Write a person body's editable fields onto a DBPerson row.

    Serialises the socials/nationalities lists to JSON and mirrors the polarsteps
    handle out of socials onto the dedicated `polarsteps` column so the
    shared-trip view keeps working; a legacy standalone `polarsteps` value is the
    fallback when socials carry no such entry.
    """
    socials = [s.model_dump() for s in body.socials] if body.socials else []
    row.name = body.name
    row.email = body.email
    row.phone = body.phone
    row.notes = body.notes
    row.socials_json = json.dumps(socials) if socials else None
    row.nationalities_json = json.dumps(body.nationalities) if body.nationalities else None
    row.residence = body.residence
    row.polarsteps = polarsteps_from_socials(socials) or body.polarsteps


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=IDOut,
             summary="Create a person")
def create_person(
    body: PersonBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Create a new person in the project's people directory."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        project_id = _get_project_id(sess, user_info_id, body.project_name, owner)
        row = DBPerson(project_id=project_id)
        _apply_person_fields(row, body)
        sess.add(row)
        cache_ref = project_cache_ref(sess, project_id)
        sess.commit()
        person_id = row.id
        bust_project_payloads(cache_ref)
    return {"id": person_id}


def _encounter_out(e: DBEncounter, group_name: str | None = None) -> dict:
    """Serialise an encounter for the person sheet.

    [group_name] is set (and `source` becomes "group") for encounters inherited
    from the person's group — see [get_person].
    """
    return {
        "id": e.id,
        "date": e.date,
        "time": e.time,
        "description": e.description,
        "geo_mode": e.geo_mode,
        "lat": e.lat,
        "lon": e.lon,
        "source": "group" if e.group_id is not None else "person",
        "person_id": e.person_id,
        "group_id": e.group_id,
        "group_name": group_name,
    }


@router.get("/{person_id}", summary="Get a person and their encounters")
def get_person(
    person_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return a person plus every encounter (date, place, note) with them.

    A person inherits the encounters logged directly against the group they
    belong to (issue #124): meeting the group means meeting its members, so those
    show up here too, tagged `source: "group"` with the group's name so the
    client can label them as inherited rather than one-to-one.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_person(sess, person_id, user_info_id, min_role="viewer")
        encounters = list(sess.exec(
            select(DBEncounter).where(DBEncounter.person_id == person_id)
        ).all())
        out = [_encounter_out(e) for e in encounters]

        if row.group_id is not None:
            group = sess.get(DBPersonGroup, row.group_id)
            inherited = sess.exec(
                select(DBEncounter).where(DBEncounter.group_id == row.group_id)
            ).all()
            out += [
                _encounter_out(e, group_name=group.name if group else None)
                for e in inherited
            ]

        out.sort(key=lambda e: (e["date"] or "", e["time"] or ""))
        return {**_person_out(row), "encounters": out}


@router.put("/{person_id}", status_code=status.HTTP_204_NO_CONTENT,
            summary="Update a person")
def update_person(
    person_id: int,
    body: PersonUpdateBody,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Update a person's details."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_person(sess, person_id, user_info_id)
        _apply_person_fields(row, body)
        sess.add(row)
        cache_ref = project_cache_ref(sess, row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)


@router.delete("/{person_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a person and their encounters")
def delete_person(
    person_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Delete a person, cascade to their encounters + timeline items, and remove the avatar."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_person(sess, person_id, user_info_id)

        encounters = sess.exec(
            select(DBEncounter).where(DBEncounter.person_id == person_id)
        ).all()
        enc_ids = [e.id for e in encounters]
        if enc_ids:
            for item in sess.exec(
                select(DBProjectItem).where(DBProjectItem.encounter_id.in_(enc_ids))
            ).all():
                sess.delete(item)
        for e in encounters:
            sess.delete(e)

        if row.avatar_photo:
            _delete_avatar_files(current_user["sub"], person_id, row.avatar_photo)

        # Make this delete visible to the optimistic lock too: a structural
        # rewrite that loaded before it would otherwise pass the CAS and
        # re-insert the item row from its snapshot, leaving a row pointing at
        # content that no longer exists (issue #173; foreign keys are off).
        bump_lock_version(sess, row.project_id)
        cache_ref = project_cache_ref(sess, row.project_id)
        sess.delete(row)
        sess.commit()
        bust_project_payloads(cache_ref)


# ── Avatar ────────────────────────────────────────────────────────────────────

@router.post("/{person_id}/avatar", status_code=status.HTTP_201_CREATED,
             response_model=IDOut, summary="Upload or replace the avatar photo")
async def upload_avatar(
    person_id: int,
    file: Annotated[UploadFile, File()],
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Upload a JPEG avatar; a 400×400 thumbnail is generated. Replaces any existing avatar."""
    user_info_id = int(current_user["sub"])
    raw = await file.read()
    with get_session() as sess:
        ensure_storage_quota(sess, user_info_id, len(raw))
    new_uuid = str(uuid_lib.uuid4())
    photo_path = _avatar_dir(current_user["sub"], person_id)
    full = photo_path / f"{new_uuid}.jpg"
    thumb = photo_path / f"{new_uuid}_thumb.jpg"
    full.write_bytes(raw)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail(_THUMB_SIZE, Image.LANCZOS)
    img.save(str(thumb), "JPEG", quality=85)
    record_written(current_user["sub"], full, thumb)

    with get_session() as sess:
        row = _get_owned_person(sess, person_id, user_info_id)
        old = row.avatar_photo
        row.avatar_photo = new_uuid
        sess.add(row)
        cache_ref = project_cache_ref(sess, row.project_id)
        sess.commit()
        bust_project_payloads(cache_ref)
    if old:
        _delete_avatar_files(current_user["sub"], person_id, old)
    return {"id": person_id}


@router.delete("/{person_id}/avatar", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remove the avatar photo")
def delete_avatar(
    person_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Remove a person's avatar photo and delete its files."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_person(sess, person_id, user_info_id)
        if row.avatar_photo:
            _delete_avatar_files(current_user["sub"], person_id, row.avatar_photo)
            row.avatar_photo = None
            sess.add(row)
            cache_ref = project_cache_ref(sess, row.project_id)
            sess.commit()
            bust_project_payloads(cache_ref)


@router.get("/{person_id}/avatar", summary="Serve full-resolution avatar")
def serve_avatar(
    person_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the full-resolution avatar JPEG."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_person(sess, person_id, user_info_id, min_role="viewer")
        uuid_str = row.avatar_photo
    if not uuid_str:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No avatar")
    full_path = photo_file(
        photo_folder(_DATA_DIR, current_user["sub"], "people", person_id), uuid_str)
    if full_path is None or not full_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(str(full_path), media_type="image/jpeg")


@router.get("/{person_id}/avatar/thumb", summary="Serve avatar thumbnail")
def serve_avatar_thumb(
    person_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the 400×400 avatar thumbnail; falls back to full-res if missing."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_person(sess, person_id, user_info_id, min_role="viewer")
        uuid_str = row.avatar_photo
    if not uuid_str:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No avatar")
    photo_dir = photo_folder(_DATA_DIR, current_user["sub"], "people", person_id)
    thumb_path = photo_file(photo_dir, uuid_str, "_thumb")
    if thumb_path is None or not thumb_path.exists():
        full_path = photo_file(photo_dir, uuid_str)
        if full_path is not None and full_path.exists():
            return FileResponse(str(full_path), media_type="image/jpeg")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(str(thumb_path), media_type="image/jpeg")


# ── Polarsteps: view a person's shared trip (issue #40) ───────────────────────
# View-only. Uses the current user's own Polarsteps connection to fetch trips a
# person (matched by their `polarsteps` handle) has shared with them. Their
# content is never persisted or shared onward.

def _person_ps_username(sess, person_id: int, user_info_id: int) -> str:
    person = _get_owned_person(sess, person_id, user_info_id, min_role="viewer")
    username = _parse_ps_username(person.polarsteps)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This person has no Polarsteps account",
        )
    return username


def _ps_not_visible_or_502(exc: Exception, *, person_id: int, trip_id: int | None = None):
    ctx = f"person_id={person_id}" + ("" if trip_id is None else f" trip_id={trip_id}")
    if isinstance(exc, requests.HTTPError) and exc.response is not None \
            and exc.response.status_code in (403, 404):
        _log.warning("polarsteps profile not visible %s status=%s", ctx, exc.response.status_code)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That Polarsteps profile or trip isn't visible to you",
        )
    _log.exception("polarsteps fetch failed %s", ctx)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("/{person_id}/polarsteps/trips",
            summary="List a person's Polarsteps trips (view-only)")
def person_polarsteps_trips(
    person_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the trips a person has shared on Polarsteps, resolved from their
    handle via the current user's Polarsteps connection."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        username = _person_ps_username(sess, person_id, user_info_id)
    client = _require_client(user_info_id)
    try:
        user = client.get_user_by_username(username)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=POLARSTEPS_TOKEN_EXPIRED_DETAIL)
    except Exception as exc:
        _ps_not_visible_or_502(exc, person_id=person_id)
    _persist_rotated_token(user_info_id, client)
    trips = list(reversed(user.get("trips") or []))
    return [format_trip(t) for t in trips]


@router.get("/{person_id}/polarsteps/trips/{trip_id}/steps",
            summary="List steps for a person's Polarsteps trip (view-only)")
def person_polarsteps_trip_steps(
    person_id: int,
    trip_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the published steps (lat/lon/date) for one of a person's trips, to
    render the route as a view-only overlay."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        _person_ps_username(sess, person_id, user_info_id)  # ownership + has-handle
    client = _require_client(user_info_id)
    try:
        raw_steps = client.get_trip_steps(trip_id)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=POLARSTEPS_TOKEN_EXPIRED_DETAIL)
    except Exception as exc:
        _ps_not_visible_or_502(exc, person_id=person_id, trip_id=trip_id)
    _persist_rotated_token(user_info_id, client)
    return [format_step(s) for s in raw_steps]

"""REST encounter endpoints — CRUD for people/groups met on a day (issue #40, #56).

Encounters are private and owner-only — never exposed in shared views. Each
encounter links one person OR one group to a day (exactly one of person_id/
group_id), with an optional place (map pin, defaulting to the day's location) and
a free-text note. Rendered as an ordered project item.

Routes:
    POST   /api/encounters              — create an encounter (person + day + place)
    PUT    /api/encounters/{id}          — update an encounter
    DELETE /api/encounters/{id}          — delete an encounter + its timeline item
"""
from __future__ import annotations

import json
import uuid as uuid_lib
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from models.db import get_session
from pydantic import BaseModel, Field
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
from api.project_shared import bust_project_payloads, project_cache_ref
from models.project_db import DBActivity, DBEncounter, DBPerson, DBPersonGroup, DBProject, DBProjectItem
from src.models.value_bounds import Lat, Lon
from src.project.project_repo import bump_lock_version

router = APIRouter(prefix="/api/encounters", tags=["encounters"])


# ── Response schemas ──────────────────────────────────────────────────────────

class IDOut(BaseModel):
    id: int = Field(description="ID of the newly created encounter")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_owned_encounter(sess, encounter_id: int, user_info_id: int, min_role: str = "editor") -> DBEncounter:
    row = sess.get(DBEncounter, encounter_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Encounter not found")
    assert_project_access(sess, user_info_id, row.project_id, min_role=min_role)
    return row


def _require_person_in_project(sess, person_id: int, project_id: int) -> None:
    person = sess.get(DBPerson, person_id)
    if person is None or person.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not in project")


def _require_group_in_project(sess, group_id: int, project_id: int) -> None:
    group = sess.get(DBPersonGroup, group_id)
    if group is None or group.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not in project")


def _require_exactly_one_of_person_or_group(
    person_id: Optional[int], group_id: Optional[int]
) -> None:
    if (person_id is None) == (group_id is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exactly one of person_id or group_id must be set",
        )


def _resolve_geo(sess, project_id: int, date: str, geo_mode: str):
    """Resolve an encounter's coordinates from the day's activities (mirrors journal)."""
    if geo_mode == "custom":
        return None, None
    items = sess.exec(
        select(DBProjectItem).where(
            DBProjectItem.project_id == project_id,
            DBProjectItem.item_type == "activity",
        )
    ).all()
    activity_ids = [i.activity_id for i in items if i.activity_id is not None]
    if not activity_ids:
        return None, None
    act_rows = sess.exec(select(DBActivity).where(DBActivity.id.in_(activity_ids))).all()
    day_acts = [r for r in act_rows if (r.start_date_local or "")[:10] == date]
    if not day_acts:
        return None, None
    day_acts.sort(key=lambda r: r.start_date_local or "")
    if geo_mode == "start_of_day":
        latlng = json.loads(day_acts[0].start_latlng_json) if day_acts[0].start_latlng_json else None
    else:  # end_of_day
        latlng = json.loads(day_acts[-1].end_latlng_json) if day_acts[-1].end_latlng_json else None
    if latlng and len(latlng) >= 2:
        return float(latlng[0]), float(latlng[1])
    return None, None


# ── Schemas ───────────────────────────────────────────────────────────────────

class EncounterBody(BaseModel):
    project_name: str = Field(description="Project the encounter belongs to")
    person_id: Optional[int] = Field(None, description="Person met (exactly one of person_id/group_id)")
    group_id: Optional[int] = Field(None, description="Group met (exactly one of person_id/group_id)")
    date: str = Field(description="Date of the encounter (YYYY-MM-DD)")
    geo_mode: str = Field("start_of_day", description="'start_of_day', 'end_of_day', or 'custom'")
    time: Optional[str] = Field(None, description="Optional time of day (HH:MM)")
    description: Optional[str] = Field(None, description="Free-text note")
    lat: Optional[Lat] = Field(None, description="Latitude (required when geo_mode='custom')")
    lon: Optional[Lon] = Field(None, description="Longitude (required when geo_mode='custom')")
    insert_after_index: Optional[int] = Field(None, description="Item-list position to insert after")


class EncounterUpdateBody(BaseModel):
    person_id: Optional[int] = Field(None, description="Person met (exactly one of person_id/group_id)")
    group_id: Optional[int] = Field(None, description="Group met (exactly one of person_id/group_id)")
    date: str = Field(description="Date of the encounter (YYYY-MM-DD)")
    geo_mode: str = Field("start_of_day", description="'start_of_day', 'end_of_day', or 'custom'")
    time: Optional[str] = Field(None)
    description: Optional[str] = Field(None)
    lat: Optional[Lat] = Field(None)
    lon: Optional[Lon] = Field(None)


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=IDOut,
             summary="Create an encounter")
def create_encounter(
    body: EncounterBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Create an encounter with a person or group on a day and insert it at the requested position."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        project_row = resolve_project(sess, user_info_id, body.project_name, owner, min_role="editor")
        project_id = project_row.id
        proj_owner_id = project_row.user_info_id
        _require_exactly_one_of_person_or_group(body.person_id, body.group_id)
        if body.person_id is not None:
            _require_person_in_project(sess, body.person_id, project_id)
        else:
            _require_group_in_project(sess, body.group_id, project_id)

        lat, lon = body.lat, body.lon
        if body.geo_mode != "custom":
            lat, lon = _resolve_geo(sess, project_id, body.date, body.geo_mode)

        row = DBEncounter(
            project_id=project_id,
            person_id=body.person_id,
            group_id=body.group_id,
            date=body.date,
            time=body.time,
            description=body.description,
            geo_mode=body.geo_mode,
            lat=lat,
            lon=lon,
        )
        sess.add(row)
        sess.flush()

        existing_items = sess.exec(
            select(DBProjectItem)
            .where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.position)
        ).all()
        # insert_after_index is an index into the caller's *visible* item list
        # (other users' journal items are hidden) — translate it (issue #106).
        visible = journal_visible_row_positions(sess, existing_items, user_info_id, proj_owner_id)
        insert_at = translate_insert_after(visible, body.insert_after_index, len(existing_items))
        # It lands a *list index*, and positions are not dense — a segment
        # delete removes one row without renumbering — so translate it into a
        # position value before comparing it against one.
        insert_pos = row_position_for_index(existing_items, insert_at)

        for item in existing_items:
            if item.position >= insert_pos:
                item.position += 1
                sess.add(item)

        sess.add(DBProjectItem(
            project_id=project_id,
            position=insert_pos,
            uid=uuid_lib.uuid4().hex,
            item_type="encounter",
            encounter_id=row.id,
        ))
        # Make this insert visible to the optimistic lock, so a structural
        # rewrite loaded before it cannot erase the row (issue #173).
        bump_lock_version(sess, project_id)
        cache_ref = project_cache_ref(sess, project_id)
        sess.commit()
        encounter_id = row.id
        bust_project_payloads(cache_ref)

    return {"id": encounter_id}


@router.put("/{encounter_id}", status_code=status.HTTP_204_NO_CONTENT,
            summary="Update an encounter")
def update_encounter(
    encounter_id: int,
    body: EncounterUpdateBody,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Update an encounter's person/group, date, place, or note."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_encounter(sess, encounter_id, user_info_id)
        _require_exactly_one_of_person_or_group(body.person_id, body.group_id)
        if body.person_id is not None:
            _require_person_in_project(sess, body.person_id, row.project_id)
        else:
            _require_group_in_project(sess, body.group_id, row.project_id)

        lat, lon = body.lat, body.lon
        if body.geo_mode != "custom":
            lat, lon = _resolve_geo(sess, row.project_id, body.date, body.geo_mode)

        row.person_id = body.person_id
        row.group_id = body.group_id
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


@router.delete("/{encounter_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete an encounter")
def delete_encounter(
    encounter_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Delete an encounter and its timeline item (the person is left in place)."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = _get_owned_encounter(sess, encounter_id, user_info_id)
        item_row = sess.exec(
            select(DBProjectItem).where(DBProjectItem.encounter_id == encounter_id)
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

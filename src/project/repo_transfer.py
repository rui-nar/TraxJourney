"""Project file (``.traxj``) ingestion into the DB — the import endpoint's back end.

Part of the ``ProjectRepo`` mixin split — see ``src/project/project_repo.py``
for the composed class and module docstring.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Dict, Optional

from sqlmodel import Session

from models.project_db import DBEncounter, DBMemory, DBPerson, DBPersonGroup, DBProject, DBProjectItem
from src.models.person import polarsteps_from_socials
from src.project.project_io import ProjectIO
from src.project.repo_core import _compute_low_res_geo


class ImportExportMixin:
    """Project file ingestion into the DB."""

    def ingest_project(
        self, sess: Session, user_info_id: int, path: str
    ) -> None:
        """Parse a ``.traxj`` project file and write it into the DB.

        The DB project name is derived from the **filename** (minus extension)
        so it stays consistent with the URL slug the API has always used.

        Idempotent: if the project already exists in the DB, the call is a
        no-op.  Activity rows are upserted so enriched data is never overwritten.

        After a successful ingest the file is renamed to ``*.migrated``
        so repeated calls are O(1) rather than re-reading the file each time.
        """
        # Use the filename-derived name as the DB key (matches the legacy URL slug)
        basename = os.path.basename(path)
        ext = ProjectIO.EXTENSION
        db_name = basename[: -len(ext)] if basename.endswith(ext) else basename

        project = ProjectIO.load(path)

        # Check for existing project before inserting
        row = self._get_project_row(sess, user_info_id, db_name)
        if row is not None:
            # Already migrated — just rename the file and return
            self._mark_migrated(path)
            return

        # 1. Upsert activities (do NOT overwrite enriched data if row exists)
        for act in project.activities:
            self._upsert_activity(sess, user_info_id, act)

        # 2. Create project row (name = filename slug, version from JSON)
        row = DBProject(
            user_info_id=user_info_id,
            name=db_name,
            version=project.version,
            filter_state_json=json.dumps({
                "start_date": project.filter_state.start_date,
                "end_date": project.filter_state.end_date,
                "activity_types": project.filter_state.activity_types,
            }),
            day_meta_json=json.dumps({
                dk: {"difficulty": dm.difficulty, "sleeping": dm.sleeping,
                     "weather": dm.weather, "journal": dm.journal,
                     "tags": dm.tags}
                for dk, dm in project.day_meta.items()
            }),
            sleeping_options_json=json.dumps(project.sleeping_options),
            low_res_geo_json=_compute_low_res_geo(project),
        )
        sess.add(row)
        sess.flush()  # populate row.id

        # 2a. Create groups first, mapping each file group id → new DB id so people
        # can be re-linked to their group below (issue #50).
        group_id_map: Dict[int, int] = {}
        for group in project.groups:
            g_row = DBPersonGroup(
                project_id=row.id,
                name=group.name,
                nationalities_json=json.dumps(group.nationalities) if group.nationalities else None,
                socials_json=json.dumps(group.socials) if group.socials else None,
            )
            sess.add(g_row)
            sess.flush()
            if group.id is not None:
                group_id_map[group.id] = g_row.id

        # 2b. Create people rows, mapping each file person id → new DB id so
        # encounter items can be re-linked below (issue #40).
        person_id_map: Dict[int, int] = {}
        for person in project.people:
            p_row = DBPerson(
                project_id=row.id,
                name=person.name,
                email=person.email,
                phone=person.phone,
                # Mirror the polarsteps handle out of socials so the shared-trip
                # view keeps working; fall back to any legacy standalone value.
                polarsteps=polarsteps_from_socials(person.socials) or person.polarsteps,
                notes=person.notes,
                avatar_photo=person.avatar_photo,
                socials_json=json.dumps(person.socials) if person.socials else None,
                nationalities_json=json.dumps(person.nationalities) if person.nationalities else None,
                residence=person.residence,
                group_id=group_id_map.get(person.group_id) if person.group_id is not None else None,
            )
            sess.add(p_row)
            sess.flush()
            if person.id is not None:
                person_id_map[person.id] = p_row.id

        # 3. Create project_item rows
        for pos, item in enumerate(project.items):
            memory_id: Optional[int] = None
            encounter_id: Optional[int] = None
            if item.item_type == "encounter" and item.encounter is not None:
                enc = item.encounter
                mapped_person = person_id_map.get(enc.person_id) if enc.person_id is not None else None
                mapped_group = group_id_map.get(enc.group_id) if enc.group_id is not None else None
                if mapped_person is None and mapped_group is None:
                    continue  # orphan encounter (person/group missing) — skip
                enc_row = DBEncounter(
                    project_id=row.id,
                    person_id=mapped_person,
                    group_id=mapped_group,
                    date=enc.date,
                    time=enc.time,
                    description=enc.description,
                    geo_mode=enc.geo_mode,
                    lat=enc.lat,
                    lon=enc.lon,
                )
                sess.add(enc_row)
                sess.flush()
                encounter_id = enc_row.id
            elif item.item_type == "memory" and item.memory is not None:
                # Persist the memory row so we get its DB id
                mem = item.memory
                mem_row = DBMemory(
                    project_id=row.id,
                    public_id=mem.public_id or uuid.uuid4().hex,
                    name=mem.name,
                    date=mem.date,
                    time=mem.time,
                    description=mem.description,
                    photos_json=json.dumps(mem.photos),
                    geo_mode=mem.geo_mode,
                    lat=mem.lat,
                    lon=mem.lon,
                )
                sess.add(mem_row)
                sess.flush()
                memory_id = mem_row.id

            db_item = DBProjectItem(
                project_id=row.id,
                position=pos,
                uid=uuid.uuid4().hex,
                item_type=item.item_type,
                activity_id=item.activity_id if item.item_type == "activity" else None,
                segment_json=(
                    json.dumps(ProjectIO._serialise_item(item)["segment"])
                    if item.item_type == "segment" else None
                ),
                segment_id=(
                    item.segment.id or None
                    if item.item_type == "segment" and item.segment is not None else None
                ),
                memory_id=memory_id,
                encounter_id=encounter_id,
            )
            sess.add(db_item)

        sess.commit()
        self._mark_migrated(path)

    @staticmethod
    def _mark_migrated(path: str) -> None:
        """Rename a project file to *.migrated to prevent re-ingestion."""
        try:
            os.rename(path, path + ".migrated")
        except OSError:
            pass  # not critical — ingest is idempotent anyway

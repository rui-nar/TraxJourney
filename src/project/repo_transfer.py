"""Project file (``.traxj``) ingestion into the DB — the import endpoint's back end.

Part of the ``ProjectRepo`` mixin split — see ``src/project/project_repo.py``
for the composed class and module docstring.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
import time
import uuid
from typing import Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models.project_db import (
    DBEncounter,
    DBJournalEntry,
    DBMemory,
    DBMemoryComment,
    DBMemoryLike,
    DBMemoryTranslation,
    DBPerson,
    DBPersonGroup,
    DBProject,
    DBProjectItem,
    DBShareMemoryContent,
)
from src.models.activity import is_activity_id
from src.models.person import polarsteps_from_socials
from src.models.project import Project
from src.project.local_ids import allocate_local_activity_id
from src.project.project_io import ProjectIO
from src.project.repo_core import _compute_low_res_geo, bump_lock_version


class ProjectNameTaken(Exception):
    """The owner already has a trip called ``name`` (issue #452)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


#: A trailing " (n)" counter, as "Keep both" appends it. Only a positive
#: integer after a space counts, so "X(2)" or "Day (one)" are plain names.
_COUNTER = re.compile(r"^(?P<base>.+) \((?P<n>[1-9][0-9]*)\)$")

#: How many times an import picks again after losing a race for a name or a
#: memory public_id to a concurrent request. Each loss means another request
#: committed in between; five in a row is not a race, it is a bug.
_ATTEMPTS = 5


def copy_name(name: str, taken: Set[str]) -> str:
    """*name* if it is free, else the first free "<base> (n)", n >= 2.

    A name that already ends in a counter counts on from its base, so a copy
    of "Alps (2)" is "Alps (3)", not "Alps (2) (2)".
    """
    if name not in taken:
        return name
    match = _COUNTER.match(name)
    base = match.group("base") if match else name
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


def _violates(exc: IntegrityError, *markers: str) -> bool:
    # SQLite names the columns ("project.user_info_id, project.name"),
    # PostgreSQL the index; accept either.
    message = str(exc.orig)
    return any(marker in message for marker in markers)


def _is_name_clash(exc: IntegrityError) -> bool:
    return _violates(exc, "project.user_info_id, project.name", "uq_project_user_name")


def _is_public_id_clash(exc: IntegrityError) -> bool:
    return _violates(exc, "memory.public_id", "ix_memory_public_id")


class ImportExportMixin:
    """Project file ingestion into the DB."""

    def _as_importers_activities(
        self, sess: Session, user_info_id: int, project: Project,
        held: Set[int] = frozenset(),
    ) -> Project:
        """*project* with every activity it holds made the importer's own.

        An activity row belongs to the account that created it, and a file can
        name any id. The file's activities whose id another account holds
        become the importer's own copies under fresh local ids: the file's
        content is kept, the other account's row is neither written nor
        referenced. An item naming an activity the file does not carry is kept
        only if that activity is already the importer's: one naming someone
        else's, or an id nobody holds yet, would start naming whichever
        account later creates it. Returns a new project; the one given is not
        changed.

        *held* are the activities the trip being written already holds, when
        it is an existing trip (a replace): those stay as they are, whoever
        owns them, as they do through any save of the trip.
        """
        activities = [a for a in project.activities if is_activity_id(a.id)]
        carried = {a.id for a in activities}
        owners = self.activity_owners(sess, list(carried) + [
            it.activity_id for it in project.items if it.item_type == "activity"])
        others = {aid for aid in carried
                  if aid not in held and owners.get(aid, user_info_id) != user_info_id}

        new_ids: Dict[int, int] = {}
        for n, act in enumerate(activities):
            if act.id in others:
                new_ids.setdefault(act.id, allocate_local_activity_id(sess))
                activities[n] = dataclasses.replace(act, id=new_ids[act.id])
        items = []
        for item in project.items:
            if item.item_type == "activity" and item.activity_id is not None:
                aid = item.activity_id
                if aid in new_ids:
                    item = dataclasses.replace(item, activity_id=new_ids[aid])
                elif not is_activity_id(aid) or (
                        aid not in carried and aid not in held
                        and owners.get(aid) != user_info_id):
                    continue
            items.append(item)

        mine = copy.copy(project)
        mine.activities = activities
        mine.items = items
        mine.rebuild_map()
        return mine

    def _taken_names(self, sess: Session, user_info_id: int) -> Set[str]:
        """Every trip name the owner has."""
        return set(sess.exec(
            select(DBProject.name).where(DBProject.user_info_id == user_info_id)
        ).all())

    def import_project(
        self, sess: Session, user_info_id: int, name: str, project: Project,
        *, copy: bool = False,
    ) -> str:
        """Write a parsed ``.traxj`` project as a new trip; return its name.

        ``copy=False``: under *name* or not at all. ``copy=True`` ("Keep
        both", issue #452): under *name* if free, else under
        :func:`copy_name`. Raises :class:`ProjectNameTaken` when *name* is
        taken and no copy was asked for, including when a concurrent request
        took it first.

        The unique index on (owner, name) and the unique memory public_id are
        what settle a race: the loser's insert fails, everything it wrote is
        rolled back, and it picks again from what is committed now.
        """
        for attempt in range(_ATTEMPTS):
            taken = self._taken_names(sess, user_info_id)
            target = copy_name(name, taken) if copy else name
            if target in taken:
                raise ProjectNameTaken(name)
            try:
                self.ingest_project(sess, user_info_id, target, project)
                return target
            except IntegrityError as exc:
                sess.rollback()
                retry = _is_name_clash(exc) or _is_public_id_clash(exc)
                if not retry or attempt == _ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def ingest_project(
        self, sess: Session, user_info_id: int, db_name: str, project: Project
    ) -> None:
        """Write a parsed ``.traxj`` project into the DB under ``db_name``.

        The caller derives ``db_name`` from the uploaded **filename** (minus
        extension), not from the name inside the file, so it stays consistent
        with the URL slug the API has always used.

        Takes the parsed project rather than a path so an import never has to
        land the upload on disk (issue #434).

        Always creates a trip: if ``db_name`` is taken, the insert fails with
        an ``IntegrityError`` and the session must be rolled back. Callers go
        through :meth:`import_project`, which decides the name. Activity rows
        are upserted so enriched data is never overwritten.
        """
        project = self._as_importers_activities(sess, user_info_id, project)

        # 1. Create the project row first (name = filename slug, version from
        # JSON), so a taken name fails before anything else is written.
        row = DBProject(user_info_id=user_info_id, name=db_name)
        _set_content_columns(row, project)
        sess.add(row)
        sess.flush()  # populate row.id

        self._write_content(sess, user_info_id, row.id, project)
        sess.commit()

    def replace_project(
        self, sess: Session, user_info_id: int, name: str, project: Project
    ) -> Optional[List["PhotoRemoval"]]:
        """Overwrite the content of the owner's trip *name* with *project*.

        "Replace" on import (issue #452). The trip itself is kept: its row
        (id, name, creation time, share links), companions, invites, sync
        settings, and the settings a .traxj import does not read. What the
        file carries is rewritten from it:

        * the timeline, people, groups and encounters;
        * memories, matched to the trip's own by ``public_id`` and updated in
          place, so their photos on disk, comments, likes and deep links
          survive (translations and shared content go if the text changed);
          the others are deleted with everything hanging off them;
        * the owner's journal entries, matched by the exported id in the same
          way. Companions' private entries are not in the owner's export and
          are kept, placed back by date.

        ``lock_version`` is advanced in SQL before any item row is touched, so
        an editor still open on the old content gets a conflict (issue #397).

        Returns the photo files the caller must delete once this has
        committed, or None if the trip does not exist (any more).
        """
        row = self._get_project_row(sess, user_info_id, name)
        if row is None:
            return None
        project_id = row.id
        bump_lock_version(sess, project_id)
        removals: List[PhotoRemoval] = []

        # The same ownership rules as any import, except that what the trip
        # already holds stays, as through any save of it: a companion's
        # activity in the owner's export is not the owner's to copy.
        held = set(sess.exec(select(DBProjectItem.activity_id).where(
            DBProjectItem.project_id == project_id,
            DBProjectItem.item_type == "activity",
        )).all())
        project = self._as_importers_activities(sess, user_info_id, project, held=held)

        # Memories: the trip's own, by public_id. Those the file still has are
        # kept for _write_content to update; the rest go.
        wanted = {
            it.memory.public_id for it in project.items
            if it.item_type == "memory" and it.memory is not None
            and isinstance(it.memory.public_id, str)
        }
        kept_memories: Dict[str, DBMemory] = {}
        for mem in sess.exec(select(DBMemory).where(DBMemory.project_id == project_id)).all():
            if mem.public_id in wanted:
                kept_memories[mem.public_id] = mem
                continue
            removals.append(PhotoRemoval(
                user_info_id, "memories", mem.id, _photos(mem.photos_json), True))
            for model in (DBMemoryComment, DBMemoryLike, DBMemoryTranslation,
                          DBShareMemoryContent):
                sess.execute(delete(model).where(model.memory_id == mem.id))
            sess.delete(mem)

        # Journal: the owner's own entries (NULL author = the owner, #106), by
        # the id the export carries. Companions' entries stay as they are.
        wanted_journal_ids = {
            it.journal.id for it in project.items
            if it.item_type == "journal" and it.journal is not None
            and type(it.journal.id) is int
        }
        kept_journals: Dict[int, DBJournalEntry] = {}
        companion_journals: List[DBJournalEntry] = []
        for entry in sess.exec(
            select(DBJournalEntry).where(DBJournalEntry.project_id == project_id)
        ).all():
            author = entry.user_info_id if entry.user_info_id is not None else user_info_id
            if author != user_info_id:
                companion_journals.append(entry)
            elif entry.id in wanted_journal_ids:
                kept_journals[entry.id] = entry
            else:
                removals.append(PhotoRemoval(
                    user_info_id, "journal", entry.id, _photos(entry.photos_json), True))
                sess.delete(entry)

        for model in (DBProjectItem, DBEncounter):
            sess.execute(delete(model).where(model.project_id == project_id))

        # People and groups: by the id the export carries, like journal
        # entries. A person's avatar lives in a folder named by their id, so a
        # person kept is a person updated in place, not recreated.
        wanted_people = {p.id for p in project.people if type(p.id) is int}
        kept_people: Dict[int, DBPerson] = {}
        for person in sess.exec(select(DBPerson).where(DBPerson.project_id == project_id)).all():
            if person.id in wanted_people:
                kept_people[person.id] = person
                continue
            if person.avatar_photo:
                removals.append(PhotoRemoval(
                    user_info_id, "people", person.id, [person.avatar_photo], True))
            sess.delete(person)
        wanted_groups = {g.id for g in project.groups if type(g.id) is int}
        kept_groups: Dict[int, DBPersonGroup] = {}
        for group in sess.exec(
            select(DBPersonGroup).where(DBPersonGroup.project_id == project_id)
        ).all():
            if group.id in wanted_groups:
                kept_groups[group.id] = group
            else:
                sess.delete(group)

        _set_content_columns(row, project)
        row.stats_json = None  # recomputed on the next read
        row.updated_at = time.time()
        sess.add(row)
        sess.flush()

        removals += self._write_content(
            sess, user_info_id, project_id, project,
            kept_memories=kept_memories, kept_journals=kept_journals,
            kept_people=kept_people, kept_groups=kept_groups,
            companion_journals=companion_journals,
        )
        sess.commit()
        return removals

    def _write_content(
        self, sess: Session, user_info_id: int, project_id: int, project: Project,
        *,
        kept_memories: Optional[Dict[str, DBMemory]] = None,
        kept_journals: Optional[Dict[int, DBJournalEntry]] = None,
        kept_people: Optional[Dict[int, DBPerson]] = None,
        kept_groups: Optional[Dict[int, DBPersonGroup]] = None,
        companion_journals: Sequence[DBJournalEntry] = (),
    ) -> List["PhotoRemoval"]:
        """Write *project*'s activities, people, groups and timeline into the
        trip *project_id*, whose item rows are empty.

        *kept_memories* (by public_id), *kept_journals*, *kept_people* and
        *kept_groups* (by id) are existing rows the file's entries update in
        place instead of creating new ones;
        *companion_journals* are other users' journal entries whose timeline
        items are placed back among the file's by date. Returns the photos
        the in-place updates dropped.
        """
        kept_memories = dict(kept_memories or {})
        kept_journals = dict(kept_journals or {})
        kept_people = dict(kept_people or {})
        kept_groups = dict(kept_groups or {})
        removals: List[PhotoRemoval] = []

        # 1a. Upsert activities (do NOT overwrite enriched data if row exists)
        for act in project.activities:
            self._upsert_activity(sess, user_info_id, act)

        # A memory keeps its exported public_id, which share deep links address
        # it by (#15), only while no other memory holds it: a copy of a trip
        # that still exists must not take its memories' ids (issue #463).
        wanted = {
            it.memory.public_id for it in project.items
            if it.item_type == "memory" and it.memory is not None
            and isinstance(it.memory.public_id, str) and it.memory.public_id
        }
        used_public_ids: Set[str] = set(sess.exec(
            select(DBMemory.public_id).where(DBMemory.public_id.in_(wanted))
        ).all()) if wanted else set()

        # 2a. Create groups first, mapping each file group id → new DB id so people
        # can be re-linked to their group below (issue #50).
        group_id_map: Dict[int, int] = {}
        for group in project.groups:
            g_row = kept_groups.pop(group.id, None) if type(group.id) is int else None
            if g_row is None:
                g_row = DBPersonGroup(project_id=project_id)
            g_row.name = group.name
            g_row.nationalities_json = json.dumps(group.nationalities) if group.nationalities else None
            g_row.socials_json = json.dumps(group.socials) if group.socials else None
            sess.add(g_row)
            sess.flush()
            if group.id is not None:
                group_id_map[group.id] = g_row.id

        # 2b. Create people rows, mapping each file person id → new DB id so
        # encounter items can be re-linked below (issue #40).
        person_id_map: Dict[int, int] = {}
        for person in project.people:
            p_row = kept_people.pop(person.id, None) if type(person.id) is int else None
            if p_row is None:
                p_row = DBPerson(project_id=project_id)
            elif p_row.avatar_photo and p_row.avatar_photo != person.avatar_photo:
                # The file names another avatar (or none): the old one's files go.
                removals.append(PhotoRemoval(
                    user_info_id, "people", p_row.id, [p_row.avatar_photo]))
            p_row.name = person.name
            p_row.email = person.email
            p_row.phone = person.phone
            # Mirror the polarsteps handle out of socials so the shared-trip
            # view keeps working; fall back to any legacy standalone value.
            p_row.polarsteps = polarsteps_from_socials(person.socials) or person.polarsteps
            p_row.notes = person.notes
            p_row.avatar_photo = person.avatar_photo
            p_row.socials_json = json.dumps(person.socials) if person.socials else None
            p_row.nationalities_json = (
                json.dumps(person.nationalities) if person.nationalities else None)
            p_row.residence = person.residence
            p_row.group_id = (
                group_id_map.get(person.group_id) if person.group_id is not None else None)
            sess.add(p_row)
            sess.flush()
            if person.id is not None:
                person_id_map[person.id] = p_row.id

        # 3. Create project_item rows
        db_items: List[Tuple[Optional[str], DBProjectItem]] = []
        for item in project.items:
            memory_id: Optional[int] = None
            journal_id: Optional[int] = None
            encounter_id: Optional[int] = None
            if item.item_type == "encounter" and item.encounter is not None:
                enc = item.encounter
                mapped_person = person_id_map.get(enc.person_id) if enc.person_id is not None else None
                mapped_group = group_id_map.get(enc.group_id) if enc.group_id is not None else None
                if mapped_person is None and mapped_group is None:
                    continue  # orphan encounter (person/group missing) — skip
                enc_row = DBEncounter(
                    project_id=project_id,
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
                mem = item.memory
                mem_row = kept_memories.pop(mem.public_id, None) if isinstance(
                    mem.public_id, str) else None
                if mem_row is not None:
                    # Same memory: update in place, keeping its id — and with
                    # it its photos on disk, comments, likes and deep links.
                    removals += _update_memory(sess, user_info_id, mem_row, mem)
                    used_public_ids.add(mem_row.public_id)
                else:
                    public_id = mem.public_id
                    if (not isinstance(public_id, str) or not public_id
                            or public_id in used_public_ids):
                        public_id = uuid.uuid4().hex
                    used_public_ids.add(public_id)
                    mem_row = DBMemory(
                        project_id=project_id,
                        public_id=public_id,
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
            elif item.item_type == "journal" and item.journal is not None:
                # An imported journal entry is the importer's, like one they
                # wrote. It used to get no row at all, and its item then read
                # back as a segment at (0, 0).
                entry = item.journal
                j_row = kept_journals.pop(entry.id, None) if type(entry.id) is int else None
                if j_row is not None:
                    removals += _update_journal(sess, user_info_id, j_row, entry)
                else:
                    j_row = DBJournalEntry(
                        project_id=project_id,
                        user_info_id=user_info_id,
                        date=entry.date,
                        time=entry.time,
                        description=entry.description,
                        photos_json=json.dumps(entry.photos),
                        geo_mode=entry.geo_mode,
                        lat=entry.lat,
                        lon=entry.lon,
                    )
                    sess.add(j_row)
                    sess.flush()
                journal_id = j_row.id

            db_items.append((_item_day(item, project), DBProjectItem(
                project_id=project_id,
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
                journal_id=journal_id,
                encounter_id=encounter_id,
            )))

        # Other users' journal entries go back after the last item of their day
        # or earlier; before everything if the file has nothing that early.
        for entry in sorted(companion_journals, key=lambda e: (e.date or "", e.time or "")):
            at = 0
            for n, (day, _row) in enumerate(db_items):
                if day is not None and day <= (entry.date or ""):
                    at = n + 1
            db_items.insert(at, (entry.date, DBProjectItem(
                project_id=project_id, uid=uuid.uuid4().hex,
                item_type="journal", journal_id=entry.id,
            )))

        for pos, (_day, db_item) in enumerate(db_items):
            db_item.position = pos
            sess.add(db_item)
        return removals


@dataclasses.dataclass
class PhotoRemoval:
    """Photo files to delete once a replace has committed.

    ``kind`` is the directory under the user's tree ("memories" or
    "journal"), ``content_id`` the memory or journal entry id naming the
    folder. ``remove_dir`` when the entry itself is gone.
    """
    user_info_id: int
    kind: str
    content_id: int
    uuids: List[str]
    remove_dir: bool = False


def _photos(photos_json: Optional[str]) -> List[str]:
    try:
        photos = json.loads(photos_json or "[]")
    except ValueError:
        return []
    return [p for p in photos if isinstance(p, str)] if isinstance(photos, list) else []


def _update_memory(sess: Session, owner_id: int, row: DBMemory, mem) -> List[PhotoRemoval]:
    text_changed = (row.name, row.description) != (mem.name, mem.description)
    dropped = [p for p in _photos(row.photos_json) if p not in set(mem.photos or [])]
    row.name = mem.name
    row.date = mem.date
    row.time = mem.time
    row.description = mem.description
    row.photos_json = json.dumps(mem.photos)
    row.geo_mode = mem.geo_mode
    row.lat = mem.lat
    row.lon = mem.lon
    sess.add(row)
    if text_changed:
        # Both are derived from the old text: a stale translation, or a share
        # link still showing what the owner just replaced.
        for model in (DBMemoryTranslation, DBShareMemoryContent):
            sess.execute(delete(model).where(model.memory_id == row.id))
    return [PhotoRemoval(owner_id, "memories", row.id, dropped)] if dropped else []


def _update_journal(sess: Session, owner_id: int, row: DBJournalEntry, entry) -> List[PhotoRemoval]:
    dropped = [p for p in _photos(row.photos_json) if p not in set(entry.photos or [])]
    row.date = entry.date
    row.time = entry.time
    row.description = entry.description
    row.photos_json = json.dumps(entry.photos)
    row.geo_mode = entry.geo_mode
    row.lat = entry.lat
    row.lon = entry.lon
    sess.add(row)
    return [PhotoRemoval(owner_id, "journal", row.id, dropped)] if dropped else []


def _item_day(item, project: Project) -> Optional[str]:
    """The "YYYY-MM-DD" an item belongs to, when it has one."""
    if item.item_type == "activity":
        act = project.activity_by_id(item.activity_id) if item.activity_id is not None else None
        when = act.start_date_local if act is not None else None
        return when.isoformat()[:10] if when is not None else None
    content = getattr(item, item.item_type, None)
    day = getattr(content, "date", None)
    return day[:10] if isinstance(day, str) and day else None


def _set_content_columns(row: DBProject, project: Project) -> None:
    """The trip columns a .traxj import writes: the rest it does not read."""
    row.version = project.version
    row.filter_state_json = json.dumps({
        "start_date": project.filter_state.start_date,
        "end_date": project.filter_state.end_date,
        "activity_types": project.filter_state.activity_types,
    })
    row.day_meta_json = json.dumps({
        dk: {"difficulty": dm.difficulty, "sleeping": dm.sleeping,
             "weather": dm.weather, "journal": dm.journal,
             "tags": dm.tags}
        for dk, dm in project.day_meta.items()
    })
    row.sleeping_options_json = json.dumps(project.sleeping_options)
    row.low_res_geo_json = _compute_low_res_geo(project)

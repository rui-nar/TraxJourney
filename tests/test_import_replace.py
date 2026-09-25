"""Importing over a trip the user already has: "Replace" (issue #452).

``POST /api/projects/import?on_conflict=replace`` overwrites the content of the
existing trip with the file, and keeps the trip itself: its record, its share
links, its companions and invites, and the settings a .traxj import does not
read. What the file carries is rewritten from it:

* the timeline, people, groups and encounters;
* memories, matched to the trip's own by ``public_id`` and updated in place,
  so their photos, comments, likes and deep links survive; memories the file
  no longer has are deleted with their photos, comments, likes, translations
  and shared content;
* the owner's journal entries. A companion's private journal is not in the
  owner's export and is kept.

``lock_version`` advances before any item row is touched, so an editor open
on the old content gets a conflict instead of overwriting the new one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.journal as journal_mod
import api.memories as memories_mod
import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.billing import UserUsage
from models.project_db import (
    DBActivity,
    DBEncounter,
    DBJournalEntry,
    DBMemory,
    DBMemoryComment,
    DBMemoryLike,
    DBMemoryTranslation,
    DBPerson,
    DBProject,
    DBProjectInvite,
    DBProjectItem,
    DBProjectMember,
    DBShareMemoryContent,
)
from models.user import UserInfo
from src.project.project_io import ProjectIO


@pytest.fixture
def env(monkeypatch, tmp_path):
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, storage_mod, journal_mod, memories_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n.title(), email=f"{n}@e.com")
                 for n in ("owner", "companion")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}

    current = {"uid": ids["owner"]}
    router.app.dependency_overrides[get_current_user] = (
        lambda: {"sub": str(current["uid"])})

    def act_as(who: str) -> None:
        current["uid"] = ids[who]

    try:
        yield (TestClient(router.app, raise_server_exceptions=False),
               engine, ids, act_as, tmp_path)
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


_POLYLINE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


def _activity(aid: int, name: str) -> dict:
    return {"id": aid, "name": name, "type": "Ride",
            "start_date": "2024-06-01T08:00:00Z",
            "start_date_local": "2024-06-01T08:00:00Z",
            "map": {"summary_polyline": _POLYLINE}}


def _memory(name: str, public_id: str, description: str = "", photos=()) -> dict:
    return {"item_type": "memory", "memory": {
        "name": name, "date": "2024-06-01", "description": description,
        "public_id": public_id, "photos": list(photos),
        "geo_mode": "custom", "lat": 45.0, "lon": 6.0}}


def _doc(items: list, *, activities=(), people=(), journal=()) -> bytes:
    items = list(items) + [
        {"item_type": "journal", "journal": {
            "date": "2024-06-01", "description": text,
            "geo_mode": "custom", "lat": 45.0, "lon": 6.0}}
        for text in journal]
    return json.dumps({
        "version": 1, "name": "ignored",
        "filter_state": {"start_date": None, "end_date": None, "activity_types": None},
        "items": items, "activities": list(activities), "people": list(people),
    }).encode("utf-8")


def _import(client, name: str, content: bytes, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    return client.post(
        "/api/projects/import", params=params,
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})


def _project(engine, uid: int, name: str = "Alps") -> DBProject:
    with Session(engine) as sess:
        row = sess.exec(select(DBProject).where(
            DBProject.user_info_id == uid, DBProject.name == name)).one()
        sess.expunge(row)
        return row


def _rows(engine, model, **where) -> list:
    with Session(engine) as sess:
        q = select(model)
        for col, val in where.items():
            q = q.where(getattr(model, col) == val)
        rows = sess.exec(q).all()
        for r in rows:
            sess.expunge(r)
        return rows


def _item_types(engine, project_id: int) -> list[str]:
    with Session(engine) as sess:
        return [i.item_type for i in sess.exec(
            select(DBProjectItem).where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.position))]


@pytest.fixture
def alps(env):
    """The owner's trip "Alps": one activity, two memories (Lake with a photo
    on disk, a comment, a like, a translation and shared content; Summit),
    one person with an encounter, the owner's journal entry, a companion with
    their own journal entry, a share link, an invite link, and settings the
    import does not read."""
    client, engine, ids, act_as, data_dir = env
    person = {"id": 1, "name": "Ann"}
    r = _import(client, "Alps", _doc(
        [{"item_type": "activity", "activity_id": 1},
         _memory("Lake", "pub-lake", "old text", photos=["ph1"]),
         _memory("Summit", "pub-summit"),
         {"item_type": "encounter", "encounter": {"person_id": 1, "date": "2024-06-01"}}],
        activities=[_activity(1, "Ride")], people=[person],
        journal=["owner old note"]))
    assert r.status_code == 201, r.text
    project = _project(engine, ids["owner"])

    token = client.post("/api/projects/Alps/members/invite").json()["token"]
    act_as("companion")
    assert client.post(f"/api/invites/{token}/accept").status_code == 200
    r = client.post(f"/api/journal/?owner={ids['owner']}", json={
        "project_name": "Alps", "date": "2024-06-01", "geo_mode": "custom",
        "lat": 45.0, "lon": 6.0, "description": "companion private note"})
    assert r.status_code == 201, r.text
    act_as("owner")
    share = client.post("/api/projects/Alps/share").json()["share_token"]

    lake = next(m for m in _rows(engine, DBMemory, project_id=project.id)
                if m.name == "Lake")
    photo_dir = Path(data_dir) / "users" / str(ids["owner"]) / "memories" / str(lake.id)
    photo_dir.mkdir(parents=True)
    for suffix in ("", "_thumb"):
        (photo_dir / f"ph1{suffix}.jpg").write_bytes(b"x" * 1000)
    with Session(engine) as sess:
        sess.add(UserUsage(user_info_id=ids["owner"], storage_bytes=5000))
        sess.add(DBMemoryComment(memory_id=lake.id, user_info_id=ids["companion"], text="nice"))
        sess.add(DBMemoryLike(memory_id=lake.id, user_info_id=ids["companion"]))
        sess.add(DBMemoryTranslation(memory_id=lake.id, lang_code="fr", name="Lac"))
        sess.add(DBShareMemoryContent(memory_id=lake.id, name_ciphertext="c"))
        row = sess.get(DBProject, project.id)
        row.track_color = "#123456"
        row.trip_end = "2024-06-30"
        sess.add(row)
        sess.commit()
    return env, project, lake, share, photo_dir


def _usage(engine, uid: int) -> int:
    with Session(engine) as sess:
        return sess.exec(select(UserUsage.storage_bytes).where(
            UserUsage.user_info_id == uid)).one()


# ── Replace ─────────────────────────────────────────────────────────────────

def test_replace_rewrites_the_content_and_keeps_the_trip(alps):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps

    r = _import(client, "Alps", _doc(
        [_memory("New place", "pub-new"), {"item_type": "activity", "activity_id": 1}],
        activities=[_activity(1, "Ride")]), on_conflict="replace")

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps", "outcome": "replaced"}
    after = _project(engine, ids["owner"])
    assert after.id == project.id
    assert after.created_at == project.created_at
    assert after.lock_version > project.lock_version
    assert _item_types(engine, project.id) == ["memory", "activity", "journal"]
    assert [m.name for m in _rows(engine, DBMemory, project_id=project.id)] == ["New place"]
    assert _rows(engine, DBPerson, project_id=project.id) == []
    assert _rows(engine, DBEncounter, project_id=project.id) == []
    # What the trip is, rather than what it holds, is kept.
    assert after.share_token == share
    assert after.track_color == "#123456" and after.trip_end == "2024-06-30"
    assert [m.user_info_id for m in _rows(engine, DBProjectMember, project_id=project.id)] == [
        ids["companion"]]
    assert len(_rows(engine, DBProjectInvite, project_id=project.id)) == 1
    assert "New place" in client.get(f"/api/share/{share}").text


def test_replace_updates_a_matching_memory_in_place(alps):
    (client, engine, ids, act_as, _), project, lake, share, photo_dir = alps

    r = _import(client, "Alps", _doc(
        [_memory("Lake", "pub-lake", "new text", photos=["ph1"])]), on_conflict="replace")

    assert r.status_code == 201, r.text
    (kept,) = _rows(engine, DBMemory, project_id=project.id)
    assert (kept.id, kept.public_id, kept.description) == (lake.id, "pub-lake", "new text")
    assert (photo_dir / "ph1.jpg").exists()
    assert len(_rows(engine, DBMemoryComment, memory_id=lake.id)) == 1
    assert len(_rows(engine, DBMemoryLike, memory_id=lake.id)) == 1
    # Derived from the old text, so no longer right.
    assert _rows(engine, DBMemoryTranslation, memory_id=lake.id) == []
    assert _rows(engine, DBShareMemoryContent, memory_id=lake.id) == []


def test_replace_keeps_derived_content_when_the_text_is_unchanged(alps):
    (client, engine, ids, act_as, _), project, lake, share, photo_dir = alps

    r = _import(client, "Alps", _doc(
        [_memory("Lake", "pub-lake", "old text", photos=["ph1"])]), on_conflict="replace")

    assert r.status_code == 201, r.text
    assert len(_rows(engine, DBMemoryTranslation, memory_id=lake.id)) == 1
    assert len(_rows(engine, DBShareMemoryContent, memory_id=lake.id)) == 1


def test_replace_deletes_a_memory_the_file_no_longer_has_with_all_it_holds(alps):
    (client, engine, ids, act_as, _), project, lake, share, photo_dir = alps
    before = _usage(engine, ids["owner"])

    r = _import(client, "Alps", _doc([_memory("Summit", "pub-summit")]),
                on_conflict="replace")

    assert r.status_code == 201, r.text
    assert [m.name for m in _rows(engine, DBMemory, project_id=project.id)] == ["Summit"]
    for model in (DBMemoryComment, DBMemoryLike, DBMemoryTranslation, DBShareMemoryContent):
        assert _rows(engine, model, memory_id=lake.id) == [], model
    assert not (photo_dir / "ph1.jpg").exists()
    assert not (photo_dir / "ph1_thumb.jpg").exists()
    assert _usage(engine, ids["owner"]) == before - 2000


def test_replace_keeps_a_companions_journal_and_rewrites_the_owners(alps):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps

    r = _import(client, "Alps", _doc([], journal=["owner new note"]), on_conflict="replace")

    assert r.status_code == 201, r.text
    notes = {(j.user_info_id, j.description)
             for j in _rows(engine, DBJournalEntry, project_id=project.id)}
    assert notes == {(ids["owner"], "owner new note"),
                     (ids["companion"], "companion private note")}
    act_as("companion")
    text = client.get(f"/api/projects/Alps?owner={ids['owner']}").text
    assert "companion private note" in text and "owner new note" not in text
    act_as("owner")
    text = client.get("/api/projects/Alps").text
    assert "owner new note" in text and "companion private note" not in text


def test_replace_refuses_a_stale_editors_save(alps):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps
    seen = client.get("/api/projects/Alps/activities/1/track").json()["lock_version"]

    r = _import(client, "Alps", _doc(
        [{"item_type": "activity", "activity_id": 1}],
        activities=[_activity(1, "Ride")]), on_conflict="replace")
    assert r.status_code == 201, r.text

    r = client.put("/api/projects/Alps/activities/1/track", json={
        "points": [{"lat": 48.0, "lng": 2.0}, {"lat": 48.1, "lng": 2.1}],
        "lock_version": seen})
    assert r.status_code == 409, r.text


def test_replace_shows_at_once_in_a_cached_view(alps):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps
    assert "Summit" in client.get("/api/projects/Alps/meta").text

    r = _import(client, "Alps", _doc([_memory("Fresh", "pub-fresh")]), on_conflict="replace")
    assert r.status_code == 201, r.text

    meta = client.get("/api/projects/Alps/meta").text
    assert "Fresh" in meta and "Summit" not in meta


def test_replace_mints_a_public_id_another_trip_holds(alps):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps
    assert _import(client, "Other", _doc([_memory("Elsewhere", "pub-other")])).status_code == 201

    r = _import(client, "Alps", _doc([_memory("Mine now", "pub-other")]), on_conflict="replace")

    assert r.status_code == 201, r.text
    (mem,) = _rows(engine, DBMemory, project_id=project.id)
    assert mem.public_id != "pub-other"
    other = _project(engine, ids["owner"], "Other")
    assert [m.public_id for m in _rows(engine, DBMemory, project_id=other.id)] == ["pub-other"]


def test_replace_does_not_count_against_the_trip_limit(alps, monkeypatch):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_PROJECTS", "1")

    r = _import(client, "Alps", _doc([]), on_conflict="replace")

    assert r.status_code == 201, r.text


def test_replace_on_a_free_name_just_imports_it(env):
    client, engine, ids, act_as, _ = env

    r = _import(client, "Alps", _doc([_memory("Lake", "pub-lake")]), on_conflict="replace")

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps", "outcome": "created"}


def test_a_malformed_file_never_replaces_anything(alps):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps

    r = _import(client, "Alps", b"{not json", on_conflict="replace")

    assert r.status_code == 400
    assert len(_rows(engine, DBMemory, project_id=project.id)) == 2


def test_replacing_a_trip_with_its_own_export_loses_nothing(alps):
    (client, engine, ids, act_as, data_dir), project, lake, share, photo_dir = alps
    exported = client.get("/api/projects/Alps/export-traxj").content
    memories = {m.public_id: m.id for m in _rows(engine, DBMemory, project_id=project.id)}
    journals = {(j.id, j.user_info_id, j.description)
                for j in _rows(engine, DBJournalEntry, project_id=project.id)}

    r = _import(client, "Alps", exported, on_conflict="replace")

    assert r.status_code == 201, r.text
    assert {m.public_id: m.id for m in _rows(engine, DBMemory, project_id=project.id)} == memories
    assert {(j.id, j.user_info_id, j.description)
            for j in _rows(engine, DBJournalEntry, project_id=project.id)} == journals
    assert (photo_dir / "ph1.jpg").exists()
    assert len(_rows(engine, DBMemoryComment, memory_id=lake.id)) == 1
    assert len(_rows(engine, DBMemoryTranslation, memory_id=lake.id)) == 1
    assert len(_rows(engine, DBPerson, project_id=project.id)) == 1
    assert len(_rows(engine, DBEncounter, project_id=project.id)) == 1
    assert sorted(_item_types(engine, project.id)) == sorted(
        ["activity", "memory", "memory", "encounter", "journal", "journal"])


def test_replace_deletes_the_photos_a_kept_memory_no_longer_lists(alps):
    (client, engine, ids, act_as, _), project, lake, share, photo_dir = alps
    before = _usage(engine, ids["owner"])

    r = _import(client, "Alps", _doc([_memory("Lake", "pub-lake", "old text")]),
                on_conflict="replace")

    assert r.status_code == 201, r.text
    assert not (photo_dir / "ph1.jpg").exists()
    assert _usage(engine, ids["owner"]) == before - 2000


# ── Activities: an activity row belongs to the account that created it ─────

def _outsider(engine) -> int:
    with Session(engine) as sess:
        bob = UserInfo(display_name="Bob", email="bob@e.com")
        sess.add(bob)
        sess.commit()
        sess.add(DBActivity(id=9001, user_info_id=bob.id, name="Bob ride",
                            summary_polyline=_POLYLINE))
        sess.commit()
        return bob.id


def _trip_activity_ids(engine, project_id: int) -> set:
    with Session(engine) as sess:
        return {i.activity_id for i in sess.exec(select(DBProjectItem).where(
            DBProjectItem.project_id == project_id,
            DBProjectItem.item_type == "activity"))}


def test_replace_copies_an_activity_another_account_holds_and_leaves_it_alone(alps):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps
    bob = _outsider(engine)

    r = _import(client, "Alps", _doc(
        [{"item_type": "activity", "activity_id": 9001},
         {"item_type": "activity", "activity_id": 9002}],
        activities=[_activity(9001, "Mine now")]), on_conflict="replace")

    assert r.status_code == 201, r.text
    with Session(engine) as sess:
        theirs = sess.get(DBActivity, 9001)
        assert (theirs.user_info_id, theirs.name) == (bob, "Bob ride")
    (copy_id,) = _trip_activity_ids(engine, project.id)
    assert copy_id < 0
    assert "Bob ride" not in client.get("/api/projects/Alps").text


def test_replace_keeps_a_companions_activity_the_trip_holds(alps):
    (client, engine, ids, act_as, _), project, lake, share, _dir = alps
    act_as("companion")
    r = client.post(f"/api/projects/Alps/activities?owner={ids['owner']}",
                    json={"activities": [_activity(7001, "Companion ride")]})
    assert r.json()["added"] == 1, r.text
    act_as("owner")
    exported = client.get("/api/projects/Alps/export-traxj").content

    r = _import(client, "Alps", exported, on_conflict="replace")

    assert r.status_code == 201, r.text
    assert _trip_activity_ids(engine, project.id) == {1, 7001}
    with Session(engine) as sess:
        row = sess.get(DBActivity, 7001)
        assert (row.user_info_id, row.name) == (ids["companion"], "Companion ride")

    # A copy is a new trip of the owner's: the companion's activity comes
    # along as the owner's own copy, and the companion's row stays theirs.
    r = _import(client, "Alps", exported, on_conflict="copy")
    assert r.status_code == 201, r.text
    copied = _project(engine, ids["owner"], r.json()["name"])
    ids_in_copy = _trip_activity_ids(engine, copied.id)
    assert 1 in ids_in_copy and 7001 not in ids_in_copy and len(ids_in_copy) == 2
    with Session(engine) as sess:
        assert sess.get(DBActivity, 7001).user_info_id == ids["companion"]

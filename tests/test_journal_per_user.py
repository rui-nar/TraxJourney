"""Per-user journal + companion photo pathing (issue #106, U2).

Journal entries are private to their author: with travel companions a shared
project holds entries from several users, and project reads only ever return
the requesting user's own (a NULL author is a legacy row owned by the project
owner). Only the author may edit/delete an entry. Memory photos, by contrast,
are shared trip content and live canonically under the project OWNER's data
dir regardless of which editor uploaded them.

Also covers the visible-index translation: item indices sent by a client refer
to its *visible* item list (other users' journal items are hidden), so
delete-at-index / reorder / insert_after_index must not land on hidden items.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.memories import router as memories_router
from api.project_items import router as project_items_router
from api.project_transfer import router as project_transfer_router
from api.projects import router as projects_router
from models.project_db import DBJournalEntry, DBProject, DBProjectItem, DBProjectMember
from models.user import UserInfo


def _jpeg_bytes(size=(20, 20), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """In-memory DB, one owner + one companion (already a member) on "Trip"."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)

    import api.journal as journal_mod
    import api.memories as mem_mod
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))

    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        companion = UserInfo(display_name="Companion", email="comp@e.com")
        sess.add(owner); sess.add(companion); sess.commit()
        sess.refresh(owner); sess.refresh(companion)
        proj = DBProject(user_info_id=owner.id, name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        sess.add(DBProjectMember(project_id=proj.id, user_info_id=companion.id,
                                 role="editor", invited_by=owner.id, created_at=0.0))
        sess.commit()
        ids = {"owner": owner.id, "companion": companion.id, "project": proj.id}

    current = {"uid": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(journal_router)
    app.include_router(memories_router)
    app.include_router(projects_router)
    app.include_router(project_items_router)
    app.include_router(project_transfer_router)

    client = TestClient(app)

    def act_as(who: str):
        current["uid"] = ids[who]

    return client, engine, ids, act_as


def _owner_q(ids) -> str:
    return f"?owner={ids['owner']}"


def _create_entry(client, ids, *, as_companion: bool, date: str) -> int:
    q = _owner_q(ids) if as_companion else ""
    r = client.post(f"/api/journal/{q}", json={
        "project_name": "Trip", "date": date, "geo_mode": "custom",
        "lat": 1.0, "lon": 2.0, "description": f"entry {date}",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _journal_ids_in_details(client, ids, *, as_companion: bool) -> list[int]:
    q = _owner_q(ids) if as_companion else ""
    r = client.get(f"/api/projects/Trip{q}")
    assert r.status_code == 200, r.text
    return [it["journal"]["id"] for it in r.json()["items"]
            if it["item_type"] == "journal"]


# ── Per-user visibility ───────────────────────────────────────────────────────

def test_each_editor_sees_only_own_journal_entries(env):
    client, engine, ids, act_as = env
    act_as("companion")
    comp_entry = _create_entry(client, ids, as_companion=True, date="2026-01-01")
    act_as("owner")
    own_entry = _create_entry(client, ids, as_companion=False, date="2026-01-02")

    with Session(engine) as sess:
        row = sess.get(DBJournalEntry, comp_entry)
        assert row.user_info_id == ids["companion"]
        assert sess.get(DBJournalEntry, own_entry).user_info_id == ids["owner"]

    assert _journal_ids_in_details(client, ids, as_companion=False) == [own_entry]
    act_as("companion")
    assert _journal_ids_in_details(client, ids, as_companion=True) == [comp_entry]

    # The lightweight meta endpoint filters identically.
    r = client.get(f"/api/projects/Trip/meta{_owner_q(ids)}")
    meta_journal = [it["journal"]["id"] for it in r.json()["items"]
                    if it["item_type"] == "journal"]
    assert meta_journal == [comp_entry]


def test_legacy_null_author_entry_belongs_to_owner(env):
    client, engine, ids, act_as = env
    with Session(engine) as sess:
        legacy = DBJournalEntry(project_id=ids["project"], date="2025-05-01",
                                geo_mode="custom", user_info_id=None)
        sess.add(legacy); sess.commit(); sess.refresh(legacy)
        sess.add(DBProjectItem(project_id=ids["project"], position=0,
                               item_type="journal", journal_id=legacy.id))
        sess.commit()
        legacy_id = legacy.id

    act_as("owner")
    assert _journal_ids_in_details(client, ids, as_companion=False) == [legacy_id]
    r = client.put(f"/api/journal/{legacy_id}", json={
        "date": "2025-05-02", "geo_mode": "custom", "lat": 1.0, "lon": 2.0,
    })
    assert r.status_code == 204

    act_as("companion")
    assert _journal_ids_in_details(client, ids, as_companion=True) == []
    r = client.put(f"/api/journal/{legacy_id}", json={
        "date": "2025-05-03", "geo_mode": "custom", "lat": 1.0, "lon": 2.0,
    })
    assert r.status_code == 403


def test_only_the_author_may_edit_or_delete(env):
    client, engine, ids, act_as = env
    act_as("companion")
    entry_id = _create_entry(client, ids, as_companion=True, date="2026-02-01")

    act_as("owner")
    body = {"date": "2026-02-02", "geo_mode": "custom", "lat": 0.0, "lon": 0.0}
    assert client.put(f"/api/journal/{entry_id}", json=body).status_code == 403
    assert client.delete(f"/api/journal/{entry_id}").status_code == 403
    upload = {"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")}
    assert client.post(f"/api/journal/{entry_id}/photos", files=upload).status_code == 403

    act_as("companion")
    assert client.put(f"/api/journal/{entry_id}", json=body).status_code == 204
    assert client.delete(f"/api/journal/{entry_id}").status_code == 204
    with Session(engine) as sess:
        assert sess.get(DBJournalEntry, entry_id) is None
        assert sess.exec(
            select(DBProjectItem).where(DBProjectItem.journal_id == entry_id)
        ).first() is None


def test_export_traxj_excludes_other_users_journal(env):
    client, _, ids, act_as = env
    act_as("owner")
    own_entry = _create_entry(client, ids, as_companion=False, date="2026-03-01")
    act_as("companion")
    comp_entry = _create_entry(client, ids, as_companion=True, date="2026-03-02")

    data = json.loads(client.get(f"/api/projects/Trip/export-traxj{_owner_q(ids)}").content)
    exported = [it["journal"]["id"] for it in data["items"] if it["item_type"] == "journal"]
    assert exported == [comp_entry]

    act_as("owner")
    data = json.loads(client.get("/api/projects/Trip/export-traxj").content)
    exported = [it["journal"]["id"] for it in data["items"] if it["item_type"] == "journal"]
    assert exported == [own_entry]


# ── Memory photos: canonical dir = project owner ─────────────────────────────

def test_companion_memory_photo_lands_in_owner_dir(env, tmp_path):
    client, _, ids, act_as = env
    act_as("companion")
    r = client.post(f"/api/memories/{_owner_q(ids)}", json={
        "project_name": "Trip", "date": "2026-01-05", "geo_mode": "custom",
        "lat": 1.0, "lon": 2.0, "name": "Shared moment",
    })
    assert r.status_code == 201, r.text
    memory_id = r.json()["id"]

    r = client.post(f"/api/memories/{memory_id}/photos",
                    files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 201, r.text
    photo_uuid = r.json()["uuid"]

    owner_file = (tmp_path / "users" / str(ids["owner"]) / "memories"
                  / str(memory_id) / f"{photo_uuid}.jpg")
    assert owner_file.exists()
    assert not (tmp_path / "users" / str(ids["companion"])).exists()

    # Both the owner and the uploading companion can serve it back.
    act_as("owner")
    assert client.get(f"/api/memories/{memory_id}/photos/{photo_uuid}").status_code == 200
    assert client.get(f"/api/memories/{memory_id}/photos/{photo_uuid}/thumb").status_code == 200
    act_as("companion")
    assert client.get(f"/api/memories/{memory_id}/photos/{photo_uuid}").status_code == 200


# ── Visible-index translation ─────────────────────────────────────────────────

def _seed_mixed_timeline(client, ids, act_as) -> dict:
    """Timeline: [memory A, companion journal J, memory B] (full positions 0/1/2).

    The owner's visible list is [A, B]; the companion's is [A, J, B].
    """
    act_as("owner")
    mem_a = client.post("/api/memories/", json={
        "project_name": "Trip", "date": "2026-04-01", "geo_mode": "custom",
        "lat": 0.0, "lon": 0.0, "name": "A",
    }).json()["id"]
    act_as("companion")
    entry_j = _create_entry(client, ids, as_companion=True, date="2026-04-02")
    act_as("owner")
    mem_b = client.post("/api/memories/", json={
        "project_name": "Trip", "date": "2026-04-03", "geo_mode": "custom",
        "lat": 0.0, "lon": 0.0, "name": "B",
    }).json()["id"]
    return {"A": mem_a, "J": entry_j, "B": mem_b}


def _ordered_items(engine, project_id) -> list[tuple]:
    with Session(engine) as sess:
        rows = sess.exec(
            select(DBProjectItem).where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.position)
        ).all()
        return [(r.item_type, r.memory_id or r.journal_id) for r in rows]


def test_delete_at_index_skips_hidden_journal_items(env):
    client, engine, ids, act_as = env
    seeded = _seed_mixed_timeline(client, ids, act_as)

    # Owner sees [A, B]; deleting visible index 1 must remove B, not J.
    act_as("owner")
    r = client.delete("/api/projects/Trip/items/1")
    assert r.status_code == 204, r.text
    assert _ordered_items(engine, ids["project"]) == [
        ("memory", seeded["A"]), ("journal", seeded["J"]),
    ]
    # And the visible index range is the owner's: [A] leaves only index 0.
    assert client.delete("/api/projects/Trip/items/1").status_code == 422


def test_insert_after_index_translates_to_full_position(env):
    client, engine, ids, act_as = env
    seeded = _seed_mixed_timeline(client, ids, act_as)

    # Owner inserts after visible index 0 (A) → must land between A and J/B,
    # never counting the hidden journal item.
    act_as("owner")
    mem_c = client.post("/api/memories/", json={
        "project_name": "Trip", "date": "2026-04-04", "geo_mode": "custom",
        "lat": 0.0, "lon": 0.0, "name": "C", "insert_after_index": 0,
    }).json()["id"]
    assert _ordered_items(engine, ids["project"]) == [
        ("memory", seeded["A"]), ("memory", mem_c),
        ("journal", seeded["J"]), ("memory", seeded["B"]),
    ]


def test_reorder_translates_visible_indices(env):
    client, engine, ids, act_as = env
    seeded = _seed_mixed_timeline(client, ids, act_as)

    # Owner moves visible item 1 (B) before visible item 0 (A).
    act_as("owner")
    r = client.put("/api/projects/Trip/items/reorder",
                   json={"from_index": 1, "to_index": 0})
    assert r.status_code == 200, r.text
    # The response is the caller's visible list only — no journal leak.
    assert [it["item_type"] for it in r.json()] == ["memory", "memory"]
    assert _ordered_items(engine, ids["project"]) == [
        ("memory", seeded["B"]), ("memory", seeded["A"]), ("journal", seeded["J"]),
    ]

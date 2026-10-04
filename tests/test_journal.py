"""Tests for journal entry CRUD endpoints and associated logic.

Covers:
  1. Create — returns id, inserts DBProjectItem, respects insert_after_index
  2. Update — mutates fields, re-resolves geo, 404 on missing
  3. Delete — removes entry + project item, 404 on missing
  4. Auth   — 403 when accessing another user's journal entry
  5. Geo resolution — custom / start_of_day / end_of_day / no activities
  6. Privacy — item_type is "journal" (the field share endpoints filter on)
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from models.project_db import DBActivity, DBJournalEntry, DBProject, DBProjectItem
from models.user import UserInfo


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_app(user_payload: dict) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: user_payload
    app.include_router(journal_router)
    return app


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def journal_env(monkeypatch, tmp_path):
    """In-memory DB + TestClient wired to one user and one project.

    Yields (client, user_id, project_id, engine).
    """
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)

    import api.journal as journal_mod
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))

    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        user_id = user.id

        project = DBProject(user_info_id=user_id, name="My Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        project_id = project.id

    user_payload = {"sub": str(user_id), "email": "alice@example.com"}
    client = TestClient(_make_app(user_payload))
    yield client, user_id, project_id, test_engine


@pytest.fixture
def created_journal(journal_env):
    """Create one journal entry via POST and return (client, journal_id, env)."""
    client, user_id, project_id, engine = journal_env
    resp = client.post("/api/journal/", json={
        "project_name": "My Trip",
        "date": "2025-06-01",
        "geo_mode": "custom",
        "description": "First entry",
    })
    assert resp.status_code == 201
    journal_id = resp.json()["id"]
    return client, journal_id, user_id, project_id, engine


# ── 1. Create ─────────────────────────────────────────────────────────────────

class TestJournalCreate:
    def test_returns_201_with_id(self, journal_env):
        client, *_ = journal_env
        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "custom",
        })
        assert resp.status_code == 201
        assert "id" in resp.json()
        assert isinstance(resp.json()["id"], int)

    def test_inserts_project_item_with_journal_type(self, journal_env):
        client, user_id, project_id, engine = journal_env
        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "custom",
        })
        journal_id = resp.json()["id"]

        with Session(engine) as sess:
            item = sess.exec(
                select(DBProjectItem).where(DBProjectItem.journal_id == journal_id)
            ).first()
        assert item is not None
        assert item.item_type == "journal"
        assert item.project_id == project_id

    def test_description_stored(self, journal_env):
        client, _, __, engine = journal_env
        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-02",
            "geo_mode": "custom",
            "description": "Stormy day on the Col.",
        })
        journal_id = resp.json()["id"]

        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row.description == "Stormy day on the Col."

    def test_unknown_project_returns_404(self, journal_env):
        client, *_ = journal_env
        resp = client.post("/api/journal/", json={
            "project_name": "Nonexistent",
            "date": "2025-06-01",
            "geo_mode": "custom",
        })
        assert resp.status_code == 404

    def test_insert_after_index_positions_correctly(self, journal_env):
        """insert_after_index=1 places the new item at position 2."""
        client, user_id, project_id, engine = journal_env

        # Seed two items directly in the DB at positions 0 and 1
        with Session(engine) as sess:
            for pos in (0, 1):
                j = DBJournalEntry(project_id=project_id, date=f"2025-0{pos+1}-01", geo_mode="custom")
                sess.add(j)
                sess.flush()
                sess.add(DBProjectItem(project_id=project_id, position=pos, item_type="journal", journal_id=j.id))
            sess.commit()

        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-15",
            "geo_mode": "custom",
            "insert_after_index": 1,
        })
        assert resp.status_code == 201
        new_id = resp.json()["id"]

        with Session(engine) as sess:
            item = sess.exec(
                select(DBProjectItem).where(DBProjectItem.journal_id == new_id)
            ).first()
        assert item.position == 2

    def test_insert_at_end_by_default(self, journal_env):
        client, _, project_id, engine = journal_env

        with Session(engine) as sess:
            for pos in (0, 1):
                j = DBJournalEntry(project_id=project_id, date="2025-01-01", geo_mode="custom")
                sess.add(j)
                sess.flush()
                sess.add(DBProjectItem(project_id=project_id, position=pos, item_type="journal", journal_id=j.id))
            sess.commit()

        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-30",
            "geo_mode": "custom",
        })
        new_id = resp.json()["id"]

        with Session(engine) as sess:
            item = sess.exec(
                select(DBProjectItem).where(DBProjectItem.journal_id == new_id)
            ).first()
        assert item.position == 2


# ── 2. Update ─────────────────────────────────────────────────────────────────

class TestJournalUpdate:
    def test_update_description_returns_200(self, created_journal):
        client, journal_id, *_ = created_journal
        resp = client.put(f"/api/journal/{journal_id}", json={
            "date": "2025-06-01",
            "geo_mode": "custom",
            "description": "Updated text.",
        })
        assert resp.status_code == 200

    def test_update_persists_new_description(self, created_journal):
        client, journal_id, _, __, engine = created_journal
        client.put(f"/api/journal/{journal_id}", json={
            "date": "2025-06-01",
            "geo_mode": "custom",
            "description": "New description.",
        })
        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row.description == "New description."

    def test_update_date_and_time(self, created_journal):
        client, journal_id, _, __, engine = created_journal
        client.put(f"/api/journal/{journal_id}", json={
            "date": "2025-07-14",
            "time": "09:30",
            "geo_mode": "custom",
        })
        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row.date == "2025-07-14"
        assert row.time == "09:30"

    def test_update_nonexistent_returns_404(self, journal_env):
        client, *_ = journal_env
        resp = client.put("/api/journal/99999", json={
            "date": "2025-06-01",
            "geo_mode": "custom",
        })
        assert resp.status_code == 404


# ── 3. Delete ─────────────────────────────────────────────────────────────────

class TestJournalDelete:
    def test_delete_returns_204(self, created_journal):
        client, journal_id, *_ = created_journal
        resp = client.delete(f"/api/journal/{journal_id}")
        assert resp.status_code == 204

    def test_delete_removes_db_entry(self, created_journal):
        client, journal_id, _, __, engine = created_journal
        client.delete(f"/api/journal/{journal_id}")
        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row is None

    def test_delete_removes_project_item(self, created_journal):
        client, journal_id, _, __, engine = created_journal
        client.delete(f"/api/journal/{journal_id}")
        with Session(engine) as sess:
            item = sess.exec(
                select(DBProjectItem).where(DBProjectItem.journal_id == journal_id)
            ).first()
        assert item is None

    def test_delete_nonexistent_returns_404(self, journal_env):
        client, *_ = journal_env
        resp = client.delete("/api/journal/99999")
        assert resp.status_code == 404

    def test_double_delete_returns_404(self, created_journal):
        client, journal_id, *_ = created_journal
        client.delete(f"/api/journal/{journal_id}")
        resp = client.delete(f"/api/journal/{journal_id}")
        assert resp.status_code == 404


# ── 4. Auth — cross-user access ───────────────────────────────────────────────

class TestJournalAuth:
    @pytest.fixture
    def other_user_journal(self, journal_env):
        """Journal entry owned by a second user in the same DB."""
        client, user_id, project_id, engine = journal_env
        with Session(engine) as sess:
            other = UserInfo(display_name="Bob", email="bob@example.com")
            sess.add(other)
            sess.commit()
            sess.refresh(other)
            other_id = other.id

            proj = DBProject(user_info_id=other_id, name="Bob's Trip")
            sess.add(proj)
            sess.commit()
            sess.refresh(proj)

            entry = DBJournalEntry(project_id=proj.id, date="2025-06-01", geo_mode="custom")
            sess.add(entry)
            sess.commit()
            sess.refresh(entry)
            entry_id = entry.id

        return client, entry_id  # client is authenticated as Alice, not Bob

    def test_update_other_users_entry_returns_403(self, other_user_journal):
        client, entry_id = other_user_journal
        resp = client.put(f"/api/journal/{entry_id}", json={
            "date": "2025-06-01",
            "geo_mode": "custom",
        })
        assert resp.status_code == 403

    def test_delete_other_users_entry_returns_403(self, other_user_journal):
        client, entry_id = other_user_journal
        resp = client.delete(f"/api/journal/{entry_id}")
        assert resp.status_code == 403


# ── 5. Geo resolution ─────────────────────────────────────────────────────────

class TestGeoResolution:
    def test_custom_mode_stores_provided_coords(self, journal_env):
        client, _, __, engine = journal_env
        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "custom",
            "lat": 45.832,
            "lon": 6.865,
        })
        journal_id = resp.json()["id"]
        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row.lat == pytest.approx(45.832)
        assert row.lon == pytest.approx(6.865)

    def test_start_of_day_with_no_activities_stores_null_coords(self, journal_env):
        client, _, __, engine = journal_env
        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "start_of_day",
        })
        journal_id = resp.json()["id"]
        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row.lat is None
        assert row.lon is None

    def test_start_of_day_resolves_from_activity(self, journal_env):
        client, user_id, project_id, engine = journal_env
        with Session(engine) as sess:
            act = DBActivity(
                id=1001,
                user_info_id=user_id,
                name="Morning Ride",
                type="Ride",
                start_date="2025-06-01T06:00:00Z",
                start_date_local="2025-06-01T08:00:00",
                start_latlng_json="[45.1, 6.2]",
            )
            sess.add(act)
            sess.flush()
            sess.add(DBProjectItem(
                project_id=project_id,
                position=0,
                item_type="activity",
                activity_id=act.id,
            ))
            sess.commit()

        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "start_of_day",
        })
        journal_id = resp.json()["id"]
        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row.lat == pytest.approx(45.1)
        assert row.lon == pytest.approx(6.2)

    def test_end_of_day_resolves_from_last_activity(self, journal_env):
        client, user_id, project_id, engine = journal_env
        with Session(engine) as sess:
            for i, (start_local, end_latlng) in enumerate([
                ("2025-06-01T08:00:00", "[45.1, 6.2]"),
                ("2025-06-01T16:00:00", "[46.3, 7.8]"),  # latest — expected
            ]):
                act = DBActivity(
                    id=2000 + i,
                    user_info_id=user_id,
                    name=f"Act {i}",
                    type="Ride",
                    start_date=f"2025-06-01T0{6+i}:00:00Z",
                    start_date_local=start_local,
                    end_latlng_json=end_latlng,
                )
                sess.add(act)
                sess.flush()
                sess.add(DBProjectItem(
                    project_id=project_id,
                    position=i,
                    item_type="activity",
                    activity_id=act.id,
                ))
            sess.commit()

        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "end_of_day",
        })
        journal_id = resp.json()["id"]
        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row.lat == pytest.approx(46.3)
        assert row.lon == pytest.approx(7.8)

    def test_update_re_resolves_geo_from_new_date(self, journal_env):
        """Updating a journal entry's date with start_of_day re-resolves coordinates."""
        client, user_id, project_id, engine = journal_env

        with Session(engine) as sess:
            act = DBActivity(
                id=3001,
                user_info_id=user_id,
                name="Ride on July 14",
                type="Ride",
                start_date="2025-07-14T06:00:00Z",
                start_date_local="2025-07-14T08:00:00",
                start_latlng_json="[48.8566, 2.3522]",
            )
            sess.add(act)
            sess.flush()
            sess.add(DBProjectItem(
                project_id=project_id,
                position=0,
                item_type="activity",
                activity_id=act.id,
            ))
            sess.commit()

        create_resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "custom",
            "lat": 0.0,
            "lon": 0.0,
        })
        journal_id = create_resp.json()["id"]

        client.put(f"/api/journal/{journal_id}", json={
            "date": "2025-07-14",
            "geo_mode": "start_of_day",
        })
        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert row.lat == pytest.approx(48.8566)
        assert row.lon == pytest.approx(2.3522)


# ── 6. Privacy ────────────────────────────────────────────────────────────────

class TestJournalPrivacy:
    def test_created_item_has_journal_item_type(self, journal_env):
        """item_type='journal' is the field the share endpoint filters on to
        ensure journal entries never appear in shared project views."""
        client, _, __, engine = journal_env
        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "custom",
        })
        journal_id = resp.json()["id"]
        with Session(engine) as sess:
            item = sess.exec(
                select(DBProjectItem).where(DBProjectItem.journal_id == journal_id)
            ).first()
        assert item.item_type == "journal"

    def test_journal_item_not_linked_to_activity_or_memory(self, journal_env):
        client, _, __, engine = journal_env
        resp = client.post("/api/journal/", json={
            "project_name": "My Trip",
            "date": "2025-06-01",
            "geo_mode": "custom",
        })
        journal_id = resp.json()["id"]
        with Session(engine) as sess:
            item = sess.exec(
                select(DBProjectItem).where(DBProjectItem.journal_id == journal_id)
            ).first()
        assert item.activity_id is None
        assert item.memory_id is None

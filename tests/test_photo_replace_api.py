"""Tests for the photo-replace endpoint on memories and journal entries (issue #33, Phase 1).

Covers, for both content types:
  * happy path — new UUID lands at the old UUID's index in photos_json, old
    files are gone from disk, new full-res + thumbnail files exist and are
    valid JPEGs, response returns the new UUID
  * 404 when old_uuid isn't in the record's photo list
  * 404/403 when the memory/journal entry isn't owned by the caller
  * thumbnail is actually regenerated (smaller than a large source image)
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.memories import router as memories_router
from models.project_db import DBJournalEntry, DBMemory, DBProject, DBProjectItem
from models.user import UserInfo


def _jpeg_bytes(size=(20, 20), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """In-memory DB + TestClient wired to one user and one project.

    Both the memories and journal routers are mounted so both content types
    can be exercised. Yields (client, user_id, project_id, engine, other_user_id).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)

    import api.journal as journal_mod
    import api.memories as mem_mod
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))

    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        user_id = user.id

        other = UserInfo(display_name="Bob", email="bob@example.com")
        sess.add(other)
        sess.commit()
        sess.refresh(other)
        other_id = other.id

        project = DBProject(user_info_id=user_id, name="My Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        project_id = project.id

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id), "email": "alice@example.com"}
    app.include_router(memories_router)
    app.include_router(journal_router)
    client = TestClient(app)
    yield client, user_id, project_id, engine, other_id


def _insert_memory(engine, project_id, photos) -> int:
    with Session(engine) as sess:
        row = DBMemory(project_id=project_id, date="2025-06-01", geo_mode="custom",
                        photos_json=json.dumps(photos))
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return row.id


def _insert_journal(engine, project_id, photos) -> int:
    with Session(engine) as sess:
        row = DBJournalEntry(project_id=project_id, date="2025-06-01", geo_mode="custom",
                              photos_json=json.dumps(photos))
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return row.id


def _other_users_project(engine, other_user_id) -> int:
    with Session(engine) as sess:
        proj = DBProject(user_info_id=other_user_id, name="Bob's Trip")
        sess.add(proj)
        sess.commit()
        sess.refresh(proj)
        return proj.id


# ── Memories ──────────────────────────────────────────────────────────────────

class TestMemoryPhotoReplace:
    def test_happy_path_preserves_index_and_regenerates_files(self, env, tmp_path):
        client, user_id, project_id, engine, _ = env
        old_a, old_b, old_c = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000003"
        memory_id = _insert_memory(engine, project_id, [old_a, old_b, old_c])

        photo_dir = tmp_path / "users" / str(user_id) / "memories" / str(memory_id)
        photo_dir.mkdir(parents=True)
        for u in (old_a, old_b, old_c):
            (photo_dir / f"{u}.jpg").write_bytes(_jpeg_bytes())
            (photo_dir / f"{u}_thumb.jpg").write_bytes(_jpeg_bytes())

        big = _jpeg_bytes(size=(1200, 1200))
        resp = client.put(
            f"/api/memories/{memory_id}/photos/{old_b}/replace",
            files={"file": ("new.jpg", big, "image/jpeg")},
        )
        assert resp.status_code == 200, resp.text
        new_uuid = resp.json()["uuid"]
        assert new_uuid != old_b

        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        photos = json.loads(row.photos_json)
        assert photos == [old_a, new_uuid, old_c]

        assert not (photo_dir / f"{old_b}.jpg").exists()
        assert not (photo_dir / f"{old_b}_thumb.jpg").exists()

        full_path = photo_dir / f"{new_uuid}.jpg"
        thumb_path = photo_dir / f"{new_uuid}_thumb.jpg"
        assert full_path.exists()
        assert thumb_path.exists()

        full_img = Image.open(full_path)
        thumb_img = Image.open(thumb_path)
        assert full_img.format == "JPEG"
        assert thumb_img.format == "JPEG"
        assert thumb_img.size[0] <= 400 and thumb_img.size[1] <= 400
        assert thumb_path.stat().st_size < full_path.stat().st_size

    def test_unknown_old_uuid_returns_404(self, env):
        client, user_id, project_id, engine, _ = env
        memory_id = _insert_memory(engine, project_id, ["real-uuid"])
        resp = client.put(
            f"/api/memories/{memory_id}/photos/does-not-exist/replace",
            files={"file": ("new.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 404

    def test_nonexistent_memory_returns_404(self, env):
        client, *_ = env
        resp = client.put(
            "/api/memories/99999/photos/some-uuid/replace",
            files={"file": ("new.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 404

    def test_other_users_memory_returns_403(self, env):
        client, user_id, project_id, engine, other_id = env
        other_project_id = _other_users_project(engine, other_id)
        memory_id = _insert_memory(engine, other_project_id, ["some-uuid"])
        resp = client.put(
            f"/api/memories/{memory_id}/photos/some-uuid/replace",
            files={"file": ("new.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 403


# ── Journal entries ───────────────────────────────────────────────────────────

class TestJournalPhotoReplace:
    def test_happy_path_preserves_index_and_regenerates_files(self, env, tmp_path):
        client, user_id, project_id, engine, _ = env
        old_a, old_b = "00000000-0000-4000-8000-000000000004", "00000000-0000-4000-8000-000000000005"
        journal_id = _insert_journal(engine, project_id, [old_a, old_b])

        photo_dir = tmp_path / "users" / str(user_id) / "journal" / str(journal_id)
        photo_dir.mkdir(parents=True)
        for u in (old_a, old_b):
            (photo_dir / f"{u}.jpg").write_bytes(_jpeg_bytes())
            (photo_dir / f"{u}_thumb.jpg").write_bytes(_jpeg_bytes())

        big = _jpeg_bytes(size=(1200, 1200))
        resp = client.put(
            f"/api/journal/{journal_id}/photos/{old_a}/replace",
            files={"file": ("new.jpg", big, "image/jpeg")},
        )
        assert resp.status_code == 200, resp.text
        new_uuid = resp.json()["uuid"]
        assert new_uuid != old_a

        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        photos = json.loads(row.photos_json)
        assert photos == [new_uuid, old_b]

        assert not (photo_dir / f"{old_a}.jpg").exists()
        assert not (photo_dir / f"{old_a}_thumb.jpg").exists()

        full_path = photo_dir / f"{new_uuid}.jpg"
        thumb_path = photo_dir / f"{new_uuid}_thumb.jpg"
        assert full_path.exists()
        assert thumb_path.exists()

        full_img = Image.open(full_path)
        thumb_img = Image.open(thumb_path)
        assert full_img.format == "JPEG"
        assert thumb_img.format == "JPEG"
        assert thumb_img.size[0] <= 400 and thumb_img.size[1] <= 400
        assert thumb_path.stat().st_size < full_path.stat().st_size

    def test_unknown_old_uuid_returns_404(self, env):
        client, user_id, project_id, engine, _ = env
        journal_id = _insert_journal(engine, project_id, ["real-uuid"])
        resp = client.put(
            f"/api/journal/{journal_id}/photos/does-not-exist/replace",
            files={"file": ("new.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 404

    def test_nonexistent_journal_returns_404(self, env):
        client, *_ = env
        resp = client.put(
            "/api/journal/99999/photos/some-uuid/replace",
            files={"file": ("new.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 404

    def test_other_users_journal_returns_403(self, env):
        client, user_id, project_id, engine, other_id = env
        other_project_id = _other_users_project(engine, other_id)
        journal_id = _insert_journal(engine, other_project_id, ["some-uuid"])
        resp = client.put(
            f"/api/journal/{journal_id}/photos/some-uuid/replace",
            files={"file": ("new.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert resp.status_code == 403

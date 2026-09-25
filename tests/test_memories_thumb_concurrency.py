"""Tests for the thumbnail endpoint's concurrency cap (see api/memories.py).

A burst of concurrent thumbnail requests — e.g. opening the trip map for a
photo-heavy project — repeatedly OOM-killed the production API container.
`serve_photo_thumb` now refuses past `_THUMB_MAX_CONCURRENT` in-flight
requests with a 503 instead of letting threads pile up unbounded.
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

import api.memories as mem_mod
import models.db as db_module
from api.deps import get_current_user
from api.memories import router as memories_router
from models.project_db import DBMemory, DBProject
from models.user import UserInfo


def _jpeg_bytes(size=(20, 20), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))

    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
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

        photo_uuid = "00000000-0000-4000-8000-000000000021"
        memory = DBMemory(
            project_id=project_id,
            date="2025-06-01",
            geo_mode="custom",
            photos_json=json.dumps([photo_uuid]),
        )
        sess.add(memory)
        sess.commit()
        sess.refresh(memory)
        memory_id = memory.id

    photo_dir = tmp_path / "users" / str(user_id) / "memories" / str(memory_id)
    photo_dir.mkdir(parents=True)
    (photo_dir / f"{photo_uuid}.jpg").write_bytes(_jpeg_bytes())
    (photo_dir / f"{photo_uuid}_thumb.jpg").write_bytes(_jpeg_bytes())

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id), "email": "alice@example.com"}
    app.include_router(memories_router)
    client = TestClient(app)
    yield client, memory_id, photo_uuid


class TestThumbConcurrencyCap:
    def test_serves_normally_when_under_the_cap(self, env):
        client, memory_id, photo_uuid = env
        resp = client.get(f"/api/memories/{memory_id}/photos/{photo_uuid}/thumb")
        assert resp.status_code == 200

    def test_refuses_with_503_once_the_cap_is_exhausted(self, env, monkeypatch):
        client, memory_id, photo_uuid = env
        # Shrink the cap to 0 in-flight slots so any request finds it exhausted,
        # without needing to actually hold N real concurrent requests open.
        monkeypatch.setattr(mem_mod, "_thumb_semaphore", __import__("threading").BoundedSemaphore(1))
        mem_mod._thumb_semaphore.acquire()  # occupy the only slot

        resp = client.get(f"/api/memories/{memory_id}/photos/{photo_uuid}/thumb")
        assert resp.status_code == 503

        mem_mod._thumb_semaphore.release()
        resp = client.get(f"/api/memories/{memory_id}/photos/{photo_uuid}/thumb")
        assert resp.status_code == 200

    def test_releases_the_slot_even_on_404(self, env):
        """A request that 404s (unknown photo) must still free its slot —
        otherwise repeated bad requests would leak the cap down to zero."""
        client, memory_id, _ = env
        for _ in range(30):
            resp = client.get(f"/api/memories/{memory_id}/photos/does-not-exist/thumb")
            assert resp.status_code == 404

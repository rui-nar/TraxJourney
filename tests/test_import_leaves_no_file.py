"""``POST /api/projects/import`` stores the trip in the DB and nothing on disk (issue #434).

The import used to write the upload into ``data/users/<id>/projects/`` and
rename it to ``*.migrated`` after ingesting. Nothing ever deleted it, and
storage accounting counts every byte under the user's directory, so each import
cost the user a full copy of the file against their quota, forever. A failed
import left the raw upload behind instead.

These tests pin that no import outcome, success or failure, leaves a file under
the user's directory or grows their measured storage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from api.project_transfer import router as project_transfer_router
from api.projects import router as projects_router
from models.billing import UserUsage
from models.user import UserInfo
from src.project.project_io import ProjectIO


@pytest.fixture
def env(monkeypatch, tmp_path):
    """One user, in-memory DB, every data path under tmp_path."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(project_shared_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(projects_router)
    app.include_router(project_transfer_router)
    # Failure paths surface as 500s rather than re-raising in the test.
    client = TestClient(app, raise_server_exceptions=False)
    return client, engine, uid, tmp_path / "users" / str(uid)


def _project_bytes() -> bytes:
    # Padded so a leftover copy would be unmistakable in the storage figure.
    return json.dumps({
        "version": 1,
        "name": "ignored",
        "trip_start": None,
        "filter_state": {"start_date": None, "end_date": None, "activity_types": None},
        "items": [],
        "activities": [],
        "padding": "x" * 50_000,
    }).encode("utf-8")


def _import(client, filename: str, content: bytes):
    return client.post(
        "/api/projects/import",
        files={"file": (filename, content, "application/json")},
    )


def _files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def _counted_storage(engine, uid: int) -> int:
    with Session(engine) as sess:
        row = sess.exec(select(UserUsage).where(UserUsage.user_info_id == uid)).first()
    return row.storage_bytes if row else 0


def test_successful_import_leaves_no_file_behind(env):
    client, _engine, _uid, user_dir = env

    r = _import(client, f"Trip{ProjectIO.EXTENSION}", _project_bytes())

    assert r.status_code == 201, r.text
    assert _files_under(user_dir) == []
    # The trip itself is there — it lives in the DB, not on disk.
    assert "Trip" in {p["name"] for p in client.get("/api/projects/").json()}


def test_import_does_not_grow_measured_storage(env):
    client, engine, uid, user_dir = env
    # A known counter value, so "unchanged" is a real observation rather than
    # an absent row read as 0.
    with Session(engine) as sess:
        sess.add(UserUsage(user_info_id=uid, storage_bytes=12_345))
        sess.commit()
    before = storage_mod.dir_size(user_dir)

    r = _import(client, f"Trip{ProjectIO.EXTENSION}", _project_bytes())

    assert r.status_code == 201, r.text
    # Both measures: the admin dashboard's walk of the user's directory, and
    # the quota counter, which the import leaves alone now it writes no file.
    assert storage_mod.dir_size(user_dir) == before
    assert _counted_storage(engine, uid) == 12_345


def test_import_that_fails_during_ingest_leaves_no_file_behind(env, monkeypatch):
    client, _engine, _uid, user_dir = env

    def _boom(*_a, **_kw):
        raise RuntimeError("ingest failed")

    monkeypatch.setattr(project_shared_mod._repo, "ingest_project", _boom)

    r = _import(client, f"Trip{ProjectIO.EXTENSION}", _project_bytes())

    assert r.status_code == 500
    assert _files_under(user_dir) == []


def test_import_of_a_malformed_file_leaves_no_file_behind(env):
    client, _engine, _uid, user_dir = env

    r = _import(client, f"Trip{ProjectIO.EXTENSION}", b'{"items": [not json')

    # Deliberately loose: a malformed file currently answers a pre-existing
    # 500, which should become a 4xx under its own issue. Only the "no file
    # left behind" half is this test's business.
    assert r.status_code >= 400
    assert _files_under(user_dir) == []


def test_rejected_upload_leaves_no_file_behind(env):
    client, _engine, _uid, user_dir = env

    r = _import(client, "Trip.json", _project_bytes())

    assert r.status_code == 400, r.text
    assert _files_under(user_dir) == []

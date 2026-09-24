"""Access control for the backup endpoints (api/backup.py).

Listing and restoring backups is admin-only. The gate is ``require_admin``,
which re-reads ``is_admin`` from the DB, so these tests run it for real against
an in-memory DB rather than overriding it: a token that still claims admin
after the account was demoted must be refused.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.backup as backup_mod
import models.db as db_module
from api.backup import router as backup_router
from api.deps import get_current_user
from models.user import LocalUser, UserInfo


@pytest.fixture
def engine(monkeypatch):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    SQLModel.metadata.create_all(test_engine)
    return test_engine


@pytest.fixture
def restore_calls(monkeypatch):
    """Record restore_db calls instead of touching a real database file."""
    calls: list[str] = []
    monkeypatch.setattr(backup_mod, "restore_db", calls.append)
    monkeypatch.setattr(
        backup_mod, "list_backups",
        lambda: [{"date": "2026-01-01", "size_bytes": 1024}],
    )
    return calls


def _mk_user(engine, *, email: str, is_admin: bool) -> int:
    with Session(engine) as sess:
        local = LocalUser(username=email, password_hash=LocalUser.hash_password("pw"))
        sess.add(local)
        sess.commit()
        sess.refresh(local)
        ui = UserInfo(
            local_auth_id=local.id,
            display_name=email,
            email=email,
            auth_provider="local",
            is_admin=is_admin,
        )
        sess.add(ui)
        sess.commit()
        sess.refresh(ui)
        return ui.id


def _client(uid: int, *, is_admin_claim: bool) -> TestClient:
    payload = {"sub": str(uid), "email": "u@x.io", "auth_provider": "local",
               "is_admin": is_admin_claim}
    app = FastAPI()
    # Only authentication is faked; require_admin runs against the DB.
    app.dependency_overrides[get_current_user] = lambda: payload
    app.include_router(backup_router)
    return TestClient(app)


def test_non_admin_cannot_list_backups(engine, restore_calls):
    uid = _mk_user(engine, email="joe@x.io", is_admin=False)
    resp = _client(uid, is_admin_claim=False).get("/api/backup/")
    assert resp.status_code == 403


def test_non_admin_cannot_restore_and_restore_never_runs(engine, restore_calls):
    uid = _mk_user(engine, email="joe@x.io", is_admin=False)
    resp = _client(uid, is_admin_claim=False).post("/api/backup/2026-01-01/restore")
    assert resp.status_code == 403
    assert restore_calls == []


def test_admin_can_list_backups(engine, restore_calls):
    uid = _mk_user(engine, email="admin@x.io", is_admin=True)
    resp = _client(uid, is_admin_claim=True).get("/api/backup/")
    assert resp.status_code == 200
    assert resp.json() == [{"date": "2026-01-01", "size_bytes": 1024}]


def test_admin_can_restore(engine, restore_calls):
    uid = _mk_user(engine, email="admin@x.io", is_admin=True)
    resp = _client(uid, is_admin_claim=True).post("/api/backup/2026-01-01/restore")
    assert resp.status_code == 200
    assert resp.json() == {"status": "restored", "date": "2026-01-01"}
    assert restore_calls == ["2026-01-01"]


def test_demoted_admin_with_stale_admin_token_is_refused(engine, restore_calls):
    uid = _mk_user(engine, email="was-admin@x.io", is_admin=True)
    with Session(engine) as sess:
        ui = sess.get(UserInfo, uid)
        ui.is_admin = False
        sess.add(ui)
        sess.commit()
    # The token was issued while the account was admin and still says so.
    client = _client(uid, is_admin_claim=True)
    assert client.get("/api/backup/").status_code == 403
    assert client.post("/api/backup/2026-01-01/restore").status_code == 403
    assert restore_calls == []

"""Server backstop on memory writes (#505, #506; decisions 3 and 13 of
docs/E2EE_REMNANTS_PLAN.md).

A trip's memories belong under its owner's key: an encrypted owner's trip
refuses plaintext (`encryption_locked` — a locked device or an old build), a
plaintext owner's trip refuses envelopes (`encryption_not_shared` — an
encrypted companion's key nobody else holds). `PUT` also takes an optional
`lock_version` (compare-and-swap) and always advances the trip's version.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.memories import encryption_guard_code, router as memories_router
from models.project_db import DBMemory, DBProject, DBProjectMember
from models.user import UserInfo

_ENV_NAME = "v1.YWJj.ZGVm"
_ENV_DESC = "v1.eGl6.enp6"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Owner + companion (editor member) on "Trip"; `encrypt_owner()` flips
    the owner's flag."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    import api.memories as mem_mod
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
    app.include_router(memories_router)
    client = TestClient(app)

    def act_as(who: str):
        current["uid"] = ids[who]

    def encrypt_owner():
        with Session(engine) as sess:
            owner = sess.get(UserInfo, ids["owner"])
            owner.encryption_enabled = True
            sess.add(owner); sess.commit()

    return client, engine, ids, act_as, encrypt_owner


def _create(client, ids, *, as_companion=False, **fields):
    body = {"project_name": "Trip", "date": "2025-06-01", "geo_mode": "custom", **fields}
    url = f"/api/memories/?owner={ids['owner']}" if as_companion else "/api/memories/"
    return client.post(url, json=body)


def _insert(engine, ids, **fields) -> int:
    with Session(engine) as sess:
        row = DBMemory(project_id=ids["project"], date="2025-06-01", geo_mode="custom",
                       photos_json="[]", **fields)
        sess.add(row); sess.commit(); sess.refresh(row)
        return row.id


def _lock_version(engine, ids) -> int:
    with Session(engine) as sess:
        return sess.get(DBProject, ids["project"]).lock_version


def _update(client, mem_id, **fields):
    return client.put(f"/api/memories/{mem_id}",
                      json={"date": "2025-06-02", "geo_mode": "custom", **fields})


# ── The helper ────────────────────────────────────────────────────────────────

def test_guard_helper_codes():
    assert encryption_guard_code(True, "plain", None) == "encryption_locked"
    assert encryption_guard_code(True, _ENV_NAME, _ENV_DESC) is None
    assert encryption_guard_code(False, _ENV_NAME, None) == "encryption_not_shared"
    assert encryption_guard_code(False, "plain", "text") is None
    assert encryption_guard_code(True, "", None) is None
    assert encryption_guard_code(False, "", None) is None


# ── Create ────────────────────────────────────────────────────────────────────

def test_encrypted_owner_create_plaintext_is_locked(env):
    client, _, ids, _, encrypt_owner = env
    encrypt_owner()
    r = _create(client, ids, name="Lisbon", description=_ENV_DESC)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_locked"}


def test_encrypted_owner_create_envelope_ok(env):
    client, _, ids, _, encrypt_owner = env
    encrypt_owner()
    r = _create(client, ids, name=_ENV_NAME, description=_ENV_DESC)
    assert r.status_code == 201, r.text


def test_encrypted_owner_create_empty_text_ok(env):
    client, _, ids, _, encrypt_owner = env
    encrypt_owner()
    r = _create(client, ids, name="", description=None)
    assert r.status_code == 201, r.text


def test_plaintext_owner_companion_envelope_not_shared(env):
    client, engine, ids, act_as, _ = env
    act_as("companion")
    r = _create(client, ids, as_companion=True, name=_ENV_NAME)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_not_shared"}
    with Session(engine) as sess:
        assert sess.exec(select(DBMemory)).all() == []


def test_plaintext_owner_companion_plaintext_ok(env):
    client, _, ids, act_as, _ = env
    act_as("companion")
    r = _create(client, ids, as_companion=True, name="Lisbon", description="Tram 28")
    assert r.status_code == 201, r.text


def test_polarsteps_adopt_plaintext_into_encrypted_trip_is_locked(env):
    """The re-import adoption path rewrites name/description too."""
    client, engine, ids, _, encrypt_owner = env
    mem_id = _insert(engine, ids, name="Lisbon")
    encrypt_owner()
    r = _create(client, ids, name="Lisbon", description="From Polarsteps",
                polarsteps_step_id=42)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_locked"}
    with Session(engine) as sess:
        row = sess.get(DBMemory, mem_id)
        assert row.polarsteps_step_id is None
        assert row.description is None


# ── Update ────────────────────────────────────────────────────────────────────

def test_encrypted_owner_update_plaintext_is_locked(env):
    client, engine, ids, _, encrypt_owner = env
    mem_id = _insert(engine, ids, name=_ENV_NAME)
    encrypt_owner()
    before = _lock_version(engine, ids)
    r = _update(client, mem_id, name="Lisbon")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_locked"}
    with Session(engine) as sess:
        assert sess.get(DBMemory, mem_id).name == _ENV_NAME
    assert _lock_version(engine, ids) == before


def test_encrypted_owner_update_envelope_ok(env):
    client, engine, ids, _, encrypt_owner = env
    mem_id = _insert(engine, ids, name="Lisbon")
    encrypt_owner()
    r = _update(client, mem_id, name=_ENV_NAME, description=_ENV_DESC)
    assert r.status_code == 200, r.text


def test_encrypted_owner_update_empty_text_ok(env):
    client, engine, ids, _, encrypt_owner = env
    mem_id = _insert(engine, ids)
    encrypt_owner()
    r = _update(client, mem_id, name="", description="")
    assert r.status_code == 200, r.text


def test_plaintext_owner_companion_update_envelope_not_shared(env):
    client, engine, ids, act_as, _ = env
    mem_id = _insert(engine, ids, name="Lisbon")
    act_as("companion")
    r = _update(client, mem_id, description=_ENV_DESC)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_not_shared"}
    with Session(engine) as sess:
        assert sess.get(DBMemory, mem_id).description is None


def test_plaintext_owner_companion_update_plaintext_ok(env):
    client, engine, ids, act_as, _ = env
    mem_id = _insert(engine, ids, name="Lisbon")
    act_as("companion")
    r = _update(client, mem_id, name="Porto")
    assert r.status_code == 200, r.text


# ── Update: lock_version (decision 13) ────────────────────────────────────────

def test_update_with_current_lock_version_returns_next(env):
    client, engine, ids, _, _ = env
    mem_id = _insert(engine, ids, name="Lisbon")
    current = _lock_version(engine, ids)
    r = _update(client, mem_id, name="Porto", lock_version=current)
    assert r.status_code == 200, r.text
    assert r.json() == {"lock_version": current + 1}
    assert _lock_version(engine, ids) == current + 1


def test_update_with_stale_lock_version_refused_and_row_unchanged(env):
    client, engine, ids, _, _ = env
    mem_id = _insert(engine, ids, name="Lisbon")
    current = _lock_version(engine, ids)
    r = _update(client, mem_id, name="Porto", lock_version=current - 1)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "stale_write"}
    with Session(engine) as sess:
        row = sess.get(DBMemory, mem_id)
        assert row.name == "Lisbon"
        assert row.date == "2025-06-01"
    assert _lock_version(engine, ids) == current


def test_update_without_lock_version_still_advances_it(env):
    client, engine, ids, _, _ = env
    mem_id = _insert(engine, ids, name="Lisbon")
    current = _lock_version(engine, ids)
    r = _update(client, mem_id, name="Porto")
    assert r.status_code == 200, r.text
    assert r.json() == {"lock_version": current + 1}
    assert _lock_version(engine, ids) == current + 1
    with Session(engine) as sess:
        assert sess.get(DBMemory, mem_id).name == "Porto"

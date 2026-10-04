"""Server backstop on journal writes (#505, #506; decisions 3 and 13 of
docs/E2EE_REMNANTS_PLAN.md).

Journal entries are author-private, so the author's key is the one their text
belongs under: an encrypted author may only store envelopes
(`encryption_locked` otherwise), a plaintext author never stores one
(`encryption_not_shared`). `PUT` also takes an optional `lock_version`
(compare-and-swap) and always advances the trip's version.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from models.project_db import DBJournalEntry, DBProject, DBProjectMember
from models.user import UserInfo

_ENV = "v1.eGl6.enp6"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Plaintext owner + companion (editor member) on "Trip"; `encrypt(who)`
    flips a user's flag."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    import api.journal as journal_mod
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
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
    client = TestClient(app)

    def act_as(who: str):
        current["uid"] = ids[who]

    def encrypt(who: str):
        with Session(engine) as sess:
            user = sess.get(UserInfo, ids[who])
            user.encryption_enabled = True
            sess.add(user); sess.commit()

    return client, engine, ids, act_as, encrypt


def _create(client, ids, *, as_companion=False, **fields):
    body = {"project_name": "Trip", "date": "2025-06-01", "geo_mode": "custom", **fields}
    url = f"/api/journal/?owner={ids['owner']}" if as_companion else "/api/journal/"
    return client.post(url, json=body)


def _insert(engine, ids, author: str, **fields) -> int:
    with Session(engine) as sess:
        row = DBJournalEntry(project_id=ids["project"], user_info_id=ids[author],
                             date="2025-06-01", geo_mode="custom", photos_json="[]", **fields)
        sess.add(row); sess.commit(); sess.refresh(row)
        return row.id


def _lock_version(engine, ids) -> int:
    with Session(engine) as sess:
        return sess.get(DBProject, ids["project"]).lock_version


def _update(client, journal_id, **fields):
    return client.put(f"/api/journal/{journal_id}",
                      json={"date": "2025-06-02", "geo_mode": "custom", **fields})


# ── Create ────────────────────────────────────────────────────────────────────

def test_encrypted_author_create_plaintext_is_locked(env):
    client, engine, ids, _, encrypt = env
    encrypt("owner")
    r = _create(client, ids, description="Dear diary")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_locked"}
    with Session(engine) as sess:
        assert sess.exec(select(DBJournalEntry)).all() == []


def test_encrypted_author_create_envelope_ok(env):
    client, _, ids, _, encrypt = env
    encrypt("owner")
    r = _create(client, ids, description=_ENV)
    assert r.status_code == 201, r.text


def test_encrypted_author_create_empty_ok(env):
    client, _, ids, _, encrypt = env
    encrypt("owner")
    r = _create(client, ids, description="")
    assert r.status_code == 201, r.text


def test_plaintext_author_create_envelope_not_shared(env):
    client, _, ids, _, _ = env
    r = _create(client, ids, description=_ENV)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_not_shared"}


def test_encrypted_companion_in_plaintext_trip_keys_on_author(env):
    """The author's key, not the owner's: an encrypted companion stores
    envelopes in a plaintext owner's trip, and is refused plaintext."""
    client, _, ids, act_as, encrypt = env
    encrypt("companion")
    act_as("companion")
    assert _create(client, ids, as_companion=True, description=_ENV).status_code == 201
    r = _create(client, ids, as_companion=True, description="Dear diary")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_locked"}


# ── Update ────────────────────────────────────────────────────────────────────

def test_encrypted_author_update_plaintext_is_locked(env):
    client, engine, ids, _, encrypt = env
    entry = _insert(engine, ids, "owner", description=_ENV)
    encrypt("owner")
    before = _lock_version(engine, ids)
    r = _update(client, entry, description="Dear diary")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_locked"}
    with Session(engine) as sess:
        assert sess.get(DBJournalEntry, entry).description == _ENV
    assert _lock_version(engine, ids) == before


def test_encrypted_author_update_envelope_ok(env):
    client, engine, ids, _, encrypt = env
    entry = _insert(engine, ids, "owner", description="Dear diary")
    encrypt("owner")
    r = _update(client, entry, description=_ENV)
    assert r.status_code == 200, r.text


def test_plaintext_author_update_envelope_not_shared(env):
    client, engine, ids, _, _ = env
    entry = _insert(engine, ids, "owner", description="Dear diary")
    r = _update(client, entry, description=_ENV)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "encryption_not_shared"}
    with Session(engine) as sess:
        assert sess.get(DBJournalEntry, entry).description == "Dear diary"


# ── Update: lock_version (decision 13) ────────────────────────────────────────

def test_update_with_current_lock_version_returns_next(env):
    client, engine, ids, _, _ = env
    entry = _insert(engine, ids, "owner", description="Dear diary")
    current = _lock_version(engine, ids)
    r = _update(client, entry, description="Day two", lock_version=current)
    assert r.status_code == 200, r.text
    assert r.json() == {"lock_version": current + 1}
    assert _lock_version(engine, ids) == current + 1


def test_update_with_stale_lock_version_refused_and_row_unchanged(env):
    client, engine, ids, _, _ = env
    entry = _insert(engine, ids, "owner", description="Dear diary")
    current = _lock_version(engine, ids)
    r = _update(client, entry, description="Day two", lock_version=current - 1)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "stale_write"}
    with Session(engine) as sess:
        row = sess.get(DBJournalEntry, entry)
        assert row.description == "Dear diary"
        assert row.date == "2025-06-01"
    assert _lock_version(engine, ids) == current


def test_update_without_lock_version_still_advances_it(env):
    client, engine, ids, _, _ = env
    entry = _insert(engine, ids, "owner", description="Dear diary")
    current = _lock_version(engine, ids)
    r = _update(client, entry, description="Day two")
    assert r.status_code == 200, r.text
    assert r.json() == {"lock_version": current + 1}
    assert _lock_version(engine, ids) == current + 1
    with Session(engine) as sess:
        assert sess.get(DBJournalEntry, entry).description == "Day two"

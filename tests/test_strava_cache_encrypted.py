"""An encrypted account's Strava list never reaches the disk (docs/E2EE_REMNANTS_PLAN.md decision 8).

The Strava picker caches the raw activity list for an hour. For most users it
is a ``stravacache`` row; that blob holds every activity's name and track in
plaintext, which an account with end-to-end encryption must not have stored.
For such an account the list lives in the API process instead, with the same
TTL, and enabling encryption deletes the row the account already had.

Strava is mocked at ``_fetch_all_strava``: the tests count fetches and never
touch the network.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import api.strava as strava_module
import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.project_db import DBStravaCache
from models.user import StravaToken, UserInfo

_RAW = [{"id": 7, "name": "Col du Galibier", "type": "Ride",
         "start_date": "2026-07-01T08:00:00Z",
         "start_date_local": "2026-07-01T10:00:00Z",
         "map": {"summary_polyline": "_p~iF~ps|U_ulLnnqC"}}]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'cache.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(strava_module, "_memory_cache", {})
    return engine


@pytest.fixture
def fetch(monkeypatch):
    mock = MagicMock(return_value=_RAW)
    monkeypatch.setattr(strava_module, "_fetch_all_strava", mock)
    monkeypatch.setattr(strava_module, "_strava_client_for_token", lambda _t: object())
    return mock


def _user(engine, *, encrypted: bool, cached_row: bool = False) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com", encryption_enabled=encrypted)
        sess.add(u)
        sess.commit()
        sess.refresh(u)
        sess.add(StravaToken(user_info_id=u.id, access_token="a", refresh_token="r",
                             expires_at=time.time() + 3600))
        if cached_row:
            sess.add(DBStravaCache(user_info_id=u.id, fetched_at=time.time(),
                                   activities_json='[{"id": 1, "name": "old"}]'))
        sess.commit()
        return u.id


@pytest.fixture
def as_user():
    def _client(uid: int) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
        return TestClient(app)
    yield _client
    app.dependency_overrides.clear()


def _row(engine, uid: int):
    with Session(engine) as sess:
        return sess.get(DBStravaCache, uid)


def _pick_twice(client) -> list[dict]:
    out = []
    for _ in range(2):
        r = client.get("/api/strava/activities")
        assert r.status_code == 200, r.text
        out.append(r.json())
    return out


def test_encrypted_user_is_cached_in_memory_and_not_on_disk(engine, fetch, as_user):
    uid = _user(engine, encrypted=True)
    first, second = _pick_twice(as_user(uid))

    assert fetch.call_count == 1, "the second request within the TTL is served from cache"
    assert (first["cached"], second["cached"]) == (False, True)
    assert [a["name"] for a in second["activities"]] == ["Col du Galibier"]
    assert _row(engine, uid) is None
    status = as_user(uid).get("/api/strava/cache/status").json()
    assert (status["cached"], status["count"]) == (True, 1)


def test_encrypted_users_memory_entry_expires_with_the_ttl(engine, fetch, as_user, monkeypatch):
    uid = _user(engine, encrypted=True)
    client = as_user(uid)
    client.get("/api/strava/activities")
    monkeypatch.setattr(strava_module, "_CACHE_TTL", -1)
    assert client.get("/api/strava/activities").json()["cached"] is False
    assert fetch.call_count == 2
    assert _row(engine, uid) is None


def test_encrypted_users_leftover_row_is_ignored_and_deleted(engine, fetch, as_user):
    uid = _user(engine, encrypted=True, cached_row=True)
    r = as_user(uid).get("/api/strava/activities")

    assert r.json()["cached"] is False, "the plaintext row is never served"
    assert fetch.call_count == 1
    assert _row(engine, uid) is None


def test_plaintext_user_still_cached_in_the_table(engine, fetch, as_user):
    uid = _user(engine, encrypted=False)
    first, second = _pick_twice(as_user(uid))

    assert fetch.call_count == 1
    assert (first["cached"], second["cached"]) == (False, True)
    assert "Col du Galibier" in _row(engine, uid).activities_json
    assert strava_module._memory_cache == {}


def test_enabling_encryption_deletes_the_row(engine, as_user):
    uid = _user(engine, encrypted=False, cached_row=True)
    r = as_user(uid).post("/api/encryption/enable", json={
        "device": {"public_key": "cGs=", "label": "test",
                   "wrapped_cmk": "d2s=", "ephemeral_public_key": "ZXBr"},
        "recovery": {"method": "recovery_key", "wrapped_cmk": "cms=", "salt": "c2FsdA=="},
    })
    assert r.status_code in (200, 201), r.text
    assert _row(engine, uid) is None


def test_disconnect_forgets_the_memory_entry(engine, fetch, as_user, monkeypatch):
    uid = _user(engine, encrypted=True)
    client = as_user(uid)
    client.get("/api/strava/activities")
    assert uid in strava_module._memory_cache
    monkeypatch.setattr(strava_module, "deauthorize_strava", lambda *_a, **_k: None)

    assert client.delete("/api/strava/disconnect").status_code in (200, 204)
    assert uid not in strava_module._memory_cache


def test_invalidate_forgets_the_memory_entry(engine, fetch, as_user):
    uid = _user(engine, encrypted=True)
    as_user(uid).get("/api/strava/activities")
    assert uid in strava_module._memory_cache
    strava_module._invalidate_cache(uid)
    assert uid not in strava_module._memory_cache

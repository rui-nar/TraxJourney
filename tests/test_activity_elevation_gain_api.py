"""``PUT /api/activities/{id}/elevation-gain`` (E2EE remnants U12, decision 12).

The server cannot measure the climb of an encrypted profile, so the device
measures it and writes it here (#366). Accepted from whoever may write the
row's E2EE fields (as ``PUT /api/activities/{id}``), only for a row whose
stored profile is an envelope, and only for a finite value within 0..50 000 m.
It advances the lock version of every trip holding the row — a
compare-and-swap on one of them when ``project`` and ``lock_version`` are
given (decision 13) — and queues their stats refresh.
``PUT /api/activities/{id}`` itself still never writes the gain.
"""
from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.activities as activities_mod
import models.db as db_module
from api.activities import activity_fields_router
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo

_URL = "/api/activities/{aid}/elevation-gain"


def _env(seed: int) -> str:
    """An envelope as ``EncryptedField.encode`` writes one (see
    test_activity_encrypted_edit.py)."""
    wrapped = b"\xfb\xff\xbf" * 24
    ciphertext = bytes([seed % 256]) * 41
    return (f"v1.{base64.b64encode(wrapped).decode()}"
            f".{base64.b64encode(ciphertext).decode()}")


@pytest.fixture
def env(monkeypatch):
    """Owner's trips "Trip" (lock 7) and "Other" (lock 3) both hold 111, an
    encrypted Strava row of the owner's, and "Trip" holds 222, a plaintext
    one. Stranger's trip "Theirs" holds 333, an encrypted row of the
    stranger's, and 334, a local row of the owner's."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com") for n in ("owner", "stranger")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}
        trips = {
            "Trip": DBProject(user_info_id=ids["owner"], name="Trip", lock_version=7),
            "Other": DBProject(user_info_id=ids["owner"], name="Other", lock_version=3),
            "Theirs": DBProject(user_info_id=ids["stranger"], name="Theirs", lock_version=5),
        }
        for t in trips.values():
            sess.add(t)
        sess.commit()
        pids = {n: t.id for n, t in trips.items()}
        sess.add(DBActivity(id=111, user_info_id=ids["owner"], name=_env(1), type="Ride",
                            elevation_profile_json=_env(2), total_elevation_gain=10.0))
        sess.add(DBActivity(id=222, user_info_id=ids["owner"], name="Plain", type="Ride",
                            elevation_profile_json='{"distances_km": [0, 1], '
                                                   '"elevations_m": [1, 2]}',
                            total_elevation_gain=10.0))
        sess.add(DBActivity(id=333, user_info_id=ids["stranger"], name=_env(3), type="Ride",
                            elevation_profile_json=_env(4), total_elevation_gain=10.0))
        sess.add(DBActivity(id=-334, user_info_id=ids["owner"], name=_env(5), type="Ride",
                            elevation_profile_json=_env(6), total_elevation_gain=10.0))
        for trip, aid in (("Trip", 111), ("Other", 111), ("Trip", 222),
                          ("Theirs", 333), ("Theirs", -334)):
            sess.add(DBProjectItem(project_id=pids[trip], position=0,
                                   item_type="activity", activity_id=aid))
        sess.commit()

    calls = []
    monkeypatch.setattr(activities_mod, "bust_geo_cache",
                        lambda owner, name: calls.append(("geo", owner, name)))
    monkeypatch.setattr(activities_mod, "queue_stats_refresh",
                        lambda bt, owner, name: calls.append(("stats", owner, name)))

    caller = {"id": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(caller["id"])}
    app.include_router(activity_fields_router)
    return dict(client=TestClient(app), engine=engine, ids=ids, caller=caller, calls=calls)


def _gain(engine, aid=111):
    with Session(engine) as sess:
        return sess.get(DBActivity, aid).total_elevation_gain


def _locks(engine):
    with Session(engine) as sess:
        return {p.name: p.lock_version for p in sess.exec(select(DBProject)).all()}


def _put(env, aid=111, **body):
    return env["client"].put(_URL.format(aid=aid), json=body)


def test_owner_writes_the_gain_of_an_encrypted_profile(env):
    resp = _put(env, total_elevation_gain=1234.5)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": 111}
    assert _gain(env["engine"]) == 1234.5
    # Every trip holding the row advances, and has its stats refreshed.
    assert _locks(env["engine"]) == {"Trip": 8, "Other": 4, "Theirs": 5}
    owner = env["ids"]["owner"]
    assert sorted(c for c in env["calls"] if c[0] == "stats") == [
        ("stats", owner, "Other"), ("stats", owner, "Trip")]
    assert sorted(c for c in env["calls"] if c[0] == "geo") == [
        ("geo", owner, "Other"), ("geo", owner, "Trip")]


def test_a_flat_activity_may_be_written_zero(env):
    assert _put(env, total_elevation_gain=0).status_code == 200
    assert _gain(env["engine"]) == 0.0


def test_compare_and_swap_with_the_current_version(env):
    resp = _put(env, total_elevation_gain=50.0, project="Trip", lock_version=7)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": 111, "lock_version": 8}
    assert _locks(env["engine"]) == {"Trip": 8, "Other": 4, "Theirs": 5}


def test_stale_version_is_refused_and_writes_nothing(env):
    resp = _put(env, total_elevation_gain=50.0, project="Trip", lock_version=6)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "stale_write"
    assert _gain(env["engine"]) == 10.0
    assert _locks(env["engine"]) == {"Trip": 7, "Other": 3, "Theirs": 5}


def test_project_and_lock_version_go_together(env):
    assert _put(env, total_elevation_gain=50.0, lock_version=7).status_code == 422
    assert _put(env, total_elevation_gain=50.0, project="Trip").status_code == 422
    assert _gain(env["engine"]) == 10.0


def test_a_trip_not_holding_the_row_is_404(env):
    resp = _put(env, total_elevation_gain=50.0, project="Nope", lock_version=7)
    assert resp.status_code == 404
    assert _gain(env["engine"]) == 10.0


def test_a_non_owner_is_404(env):
    env["caller"]["id"] = env["ids"]["stranger"]
    before = _locks(env["engine"])
    assert _put(env, total_elevation_gain=50.0).status_code == 404
    assert _gain(env["engine"]) == 10.0
    assert _locks(env["engine"]) == before


def test_the_owner_of_every_trip_holding_a_local_row_may_write_it(env):
    """Decision 14: a local row someone else imported, held only by the
    caller's trips."""
    env["caller"]["id"] = env["ids"]["stranger"]
    assert _put(env, aid=-334, total_elevation_gain=77.0).status_code == 200
    assert _gain(env["engine"], -334) == 77.0


def test_a_plaintext_profile_is_refused_as_not_encrypted(env):
    resp = _put(env, aid=222, total_elevation_gain=50.0)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "not_encrypted"
    assert _gain(env["engine"], 222) == 10.0
    assert _locks(env["engine"]) == {"Trip": 7, "Other": 3, "Theirs": 5}


@pytest.mark.parametrize("raw", ["-0.5", "50000.01", "1e9", "NaN", "Infinity",
                                 "-Infinity", '"lots"', "null"])
def test_a_bad_value_is_422_and_writes_nothing(env, raw):
    resp = env["client"].put(_URL.format(aid=111),
                             content=f'{{"total_elevation_gain": {raw}}}',
                             headers={"content-type": "application/json"})
    assert resp.status_code == 422, resp.text
    assert _gain(env["engine"]) == 10.0
    assert _locks(env["engine"]) == {"Trip": 7, "Other": 3, "Theirs": 5}


def test_the_upper_bound_itself_is_accepted(env):
    assert _put(env, total_elevation_gain=50_000).status_code == 200


def test_the_activity_fields_put_still_never_writes_the_gain(env):
    resp = env["client"].put("/api/activities/111", json={"total_elevation_gain": 999.0})
    # Ignored as an unknown field today; refusing it outright would do too.
    assert resp.status_code in (200, 422), resp.text
    assert _gain(env["engine"]) == 10.0

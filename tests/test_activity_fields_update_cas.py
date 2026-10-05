"""``PUT /api/activities/{id}`` for the encryption catch-up (E2EE remnants U2).

* Optional ``project`` + ``lock_version``: a compare-and-swap on that trip's
  lock version (decision 13). The row must be in that trip (404 otherwise);
  a mismatch answers 409 ``stale_write`` and writes nothing; the response
  carries the trip's new lock version.
* Who may write (decision 14): the row's owner, or the owner of every trip
  that references a local (negative id) row.
* An envelope ``name`` nulls ``split_base_name``, the pre-split plaintext
  name (decision 10).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.activities import activity_fields_router
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo

_ENC_NAME = "v1.d2VsY29tZQ==.Y2lwaGVydGV4dA=="
_ENC_POLY = "v1.d2VsY29tZQ==.cG9seWxpbmU="


@pytest.fixture
def env(monkeypatch):
    """Alice (the caller) owns trips "Trip" (lock 3) and "Other" (lock 10);
    Bob owns "Bobs" (lock 20). Alice's activity 111 is in "Trip" and in
    "Bobs"."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        alice = UserInfo(display_name="Alice", email="a@e.com")
        bob = UserInfo(display_name="Bob", email="b@e.com")
        sess.add(alice); sess.add(bob); sess.commit()
        sess.refresh(alice); sess.refresh(bob)
        trip = DBProject(user_info_id=alice.id, name="Trip", lock_version=3)
        other = DBProject(user_info_id=alice.id, name="Other", lock_version=10)
        bobs = DBProject(user_info_id=bob.id, name="Bobs", lock_version=20)
        sess.add(trip); sess.add(other); sess.add(bobs); sess.commit()
        for p in (trip, other, bobs):
            sess.refresh(p)
        sess.add(DBActivity(id=111, user_info_id=alice.id, name="Ride (1/2)",
                            type="Ride", split_base_name="Ride"))
        sess.add(DBProjectItem(project_id=trip.id, position=0,
                               item_type="activity", activity_id=111))
        sess.add(DBProjectItem(project_id=bobs.id, position=0,
                               item_type="activity", activity_id=111))
        sess.commit()
        ids = dict(alice=alice.id, bob=bob.id, trip=trip.id, other=other.id, bobs=bobs.id)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(ids["alice"]), "email": "a@e.com"}
    app.include_router(activity_fields_router)
    return TestClient(app), engine, ids


def _add_activity(engine, activity_id, owner_id, project_ids, **fields):
    with Session(engine) as sess:
        sess.add(DBActivity(id=activity_id, user_info_id=owner_id, name="Walk",
                            type="Walk", **fields))
        for pid in project_ids:
            sess.add(DBProjectItem(project_id=pid, position=1,
                                   item_type="activity", activity_id=activity_id))
        sess.commit()


def _versions(engine):
    with Session(engine) as sess:
        return {p.name: p.lock_version for p in sess.exec(select(DBProject)).all()}


def _row(engine, activity_id=111):
    with Session(engine) as sess:
        a = sess.get(DBActivity, activity_id)
        return a.name, a.summary_polyline, a.split_base_name


# ── split_base_name (decision 10) ────────────────────────────────────────────

def test_envelope_name_nulls_split_base_name(env):
    client, engine, _ = env
    resp = client.put("/api/activities/111", json={"name": _ENC_NAME})
    assert resp.status_code == 200, resp.text
    assert _row(engine) == (_ENC_NAME, None, None)


def test_write_without_envelope_name_keeps_split_base_name(env):
    client, engine, _ = env
    resp = client.put("/api/activities/111", json={"summary_polyline": _ENC_POLY})
    assert resp.status_code == 200, resp.text
    assert _row(engine)[2] == "Ride"


# ── CAS (decision 13) ────────────────────────────────────────────────────────

def test_cas_with_current_version_writes_and_returns_next_version(env):
    client, engine, _ = env
    resp = client.put("/api/activities/111",
                      json={"name": _ENC_NAME, "project": "Trip", "lock_version": 3})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": 111, "lock_version": 4}
    assert _row(engine)[0] == _ENC_NAME
    # The CAS trip advances by one; the other trip holding the row is
    # bumped as before; a trip not holding it is untouched.
    assert _versions(engine) == {"Trip": 4, "Other": 10, "Bobs": 21}


def test_cas_with_stale_version_writes_nothing(env):
    client, engine, _ = env
    before = (_row(engine), _versions(engine))
    resp = client.put("/api/activities/111",
                      json={"name": _ENC_NAME, "project": "Trip", "lock_version": 2})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "stale_write"
    assert (_row(engine), _versions(engine)) == before


@pytest.mark.parametrize("project", ["Other", "Bobs", "Nope"])
def test_cas_on_a_trip_not_holding_the_row_is_404(env, project):
    client, engine, _ = env
    before = (_row(engine), _versions(engine))
    resp = client.put("/api/activities/111",
                      json={"name": _ENC_NAME, "project": project, "lock_version": 10})
    assert resp.status_code == 404, resp.text
    assert (_row(engine), _versions(engine)) == before


@pytest.mark.parametrize("body", [{"lock_version": 3}, {"project": "Trip"}])
def test_cas_needs_both_project_and_lock_version(env, body):
    client, engine, _ = env
    before = (_row(engine), _versions(engine))
    resp = client.put("/api/activities/111", json={"name": _ENC_NAME, **body})
    assert resp.status_code == 422, resp.text
    assert (_row(engine), _versions(engine)) == before


def test_without_cas_every_trip_holding_the_row_advances(env):
    client, engine, _ = env
    resp = client.put("/api/activities/111", json={"name": _ENC_NAME})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": 111}
    assert _versions(engine) == {"Trip": 4, "Other": 10, "Bobs": 21}


# ── Who may write (decision 14) ──────────────────────────────────────────────

def test_local_row_of_another_user_only_in_callers_trips_is_writable(env):
    client, engine, ids = env
    _add_activity(engine, -5, ids["bob"], [ids["trip"], ids["other"]])
    resp = client.put("/api/activities/-5",
                      json={"name": _ENC_NAME, "project": "Trip", "lock_version": 3})
    assert resp.status_code == 200, resp.text
    assert _row(engine, -5)[0] == _ENC_NAME
    assert _versions(engine) == {"Trip": 4, "Other": 11, "Bobs": 20}


def test_strava_row_of_another_user_only_in_callers_trips_is_refused(env):
    client, engine, ids = env
    _add_activity(engine, 500, ids["bob"], [ids["trip"]])
    resp = client.put("/api/activities/500", json={"name": _ENC_NAME})
    assert resp.status_code == 404, resp.text
    assert _row(engine, 500)[0] == "Walk"


def test_unreferenced_local_row_of_another_user_is_refused(env):
    client, engine, ids = env
    _add_activity(engine, -6, ids["bob"], [])
    resp = client.put("/api/activities/-6", json={"name": _ENC_NAME})
    assert resp.status_code == 404, resp.text
    assert _row(engine, -6)[0] == "Walk"


def test_local_row_also_in_another_users_trip_is_refused(env):
    client, engine, ids = env
    _add_activity(engine, -7, ids["bob"], [ids["trip"], ids["bobs"]])
    resp = client.put("/api/activities/-7", json={"name": _ENC_NAME})
    assert resp.status_code == 404, resp.text
    assert _row(engine, -7)[0] == "Walk"
    assert _versions(engine) == {"Trip": 3, "Other": 10, "Bobs": 20}


def test_own_row_in_another_users_trip_stays_writable(env):
    """The row's owner may write it wherever it is referenced — as before."""
    client, engine, _ = env
    resp = client.put("/api/activities/111", json={"name": _ENC_NAME})
    assert resp.status_code == 200, resp.text

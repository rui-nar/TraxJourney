"""Strava sync and the activity browser work per trip, by Strava id (#592).

A row belongs to the one trip holding it, and its id is not its Strava id
(plan Decisions 1, 2 and 7). So a sync adds a Strava activity to a trip unless
that trip holds a row with its ``strava_id``: another trip's row for the same
activity does not count, and a row whose id happens to equal a Strava id says
nothing to the browser's ``in_project`` flag.

Strava is mocked at ``_fetch_all_strava``. A file-backed SQLite is used, as the
routes open sessions of their own.
"""
from __future__ import annotations

import json
import time

import polyline as polyline_lib
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import api.strava as strava_module
import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.project_db import (
    DBActivity,
    DBProject,
    DBProjectItem,
    DBProjectMember,
    DBStravaCache,
)
from models.user import StravaToken, UserInfo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]


@pytest.fixture
def engine(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'sync.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_TRIP_DAYS", "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _raw(strava_id: int, name: str = "Ride") -> dict:
    return {
        "id": strava_id, "name": name, "type": "Ride",
        "distance": 1000.0, "moving_time": 100, "elapsed_time": 120,
        "total_elevation_gain": 0.0,
        "start_date": "2026-06-10T09:00:00Z", "start_date_local": "2026-06-10T09:00:00Z",
        "map": {"summary_polyline": polyline_lib.encode(_TRACK)},
    }


@pytest.fixture
def strava(monkeypatch):
    """Strava as the syncing account sees it: set ``listed`` to its activities."""
    listed: list = []
    monkeypatch.setattr(strava_module, "_strava_client_for_token", lambda _row: object())
    monkeypatch.setattr(strava_module, "_fetch_all_strava", lambda _c, **_k: list(listed))
    monkeypatch.setattr(strava_module, "_save_cache", lambda *_a: None)
    return listed


# ── Seeding ────────────────────────────────────────────────────────────────────

def _user(engine, name: str, *, connected: bool = True) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name=name, email=f"{name}@e.com")
        sess.add(u)
        sess.commit()
        if connected:
            sess.add(StravaToken(user_info_id=u.id, access_token="t",
                                 refresh_token="r", expires_at=9e12))
            sess.commit()
        return u.id


def _row(engine, row_id: int, owner: int, strava_id, name: str = "Ride", **kw) -> None:
    with Session(engine) as sess:
        sess.add(DBActivity(id=row_id, user_info_id=owner, name=name, type="Ride",
                            start_date="2026-06-10T09:00:00Z",
                            start_date_local="2026-06-10T09:00:00Z",
                            strava_id=strava_id,
                            strava_user_id=owner if strava_id is not None else None, **kw))
        sess.commit()


def _trip(engine, owner: int, name: str, row_ids=(), members=()) -> int:
    with Session(engine) as sess:
        proj = DBProject(user_info_id=owner, name=name)
        sess.add(proj)
        sess.commit()
        for pos, aid in enumerate(row_ids):
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        for member in members:
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=member,
                                     role="editor", invited_by=owner))
        sess.commit()
        return proj.id


def _as(uid: int) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "x@e.com"}
    return TestClient(app)


def _trip_rows(engine, project_id: int) -> list[DBActivity]:
    with Session(engine) as sess:
        return list(sess.exec(select(DBActivity).join(
            DBProjectItem, DBProjectItem.activity_id == DBActivity.id).where(
            DBProjectItem.project_id == project_id)).all())


def _all_rows(engine) -> set[int]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBActivity.id)).all())


# ── Sync ───────────────────────────────────────────────────────────────────────

def test_sync_into_a_trip_gives_it_its_own_row_of_an_activity_another_trip_holds(
        engine, strava):
    """Strava activity 900 is already in "Other": syncing "Trip" makes a row
    of its own, and leaves the other trip's row as it was."""
    uid = _user(engine, "owner")
    _row(engine, 900, uid, 900, name="Ride (1/2)", split_base_name="Ride", is_edited=True)
    other = _trip(engine, uid, "Other", [900])
    trip = _trip(engine, uid, "Trip")
    strava.append(_raw(900))

    r = _as(uid).post("/api/projects/Trip/strava/sync")

    assert r.status_code == 200, r.text
    assert r.json() == {"added": 1, "total": 1}
    [mine] = _trip_rows(engine, trip)
    assert mine.id != 900
    assert mine.id > 0      # a Strava row keeps the positive sign (plan Decision 3)
    assert (mine.strava_id, mine.user_info_id, mine.strava_user_id, mine.name) == (
        900, uid, uid, "Ride")
    [theirs] = _trip_rows(engine, other)
    assert (theirs.id, theirs.name, theirs.is_edited) == (900, "Ride (1/2)", True)


def test_a_companions_sync_rows_belong_to_the_trip_owner_and_record_the_companion(
        engine, strava):
    """Plan Decisions 4 and 5: the row is the trip owner's; the companion's
    account is the one whose token refreshes it."""
    owner = _user(engine, "owner", connected=False)
    companion = _user(engine, "companion")
    trip = _trip(engine, owner, "Trip", members=[companion])
    strava.append(_raw(901))

    r = _as(companion).post("/api/projects/Trip/strava/sync", params={"owner": owner})

    assert r.status_code == 200, r.text
    [row] = _trip_rows(engine, trip)
    assert (row.strava_id, row.user_info_id, row.strava_user_id) == (901, owner, companion)


def test_resyncing_an_activity_the_trip_holds_adds_nothing_and_keeps_its_name(
        engine, strava):
    """The trip holds Strava activity 900 under row 5001, split and renamed:
    a sync finds it by ``strava_id``, so nothing is added and the "(1/2)"
    survives (plan Decision 7)."""
    uid = _user(engine, "owner")
    _row(engine, 5001, uid, 900, name="Ride (1/2)", split_base_name="Ride", is_edited=True)
    trip = _trip(engine, uid, "Trip", [5001])
    strava.append(_raw(900, name="Strava name"))

    r = _as(uid).post("/api/projects/Trip/strava/sync")

    assert r.status_code == 200, r.text
    assert r.json() == {"added": 0, "total": 1}
    assert _all_rows(engine) == {5001}
    [row] = _trip_rows(engine, trip)
    assert (row.id, row.name) == (5001, "Ride (1/2)")
    with Session(engine) as sess:
        items = sess.exec(select(DBProjectItem).where(DBProjectItem.project_id == trip)).all()
        assert [it.activity_id for it in items] == [5001]


# ── The activity browser's in_project ──────────────────────────────────────────

def _browse(uid: int, trip: str, **params) -> dict:
    r = _as(uid).get("/api/strava/activities", params={"project": trip, **params})
    assert r.status_code == 200, r.text
    return {a["id"]: a["in_project"] for a in r.json()["activities"]}


def _cache(engine, uid: int, *strava_ids: int) -> None:
    with Session(engine) as sess:
        sess.add(DBStravaCache(user_info_id=uid, fetched_at=time.time(),
                               activities_json=json.dumps([_raw(s) for s in strava_ids])))
        sess.commit()


def test_in_project_is_per_trip_and_by_strava_id(engine):
    """"Trip" holds Strava activity 900 under row 5001, and row 902 for Strava
    activity 777; "Other" holds 901 under its own id. Each trip flags its own
    Strava ids, and a row id never counts as one."""
    uid = _user(engine, "owner")
    _row(engine, 5001, uid, 900)
    _row(engine, 902, uid, 777)
    _row(engine, 901, uid, 901)
    _trip(engine, uid, "Trip", [5001, 902])
    _trip(engine, uid, "Other", [901])
    _cache(engine, uid, 900, 901, 902)

    assert _browse(uid, "Trip") == {900: True, 901: False, 902: False}
    assert _browse(uid, "Other") == {900: False, 901: True, 902: False}


def test_in_project_for_a_companion_browsing_the_owners_trip(engine):
    owner = _user(engine, "owner", connected=False)
    companion = _user(engine, "companion")
    _row(engine, 5002, owner, 910)
    _trip(engine, owner, "Trip", [5002], members=[companion])
    _cache(engine, companion, 910, 911)

    assert _browse(companion, "Trip", owner=owner) == {910: True, 911: False}

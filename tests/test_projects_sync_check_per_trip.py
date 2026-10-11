"""``GET /api/projects/{name}/sync/check`` offers per trip, by Strava id (#592).

A Strava activity is offered unless the trip holds a row with its
``strava_id``. Another trip's row for it does not count, and a row whose id
happens to equal a Strava id says nothing (plan Decisions 2 and 7).
"""
from __future__ import annotations

import json
import time

import polyline as polyline_lib
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.project_db import (
    DBActivity,
    DBProject,
    DBProjectItem,
    DBProjectSyncMeta,
    DBStravaCache,
)
from models.user import StravaToken, UserInfo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]


@pytest.fixture
def engine(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'check.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _raw(strava_id: int) -> dict:
    return {
        "id": strava_id, "name": "Ride", "type": "Ride",
        "distance": 1000.0, "moving_time": 100, "elapsed_time": 120,
        "total_elevation_gain": 0.0,
        "start_date": "2026-06-10T09:00:00Z", "start_date_local": "2026-06-10T09:00:00Z",
        "map": {"summary_polyline": polyline_lib.encode(_TRACK)},
    }


def _seed(engine) -> int:
    """"Trip" holds Strava activity 900 under row 5001, and row 902 for Strava
    activity 777; "Other" holds 901 under its own id. The cached Strava list
    has 900, 901 and 902."""
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u)
        sess.commit()
        uid = u.id
        sess.add(StravaToken(user_info_id=uid, access_token="t",
                             refresh_token="r", expires_at=9e12))
        sess.add(DBStravaCache(user_info_id=uid, fetched_at=time.time(),
                               activities_json=json.dumps([_raw(s) for s in (900, 901, 902)])))
        for row_id, strava_id in ((5001, 900), (902, 777), (901, 901)):
            sess.add(DBActivity(id=row_id, user_info_id=uid, name="Ride", type="Ride",
                                start_date="2026-06-10T09:00:00Z",
                                strava_id=strava_id, strava_user_id=uid))
        for name, rows in (("Trip", (5001, 902)), ("Other", (901,))):
            proj = DBProject(user_info_id=uid, name=name)
            sess.add(proj)
            sess.commit()
            sess.add(DBProjectSyncMeta(project_id=proj.id, auto_sync_enabled=True))
            for pos, aid in enumerate(rows):
                sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                       item_type="activity", activity_id=aid))
        sess.commit()
        return uid


def _offered(uid: int, trip: str) -> list[int]:
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    r = TestClient(app).get(f"/api/projects/{trip}/sync/check")
    assert r.status_code == 200, r.text
    return sorted(a["id"] for a in r.json()["strava"])


def test_sync_check_offers_what_the_trip_lacks_by_strava_id(engine):
    uid = _seed(engine)

    assert _offered(uid, "Trip") == [901, 902]
    assert _offered(uid, "Other") == [900, 902]

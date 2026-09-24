"""Heart rate is never stored or served (issue #442).

Strava sends average_heartrate / max_heartrate and its has_heartrate /
heartrate_opt_out / display_hide_heartrate_option flags with every activity.
Heart rate is health data under GDPR and no feature uses it, so an import must
leave no trace of it anywhere: not on the parsed Activity, not in the activity
table, not in the raw per-user Strava cache (which stores the payload whole),
and not in any payload the API sends back — project detail, Strava browse,
the .traxj export.

Every assertion scans for the substring "heartrate" rather than naming fields,
so a field that slips back in under any of Strava's names is caught.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import polyline as polyline_lib
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.activities as activities_module
import api.strava as strava_module
import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.project_db import DBActivity, DBProject, DBStravaCache
from models.user import StravaToken, UserInfo
from src.models.activity import Activity, strip_heartrate

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]


def _raw_with_heartrate(act_id: int = 501) -> dict:
    """A Strava summary activity as Strava sends it for a heart-rate-equipped ride."""
    return {
        "id": act_id,
        "name": "Ride with HR",
        "type": "Ride",
        "distance": 5000.0,
        "moving_time": 1800,
        "elapsed_time": 2000,
        "total_elevation_gain": 50.0,
        "start_date": "2024-06-02T10:00:00Z",
        "start_date_local": "2024-06-02T12:00:00Z",
        "map": {"summary_polyline": polyline_lib.encode(_TRACK)},
        "has_heartrate": True,
        "average_heartrate": 142.5,
        "max_heartrate": 181,
        "heartrate_opt_out": False,
        "display_hide_heartrate_option": True,
    }


def _mentions_heartrate(obj) -> bool:
    """True if any key anywhere in *obj* (nested dicts/lists) contains 'heartrate'."""
    if isinstance(obj, dict):
        return any("heartrate" in str(k) or _mentions_heartrate(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_mentions_heartrate(v) for v in obj)
    return False


# ── Model ─────────────────────────────────────────────────────────────────────

def test_activity_parses_strava_payload_without_heartrate():
    act = Activity.from_strava_api(_raw_with_heartrate())
    assert act.name == "Ride with HR"
    assert not [f for f in vars(act) if "heartrate" in f]
    assert not _mentions_heartrate(act.to_strava_dict())


def test_activity_table_has_no_heartrate_column():
    assert not [c.name for c in DBActivity.__table__.columns if "heartrate" in c.name]


def test_strip_heartrate_keeps_everything_else():
    raw = _raw_with_heartrate()
    cleaned = strip_heartrate(raw)
    assert not _mentions_heartrate(cleaned)
    assert cleaned == {k: v for k, v in raw.items() if "heartrate" not in k}
    assert cleaned["map"] == raw["map"]
    assert "has_heartrate" in raw, "input must not be mutated"


# ── End to end through the API ────────────────────────────────────────────────

@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u)
        sess.commit()
        sess.refresh(u)
        sess.add(StravaToken(
            user_info_id=u.id, access_token="tok", refresh_token="ref",
            expires_at=time.time() + 3600,
        ))
        sess.add(DBProject(user_info_id=u.id, name="Trip"))
        sess.commit()
        uid = u.id

    # Strava itself is replaced by a canned page carrying heart rate; the
    # post-import stream enrichment (which needs a real client) is skipped.
    monkeypatch.setattr(strava_module, "_fetch_all_strava",
                        lambda *_a, **_k: [_raw_with_heartrate()])
    monkeypatch.setattr(activities_module, "_strava_client_for_user",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(
        strava_module, "_strava_client_for_token",
        lambda row: SimpleNamespace(token_data={
            "access_token": row.access_token,
            "refresh_token": row.refresh_token,
            "expires_at": row.expires_at,
        }),
    )
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    try:
        yield TestClient(app), engine, uid
    finally:
        app.dependency_overrides.clear()


def _cached_blob(engine, uid: int) -> str:
    with Session(engine) as sess:
        return sess.get(DBStravaCache, uid).activities_json


def test_browse_scrubs_heartrate_from_cache_and_response(env):
    client, engine, uid = env
    resp = client.get("/api/strava/activities?refresh=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [a["id"] for a in body["activities"]] == [501]
    assert not _mentions_heartrate(body)

    blob = _cached_blob(engine, uid)
    assert "heartrate" not in blob
    # The rest of the cached payload is intact: a second, cached browse works.
    assert json.loads(blob)[0]["map"]["summary_polyline"]
    again = client.get("/api/strava/activities").json()
    assert again["cached"] is True
    assert not _mentions_heartrate(again)


def test_sync_stores_and_serves_activity_without_heartrate(env):
    client, engine, uid = env
    resp = client.post("/api/projects/Trip/strava/sync")
    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] == 1

    assert "heartrate" not in _cached_blob(engine, uid)
    with Session(engine) as sess:
        row = sess.get(DBActivity, 501)
        assert row is not None and row.name == "Ride with HR"
        assert not [k for k in vars(row) if "heartrate" in k]

    detail = client.get("/api/projects/Trip").json()
    assert [a["id"] for a in detail["activities"]] == [501]
    assert not _mentions_heartrate(detail)

    export = client.get("/api/projects/Trip/export-traxj")
    assert export.status_code == 200, export.text
    assert "heartrate" not in export.text


def test_bulk_import_of_raw_payload_drops_heartrate(env):
    """The client-side import path posts raw Strava dicts straight to the server."""
    client, engine, uid = env
    resp = client.post("/api/projects/Trip/activities",
                       json={"activities": [_raw_with_heartrate(502)]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] == 1

    detail = client.get("/api/projects/Trip").json()
    assert [a["id"] for a in detail["activities"]] == [502]
    assert not _mentions_heartrate(detail)

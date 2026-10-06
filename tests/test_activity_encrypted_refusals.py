"""The plaintext track routes refuse an encrypted activity (E2EE remnants U2).

``PUT …/track`` and ``POST …/split`` take decrypted points and store the
result in plaintext. An old app build would send them for an activity whose
stored polyline or profile is a client-side envelope, so both answer 409
``encrypted_edit_on_device`` before decoding anything, and leave the row and
the trip's lock version as they were.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.activities import router as activities_router
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03), (48.0, 2.04)]
_ELEV = [100.0, 120.0, 110.0, 140.0, 130.0]
_PLAIN_POLY = polyline_lib.encode(_TRACK)
_PLAIN_EP = json.dumps({"distances_km": [0.0, 1.0, 2.0, 3.0, 4.0], "elevations_m": _ELEV})
_ENC_POLY = "v1.d2VsY29tZQ==.cG9seWxpbmU="
_ENC_EP = "v1.d2VsY29tZQ==.ZWxldmF0aW9u"

_POINTS = [{"lat": lat, "lng": lng, "elev": 100.0} for lat, lng in _TRACK[:3]]


def _env(monkeypatch, *, polyline: str, profile: str):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        proj = DBProject(user_info_id=u.id, name="Trip", lock_version=5)
        sess.add(proj); sess.commit(); sess.refresh(proj)
        sess.add(DBActivity(
            id=111, user_info_id=u.id, name="Ride", type="Ride",
            distance=4000.0, moving_time=1000, elapsed_time=1200,
            total_elevation_gain=60.0, summary_polyline=polyline,
            elevation_profile_json=profile,
        ))
        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="activity", activity_id=111))
        sess.commit()
        uid = u.id
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    app.include_router(activities_router)
    return TestClient(app, raise_server_exceptions=False), engine


def _snapshot(engine):
    with Session(engine) as sess:
        a = sess.get(DBActivity, 111)
        p = sess.exec(select(DBProject)).first()
        n_items = len(sess.exec(select(DBProjectItem)).all())
        return (a.summary_polyline, a.elevation_profile_json, a.is_edited,
                a.distance, a.original_polyline, p.lock_version, n_items)


@pytest.fixture
def no_decode(monkeypatch):
    """The stored geometry must not be decoded at all: make decoding raise."""
    def _boom(*_a, **_k):
        raise AssertionError("polyline decoder called on an encrypted row")
    monkeypatch.setattr(polyline_lib, "decode", _boom)


@pytest.mark.parametrize("polyline,profile", [
    (_ENC_POLY, _ENC_EP),
    (_PLAIN_POLY, _ENC_EP),
    (_ENC_POLY, _PLAIN_EP),
], ids=["both-enveloped", "profile-only", "polyline-only"])
def test_put_track_refuses_encrypted_row(monkeypatch, no_decode, polyline, profile):
    client, engine = _env(monkeypatch, polyline=polyline, profile=profile)
    before = _snapshot(engine)
    resp = client.put("/api/projects/Trip/activities/111/track", json={"points": _POINTS})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "encrypted_edit_on_device"
    assert _snapshot(engine) == before


@pytest.mark.parametrize("polyline,profile", [
    (_ENC_POLY, _ENC_EP),
    (_PLAIN_POLY, _ENC_EP),
    (_ENC_POLY, _PLAIN_EP),
], ids=["both-enveloped", "profile-only", "polyline-only"])
@pytest.mark.parametrize("with_points", [False, True], ids=["stored", "edited-points"])
def test_split_refuses_encrypted_row(monkeypatch, no_decode, polyline, profile, with_points):
    client, engine = _env(monkeypatch, polyline=polyline, profile=profile)
    before = _snapshot(engine)
    body = {"split_index": 1}
    if with_points:
        body["points"] = _POINTS
    resp = client.post("/api/projects/Trip/activities/111/split", json=body)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "encrypted_edit_on_device"
    assert _snapshot(engine) == before


def test_put_track_on_plaintext_row_still_edits(monkeypatch):
    client, engine = _env(monkeypatch, polyline=_PLAIN_POLY, profile=_PLAIN_EP)
    resp = client.put("/api/projects/Trip/activities/111/track",
                      json={"points": _POINTS, "lock_version": 5})
    assert resp.status_code == 200, resp.text
    with Session(engine) as sess:
        a = sess.get(DBActivity, 111)
        assert a.is_edited is True
        assert a.original_polyline == _PLAIN_POLY
        assert a.summary_polyline == polyline_lib.encode(_TRACK[:3])


def test_split_on_plaintext_row_still_splits(monkeypatch):
    client, engine = _env(monkeypatch, polyline=_PLAIN_POLY, profile=_PLAIN_EP)
    resp = client.post("/api/projects/Trip/activities/111/split",
                       json={"split_index": 2, "lock_version": 5})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["activities"]) == 2

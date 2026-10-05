"""Track reset: compare-and-swap and the nothing-to-restore refusal (E2EE remnants U2).

Reset takes an optional ``{lock_version}`` body. With it the reset is a
compare-and-swap on the trip's lock version (409 ``stale_write`` and nothing
written on a mismatch); without it the reset behaves as it always has.

An edited row whose ``original_polyline`` is null — the shipped encryption
migration nulled the snapshots — has nothing to restore. Copying the nulls
back used to wipe the track; it now answers 409 ``nothing_to_restore`` and
leaves the row alone.
"""
from __future__ import annotations

import json
import logging

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
_ORIG_POLY = polyline_lib.encode(_TRACK)
_ORIG_EP = json.dumps({"distances_km": [0.0, 1.0, 2.0, 3.0, 4.0],
                       "elevations_m": [100.0, 120.0, 110.0, 140.0, 130.0]})
_EDITED_POLY = polyline_lib.encode(_TRACK[:3])
_EDITED_EP = json.dumps({"distances_km": [0.0, 1.0, 2.0],
                         "elevations_m": [100.0, 120.0, 110.0]})
_ENC_POLY = "v1.d2VsY29tZQ==.cG9seWxpbmU="
_ENC_EP = "v1.d2VsY29tZQ==.ZWxldmF0aW9u"

_URL = "/api/projects/Trip/activities/111/reset"


@pytest.fixture
def make_env(monkeypatch):
    def _make(**row):
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
            proj = DBProject(user_info_id=u.id, name="Trip", lock_version=7)
            sess.add(proj); sess.commit(); sess.refresh(proj)
            fields = dict(
                id=111, user_info_id=u.id, name="Ride", type="Ride",
                distance=2000.0, moving_time=500, elapsed_time=600,
                total_elevation_gain=20.0, summary_polyline=_EDITED_POLY,
                elevation_profile_json=_EDITED_EP, is_edited=True,
                original_polyline=_ORIG_POLY, original_elevation_profile_json=_ORIG_EP,
                original_total_elevation_gain=60.0,
            )
            fields.update(row)
            sess.add(DBActivity(**fields))
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                   item_type="activity", activity_id=111))
            sess.commit()
            uid = u.id
        app = FastAPI()
        app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
        app.include_router(activities_router)
        return TestClient(app), engine
    return _make


def _state(engine):
    with Session(engine) as sess:
        a = sess.get(DBActivity, 111)
        p = sess.exec(select(DBProject)).first()
        return dict(
            polyline=a.summary_polyline, profile=a.elevation_profile_json,
            low_res=a.elevation_profile_low_res_json, is_edited=a.is_edited,
            distance=a.distance, moving_time=a.moving_time,
            original_polyline=a.original_polyline,
            original_profile=a.original_elevation_profile_json,
            lock_version=p.lock_version,
        )


def test_reset_with_current_lock_version_restores_and_advances(make_env):
    client, engine = make_env()
    resp = client.post(_URL, json={"lock_version": 7})
    assert resp.status_code == 200, resp.text
    s = _state(engine)
    assert s["polyline"] == _ORIG_POLY
    assert s["is_edited"] is False
    assert s["lock_version"] == 8


def test_reset_with_stale_lock_version_is_refused_and_writes_nothing(make_env):
    client, engine = make_env()
    before = _state(engine)
    resp = client.post(_URL, json={"lock_version": 6})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "stale_write"
    assert _state(engine) == before


def test_reset_without_body_behaves_as_before(make_env):
    client, engine = make_env()
    resp = client.post(_URL)
    assert resp.status_code == 200, resp.text
    s = _state(engine)
    assert s["polyline"] == _ORIG_POLY
    assert s["profile"] == _ORIG_EP
    assert s["is_edited"] is False
    assert s["original_polyline"] is None
    assert s["lock_version"] == 8


def test_reset_with_empty_body_behaves_as_before(make_env):
    client, engine = make_env()
    resp = client.post(_URL, json={})
    assert resp.status_code == 200, resp.text
    assert _state(engine)["lock_version"] == 8


@pytest.mark.parametrize("track", [
    dict(summary_polyline=_EDITED_POLY, elevation_profile_json=_EDITED_EP),
    dict(summary_polyline=_ENC_POLY, elevation_profile_json=_ENC_EP),
], ids=["plaintext", "encrypted"])
def test_reset_with_null_original_polyline_is_refused_and_keeps_the_track(make_env, track):
    """The shipped migration nulled original_* on edited rows; reset used to
    copy those nulls into the geometry and wipe the track."""
    client, engine = make_env(original_polyline=None,
                              original_elevation_profile_json=None, **track)
    before = _state(engine)
    resp = client.post(_URL)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "nothing_to_restore"
    assert _state(engine) == before
    assert before["polyline"] == track["summary_polyline"]


def test_reset_of_unedited_row_is_still_the_old_conflict(make_env):
    client, _ = make_env(is_edited=False, original_polyline=None,
                         original_elevation_profile_json=None)
    resp = client.post(_URL)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Activity has no edit to reset"


@pytest.mark.parametrize("originals", [
    dict(original_polyline=_ENC_POLY, original_elevation_profile_json=_ENC_EP),
    dict(original_polyline=_ORIG_POLY, original_elevation_profile_json=_ENC_EP),
    dict(original_polyline=_ENC_POLY, original_elevation_profile_json=_ORIG_EP),
], ids=["both-enveloped", "profile-enveloped", "polyline-enveloped"])
def test_reset_with_enveloped_originals_is_refused_and_keeps_the_row(
        make_env, originals, caplog):
    """The server cannot measure an enveloped original; until the scalar
    snapshot restore exists it refuses rather than decode ciphertext, and
    logs that the guard fired."""
    client, engine = make_env(summary_polyline=_ENC_POLY, elevation_profile_json=_ENC_EP,
                              **originals)
    before = _state(engine)
    with caplog.at_level(logging.ERROR, logger="src.project.repo_activities"):
        resp = client.post(_URL)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "nothing_to_restore"
    assert _state(engine) == before
    assert any(r.levelno == logging.ERROR and "111" in r.getMessage()
               for r in caplog.records), caplog.records

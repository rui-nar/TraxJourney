"""Regression tests for the activity-mutation lock_version audit fixes:

  - Fix 1: edit_activity_track / split_activity / delete_local_activity /
    reset_activity_track, and the background Strava-stream enrichment
    completion path, must all advance ``DBProject.lock_version`` — the only
    signal the native app's on-disk cache uses to know its cached heavy
    payloads are stale (issue #173).
  - Fix 2: edit_activity_track / split_activity accept the lock_version the
    client last saw and 409 (instead of silently overwriting) when it no
    longer matches — two tabs racing on the same activity.
  - Fix 3: TrackPointIn rejects out-of-range / non-finite lat/lng with a
    clean 422 instead of persisting malformed points.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.activities import router as activities_router
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.project.project_repo import StaleWriteError

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03), (48.0, 2.04)]
_ELEV = [100.0, 120.0, 110.0, 140.0, 130.0]


def _seed(engine):
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        proj = DBProject(user_info_id=u.id, name="My Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)

        poly = polyline_lib.encode(_TRACK)
        dist_km = [i * 1.0 for i in range(len(_TRACK))]
        act = DBActivity(
            id=111, user_info_id=u.id, name="Ride", type="Ride",
            strava_id=111, strava_user_id=u.id,
            distance=4000.0, moving_time=1000, elapsed_time=1200,
            total_elevation_gain=60.0, summary_polyline=poly,
            elevation_profile_json=json.dumps(
                {"distances_km": dist_km, "elevations_m": _ELEV}),
            start_latlng_json=json.dumps([48.0, 2.0]),
            end_latlng_json=json.dumps([48.0, 2.04]),
        )
        sess.add(act)
        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="activity", activity_id=111))
        sess.commit()
        return u.id


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    uid = _seed(engine)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    app.include_router(activities_router)

    # Mirrors api/router.py's StaleWriteError -> 409 mapping.
    @app.exception_handler(StaleWriteError)
    async def _stale_write_handler(_request, exc):  # noqa: ANN001
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    return TestClient(app), engine


def _lock_version(engine, project_id=1):
    with Session(engine) as sess:
        return sess.get(DBProject, project_id).lock_version


def _points(n=3):
    return [{"lat": lat, "lng": lng, "elev": e}
            for (lat, lng), e in zip(_TRACK[:n], _ELEV[:n])]


class TestLockVersionBump:
    """Fix 1: every activity mutation must advance DBProject.lock_version."""

    def test_edit_track_bumps_lock_version(self, env):
        client, engine = env
        before = _lock_version(engine)
        resp = client.put("/api/projects/My Trip/activities/111/track",
                          json={"points": _points()})
        assert resp.status_code == 200, resp.text
        assert _lock_version(engine) == before + 1

    def test_split_bumps_lock_version(self, env):
        client, engine = env
        before = _lock_version(engine)
        resp = client.post("/api/projects/My Trip/activities/111/split",
                           json={"split_index": 2})
        assert resp.status_code == 200, resp.text
        assert _lock_version(engine) == before + 1

    def test_reset_bumps_lock_version(self, env):
        client, engine = env
        client.put("/api/projects/My Trip/activities/111/track", json={"points": _points()})
        before = _lock_version(engine)
        resp = client.post("/api/projects/My Trip/activities/111/reset")
        assert resp.status_code == 200, resp.text
        assert _lock_version(engine) == before + 1

    def test_delete_local_activity_bumps_lock_version(self, env):
        client, engine = env
        resp = client.post("/api/projects/My Trip/activities/111/split",
                           json={"split_index": 2})
        tail_id = next(a["id"] for a in resp.json()["activities"] if a["id"] < 0)
        before = _lock_version(engine)
        resp = client.delete(f"/api/projects/My Trip/activities/{tail_id}/local")
        assert resp.status_code == 204, resp.text
        assert _lock_version(engine) == before + 1

    def test_background_enrichment_bumps_lock_version(self, env, monkeypatch):
        """The Strava-stream enrichment completion path (issue #173) — once it
        successfully writes a polyline, the native cache must be told too."""
        import time as time_mod
        import api.activities as activities_mod
        from models.user import StravaToken

        client, engine = env
        with Session(engine) as sess:
            sess.add(StravaToken(
                user_info_id=1, access_token="tok", refresh_token="ref",
                expires_at=time_mod.time() + 3600,
            ))
            sess.commit()

        class _FakeClient:
            remaining_requests = 100
            def get_activity_streams(self, _aid):
                return {
                    "latlng": {"data": [[48.0, 2.0], [48.1, 2.1]]},
                    "altitude": {"data": [100.0, 110.0]},
                    "distance": {"data": [0.0, 1000.0]},
                }

        monkeypatch.setattr(
            activities_mod, "_strava_client_for_user", lambda _uid: _FakeClient())

        before = _lock_version(engine)
        activities_mod._enrich_activities_background([111], 1, 1, "My Trip")
        assert _lock_version(engine) == before + 1

    def test_background_enrichment_without_writes_does_not_bump(self, env, monkeypatch):
        """No polyline/elevation was written — nothing for the cache to notice,
        so the version must not move."""
        import api.activities as activities_mod

        client, engine = env
        monkeypatch.setattr(
            activities_mod, "_strava_client_for_user", lambda _uid: None)

        before = _lock_version(engine)
        activities_mod._enrich_activities_background([111], 1, 1, "My Trip")
        assert _lock_version(engine) == before


class TestOptimisticLock:
    """Fix 2: a save/split against a stale lock_version 409s instead of
    silently overwriting a concurrent edit."""

    def test_stale_edit_returns_409_and_does_not_overwrite(self, env):
        client, engine = env
        # Both "tabs" open the editor at the same starting version.
        track = client.get("/api/projects/My Trip/activities/111/track").json()
        v0 = track["lock_version"]
        assert v0 == 0

        # Tab A saves first and wins.
        resp_a = client.put(
            "/api/projects/My Trip/activities/111/track",
            json={"points": _points(3), "lock_version": v0},
        )
        assert resp_a.status_code == 200, resp_a.text

        # Tab B is still working from v0 — must be rejected, not applied on
        # top of Tab A's edit.
        resp_b = client.put(
            "/api/projects/My Trip/activities/111/track",
            json={"points": _points(2), "lock_version": v0},
        )
        assert resp_b.status_code == 409, resp_b.text

        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            # Tab A's 3-point trim persisted; Tab B's 2-point trim did not.
            assert len(polyline_lib.decode(row.summary_polyline)) == 3

    def test_matching_lock_version_saves_normally(self, env):
        client, _ = env
        track = client.get("/api/projects/My Trip/activities/111/track").json()
        resp = client.put(
            "/api/projects/My Trip/activities/111/track",
            json={"points": _points(3), "lock_version": track["lock_version"]},
        )
        assert resp.status_code == 200, resp.text

    def test_omitted_lock_version_saves_unconditionally(self, env):
        """Backward compatible: a caller that never sends lock_version (older
        client) still saves — Fix 2 is opt-in, not a breaking requirement."""
        client, _ = env
        resp = client.put(
            "/api/projects/My Trip/activities/111/track",
            json={"points": _points(3)},
        )
        assert resp.status_code == 200, resp.text

    def test_stale_split_returns_409_and_does_not_double_split(self, env):
        client, engine = env
        track = client.get("/api/projects/My Trip/activities/111/track").json()
        v0 = track["lock_version"]

        resp_a = client.post(
            "/api/projects/My Trip/activities/111/split",
            json={"split_index": 2, "lock_version": v0},
        )
        assert resp_a.status_code == 200, resp_a.text

        resp_b = client.post(
            "/api/projects/My Trip/activities/111/split",
            json={"split_index": 1, "lock_version": v0},
        )
        assert resp_b.status_code == 409, resp_b.text

        with Session(engine) as sess:
            tails = sess.exec(select(DBActivity).where(DBActivity.id < 0)).all()
        assert len(tails) == 1  # only Tab A's split took effect


def _put_track_raw(client, payload):
    """PUT /track with a hand-encoded JSON body.

    httpx's ``json=`` convenience param refuses to encode NaN/Infinity
    (``allow_nan=False``) — but a client can still send them over the wire
    (stdlib ``json.dumps``/``json.loads`` allow them by default), so this
    sends the raw bytes to exercise exactly what the server has to guard
    against.
    """
    body = json.dumps(payload, allow_nan=True).encode()
    return client.put(
        "/api/projects/My Trip/activities/111/track",
        content=body,
        headers={"content-type": "application/json"},
    )


class TestCoordinateValidation:
    """Fix 3: TrackPointIn rejects out-of-range / non-finite lat/lng."""

    @pytest.mark.parametrize("bad_lat", [95.0, -95.0])
    def test_out_of_range_lat_rejected(self, env, bad_lat):
        client, engine = env
        resp = client.put(
            "/api/projects/My Trip/activities/111/track",
            json={"points": [{"lat": bad_lat, "lng": 2.0},
                              {"lat": 48.0, "lng": 2.01}]},
        )
        assert resp.status_code == 422, resp.text
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            assert row.summary_polyline == polyline_lib.encode(_TRACK)  # unchanged

    @pytest.mark.parametrize("bad_lng", [185.0, -185.0])
    def test_out_of_range_lng_rejected(self, env, bad_lng):
        client, engine = env
        resp = client.put(
            "/api/projects/My Trip/activities/111/track",
            json={"points": [{"lat": 48.0, "lng": bad_lng},
                              {"lat": 48.0, "lng": 2.01}]},
        )
        assert resp.status_code == 422, resp.text
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            assert row.summary_polyline == polyline_lib.encode(_TRACK)  # unchanged

    @pytest.mark.parametrize("bad_lat", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_lat_rejected(self, env, bad_lat):
        client, engine = env
        resp = _put_track_raw(client, {"points": [{"lat": bad_lat, "lng": 2.0},
                                                    {"lat": 48.0, "lng": 2.01}]})
        assert resp.status_code == 422, resp.text
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            assert row.summary_polyline == polyline_lib.encode(_TRACK)  # unchanged

    @pytest.mark.parametrize("bad_lng", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_lng_rejected(self, env, bad_lng):
        client, engine = env
        resp = _put_track_raw(client, {"points": [{"lat": 48.0, "lng": bad_lng},
                                                    {"lat": 48.0, "lng": 2.01}]})
        assert resp.status_code == 422, resp.text
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            assert row.summary_polyline == polyline_lib.encode(_TRACK)  # unchanged

    def test_valid_points_still_accepted(self, env):
        client, _ = env
        resp = client.put(
            "/api/projects/My Trip/activities/111/track",
            json={"points": _points(2)},
        )
        assert resp.status_code == 200, resp.text

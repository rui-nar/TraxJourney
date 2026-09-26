"""A non-finite elevation is a missing one, wherever it comes from (issue #462).

gpxpy reads ``<ele>NaN</ele>`` and ``<ele>inf</ele>`` as floats, and nothing
checked them: a GPX upload stored NaN or Infinity in the activity's elevation
profile, its gain and its high/low. The trip's JSON then held tokens the
client's parser refuses, so the trip would not open, and its export was a file
the import refuses. The track editor took the same values from a request body,
and a Strava stream or summary parsed from JSON could carry them too.

Every elevation input now treats NaN and ±Infinity as "no reading", as a
missing ``<ele>`` has always been treated: left out of gain and high/low, and
interpolated across in the stored profile (#374). So every trip the app can
create opens, exports, and imports back.
"""
from __future__ import annotations

import json
import math
import time

import polyline as polyline_lib
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.activities as activities_module
import api.project_shared as project_shared_mod
import api.project_transfer as project_transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import StravaToken, UserInfo
from src.models.activity import Activity
import src.models.track_edit as track_edit
from src.models.track_edit import TrackPoint, recompute_track_metrics
from src.project.project_io import ProjectIO
from tests.test_gpx_import_api import _gpx_xml

_NAN, _INF = float("nan"), float("inf")
_BAD = {"NaN": _NAN, "Infinity": _INF, "-Infinity": -_INF}


def _finite(value) -> bool:
    return value is None or math.isfinite(value)


def _strict_json(text: str):
    """Parse as the client does: Dart's jsonDecode refuses NaN and Infinity."""
    def _refuse(token):
        raise ValueError(f"not JSON: {token}")
    return json.loads(text, parse_constant=_refuse)


def _stored_profile_is_finite(ep_json) -> bool:
    if ep_json is None:
        return True
    ep = _strict_json(ep_json)
    return all(math.isfinite(v) for v in ep["distances_km"] + ep["elevations_m"])


def _row_is_finite(row: DBActivity) -> bool:
    return (math.isfinite(row.total_elevation_gain) and _finite(row.elev_high)
            and _finite(row.elev_low)
            and _stored_profile_is_finite(row.elevation_profile_json)
            and _stored_profile_is_finite(row.elevation_profile_low_res_json))


# ── The canonical point and the metrics computed from it ────────────────────

@pytest.mark.parametrize("bad", _BAD.values(), ids=_BAD.keys())
def test_a_track_point_holds_a_non_finite_elevation_as_missing(bad):
    assert TrackPoint(lat=45.0, lng=6.0, elev=bad).elev is None
    assert TrackPoint(lat=45.0, lng=6.0, elev=812.5).elev == 812.5


@pytest.mark.parametrize("bad", _BAD.values(), ids=_BAD.keys())
def test_metrics_skip_a_non_finite_elevation_like_a_missing_one(bad):
    lats = [45.0 + i * 0.001 for i in range(5)]
    elevs = [500.0, 510.0, 520.0, 515.0, 530.0]
    with_bad = [TrackPoint(lat, 6.0, bad if i == 2 else e)
                for i, (lat, e) in enumerate(zip(lats, elevs))]
    with_gap = [TrackPoint(lat, 6.0, None if i == 2 else e)
                for i, (lat, e) in enumerate(zip(lats, elevs))]

    got, want = recompute_track_metrics(with_bad), recompute_track_metrics(with_gap)

    assert got == want
    assert math.isfinite(got.total_elevation_gain)
    assert got.elev_high == 530.0 and got.elev_low == 500.0


# ── Strava streams and summaries ────────────────────────────────────────────

def test_a_stream_profile_fills_non_finite_and_missing_altitudes():
    distance = [0.0, 100.0, 200.0, 300.0, 400.0]
    altitude = [100.0, _NAN, None, _INF, 140.0]

    dists, elevs = track_edit.elevation_profile_from_streams(distance, altitude)

    assert dists == [0.0, 0.1, 0.2, 0.3, 0.4]
    assert elevs == pytest.approx([100.0, 110.0, 120.0, 130.0, 140.0])


def test_a_stream_sample_with_no_usable_distance_is_left_out():
    dists, elevs = track_edit.elevation_profile_from_streams(
        [0.0, _NAN, 200.0, None, 400.0], [100.0, 999.0, 120.0, 999.0, 140.0])

    assert dists == [0.0, 0.2, 0.4]
    assert elevs == [100.0, 120.0, 140.0]


@pytest.mark.parametrize("altitude", [[_NAN, _INF, None], [100.0], []])
def test_a_stream_with_no_usable_profile_gives_none(altitude):
    assert track_edit.elevation_profile_from_streams([0.0, 100.0, 200.0][:len(altitude)], altitude) is None


def test_a_strava_summary_with_non_finite_elevations_keeps_none_of_them():
    act = Activity.from_strava_api({
        "id": 1, "name": "Ride", "type": "Ride",
        "total_elevation_gain": _INF, "elev_high": _NAN, "elev_low": -_INF,
    })

    assert act.total_elevation_gain == 0.0
    assert act.elev_high is None and act.elev_low is None


class _Streams:
    """A Strava client whose streams carry every kind of bad altitude."""
    remaining_requests = 50

    def __init__(self, raw=None):
        self._raw = raw

    def get_activity_streams(self, activity_id):
        return {
            "latlng": {"data": [[48.0, 2.0], [48.0, 2.01], [48.0, 2.02], [48.0, 2.03]]},
            "altitude": {"data": [100.0, _NAN, _INF, 130.0]},
            "distance": {"data": [0.0, 740.0, 1480.0, 2220.0]},
        }

    def get_activity(self, activity_id):
        return self._raw


def test_in_memory_enrichment_stores_a_finite_profile():
    act = Activity.from_strava_api({"id": 7, "name": "Ride", "type": "Ride"})

    activities_module._enrich_activities([act], _Streams())

    dists, elevs = act.elevation_profile
    assert all(math.isfinite(v) for v in dists + elevs)
    assert elevs == pytest.approx([100.0, 110.0, 120.0, 130.0])


@pytest.fixture
def strava_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    for fn in ("bust_geo_cache", "warm_geo_cache", "warm_meta_cache"):
        monkeypatch.setattr(activities_module, fn, lambda *a, **k: None)
    with Session(engine) as sess:
        user = UserInfo(display_name="A", email="a@e.com")
        sess.add(user); sess.commit(); sess.refresh(user)
        proj = DBProject(user_info_id=user.id, name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        sess.add(StravaToken(user_info_id=user.id, access_token="tok",
                             refresh_token="ref", expires_at=time.time() + 3600))
        sess.add(DBActivity(
            id=111, user_info_id=user.id, name="Ride", type="Ride", distance=2220.0,
            start_date="2024-06-01T10:00:00Z", start_date_local="2024-06-01T12:00:00Z"))
        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="activity", activity_id=111))
        sess.commit()
        return engine, user.id


def _row(engine) -> DBActivity:
    with Session(engine) as sess:
        return sess.exec(select(DBActivity).where(DBActivity.id == 111)).one()


def test_background_enrichment_stores_a_finite_profile(strava_db, monkeypatch):
    engine, uid = strava_db
    monkeypatch.setattr(activities_module, "_strava_client_for_user", lambda _u: _Streams())

    activities_module._enrich_activities_background([111], uid, uid, "Trip")

    row = _row(engine)
    assert row.elevation_profile_json is not None
    assert _row_is_finite(row)


def test_a_refetch_stores_finite_elevations(strava_db, monkeypatch):
    engine, uid = strava_db
    raw = {"id": 111, "name": "Ride", "type": "Ride", "distance": 2220.0,
           "start_date": "2024-06-01T10:00:00Z", "start_date_local": "2024-06-01T12:00:00Z",
           "total_elevation_gain": _INF, "elev_high": _INF, "elev_low": -_INF}
    monkeypatch.setattr(activities_module, "_strava_client_for_user",
                        lambda _u: _Streams(raw))

    activities_module._refresh_activity_job(uid, uid, "Trip", 111)

    row = _row(engine)
    assert row.refresh_status == "resolved", row.refresh_error
    assert row.elevation_profile_json is not None
    assert _row_is_finite(row)


# ── End to end: upload, open, edit, export, import back ─────────────────────

@pytest.fixture
def app(monkeypatch, tmp_path):
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, project_transfer_mod, storage_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="A", email="a@e.com")
        sess.add(user); sess.commit()
        uid = user.id
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        client = TestClient(router.app, raise_server_exceptions=False)
        assert client.post("/api/projects", json={"name": "Trip"}).status_code == 201
        yield client, engine
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _upload_gpx(client, elevations) -> int:
    track = [(48.0 + i * 0.001, 2.0 + i * 0.001, e) for i, e in enumerate(elevations)]
    r = client.post(
        "/api/projects/Trip/activities/import-gpx",
        files={"file": ("t.gpx", _gpx_xml([[track]]).encode(), "application/gpx+xml")},
        data={"date": "2024-06-01", "start_time": "09:00", "end_time": "10:00",
              "activity_type": "Hike"})
    assert r.status_code == 200, r.text
    return r.json()["activity_id"]


def _activity_row(engine, activity_id) -> DBActivity:
    with Session(engine) as sess:
        return sess.get(DBActivity, activity_id)


_ELES = {"NaN": "NaN", "inf": "inf", "-inf": "-inf", "1e999": "1e999"}


@pytest.mark.parametrize("ele", _ELES.values(), ids=_ELES.keys())
def test_a_gpx_upload_with_a_non_finite_ele_opens_exports_and_imports_back(app, ele):
    client, engine = app
    activity_id = _upload_gpx(client, [100.0, 110.0, ele, 130.0, 125.0])

    row = _activity_row(engine, activity_id)
    assert _row_is_finite(row)
    assert row.elev_high == 130.0 and row.elev_low == 100.0
    assert json.loads(row.elevation_profile_json)["elevations_m"][2] == pytest.approx(120.0, abs=1.0)

    r = client.get("/api/projects/Trip")
    assert r.status_code == 200, r.text
    _strict_json(r.text)
    r = client.get("/api/projects/Trip/meta")
    assert r.status_code == 200, r.text
    _strict_json(r.text)

    exported = client.get("/api/projects/Trip/export-traxj")
    assert exported.status_code == 200, exported.text
    r = client.post("/api/projects/import", files={
        "file": (f"Back{ProjectIO.EXTENSION}", exported.content, "application/json")})
    assert r.status_code == 201, r.text


def test_a_gpx_upload_whose_every_ele_is_non_finite_has_no_elevation(app):
    client, engine = app
    activity_id = _upload_gpx(client, ["NaN", "inf", "-inf"])

    row = _activity_row(engine, activity_id)
    assert row.elevation_profile_json is None
    assert row.total_elevation_gain == 0.0
    assert row.elev_high is None and row.elev_low is None


def _edit(client, activity_id, elevs, token=None):
    points = [{"lat": 48.0 + i * 0.001, "lng": 2.0 + i * 0.001, "elev": e}
              for i, e in enumerate(elevs)]
    body = json.dumps({"points": points})
    if token is not None:
        body = body.replace('"elev": 0.0', f'"elev": {token}')
        assert token in body
    return client.put(f"/api/projects/Trip/activities/{activity_id}/track", content=body,
                      headers={"Content-Type": "application/json"})


@pytest.mark.parametrize("token", _BAD.keys())
def test_the_track_editor_refuses_a_non_finite_elev(app, token):
    """A body holding NaN or Infinity is refused before any route runs
    (api/json_guard.py): no JSON document the app writes holds one."""
    client, engine = app
    activity_id = _upload_gpx(client, [100.0, 110.0, 120.0, 130.0, 125.0])
    before = _activity_row(engine, activity_id).summary_polyline

    r = _edit(client, activity_id, [100.0, 110.0, 0.0, 130.0], token)

    assert r.status_code == 422, r.text
    assert _activity_row(engine, activity_id).summary_polyline == before


@pytest.mark.parametrize("elev", [99999.0, -30000.0])
def test_the_track_editor_takes_an_implausible_elev_as_missing(app, elev):
    client, engine = app
    activity_id = _upload_gpx(client, [100.0, 110.0, 120.0, 130.0, 125.0])

    r = _edit(client, activity_id, [100.0, 110.0, elev, 130.0])

    assert r.status_code == 200, r.text
    _strict_json(r.text)
    row = _activity_row(engine, activity_id)
    assert _row_is_finite(row)
    assert row.elev_high == 130.0 and row.elev_low == 100.0

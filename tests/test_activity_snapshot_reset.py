"""Scalar edit snapshots and the exact reset (E2EE remnants U12, decision 7).

The first edit of a track — plaintext or encrypted path — snapshots the
figures beside the geometry: distance, moving and elapsed time, average speed,
elevation extremes and both endpoints. Reset restores them exactly, so it no
longer has to measure the original track, which it cannot do once that track
is ciphertext. A split tail's snapshot is its own post-split values.

Reset of an encrypted row puts the original envelopes back verbatim, the
profile envelope in both profile columns (R5-3). An enveloped original with
no scalar snapshot still has nothing to restore (test_activity_reset.py).
"""
from __future__ import annotations

import base64
import json

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from api.activities import router as activities_router
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03), (48.0, 2.04)]
_ELEV = [100.0, 120.0, 110.0, 140.0, 130.0]
_SCALARS = ("distance", "moving_time", "elapsed_time", "average_speed",
            "elev_high", "elev_low", "start_latlng_json", "end_latlng_json")
_RESTORED = _SCALARS + ("total_elevation_gain", "summary_polyline",
                        "elevation_profile_json", "elevation_profile_low_res_json")


def _env(seed: int) -> str:
    """An envelope as ``EncryptedField.encode`` writes one (see
    test_activity_encrypted_edit.py)."""
    wrapped = b"\xfb\xff\xbf" * 24
    ciphertext = bytes([seed % 256]) * 41
    return (f"v1.{base64.b64encode(wrapped).decode()}"
            f".{base64.b64encode(ciphertext).decode()}")


# Figures a synced activity arrives with: Strava's, none of which the track's
# own geometry reproduces (its haversine length is ~2976 m, not 4000).
_PLAIN = dict(
    summary_polyline=polyline_lib.encode(_TRACK),
    elevation_profile_json=json.dumps({"distances_km": [0.0, 1.0, 2.0, 3.0, 4.0],
                                       "elevations_m": _ELEV}),
    elevation_profile_low_res_json=json.dumps({"distances_km": [0.0, 4.0],
                                               "elevations_m": [100.0, 130.0]}),
    start_latlng_json=json.dumps([48.0001, 2.0001]),
    end_latlng_json=json.dumps([48.0002, 2.0402]),
    distance=4000.0, moving_time=1000, elapsed_time=1200, average_speed=3.97,
    total_elevation_gain=60.0, elev_high=141.5, elev_low=99.5,
)
_ENCRYPTED = dict(
    summary_polyline=_env(1), elevation_profile_json=_env(2),
    elevation_profile_low_res_json=_env(2), start_latlng_json=_env(3),
    end_latlng_json=_env(4), distance=4000.0, moving_time=1000, elapsed_time=1200,
    average_speed=4.0, total_elevation_gain=60.0, elev_high=140.0, elev_low=100.0,
)


@pytest.fixture
def make_env(monkeypatch):
    def _make(fields):
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
            proj = DBProject(user_info_id=u.id, name="Trip", lock_version=1)
            sess.add(proj); sess.commit(); sess.refresh(proj)
            sess.add(DBActivity(id=111, user_info_id=u.id, name="Ride", type="Ride",
                                start_date="2026-05-01T08:00:00Z", **fields))
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                   item_type="activity", activity_id=111))
            sess.commit()
            uid = u.id
        app = FastAPI()
        app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
        app.include_router(activities_router)
        return TestClient(app), engine
    return _make


def _row(engine, aid=111):
    with Session(engine) as sess:
        a = sess.get(DBActivity, aid)
        return {c: getattr(a, c) for c in DBActivity.model_fields}


def _trim(client):
    points = [{"lat": lat, "lng": lng, "elev": e}
              for (lat, lng), e in zip(_TRACK[:3], _ELEV[:3])]
    resp = client.put("/api/projects/Trip/activities/111/track", json={"points": points})
    assert resp.status_code == 200, resp.text


def _reset(client):
    resp = client.post("/api/projects/Trip/activities/111/reset")
    assert resp.status_code == 200, resp.text


def test_a_plaintext_edit_snapshots_every_scalar(make_env):
    client, engine = make_env(_PLAIN)
    _trim(client)
    row = _row(engine)
    for field in _SCALARS:
        assert row[f"original_{field}"] == _PLAIN[field], field
        assert row[field] != _PLAIN[field], field       # the edit did change it


def test_a_second_plaintext_edit_keeps_the_first_snapshot(make_env):
    client, engine = make_env(_PLAIN)
    _trim(client)
    points = [{"lat": lat, "lng": lng} for lat, lng in _TRACK[:2]]
    assert client.put("/api/projects/Trip/activities/111/track",
                      json={"points": points}).status_code == 200
    row = _row(engine)
    for field in _SCALARS:
        assert row[f"original_{field}"] == _PLAIN[field], field


def test_reset_after_a_plaintext_edit_restores_every_scalar_exactly(make_env):
    client, engine = make_env(_PLAIN)
    _trim(client)
    _reset(client)
    row = _row(engine)
    for field in _RESTORED:
        if field == "elevation_profile_low_res_json":
            continue        # re-derived from the full profile, as always
        assert row[field] == _PLAIN[field], field
    assert row["is_edited"] is False
    for field in _SCALARS + ("polyline", "elevation_profile_json", "total_elevation_gain"):
        assert row[f"original_{field}"] is None, field


def test_reset_after_an_encrypted_edit_restores_envelopes_and_scalars_exactly(make_env):
    client, engine = make_env(_ENCRYPTED)
    piece = dict(
        summary_polyline=_env(11), elevation_profile_json=_env(12),
        start_latlng_json=_env(13), end_latlng_json=_env(14),
        distance=2500.0, moving_time=600, elapsed_time=700, average_speed=4.2,
        total_elevation_gain=30.0, elev_high=130.0, elev_low=101.0,
    )
    resp = client.put("/api/projects/Trip/activities/111/track/encrypted",
                      json=dict(piece, lock_version=1))
    assert resp.status_code == 200, resp.text
    _reset(client)
    row = _row(engine)
    for field in _RESTORED:
        assert row[field] == _ENCRYPTED[field], field
    # One envelope in both profile columns: the low-res copy cannot be derived
    # from ciphertext (R5-3).
    assert row["elevation_profile_low_res_json"] == row["elevation_profile_json"] == _env(2)
    assert row["is_edited"] is False
    for field in _SCALARS + ("polyline", "elevation_profile_json", "total_elevation_gain"):
        assert row[f"original_{field}"] is None, field


def test_reset_restores_an_enveloped_original_that_has_a_scalar_snapshot(make_env):
    """An edited row the catch-up encrypted: its originals are envelopes now,
    with the scalar snapshot taken at the edit (or by the backfill)."""
    snapshot = {f"original_{f}": _ENCRYPTED[f] for f in _SCALARS}
    client, engine = make_env(dict(
        _ENCRYPTED, summary_polyline=_env(31), elevation_profile_json=_env(32),
        elevation_profile_low_res_json=_env(32), distance=10.0, moving_time=5,
        elapsed_time=6, is_edited=True, original_polyline=_ENCRYPTED["summary_polyline"],
        original_elevation_profile_json=_ENCRYPTED["elevation_profile_json"],
        original_total_elevation_gain=_ENCRYPTED["total_elevation_gain"], **snapshot,
    ))
    _reset(client)
    row = _row(engine)
    for field in _RESTORED:
        assert row[field] == _ENCRYPTED[field], field


def test_reset_restores_a_null_endpoint_snapshot_as_null(make_env):
    """The backfill leaves an endpoint null where reset would have: the
    snapshot is restored as it is, null included."""
    snapshot = {f"original_{f}": _PLAIN[f] for f in _SCALARS}
    snapshot.update(original_start_latlng_json=None, original_end_latlng_json=None)
    client, engine = make_env(dict(
        _PLAIN, is_edited=True, original_polyline=_PLAIN["summary_polyline"],
        original_elevation_profile_json=_PLAIN["elevation_profile_json"],
        original_total_elevation_gain=60.0, **snapshot,
    ))
    _reset(client)
    row = _row(engine)
    assert row["start_latlng_json"] is None and row["end_latlng_json"] is None
    assert row["distance"] == _PLAIN["distance"]


def test_reset_without_a_scalar_snapshot_still_recomputes_from_the_track(make_env):
    """A row edited before snapshots existed, the backfill having nothing to
    read: reset measures the plaintext original as it always did."""
    from src.models.track_edit import align_points, recompute_track_metrics

    client, engine = make_env(dict(
        _PLAIN, summary_polyline=polyline_lib.encode(_TRACK[:3]), distance=2000.0,
        is_edited=True, original_polyline=_PLAIN["summary_polyline"],
        original_elevation_profile_json=_PLAIN["elevation_profile_json"],
        original_total_elevation_gain=60.0,
    ))
    _reset(client)
    measured = recompute_track_metrics(align_points(
        _PLAIN["summary_polyline"], ([0.0, 1.0, 2.0, 3.0, 4.0], _ELEV)))
    row = _row(engine)
    assert row["distance"] == pytest.approx(measured.distance)
    assert row["elev_high"] == measured.elev_high


def test_a_plaintext_split_tail_snapshots_its_own_values(make_env):
    client, engine = make_env(_PLAIN)
    resp = client.post("/api/projects/Trip/activities/111/split", json={"split_index": 2})
    assert resp.status_code == 200, resp.text
    tail_id = next(a["id"] for a in resp.json()["activities"] if a["id"] < 0)
    tail = _row(engine, tail_id)
    for field in _SCALARS:
        assert tail[f"original_{field}"] == tail[field], field
    head = _row(engine, 111)
    for field in _SCALARS:
        assert head[f"original_{field}"] == _PLAIN[field], field

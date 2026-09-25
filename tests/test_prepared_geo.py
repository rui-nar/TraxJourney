"""Prepared geometry persisted per activity — issue #369, stage B1.

The cold build of the simplified geo endpoint used to decode every activity
polyline and run a Ramer-Douglas-Peucker pass over the whole trip, on the
request path, holding the GIL: 4–5 s for a 219-activity trip, during which
unrelated requests measured 20–40x slower. Now every path that writes a
polyline also writes ``activity_geo_prepared`` — the working set, the
per-vertex zoom levels of stage A, and the bounding box — and the endpoint
serves from that table.

What these pin, in order:

* the blob codec round-trips exactly (the int32 scaling is lossless for
  polyline-decoded data, negative coordinates included);
* every writer leaves a current row, an encrypted polyline leaves none, a
  deleted row takes its side row with it;
* the served payload for a prepared trip equals what the pre-change path
  produced — computed here from ``_build_full_geo_features`` and
  ``simplify_for_zoom``, not from the code under test;
* above all, a cold request on a fully prepared trip calls ``polyline.decode``
  **zero** times. That is the difference between "faster" and "off the
  request path", and it is checkable rather than inferrable;
* a missing or stale row is prepared on the request path once and written
  back, so the second open is fast and a version bump needs no migration;
* the migration applies on a fresh database and on one at the previous head.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from array import array
from datetime import datetime, timezone
from pathlib import Path

import polyline as polyline_lib
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.geo as geo_mod
import models.db as db_module
from api.activities import activity_fields_router
from api.deps import get_current_user
from api.geo import (
    _build_full_geo_features,
    _geo_cache,
    _geo_gen,
    _track_cache,
    _track_cache_bytes,
)
from api.geo import router as geo_router
from models.project_db import DBActivity, DBActivityGeoPrepared, DBProject, DBProjectItem
from models.user import UserInfo
from src.models.activity import Activity
from src.models.prepared_geo import (
    COORD_SCALE,
    PreparedGeoFormatError,
    pack_prepared_line,
    prepare_polyline,
    unpack_prepared_line,
)
from src.models.simplify import (
    MAX_INPUT_POINTS,
    MIN_POINTS,
    PREPARED_GEO_VERSION,
    floor_line,
    line_bbox,
    simplify_for_zoom,
    vertex_levels,
    working_set,
)
from src.models.track_edit import align_points
from src.project.project_repo import ProjectRepo
from src.project.repo_activities import store_prepared_geometry

_ENC_POLY = "v1.YWJj.ZGVm"

# Every level the endpoints can be asked for, in the steps a session takes.
_LEVELS = (0, 3, 6, 9, 12, 15, 18, 22)


def _wiggly(n: int, *, lon: float = -70.0, lat: float = 45.0) -> list[tuple[float, float]]:
    """A winding ``(lat, lon)`` track through the western hemisphere.

    West of Greenwich on purpose: negative longitudes are where an int()
    truncation in the codec would show, and where nothing else in the
    suite looks. Round-tripped through the polyline codec so the fixture
    has the five-decimal precision every real track has.
    """
    out = []
    for i in range(n):
        lon += 0.00003 + math.sin(i / 50.0) * 0.00002
        lat += math.cos(i / 37.0) * 0.00002
        out.append((lat, lon))
    return polyline_lib.decode(polyline_lib.encode(out))


def _lonlat(track: list[tuple[float, float]]) -> list[list[float]]:
    return [[lon, lat] for lat, lon in track]


# ── the codec ────────────────────────────────────────────────────────────────

def test_the_blob_round_trips_exactly():
    work = working_set(_lonlat(_wiggly(6000)))
    levels = vertex_levels(work)
    blob = pack_prepared_line(work, levels, line_bbox(work))
    flat, got_levels, bbox, version = unpack_prepared_line(blob)
    assert isinstance(flat, array) and flat.typecode == "i"
    assert version == PREPARED_GEO_VERSION
    assert got_levels == levels
    assert bbox == line_bbox(work)
    # ==, not approx: the format claims the integer scaling is lossless for
    # polyline-decoded data, and a coordinate off by 1e-5 would be a
    # different map than the one the user saved.
    assert [[flat[2 * i] / COORD_SCALE, flat[2 * i + 1] / COORD_SCALE]
            for i in range(len(work))] == work


def test_the_scaling_is_lossless_over_the_whole_globe():
    # polyline.decode computes k / 100000.0; the codec must recover k from
    # that float for every k a coordinate can be, negatives and the
    # antimeridian included.
    rng = random.Random(369)
    ks = [rng.randint(-180_00000, 180_00000) for _ in range(20000)]
    ks += [-180_00000, -90_00000, -1, 0, 1, 90_00000, 180_00000]
    for k in ks:
        assert round((k / COORD_SCALE) * COORD_SCALE) == k, k


def test_the_blob_is_nine_bytes_per_coordinate_plus_a_header():
    work = working_set(_lonlat(_wiggly(5000)))
    blob = pack_prepared_line(work, vertex_levels(work), line_bbox(work))
    assert len(work) == MAX_INPUT_POINTS
    assert len(blob) == 37 + 9 * MAX_INPUT_POINTS


def test_a_levels_points_mismatch_is_a_programming_error():
    work = _lonlat(_wiggly(100))
    with pytest.raises(ValueError):
        pack_prepared_line(work, bytes(99), line_bbox(work))


def test_a_truncated_or_corrupt_blob_is_refused_not_misread():
    work = _lonlat(_wiggly(100))
    blob = pack_prepared_line(work, vertex_levels(work), line_bbox(work))
    for bad in (b"", blob[:10], blob[:-1], blob + b"\0"):
        with pytest.raises(PreparedGeoFormatError):
            unpack_prepared_line(bad)


def test_prepare_polyline_declines_what_the_server_cannot_prepare():
    assert prepare_polyline(None) is None
    assert prepare_polyline("") is None
    assert prepare_polyline(_ENC_POLY) is None
    assert prepare_polyline(polyline_lib.encode([(45.0, 7.0)])) is None, "nothing to draw"


def test_prepare_polyline_prepares_a_two_point_track():
    # Served verbatim at every level — but a row for it means the trip is
    # fully prepared rather than one activity short on every cold open.
    blob = prepare_polyline(polyline_lib.encode([(45.0, 7.0), (45.1, 7.1)]))
    flat, levels, _bbox, _version = unpack_prepared_line(blob)
    assert len(flat) == 4 and levels == bytes(2)


def test_prepare_polyline_holds_the_working_set_not_the_track():
    blob = prepare_polyline(polyline_lib.encode(_wiggly(9000)))
    flat, levels, _bbox, _version = unpack_prepared_line(blob)
    assert len(flat) == 2 * MAX_INPUT_POINTS and len(levels) == MAX_INPUT_POINTS


def test_prepare_polyline_raises_on_a_polyline_that_does_not_decode():
    # As everywhere else; the write path is what catches it.
    with pytest.raises(Exception):
        prepare_polyline("not a polyline")


# ── the writers ──────────────────────────────────────────────────────────────

_TRACK = _wiggly(400)


def _activity(id: int, **overrides) -> Activity:
    base = dict(
        id=id, name="Ride", type="Ride", distance=5000.0, moving_time=1200,
        elapsed_time=1300, total_elevation_gain=80.0,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        start_date_local=datetime(2026, 1, 1, tzinfo=timezone.utc),
        timezone="UTC", achievement_count=0, kudos_count=0, comment_count=0,
        athlete_count=1, photo_count=0, trainer=False, commute=False, manual=False,
        private=False, flagged=False, average_speed=3.0, max_speed=5.0,
        has_heartrate=False, pr_count=0, total_photo_count=0, has_kudoed=False,
        start_latlng=list(_TRACK[0]), end_latlng=list(_TRACK[-1]),
        summary_polyline=polyline_lib.encode(_TRACK),
        elevation_profile=None,
    )
    base.update(overrides)
    return Activity(**base)


@pytest.fixture
def repo_env(monkeypatch):
    """One user, one project, activity 111 with a plaintext track and NO
    prepared row — every writer under test has to be the one that makes it."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    _geo_cache.clear()
    _geo_gen.clear()
    _track_cache.clear()

    with Session(engine) as sess:
        user = UserInfo(display_name="Alice", email="a@b.c")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id
        project = DBProject(user_info_id=uid, name="Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        dist_km = [i * 0.01 for i in range(len(_TRACK))]
        sess.add(DBActivity(
            id=111, user_info_id=uid, name="Ride", type="Ride",
            distance=4000.0, moving_time=1000, elapsed_time=1200,
            summary_polyline=polyline_lib.encode(_TRACK),
            elevation_profile_json=json.dumps(
                {"distances_km": dist_km, "elevations_m": [100.0] * len(_TRACK)}),
            start_latlng_json=json.dumps(list(_TRACK[0])),
            end_latlng_json=json.dumps(list(_TRACK[-1])),
            start_date="2026-06-01T10:00:00Z",
            start_date_local="2026-06-01T12:00:00Z",
        ))
        sess.add(DBProjectItem(
            project_id=project.id, position=0, item_type="activity", activity_id=111))
        sess.commit()
        pid = project.id
    return engine, uid, pid, ProjectRepo()


def _row(engine, activity_id: int):
    with Session(engine) as sess:
        return sess.get(DBActivityGeoPrepared, activity_id)


def _assert_current(engine, activity_id: int) -> None:
    """The prepared row is exactly what the polyline on the row prepares to."""
    with Session(engine) as sess:
        prepared = sess.get(DBActivityGeoPrepared, activity_id)
        activity = sess.get(DBActivity, activity_id)
        assert prepared is not None, f"no prepared row for {activity_id}"
        assert prepared.version == PREPARED_GEO_VERSION
        assert prepared.blob == prepare_polyline(activity.summary_polyline)


def test_the_fixture_starts_unprepared(repo_env):
    engine, *_ = repo_env
    assert _row(engine, 111) is None


def test_enrichment_writes_the_row(repo_env):
    engine, _uid, _pid, repo = repo_env
    with Session(engine) as sess:
        repo.update_activity_enrichment(
            sess, 111, polyline_lib.encode(_wiggly(500, lon=-71.0)), None,
            owner_id=_uid)
    _assert_current(engine, 111)


def test_a_track_edit_writes_the_row(repo_env):
    engine, _uid, pid, repo = repo_env
    points = align_points(polyline_lib.encode(_TRACK), None)[:250]
    with Session(engine) as sess:
        assert repo.edit_activity_track(sess, pid, 111, points)
    _assert_current(engine, 111)
    flat, *_ = unpack_prepared_line(_row(engine, 111).blob)
    assert len(flat) == 2 * 250, "the row follows the edit, not the original"


def test_a_reset_writes_the_row_back_to_the_original(repo_env):
    engine, _uid, pid, repo = repo_env
    points = align_points(polyline_lib.encode(_TRACK), None)[:250]
    with Session(engine) as sess:
        repo.edit_activity_track(sess, pid, 111, points)
    with Session(engine) as sess:
        assert repo.reset_activity_track(sess, pid, 111)
    _assert_current(engine, 111)
    flat, *_ = unpack_prepared_line(_row(engine, 111).blob)
    assert len(flat) == 2 * len(_TRACK)


def test_a_split_writes_a_row_for_both_pieces(repo_env):
    engine, uid, pid, repo = repo_env
    with Session(engine) as sess:
        tail_id = repo.split_activity(sess, uid, pid, 111, split_index=200)
    assert tail_id is not None and tail_id < 0
    _assert_current(engine, 111)
    _assert_current(engine, tail_id)
    head_flat, *_ = unpack_prepared_line(_row(engine, 111).blob)
    tail_flat, *_ = unpack_prepared_line(_row(engine, tail_id).blob)
    assert len(head_flat) == 2 * 201 and len(tail_flat) == 2 * 200


def test_an_upsert_of_a_new_activity_writes_the_row(repo_env):
    engine, uid, _pid, repo = repo_env
    with Session(engine) as sess:
        repo._upsert_activity(sess, uid, _activity(222))
        sess.commit()
    _assert_current(engine, 222)


def test_an_upsert_filling_a_null_polyline_writes_the_row(repo_env):
    engine, uid, _pid, repo = repo_env
    with Session(engine) as sess:
        sess.add(DBActivity(id=333, user_info_id=uid, name="Bare", type="Ride",
                            start_date="2026-01-01T00:00:00Z"))
        sess.commit()
    with Session(engine) as sess:
        repo._upsert_activity(sess, uid, _activity(333))
        sess.commit()
    _assert_current(engine, 333)


def test_a_force_refresh_rewrites_the_row(repo_env):
    engine, uid, pid, repo = repo_env
    with Session(engine) as sess:
        repo.force_update_activity(
            sess, uid, _activity(111, summary_polyline=polyline_lib.encode(_wiggly(300, lat=46.0))),
            pid)
    _assert_current(engine, 111)
    flat, *_ = unpack_prepared_line(_row(engine, 111).blob)
    assert len(flat) == 2 * 300


def test_a_force_refresh_that_drops_the_polyline_drops_the_row(repo_env):
    engine, uid, pid, repo = repo_env
    with Session(engine) as sess:
        repo.update_activity_enrichment(sess, 111, polyline_lib.encode(_TRACK), None, owner_id=uid)
    assert _row(engine, 111) is not None
    with Session(engine) as sess:
        repo.force_update_activity(sess, uid, _activity(111, summary_polyline=None), pid)
    assert _row(engine, 111) is None, "a row for a track that no longer exists"


def test_a_polyline_that_does_not_decode_fails_no_write(repo_env):
    engine, uid, _pid, repo = repo_env
    with Session(engine) as sess:
        repo._upsert_activity(sess, uid, _activity(444, summary_polyline="freshpoly"))
        sess.commit()
    with Session(engine) as sess:
        assert sess.get(DBActivity, 444).summary_polyline == "freshpoly"
    assert _row(engine, 444) is None


# ── encryption: the server holds no key, so it holds no prepared plaintext ──

def test_an_encrypted_polyline_leaves_no_row_on_upsert(repo_env):
    engine, uid, _pid, repo = repo_env
    with Session(engine) as sess:
        repo._upsert_activity(sess, uid, _activity(555, summary_polyline=_ENC_POLY))
        sess.commit()
    assert _row(engine, 555) is None


def test_enrichment_of_an_encrypted_row_leaves_no_row(repo_env):
    engine, uid, _pid, repo = repo_env
    with Session(engine) as sess:
        sess.add(DBActivity(id=555, user_info_id=uid, name="Secret", type="Ride",
                            summary_polyline=_ENC_POLY, start_date="2026-01-01T00:00:00Z"))
        sess.commit()
    with Session(engine) as sess:
        repo.update_activity_enrichment(sess, 555, polyline_lib.encode(_TRACK), None, owner_id=uid)
    assert _row(engine, 555) is None


def test_encrypting_a_track_removes_the_plaintext_row(repo_env):
    """The encryption-enable migration (issue #29) swaps the plaintext polyline
    for ciphertext through PUT /api/activities/{id}. The prepared row derived
    from the plaintext must go with it, or the simplified endpoints would keep
    serving the very track the user just encrypted."""
    engine, uid, _pid, repo = repo_env
    with Session(engine) as sess:
        repo.update_activity_enrichment(sess, 111, polyline_lib.encode(_TRACK), None, owner_id=uid)
    assert _row(engine, 111) is not None

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(activity_fields_router)
    resp = TestClient(app).put("/api/activities/111", json={"summary_polyline": _ENC_POLY})
    assert resp.status_code == 200, resp.text
    assert _row(engine, 111) is None


# ── deletion: no PRAGMA foreign_keys, so the side row goes explicitly ────────

def test_deleting_a_local_activity_deletes_its_row(repo_env):
    engine, uid, pid, repo = repo_env
    with Session(engine) as sess:
        tail_id = repo.split_activity(sess, uid, pid, 111, split_index=200)
    assert _row(engine, tail_id) is not None
    with Session(engine) as sess:
        assert repo.delete_local_activity(sess, pid, tail_id)
    assert _row(engine, tail_id) is None
    assert _row(engine, 111) is not None, "the head keeps its row"


def test_resetting_a_split_root_deletes_the_pieces_rows(repo_env):
    engine, uid, pid, repo = repo_env
    with Session(engine) as sess:
        tail_id = repo.split_activity(sess, uid, pid, 111, split_index=200)
    with Session(engine) as sess:
        assert repo.reset_activity_track(sess, pid, 111)
    assert _row(engine, tail_id) is None
    _assert_current(engine, 111)


def test_deleting_an_account_deletes_its_rows(repo_env):
    from src.auth.account_deletion import delete_user_and_data
    engine, uid, _pid, repo = repo_env
    with Session(engine) as sess:
        repo.update_activity_enrichment(sess, 111, polyline_lib.encode(_TRACK), None, owner_id=uid)
    assert _row(engine, 111) is not None
    with Session(engine) as sess:
        delete_user_and_data(sess, uid)
    assert _row(engine, 111) is None


# ── the reader ───────────────────────────────────────────────────────────────
#
# A trip with every kind of item the builder distinguishes: a long plaintext
# track, a two-point track, an encrypted one, a GPX-style activity with no
# polyline (straight-line fallback), a ferry with a resolved route and a
# flight on a great-circle arc.

_LONG = _wiggly(6000)
_SHORT = [(44.0, -72.0), (44.1, -72.1)]
_FERRY_ROUTE = [[-70.0, 45.0], [-70.2, 44.0], [-69.5, 44.6], [-69.8, 44.2], [-70.0, 45.5]]


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    _geo_cache.clear()
    _geo_gen.clear()
    _track_cache.clear()

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(owner)
        sess.commit()
        sess.refresh(owner)
        uid = owner.id
        project = DBProject(user_info_id=uid, name="Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)

        def add(position, activity):
            sess.add(activity)
            sess.add(DBProjectItem(project_id=project.id, position=position,
                                   item_type="activity", activity_id=activity.id))

        add(0, DBActivity(
            id=111, user_info_id=uid, name="Long ride", type="Ride",
            start_date="2026-06-01T00:00:00Z",
            summary_polyline=polyline_lib.encode(_LONG),
            start_latlng_json=json.dumps(list(_LONG[0])),
            end_latlng_json=json.dumps(list(_LONG[-1]))))
        add(1, DBActivity(
            id=222, user_info_id=uid, name="Secret", type="Ride",
            start_date="2026-06-02T00:00:00Z", summary_polyline=_ENC_POLY))
        add(2, DBActivity(
            id=333, user_info_id=uid, name="GPX walk", type="Hike",
            start_date="2026-06-03T00:00:00Z", source="gpx",
            start_latlng_json=json.dumps([44.5, -71.5]),
            end_latlng_json=json.dumps([44.6, -71.4])))
        add(3, DBActivity(
            id=444, user_info_id=uid, name="Two points", type="Run",
            start_date="2026-06-04T00:00:00Z",
            summary_polyline=polyline_lib.encode(_SHORT),
            start_latlng_json=json.dumps(list(_SHORT[0])),
            end_latlng_json=json.dumps(list(_SHORT[-1]))))
        sess.add(DBProjectItem(
            project_id=project.id, position=4, item_type="segment",
            segment_id="seg-ferry",
            segment_json=json.dumps({
                "id": "seg-ferry", "segment_type": "boat", "label": "Ferry",
                "start": {"lat": 45.0, "lon": -70.0}, "end": {"lat": 45.5, "lon": -70.0},
                "route_mode": "ferry", "route_polyline": json.dumps(_FERRY_ROUTE),
                "route_status": "resolved",
            })))
        sess.add(DBProjectItem(
            project_id=project.id, position=5, item_type="segment",
            segment_id="seg-flight",
            segment_json=json.dumps({
                "id": "seg-flight", "segment_type": "flight", "label": "Flight",
                "start": {"lat": 45.5, "lon": -70.0}, "end": {"lat": 48.0, "lon": -60.0},
            })))
        sess.commit()

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(geo_router)
    return TestClient(app), uid, engine


def _prepare_everything(engine) -> None:
    """What the writers would have done: a row for every activity they can prepare."""
    with Session(engine) as sess:
        for row in sess.exec(select(DBActivity)).all():
            store_prepared_geometry(sess, row)
        sess.commit()


def _count_decodes(monkeypatch) -> list:
    calls = []
    real = geo_mod.polyline_lib.decode
    monkeypatch.setattr(geo_mod.polyline_lib, "decode",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    return calls


def _get(client, zoom, bbox=None):
    url = f"/api/geo/project/simplified?name=Trip&zoom={zoom}"
    if bbox is not None:
        url += "&bbox=" + ",".join(str(v) for v in bbox)
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    return resp


def _served(resp) -> list[tuple]:
    return [(f["properties"], f["geometry"]["coordinates"]) for f in resp.json()["features"]]


def _expected(engine, uid, level, box=None) -> list[tuple]:
    """The pre-change path, spelled out: full-resolution features from a heavy
    load, then per line what the endpoint served before anything was persisted
    — ``simplify_for_zoom`` for a line the box can show, the floor of its
    working set for one it cannot."""
    with Session(engine) as sess:
        project = ProjectRepo().get_project(sess, uid, "Trip")
    out = []
    for feature in _build_full_geo_features(project, encoded=False):
        coords = feature["geometry"]["coordinates"]
        if len(coords) < 3:
            served = coords
        elif box is not None and not geo_mod.bboxes_intersect(line_bbox(working_set(coords)), box):
            served = floor_line(working_set(coords))
        else:
            served = simplify_for_zoom(coords, level)
        # Through JSON, as the response is: tuples become lists, floats stay.
        out.append((feature["properties"], json.loads(json.dumps(served))))
    return out


def test_a_cold_request_on_a_prepared_trip_decodes_nothing(env, monkeypatch):
    """The point of the stage. Not "fewer decodes": none."""
    client, _uid, engine = env
    _prepare_everything(engine)
    _track_cache.clear()
    calls = _count_decodes(monkeypatch)
    _get(client, 12)
    assert calls == [], "a prepared trip must not touch a polyline on the request path"
    # And a whole session's sweep, boxes and all, stays at zero.
    for zoom in (6, 9, 15, 18, 22):
        _get(client, zoom, bbox=(-71, 44, -69, 46))
    assert calls == []


def test_a_prepared_trip_serves_exactly_what_the_old_path_served(env):
    client, uid, engine = env
    _prepare_everything(engine)
    _track_cache.clear()
    for level in _LEVELS:
        assert _served(_get(client, level)) == _expected(engine, uid, level), level


def test_a_prepared_trip_serves_exactly_what_the_old_path_served_with_a_box(env):
    client, uid, engine = env
    _prepare_everything(engine)
    _track_cache.clear()
    # A box around the long ride only: the ferry, flight and GPX walk are off
    # screen and must come back at their floors, as before.
    raw = (-70.05, 44.95, -69.6, 45.2)
    for level in (9, 12, 15):
        snapped, _ = geo_mod.snap_bbox_to_tiles(raw, level)
        assert _served(_get(client, level, bbox=raw)) == _expected(engine, uid, level, snapped), level


def test_every_kind_of_item_is_present_and_the_encrypted_one_is_not(env):
    client, _uid, engine = env
    _prepare_everything(engine)
    _track_cache.clear()
    features = _get(client, 12).json()["features"]
    ids = [f["properties"].get("activity_id") or f["properties"].get("segment_id")
           for f in features]
    assert ids == [111, 333, 444, "seg-ferry", "seg-flight"]
    by_id = {i: f["geometry"]["coordinates"] for i, f in zip(ids, features)}
    assert len(by_id[111]) > MIN_POINTS
    assert by_id[333] == [[-71.5, 44.5], [-71.4, 44.6]], "GPX: straight line from start to end"
    assert by_id[444] == _lonlat(_SHORT), "two points, verbatim"
    assert by_id["seg-ferry"] == _FERRY_ROUTE


def test_an_unprepared_trip_is_prepared_on_the_way_through_and_written_back(env, monkeypatch):
    client, uid, engine = env
    assert _row(engine, 111) is None and _row(engine, 444) is None
    calls = _count_decodes(monkeypatch)

    first = _served(_get(client, 12))
    assert len(calls) == 2, "one decode per activity that has a polyline — 111 and 444"
    assert first == _expected(engine, uid, 12)
    _assert_current(engine, 111)
    _assert_current(engine, 444)
    assert _row(engine, 222) is None, "encrypted: the server cannot prepare it"
    assert _row(engine, 333) is None, "no polyline: nothing to prepare"

    # The next cold open of this trip finds the rows.
    _track_cache.clear()
    calls.clear()
    assert _served(_get(client, 12)) == first
    assert calls == []


def test_a_row_at_another_version_is_treated_as_missing_and_rewritten(env, monkeypatch):
    client, uid, engine = env
    _prepare_everything(engine)
    with Session(engine) as sess:
        stale = sess.get(DBActivityGeoPrepared, 111)
        stale.version = PREPARED_GEO_VERSION + 1
        # Deliberately not a v1 blob at all: a stale row must not be unpacked.
        stale.blob = b"whatever a later format looks like"
        sess.add(stale)
        sess.commit()
    _track_cache.clear()
    calls = _count_decodes(monkeypatch)
    served = _served(_get(client, 15))
    assert len(calls) == 1, "only the stale row is prepared again"
    assert served == _expected(engine, uid, 15)
    _assert_current(engine, 111)


def test_a_write_back_that_fails_still_serves_the_trip(env, monkeypatch, caplog):
    client, uid, engine = env
    monkeypatch.setattr(Session, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("locked")))
    assert _served(_get(client, 12)) == _expected(engine, uid, 12)
    assert _row(engine, 111) is None
    assert "could not write back prepared geometry" in caplog.text


def test_the_track_cache_does_not_grow_across_a_level_sweep(env):
    # The memo is gone: whatever a session does, the trip costs the trip.
    client, _uid, engine = env
    _prepare_everything(engine)
    _track_cache.clear()
    _get(client, 12)
    after_one = _track_cache_bytes()
    for zoom in (0, 6, 9, 15, 18, 22):
        _get(client, zoom)
        _get(client, zoom, bbox=(-71, 44, -69, 46))
    assert _track_cache_bytes() == after_one


def test_a_prepared_line_costs_nine_bytes_per_coordinate(env):
    client, _uid, engine = env
    _prepare_everything(engine)
    _track_cache.clear()
    _get(client, 12)
    track = _track_cache_get_any()
    long_ride = next(line for line in track.lines if line.properties.get("activity_id") == 111)
    assert isinstance(long_ride.points, array) and long_ride.points.typecode == "i"
    assert long_ride.nbytes() == 9 * MAX_INPUT_POINTS + MIN_POINTS * geo_mod._LIST_COORD_BYTES


def _track_cache_get_any():
    (track, _deadline, _gen), = _track_cache.values()
    return track


def test_the_cold_build_is_logged_with_its_phases(env, caplog):
    client, _uid, engine = env
    _prepare_everything(engine)
    _track_cache.clear()
    with caplog.at_level("INFO", logger="api.geo"):
        _get(client, 12)
        _get(client, 15, bbox=(-71, 44, -69, 46))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("geo_simplified")]
    assert len(lines) == 2
    assert "level=12 box=all" in lines[0] and "cache=MISS" in lines[0]
    assert "level=15 box=" in lines[1] and "cache=HIT" in lines[1]
    for line in lines:
        assert "load=" in line and "build=" in line and "gzip=" in line


# ── the migration ────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PREVIOUS_HEAD = "e3c8a1d5b742"


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def _columns(db_path: Path, table: str) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "prepared.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")
    return path


def test_the_migration_creates_the_table_on_a_fresh_database(db_path):
    command.upgrade(_alembic_config(db_path), "head")
    assert _columns(db_path, "activity_geo_prepared") == ["activity_id", "version", "blob"]


def test_the_migration_applies_to_a_database_at_the_previous_head(db_path):
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, _PREVIOUS_HEAD)
    assert _columns(db_path, "activity_geo_prepared") == []
    # Data at the previous head: the activity table is unchanged by this
    # migration, so the ORM's row shape is the one that schema has.
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with Session(engine) as sess:
        sess.add(UserInfo(id=1, display_name="a", email="a@b.c"))
        sess.add(DBActivity(id=111, user_info_id=1, name="Ride", type="Ride",
                            summary_polyline=polyline_lib.encode(_TRACK),
                            start_date="2026-01-01T00:00:00Z"))
        sess.commit()
    engine.dispose()
    command.upgrade(cfg, "head")
    assert _columns(db_path, "activity_geo_prepared") == ["activity_id", "version", "blob"]
    with sqlite3.connect(db_path) as conn:
        # Existing rows are untouched and unprepared: the upgrade is instant,
        # and the reader prepares them on first open.
        assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM activity_geo_prepared").fetchone()[0] == 0


def test_the_migration_downgrades(db_path):
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PREVIOUS_HEAD)
    assert _columns(db_path, "activity_geo_prepared") == []


def test_the_read_path_cannot_overwrite_a_write_that_landed_while_it_prepared(env, monkeypatch):
    """The race the adversarial review of #394 caught, pinned on the path it was in.

    A cold open prepares unprepared activities outside any transaction and then
    writes the rows back. A writer landing in between has already changed the
    polyline; an unguarded write-back would store geometry built from the value
    read *before* that write, at the current version, where it looks fresh and
    is served on every later cold open.

    Until this test existed, nothing caught the guard going missing on this
    path: with the compare-and-swap neutered, every other test in this file
    still passed. The sweep in stage B2 got a test for the same race first, which
    is how the gap here was found.
    """
    client, uid, engine = env
    assert _row(engine, 111) is None
    real_prepare = geo_mod.prepare_polyline
    moved = polyline_lib.encode([(46.0 + i * 0.0001, 8.0 + i * 0.0001) for i in range(80)])

    def prepare_then_let_a_writer_in(polyline):
        blob = real_prepare(polyline)
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            if row.summary_polyline != moved:
                row.summary_polyline = moved
                sess.add(row)
                sess.commit()
        return blob

    monkeypatch.setattr(geo_mod, "prepare_polyline", prepare_then_let_a_writer_in)
    assert _get(client, 12).status_code == 200

    # The write-back was refused for 111, so no row claims to be current for
    # geometry that is no longer that activity's.
    assert _row(engine, 111) is None

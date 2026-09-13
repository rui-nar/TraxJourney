"""Zoom level of detail for map geometry — issue #295.

The measurement behind it: a 219-activity trip carries 1,465,345 coordinates
and the map renders 6,051 of them, holding the rest as roughly 180 MB of Dart
heap on a device whose heap plateaus at ~625 MB and which is killed above
~1.3 GB. Simplifying to what a zoom can actually resolve removes the
difference, rather than decimating it away client-side after paying to
transfer and materialise it.
"""
from __future__ import annotations

import math

import pytest

from api.geo import _features_for, _prepare_track
from src.models.simplify import simplify_lonlat, zoom_tolerance_m


def _simplified(features: list[dict], zoom: float, box: tuple | None = None) -> list[dict]:
    """Run *features* through the production path: prepare, then serve.

    Not a helper of its own: this is exactly what the route does between
    building the features and gzipping them, so these assertions pin
    production rather than a wrapper that only tests use (issue #338).
    """
    return _features_for(_prepare_track(features), zoom, box)


def _line(n: int, *, jitter: float = 0.0) -> list[list[float]]:
    """A roughly straight east-west line of *n* points, optionally wobbling by
    *jitter* degrees so simplification has something to remove."""
    return [
        [7.0 + i * 0.0001, 45.0 + (jitter if i % 2 else 0.0)]
        for i in range(n)
    ]


def _feature(coords: list[list[float]], **props) -> dict:
    return {
        "type": "Feature",
        "properties": {"type": "activity", "activity_id": "1", **props},
        "geometry": {"type": "LineString", "coordinates": coords},
    }


# ── tolerance ────────────────────────────────────────────────────────────────

def test_tolerance_halves_with_each_zoom_level():
    a = zoom_tolerance_m(10, 45.0)
    b = zoom_tolerance_m(11, 45.0)
    assert b == pytest.approx(a / 2, rel=1e-6)


def test_tolerance_shrinks_away_from_the_equator():
    # Web Mercator stretches with latitude: a pixel covers less ground further
    # north, so a latitude-blind tolerance would over-simplify northern tracks.
    assert zoom_tolerance_m(10, 60.0) < zoom_tolerance_m(10, 0.0)
    assert zoom_tolerance_m(10, 60.0) == pytest.approx(
        zoom_tolerance_m(10, 0.0) * math.cos(math.radians(60.0)), rel=1e-6)


# ── simplification ───────────────────────────────────────────────────────────

def test_endpoints_are_always_kept():
    # A simplified line must still start and end exactly where it did, or the
    # track visibly detaches from its neighbours.
    poly = _line(500, jitter=0.0002)
    out = simplify_lonlat(poly, 50.0)
    assert out[0] == poly[0]
    assert out[-1] == poly[-1]


def test_a_coarser_zoom_yields_fewer_points():
    poly = _line(2000, jitter=0.0002)
    far = _simplified([_feature(poly)], 8)
    near = _simplified([_feature(poly)], 16)
    n_far = len(far[0]["geometry"]["coordinates"])
    n_near = len(near[0]["geometry"]["coordinates"])
    assert n_far < n_near <= len(poly)


def test_whole_trip_zoom_collapses_a_dense_track():
    # The case that matters: at the zoom that shows a whole trip, a pixel is
    # hundreds of metres and almost nothing survives.
    poly = _line(5000, jitter=0.00005)
    out = _simplified([_feature(poly)], 6)
    assert len(out[0]["geometry"]["coordinates"]) < len(poly) // 10


def test_short_lines_are_returned_untouched():
    for n in (0, 1, 2):
        poly = _line(n)
        out = _simplified([_feature(poly)], 10)
        assert out[0]["geometry"]["coordinates"] == poly


def test_the_input_features_are_not_mutated():
    # They may be shared with a cache entry; mutating in place would poison it.
    poly = _line(2000, jitter=0.0002)
    feature = _feature(poly)
    _simplified([feature], 8)
    assert len(feature["geometry"]["coordinates"]) == 2000


def test_an_encoded_polyline_is_left_alone():
    # Re-encoding would cost a decode plus an encode per activity to save
    # bytes the caller did not ask to save; the encoded variant exists to
    # avoid exactly that work.
    feature = {
        "type": "Feature",
        "properties": {"activity_id": "1", "polyline": "abcdef"},
        "geometry": {"type": "LineString", "coordinates": []},
    }
    out = _simplified([feature], 6)
    assert out[0]["properties"]["polyline"] == "abcdef"
    assert out[0]["geometry"]["coordinates"] == []


def test_malformed_geometry_passes_through_rather_than_raising():
    for geom in ({}, {"coordinates": None}, {"coordinates": [["x", "y"], [1, 2], [3, 4]]}):
        out = _simplified([{"type": "Feature", "geometry": geom}], 10)
        assert len(out) == 1


def test_latitude_is_taken_per_feature():
    # A trip spanning many latitudes must be simplified correctly along its
    # whole length, not against one global scale factor.
    equator = [[0.0 + i * 0.0001, 0.0 + (0.0002 if i % 2 else 0.0)] for i in range(1000)]
    north = [[0.0 + i * 0.0001, 60.0 + (0.0002 if i % 2 else 0.0)] for i in range(1000)]
    out = _simplified([_feature(equator), _feature(north)], 12)
    # Same shape at different latitudes: the northern one keeps at least as
    # much detail, because a pixel there covers less ground.
    assert len(out[1]["geometry"]["coordinates"]) >= len(out[0]["geometry"]["coordinates"])


# ── The route (issue #295) ───────────────────────────────────────────────────

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

import models.db as db_module
from models.db import _configure_sqlite, _make_engine
from api.deps import get_current_user
from api.geo import _geo_cache, _geo_gen, _track_cache, router as geo_router
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo

_POINTS = 3000


@pytest.fixture
def env(monkeypatch, tmp_path):
    # A file, pooled the way the app pools it, because the build-lock tests
    # below send two requests at once. An in-memory StaticPool engine hands
    # both threads the same sqlite3 connection, and their interleaved queries
    # corrupt each other's rows — a flaky IndexError or a spurious 404 that has
    # nothing to do with the code under test.
    engine = _make_engine(f"sqlite:///{(tmp_path / 'geo.db').as_posix()}")
    _configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    _geo_cache.clear()
    _geo_gen.clear()
    _track_cache.clear()

    import polyline as polyline_lib

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        stranger = UserInfo(display_name="Nosy", email="nosy@e.com")
        sess.add(owner)
        sess.add(stranger)
        sess.commit()
        sess.refresh(owner)
        sess.refresh(stranger)
        uid, sid = owner.id, stranger.id

        project = DBProject(user_info_id=uid, name="Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)

        # A wobbling track, so simplification has something to remove.
        pts = [(45.0 + (0.0002 if i % 2 else 0.0), 7.0 + i * 0.0001)
               for i in range(_POINTS)]
        sess.add(DBActivity(
            id=111, user_info_id=uid, name="Ride", type="Ride",
            start_date="2026-06-01T00:00:00Z",
            summary_polyline=polyline_lib.encode(pts),
            start_latlng_json=json.dumps(list(pts[0])),
            end_latlng_json=json.dumps(list(pts[-1])),
        ))
        sess.add(DBProjectItem(
            project_id=project.id, position=0, item_type="activity", activity_id=111))
        sess.commit()

    current = {"uid": uid}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(geo_router)
    yield TestClient(app), uid, sid, lambda who: current.update(uid=who)
    engine.dispose()


def _points(resp) -> int:
    return len(resp.json()["features"][0]["geometry"]["coordinates"])


def test_route_returns_fewer_points_at_a_coarser_zoom(env):
    client, *_ = env
    far = client.get("/api/geo/project/simplified?name=Trip&zoom=8")
    near = client.get("/api/geo/project/simplified?name=Trip&zoom=17")
    assert far.status_code == near.status_code == 200
    assert _points(far) < _points(near) <= _POINTS


def test_route_saves_substantially_against_full_resolution(env):
    client, *_ = env
    full = client.get("/api/geo/project?name=Trip")
    coarse = client.get("/api/geo/project/simplified?name=Trip&zoom=8")
    assert _points(coarse) * 10 < _points(full), (
        "the whole point is holding a fraction of the geometry at trip zoom"
    )


def test_second_request_for_the_same_level_is_a_cache_hit(env):
    client, *_ = env
    assert client.get(
        "/api/geo/project/simplified?name=Trip&zoom=10").headers["x-cache"] == "MISS"
    assert client.get(
        "/api/geo/project/simplified?name=Trip&zoom=10").headers["x-cache"] == "HIT"


def test_zoom_is_rounded_up_so_a_level_is_never_under_detailed(env):
    # Flooring served 11.9 the zoom-11 tolerance — 1.87x too coarse, about two
    # pixels of visible drift. Ceiling shares a cache entry with 12, not 11.
    client, *_ = env
    client.get("/api/geo/project/simplified?name=Trip&zoom=12")
    hit = client.get("/api/geo/project/simplified?name=Trip&zoom=11.9")
    assert hit.headers["x-cache"] == "HIT"


def test_out_of_range_zoom_is_rejected(env):
    client, *_ = env
    for z in (-1, 23, 100):
        assert client.get(
            f"/api/geo/project/simplified?name=Trip&zoom={z}").status_code == 400


def test_unknown_project_is_404(env):
    client, *_ = env
    assert client.get(
        "/api/geo/project/simplified?name=NoSuchTrip&zoom=10").status_code == 404


def test_a_stranger_cannot_read_someone_elses_geometry(env):
    client, _uid, sid, act_as = env
    act_as(sid)
    assert client.get(
        "/api/geo/project/simplified?name=Trip&zoom=10").status_code in (403, 404)


def test_full_resolution_endpoint_is_unchanged(env):
    # It is an additional endpoint: shipped clients keep the geometry they
    # expect.
    client, *_ = env
    assert _points(client.get("/api/geo/project?name=Trip")) == _POINTS


# ── Cost and coarseness bounds (issue #295 regressions) ──────────────────────
#
# The first cut of this endpoint measured 21.6 s per request on a 219-activity
# trip and returned 515 coordinates for it — 2.4 per activity, a straight line
# per leg. Both are bounded now, and both bounds are asserted because either
# one silently drifting makes the endpoint worse than what it replaced.

from src.models.simplify import simplify_for_zoom


def _wiggly(n: int) -> list[list[float]]:
    lat, lon, out = 45.0, 7.0, []
    for i in range(n):
        lon += 0.00003 + math.sin(i / 50.0) * 0.00002
        lat += math.cos(i / 37.0) * 0.00002
        out.append([lon, lat])
    return out


def test_a_track_never_collapses_below_the_floor():
    # Whole-trip zoom, where a pixel is hundreds of metres.
    out = simplify_for_zoom(_wiggly(5000), 5)
    assert len(out) >= 32


def test_the_floor_is_at_least_what_the_client_would_render_anyway():
    # The client's own budget worked out at ~27 points per activity on the
    # trip that prompted this. A floor below that is a regression.
    assert simplify_for_zoom(_wiggly(5000), 5).__len__() >= 27


def test_the_floor_keeps_the_shape_rather_than_just_the_ends():
    out = simplify_for_zoom(_wiggly(5000), 4)
    # A strided sample of a winding path visits many distinct latitudes; two
    # endpoints would not.
    assert len({round(p[1], 4) for p in out}) > 5


def test_work_is_bounded_regardless_of_input_size():
    small = simplify_for_zoom(_wiggly(4000), 13)
    large = simplify_for_zoom(_wiggly(200000), 13)
    # Both run over at most max_input_points, so the larger input cannot
    # return a wildly larger result — that boundedness is the cost guarantee.
    assert len(large) <= 4000
    assert len(small) <= 4000


def test_zooming_in_still_yields_more_detail_than_the_floor():
    coarse = simplify_for_zoom(_wiggly(20000), 5)
    fine = simplify_for_zoom(_wiggly(20000), 15)
    assert len(fine) > len(coarse)


def test_endpoints_survive_the_floor_path():
    poly = _wiggly(5000)
    out = simplify_for_zoom(poly, 4)
    assert out[0] == poly[0]
    assert out[-1] == poly[-1]


# ── Viewport bounding box (issue #324) ────────────────────────────────
#
# Zoom bounds the *detail*, not the *extent*: at zoom 15 a deep zoom still
# returned the whole trip at that detail, and simplified all of it. Measured on
# a 219-activity trip, simplification alone cost 0.19 s at zoom 9, 2.94 s at
# zoom 12 and 6.98 s at zoom 15 — the cost rises with zoom while the saving
# falls. The box makes the deep-zoom case bounded in both directions.

from src.models.simplify import bboxes_intersect, line_bbox, snap_bbox_to_tiles


def _at(lon: float, lat: float, n: int = 400) -> list[list[float]]:
    """A wobbling line near (lon, lat), small enough to sit inside one viewport."""
    return [[lon + i * 0.00002, lat + (0.0002 if i % 2 else 0.0)] for i in range(n)]


def test_a_line_outside_the_box_falls_to_the_whole_trip_floor():
    # Not dropped, and not two points: exactly what a whole-trip zoom would
    # have produced for it. That equivalence is the safety argument — the
    # feature is no coarser than something the client already renders and
    # exports at another zoom.
    poly = _at(7.0, 45.0, 5000)
    off = simplify_for_zoom(poly, 15, bbox=(20.0, 20.0, 21.0, 21.0))
    assert len(off) == 32
    assert off[0] == poly[0] and off[-1] == poly[-1]
    assert off == simplify_for_zoom(poly, 3)


def test_a_line_inside_the_box_is_untouched_by_it():
    poly = _at(7.0, 45.0, 5000)
    assert (simplify_for_zoom(poly, 15, bbox=(6.0, 44.0, 8.0, 46.0))
            == simplify_for_zoom(poly, 15))


def test_a_line_merely_clipping_the_box_still_counts_as_visible():
    # Its bbox overlaps even though most of it is off screen; drawing only the
    # part inside would leave a visible gap at the edge of the viewport.
    poly = _at(7.0, 45.0, 5000)
    assert len(simplify_for_zoom(poly, 15, bbox=(7.05, 44.9, 9.0, 46.0))) > 32


def test_malformed_geometry_still_passes_through_with_a_box():
    # The intersection test compares coordinates, so bad data reaches it
    # first now. One bad point must still not 500 a whole project's map.
    for geom in ({}, {"coordinates": None},
                 {"coordinates": [["x", "y"], [1, 2], [3, 4]]},
                 {"coordinates": [[1], [2], [3]]}):
        out = _simplified(
            [{"type": "Feature", "geometry": geom}], 12, (6.0, 44.0, 8.0, 46.0))
        assert len(out) == 1


def test_a_line_entering_the_box_late_is_still_visible():
    # The intersection test short-circuits on the first point, because at
    # whole-trip zoom every line is on screen and the full walk would be pure
    # added cost (measured 0.24 s -> 0.34 s on a 1.47 M-point trip). A line
    # that starts outside and enters must not be lost to that shortcut.
    poly = [[7.0 + i * 0.001, 45.0 + (0.0002 if i % 2 else 0.0)]
            for i in range(2000)]
    kept = simplify_for_zoom(poly, 15, bbox=(8.0, 44.9, 8.5, 45.1))
    assert len(kept) > 32
    assert kept == simplify_for_zoom(poly, 15), "kept exactly as if unscoped"


def test_the_box_never_drops_a_feature():
    # geo is read as a description of the WHOLE trip by the segment-overlay
    # reconciliation (a tombstone is cleared when the server geo no longer
    # mentions its id), by fit-to-bounds, and by the export path. A missing
    # feature would silently break all three.
    features = [_feature(_at(7.0, 45.0)), _feature(_at(30.0, 60.0))]
    out = _simplified(features, 15, (6.0, 44.0, 8.0, 46.0))
    assert len(out) == 2
    assert all(len(f["geometry"]["coordinates"]) >= 2 for f in out)


def test_the_box_is_where_the_server_cpu_goes(monkeypatch):
    # The saving is structural, not incidental: an off-box line must not be
    # filtered at all — it is served at its precomputed floor.
    import api.geo as geo_mod

    calls = []
    real = geo_mod._PreparedLine.at_level
    monkeypatch.setattr(geo_mod._PreparedLine, "at_level",
                        lambda line, level: calls.append(level) or real(line, level))
    features = [_feature(_at(7.0, 45.0)) for _ in range(3)]
    features += [_feature(_at(30.0 + i, 60.0)) for i in range(20)]
    _simplified(features, 15, (6.0, 44.0, 8.0, 46.0))
    assert len(calls) == 3, "only the three visible lines are worth filtering"


# ── Snapping, which is what bounds the cache ────────────────────────────────

def test_snapping_is_outward_so_the_answer_is_a_superset():
    box = (7.3, 45.3, 7.4, 45.4)
    snapped, _ = snap_bbox_to_tiles(box, 12)
    assert snapped[0] <= box[0] and snapped[1] <= box[1]
    assert snapped[2] >= box[2] and snapped[3] >= box[3]


def test_neighbouring_viewports_share_one_key():
    # Raw viewport floats would mean a distinct cache entry per pan pixel.
    # This project has already OOM-killed its API container once with a payload
    # cache bounded by the wrong thing.
    a = snap_bbox_to_tiles((7.30, 45.30, 7.32, 45.32), 12)[1]
    b = snap_bbox_to_tiles((7.3001, 45.3001, 7.3201, 45.3201), 12)[1]
    assert a == b


def test_a_far_pan_does_get_its_own_key():
    a = snap_bbox_to_tiles((7.3, 45.3, 7.4, 45.4), 12)[1]
    b = snap_bbox_to_tiles((9.3, 45.3, 9.4, 45.4), 12)[1]
    assert a != b


def test_a_degenerate_box_still_covers_a_whole_tile():
    snapped, tiles = snap_bbox_to_tiles((7.0, 45.0, 7.0000001, 45.0000001), 12)
    assert tiles[2] > tiles[0] and tiles[3] > tiles[1]
    assert snapped[0] < snapped[2] and snapped[1] < snapped[3]


def test_snapping_clamps_to_the_world():
    snapped, tiles = snap_bbox_to_tiles((-180.0, -89.0, 180.0, 89.0), 3)
    assert tiles == (0, 0, 8, 8)
    assert snapped[0] == -180.0 and snapped[2] == 180.0


def test_line_bbox_tolerates_an_elevation_third_element():
    # GeoJSON positions legally carry one; `for x, y in poly` would raise.
    assert line_bbox([[1.0, 2.0, 300.0], [3.0, 4.0, 310.0]]) == (1.0, 2.0, 3.0, 4.0)


def test_touching_boxes_intersect():
    assert bboxes_intersect((0, 0, 1, 1), (1, 1, 2, 2))
    assert not bboxes_intersect((0, 0, 1, 1), (1.001, 1, 2, 2))


# ── The route ────────────────────────────────────────────────────────────────

def test_the_box_shrinks_the_payload_for_an_off_screen_trip(env):
    client, *_ = env
    whole = client.get("/api/geo/project/simplified?name=Trip&zoom=15")
    # A viewport on the other side of Europe: the trip is off screen entirely.
    scoped = client.get(
        "/api/geo/project/simplified?name=Trip&zoom=15&bbox=20,50,21,51")
    assert scoped.status_code == 200
    assert _points(scoped) < _points(whole)
    assert len(scoped.json()["features"]) == len(whole.json()["features"])


def test_the_box_keeps_full_detail_for_what_is_on_screen(env):
    client, *_ = env
    whole = client.get("/api/geo/project/simplified?name=Trip&zoom=15")
    on_screen = client.get(
        "/api/geo/project/simplified?name=Trip&zoom=15&bbox=6,44,9,46")
    assert _points(on_screen) == _points(whole)


def test_whole_trip_zoom_degenerates_to_the_trip_bounds(env):
    # At a zoom that shows the entire trip the box contains it, so nothing is
    # scoped away. This must fall out, not need a special mode.
    client, *_ = env
    scoped = client.get(
        "/api/geo/project/simplified?name=Trip&zoom=6&bbox=-20,30,40,60")
    plain = client.get("/api/geo/project/simplified?name=Trip&zoom=6")
    assert _points(scoped) == _points(plain)


def test_a_second_box_at_the_same_level_does_not_rebuild(env):
    """The regression this replaces (issue #324).

    With the box in the cache key, every pan to a new tile range rebuilt the
    level — and a rebuild decodes every activity polyline before it simplifies
    anything (1.83 s measured for a 219-activity trip), on a single-process
    server where that CPU-bound work also starves unrelated requests. The
    level is what costs; the box is a pass over the result.
    """
    client, *_ = env
    # zoom 17, where the on-box answer is far above the floor — at coarse
    # zooms both boxes land on it and the assertion below could not tell a
    # reused level from a rebuilt one.
    first = client.get("/api/geo/project/simplified?name=Trip&zoom=17&bbox=6,44,9,46")
    assert first.headers["x-cache"] == "MISS"
    elsewhere = client.get(
        "/api/geo/project/simplified?name=Trip&zoom=17&bbox=20,50,21,51")
    assert elsewhere.headers["x-cache"] == "HIT"
    # Reused the level, but still answered for *this* box: the track is
    # nowhere near it, so it comes back at the floor.
    assert _points(elsewhere) <= 32 < _points(first)


def test_a_bust_still_rebuilds_the_level(env):
    # The level cache holds Python objects rather than bytes, so it needs the
    # same generation guard the byte cache has, or an edit would leave a stale
    # map served for the whole 15-minute TTL.
    from api.geo import bust_geo_cache
    client, uid, *_ = env
    client.get("/api/geo/project/simplified?name=Trip&zoom=12")
    assert client.get(
        "/api/geo/project/simplified?name=Trip&zoom=12").headers["x-cache"] == "HIT"
    bust_geo_cache(uid, "Trip")
    assert client.get(
        "/api/geo/project/simplified?name=Trip&zoom=12").headers["x-cache"] == "MISS"


def test_a_tiny_pan_reuses_the_cached_entry(env):
    client, *_ = env
    client.get("/api/geo/project/simplified?name=Trip&zoom=12&bbox=7.30,45.30,7.32,45.32")
    hit = client.get(
        "/api/geo/project/simplified?name=Trip&zoom=12&bbox=7.3001,45.3001,7.3201,45.3201")
    assert hit.headers["x-cache"] == "HIT", (
        "the server snaps the box itself, so an unsnapped client cannot mint "
        "an entry per pan pixel"
    )


def test_a_boxed_response_does_not_poison_the_unboxed_one(env):
    client, *_ = env
    # zoom 17, where the unboxed answer is far above the floor, so "the boxed
    # entry was served instead" would be unmissable.
    client.get("/api/geo/project/simplified?name=Trip&zoom=17&bbox=20,50,21,51")
    plain = client.get("/api/geo/project/simplified?name=Trip&zoom=17")
    # A cache hit here is correct now — the cached level is box-independent and
    # the box is applied per request. What must hold is the *content*: the
    # unboxed caller gets the unboxed answer, not the floored one the previous
    # caller was served.
    assert _points(plain) > 32


def test_an_older_client_sending_no_box_is_unaffected(env):
    client, *_ = env
    before = _points(client.get("/api/geo/project/simplified?name=Trip&zoom=12"))
    client.get("/api/geo/project/simplified?name=Trip&zoom=12&bbox=20,50,21,51")
    assert _points(client.get("/api/geo/project/simplified?name=Trip&zoom=12")) == before


@pytest.mark.parametrize("bad", [
    "1,2,3",
    "1,2,3,4,5",
    "a,b,c,d",
    "9,44,6,46",       # inverted longitude
    "6,46,9,44",       # inverted latitude
    "-200,44,9,46",    # out of range
    "6,-95,9,46",
    "",
])
def test_a_malformed_box_is_rejected_rather_than_ignored(env, bad):
    # Falling through as "no box" would serve the whole trip while the client
    # believed it held viewport-scoped geometry.
    client, *_ = env
    assert client.get(
        f"/api/geo/project/simplified?name=Trip&zoom=12&bbox={bad}").status_code == 400


def test_the_box_does_not_change_the_auth_boundary(env):
    client, _uid, sid, act_as = env
    act_as(sid)
    assert client.get(
        "/api/geo/project/simplified?name=Trip&zoom=12&bbox=6,44,9,46"
    ).status_code in (403, 404)


# ── The track cache (issue #338) ─────────────────────────────────────────────
#
# The unit of caching is the line, not the level. These pin the parts X-Cache
# cannot see: that a trip too large to cache is refused and logged rather than
# silently rebuilt forever; that a bust drops the entry directly, which is what
# still holds when the shared Redis generation bump is lost; that eviction is
# least-recently-used; and, above all, that serving from the cached working
# set gives exactly what simplifying from the original gave, since that
# equivalence is the whole safety argument for keeping the working set instead
# of the track.

from array import array

from api.geo import (
    _track_cache_bytes,
    _LIST_COORD_BYTES,
    _TRACK_CACHE_MAX_BYTES,
    _TRACK_CACHE_MAX_ENTRY_BYTES,
    _evict_tracks,
    _geo_cache_lock,
    _track_cache,
    _track_cache_bytes,
    _track_cache_get,
    _track_cache_store,
)
from src.models.simplify import (
    MAX_INPUT_POINTS,
    floor_line,
    restrict_to_bbox,
    simplify_for_zoom,
    working_set,
)


def _feat(n: int, lon: float = 7.0) -> dict:
    return _feature([[lon + i * 0.0001, 45.0] for i in range(n)])


def _track(n_lines: int = 1, n_points: int = 100) -> "object":
    return _prepare_track([_feat(n_points, lon=7.0 + i) for i in range(n_lines)])


def _activity_track(n_lines: int, n_points: int) -> "object":
    """*n_lines* activity lines as the serving path holds them — unpacked
    from a prepared row, 9 bytes per coordinate (issue #369). One blob,
    shared: what is being sized here is the cache, not the packing."""
    from api.geo import _line_from_blob, _PreparedTrack
    from src.models.prepared_geo import pack_prepared_line
    from src.models.simplify import line_bbox, vertex_levels
    work = [[7.0 + i * 0.0001, 45.0] for i in range(n_points)]
    blob = pack_prepared_line(work, vertex_levels(work), line_bbox(work))
    return _PreparedTrack(
        [_line_from_blob({"activity_id": i}, blob) for i in range(n_lines)])


# ── the equivalence the design rests on ──────────────────────────────────────

def test_simplifying_from_the_working_set_is_identical_at_every_zoom():
    # The cache holds the working set instead of the decoded track, so this
    # has to be an identity and not an approximation — otherwise a cached trip
    # renders differently from a cold one.
    poly = [[7.0 + i * 0.0001, 45.0 + (0.0002 if i % 2 else 0.0)]
            for i in range(20000)]
    work = working_set(poly)
    assert len(work) == MAX_INPUT_POINTS
    for zoom in (4, 8, 12, 15, 18, 22):
        assert simplify_for_zoom(work, zoom) == simplify_for_zoom(poly, zoom), zoom


def test_the_working_set_is_held_as_a_typed_array():
    # 16 bytes per coordinate against 128 for a [lon, lat] list. This is what
    # makes caching a decoded trip affordable at all, so it is asserted rather
    # than left to the comment.
    track = _track(n_lines=3, n_points=1000)
    assert all(isinstance(line.points, array) for line in track.lines)
    assert track.nbytes < 3 * 1000 * _LIST_COORD_BYTES // 4


def test_a_position_carrying_elevation_keeps_it():
    # A GeoJSON position legally has a third element and simplification
    # preserves it; packing to pairs would silently drop it.
    poly = [[7.0 + i * 0.001, 45.0, 300.0 + i] for i in range(200)]
    track = _prepare_track([_feature(poly)])
    assert not isinstance(track.lines[0].points, array)
    served = _features_for(track, 18, None)
    assert all(len(p) == 3 for p in served[0]["geometry"]["coordinates"])


def test_the_floor_keeps_the_shape_and_both_ends():
    poly = [[7.0 + i * 0.001, 45.0] for i in range(200)]
    track = _prepare_track([_feature(poly)])
    floor = track.lines[0].floor
    assert len(floor) == 32
    assert floor[0] == poly[0] and floor[-1] == poly[-1]


def test_an_off_box_line_is_served_at_the_floor_and_never_dropped():
    far = _feature([[30.0 + i * 0.001, 60.0] for i in range(2000)])
    near = _feature([[7.0 + i * 0.001, 45.0] for i in range(2000)])
    served = _features_for(_prepare_track([far, near]), 17,
                           (6.0, 44.0, 9.0, 46.0))
    assert len(served) == 2
    off = served[0]["geometry"]["coordinates"]
    assert len(off) == 32
    assert off[0] == far["geometry"]["coordinates"][0]
    assert off[-1] == far["geometry"]["coordinates"][-1]


def test_a_zoom_sweep_decodes_the_trip_only_once(env, monkeypatch):
    """The decode is a fixed 1.83 s floor for a 219-activity trip, paid on
    every cold build before anything is simplified. #331 paid it once per zoom
    level — six levels in one session measured 37.6 s on device. Holding the
    working set means the sweep pays it once."""
    import api.geo as geo_mod

    calls = []
    real = geo_mod.polyline_lib.decode
    monkeypatch.setattr(geo_mod.polyline_lib, "decode",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    client, *_ = env
    for zoom in (9, 11, 13, 15, 17):
        resp = client.get(
            f"/api/geo/project/simplified?name=Trip&zoom={zoom}&bbox=6,44,9,46")
        assert resp.status_code == 200
    assert len(calls) == 1, "one decode for the whole sweep, not one per level"


# ── the cached objects are handed to every request, so nothing may mutate ────

def test_serving_never_mutates_the_prepared_track():
    track = _track(n_lines=2, n_points=400)
    before = [list(line.points) for line in track.lines]
    floors = [list(line.floor) for line in track.lines]
    _features_for(track, 17, None)
    _features_for(track, 9, (20.0, 50.0, 21.0, 51.0))
    assert [list(line.points) for line in track.lines] == before
    assert [list(line.floor) for line in track.lines] == floors


def test_two_requests_at_the_same_level_get_the_same_geometry():
    track = _track(n_lines=2, n_points=400)
    a = _features_for(track, 14, None)
    b = _features_for(track, 14, None)
    assert [f["geometry"]["coordinates"] for f in a] == \
           [f["geometry"]["coordinates"] for f in b]


# ── bounds ───────────────────────────────────────────────────────────────────

def test_a_trip_too_large_to_cache_is_not_stored():
    _track_cache.clear()
    per_line = _activity_track(1, MAX_INPUT_POINTS).nbytes
    n_lines = _TRACK_CACHE_MAX_ENTRY_BYTES // per_line + 2
    track = _activity_track(n_lines, MAX_INPUT_POINTS)
    _track_cache_store((1, "Big"), track, 0)
    assert _track_cache_get((1, "Big")) is None, (
        "storing it would let one entry evict everything else and still not fit"
    )


def test_a_trip_within_the_entry_cap_is_stored():
    _track_cache.clear()
    _track_cache_store((1, "Ok"), _track(), 0)
    assert _track_cache_get((1, "Ok")) is not None


def test_a_long_trip_is_still_cached_rather_than_rebuilt_every_request():
    """The regression the entry cap used to cause.

    ``base_bytes`` is a property of the *trip*, not of the level being asked
    for — so unlike the level cache's per-level refusal, this one does not go
    away at shallow zoom. A trip past the cap was uncacheable at EVERY zoom,
    and every request then repeated the DB load and the polyline decode:
    seconds of CPU-bound Python holding the GIL on a single-process uvicorn,
    which is the mechanism behind real 502s on /meta and /low-res.

    800 activities at the working-set cap is ~31 MB, right under the budget
    — a long trip at a few activities a day, not a pathological one. It must
    be cached.
    """
    _track_cache.clear()
    track = _activity_track(800, MAX_INPUT_POINTS)
    assert track.nbytes > _TRACK_CACHE_MAX_BYTES * 9 // 10, "fixture must be near the cap"
    _track_cache_store((1, "LongTrip"), track, 0)
    assert _track_cache_get((1, "LongTrip")) is not None, (
        "refusing to cache costs a rebuild on every request, forever; evicting "
        "another trip costs one rebuild"
    )


def test_a_large_trip_evicts_others_rather_than_being_refused():
    # The corollary: the only trip refused is one that could not be held even
    # alone. A trip big enough to push the cache past its budget is stored, and
    # the room comes from evicting older entries — one rebuild for them, rather
    # than a rebuild on every request for it.
    _track_cache.clear()
    _track_cache_store((1, "Small"), _track(), 0)
    big = _activity_track(700, MAX_INPUT_POINTS)
    assert big.nbytes > _TRACK_CACHE_MAX_BYTES // 2
    _track_cache_store((2, "Big"), big, 0)
    assert _track_cache_get((2, "Big")) is not None, 'stored, not refused'
    assert _track_cache_bytes() <= _TRACK_CACHE_MAX_BYTES, 'budget still holds'



def test_a_bust_drops_the_entry_rather_than_only_marking_it_stale():
    # Not the same as the route-level bust test: that one passes on the
    # generation guard alone. This pins the *direct* drop, which is what still
    # holds if the shared generation bump is lost.
    from api.geo import bust_geo_cache
    _track_cache.clear()
    _track_cache_store((7, "Trip"), _track(), 0)
    assert (7, "Trip") in _track_cache
    bust_geo_cache(7, "Trip")
    assert (7, "Trip") not in _track_cache


def test_reading_a_track_refreshes_its_deadline():
    # Eviction picks the entry closest to expiry, and every entry shares one
    # TTL — so without this, "closest to expiry" means "stored first" and the
    # trip being looked at right now is evicted before a cold one.
    _track_cache.clear()
    _track_cache_store((1, "Old"), _track(), 0)
    _track_cache_store((1, "New"), _track(), 0)
    before = _track_cache[(1, "Old")][1]
    _track_cache_get((1, "Old"))
    assert _track_cache[(1, "Old")][1] > before
    assert _track_cache[(1, "Old")][1] > _track_cache[(1, "New")][1]


def test_the_cache_evicts_down_to_its_byte_budget():
    _track_cache.clear()
    for i in range(4):
        _track_cache_store((1, f"Trip{i}"), _track(n_lines=2, n_points=500), 0)
    with _geo_cache_lock:
        held = _track_cache_bytes()
        monkeypatched = held // 2
        # Shrink the budget rather than build a 48 MB fixture.
        import api.geo as geo_mod
        original = geo_mod._TRACK_CACHE_MAX_BYTES
        geo_mod._TRACK_CACHE_MAX_BYTES = monkeypatched
        try:
            _evict_tracks((1, "Trip3"))
            assert _track_cache_bytes() <= monkeypatched
            assert (1, "Trip3") in _track_cache, "the newest entry survives"
        finally:
            geo_mod._TRACK_CACHE_MAX_BYTES = original


def test_the_accounting_counts_a_shared_floor_once():
    # A line short enough that striding returns its argument holds the same
    # list as both its working set and its floor. Counting it twice would make
    # the budget claim wrong in the safe direction, but wrong.
    track = _prepare_track([_feat(5)])
    line = track.lines[0]
    assert line.floor is line.points
    assert track.nbytes == 5 * _LIST_COORD_BYTES + len(line.levels)


# ── restrict_to_bbox and floor_line ─────────────────────────────────────────
#
# restrict_to_bbox is no longer on the request path — the box is applied by
# choosing which lines to simplify, not by reducing an already-simplified
# level — but it is still the library statement of what flooring means, and
# these pin the floor the prepared track precomputes.

def test_a_visible_line_is_returned_unchanged_and_unaliased():
    poly = [[7.0 + i * 0.001, 45.0] for i in range(200)]
    assert restrict_to_bbox(poly, (6.0, 44.0, 9.0, 46.0)) is poly


def test_an_off_box_line_is_floored_keeping_both_ends():
    poly = [[7.0 + i * 0.001, 45.0] for i in range(200)]
    out = restrict_to_bbox(poly, (20.0, 50.0, 21.0, 51.0))
    assert len(out) == 32
    assert out[0] == poly[0] and out[-1] == poly[-1]


def test_a_line_already_at_the_floor_is_left_alone():
    poly = [[7.0 + i * 0.001, 45.0] for i in range(20)]
    assert restrict_to_bbox(poly, (20.0, 50.0, 21.0, 51.0)) is poly


def test_the_precomputed_floor_matches_what_a_box_would_have_given():
    # The prepared track floors the working set once, up front, instead of
    # flooring a simplified level per box. Same count, same endpoints — the
    # interior differs only for geometry that is off screen by definition.
    poly = [[7.0 + i * 0.0001, 45.0 + (0.0002 if i % 2 else 0.0)]
            for i in range(3000)]
    far = (20.0, 50.0, 21.0, 51.0)
    in_one_pass = simplify_for_zoom(poly, 14, bbox=far)
    precomputed = floor_line(working_set(poly))
    assert len(in_one_pass) == len(precomputed)
    assert in_one_pass[0] == precomputed[0]
    assert in_one_pass[-1] == precomputed[-1]


# ── one preparation per trip, however many ask at once (issue #369 stage C) ──

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import api.geo as geo_mod
from api.geo import _track_build_locks


def test_two_cold_requests_for_one_trip_prepare_it_once(env, monkeypatch):
    """The loser of a race waits for the winner's entry instead of rebuilding.

    Both requests used to build the whole trip — measured 3,474 ms each for one
    answer, two decodes contending for one GIL. What makes this testable is the
    build being slow: the second request has to arrive while the first is still
    inside it, so the fixture holds the build open until both are in flight.
    """
    client, uid, _sid, _as_user = env
    _track_build_locks.clear()

    calls = []
    both_in_flight = threading.Barrier(2, timeout=10)
    real_prepared_lines = geo_mod._prepared_lines

    def counting_prepared_lines(project):
        calls.append(project.name)
        # Only the winner reaches here. Hold it open long enough that the loser
        # is provably queued on the lock rather than merely late.
        time.sleep(0.5)
        return real_prepared_lines(project)

    monkeypatch.setattr(geo_mod, "_prepared_lines", counting_prepared_lines)

    def fetch():
        both_in_flight.wait()
        return client.get("/api/geo/project/simplified?name=Trip&zoom=12")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(fetch), pool.submit(fetch)]]

    assert [r.status_code for r in responses] == [200, 200]
    assert len(calls) == 1, f"trip prepared {len(calls)} times, expected once"
    # Both get the real payload, not an empty one — the waiter reads the entry
    # the winner stored.
    for response in responses:
        features = response.json()["features"]
        assert sum(len(f["geometry"]["coordinates"]) for f in features) > 0


def test_a_waiter_still_reports_a_miss(env, monkeypatch):
    """X-Cache describes the state the request arrived in, not who built it.

    A waiter that got its answer from the winner's entry was still a request
    for an unprepared trip, and reporting HIT would make the header useless for
    spotting cold trips in a log.
    """
    client, uid, _sid, _as_user = env
    _track_build_locks.clear()

    barrier = threading.Barrier(2, timeout=10)
    real_prepared_lines = geo_mod._prepared_lines

    def slow_prepared_lines(project):
        time.sleep(0.5)
        return real_prepared_lines(project)

    monkeypatch.setattr(geo_mod, "_prepared_lines", slow_prepared_lines)

    def fetch():
        barrier.wait()
        return client.get("/api/geo/project/simplified?name=Trip&zoom=12")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(fetch), pool.submit(fetch)]]

    assert {r.headers["X-Cache"] for r in responses} == {"MISS"}


def test_the_lock_registry_does_not_grow_without_bound(env):
    """Unlocked entries are dropped once the dict gets large.

    A lock per project, kept forever, is the same unbounded-growth shape that
    OOM-killed this container on ordinary traffic (#209).
    """
    _track_build_locks.clear()
    for i in range(geo_mod._TRACK_BUILD_LOCKS_MAX + 50):
        geo_mod._track_build_lock((1, f"Trip {i}"))
    assert len(_track_build_locks) <= geo_mod._TRACK_BUILD_LOCKS_MAX


def test_a_held_lock_is_never_pruned(env):
    """Pruning must not drop the entry a build is currently using."""
    _track_build_locks.clear()
    held_key = (1, "In flight")
    held = geo_mod._track_build_lock(held_key)
    held.acquire()
    try:
        for i in range(geo_mod._TRACK_BUILD_LOCKS_MAX + 50):
            geo_mod._track_build_lock((1, f"Trip {i}"))
        assert _track_build_locks.get(held_key) is held
    finally:
        held.release()

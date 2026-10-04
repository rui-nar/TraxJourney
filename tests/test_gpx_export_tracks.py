"""A GPX export holds one track per activity, and reads back as those activities (#367).

The export used to write one ``<trk>`` of segments, one per activity and one
per connecting segment. The importer joins a track's segments into one
candidate, so exporting a trip and importing it back gave one activity
covering the whole trip and the gaps in between. Only the first point carried
a time, and that was the local wall clock written as UTC.

Now each activity is its own ``<trk>``, named and typed as stored, timed from
its true UTC start to its end on every point, and carrying its stored moving
time and distance. Each connecting segment is its own ``<trk>`` too, typed so
an import can tell it apart.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import gpxpy
import polyline
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.project_shared as project_shared_mod
import api.project_transfer as project_transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity
from models.user import UserInfo
from src.gpx.export_format import (
    CONNECTION_TRACK_TYPE, EXTENSION_NAMESPACE, activity_extensions,
    read_activity_extensions, spread_times,
)
from src.gpx.importer import candidates, map_activity_type, parse_gpx_bytes
from src.models.great_circle import haversine_km
from src.project.project_io import ProjectIO

_GPX_NS = "{http://www.topografix.com/GPX/1/1}"


@pytest.fixture
def client(monkeypatch, tmp_path):
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
        sess.add(user)
        sess.commit()
        uid = user.id
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app, raise_server_exceptions=False)
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _line(lat, lon, count, step=0.002):
    return [(round(lat + i * step, 5), round(lon + i * step, 5)) for i in range(count)]


def _length_m(track):
    return sum(haversine_km(a[0], a[1], b[0], b[1]) * 1000.0
               for a, b in zip(track, track[1:]))


def _activity(aid, name, kind, track, start, elapsed, moving, distance,
              local=None, zone="(GMT+00:00) UTC", source=None):
    return {
        "id": aid, "name": name, "type": kind, "distance": distance,
        "moving_time": moving, "elapsed_time": elapsed,
        "start_date": start, "start_date_local": local or start,
        "timezone": zone,
        "start_latlng": list(track[0]), "end_latlng": list(track[-1]),
        "map": {"summary_polyline": polyline.encode(track)},
        "source": source,
    }


#: A Strava run in Lisbon. Its polyline is simplified: 6 points standing for
#: a recording whose stored distance is longer than the line through them.
_RUN_TRACK = _line(38.70, -9.14, 6)
_RUN = _activity(
    101, "Morning run", "Run", _RUN_TRACK, "2024-06-01T07:00:00Z",
    elapsed=3600, moving=3000, distance=round(_length_m(_RUN_TRACK) * 1.3, 1),
    local="2024-06-01T08:00:00Z", zone="(GMT+00:00) Europe/Lisbon")

#: A GPX import with a known instant: Tokyo, 09:00 local.
_HIKE_TRACK = _line(35.68, 139.76, 30, step=0.001)
_HIKE = _activity(
    -102, "Temple hike", "hike", _HIKE_TRACK, "2024-06-02T00:00:00Z",
    elapsed=7200, moving=6000, distance=round(_length_m(_HIKE_TRACK), 1),
    local="2024-06-02T09:00:00Z", zone="Asia/Tokyo", source="gpx")

#: A Strava activity of a type beyond run, ride, hike and walk.
_KAYAK_TRACK = _line(35.30, 139.50, 12)
_KAYAK = _activity(
    103, "Bay paddle", "Kayaking", _KAYAK_TRACK, "2024-06-03T01:30:00Z",
    elapsed=5400, moving=5000, distance=4321.5,
    local="2024-06-03T10:30:00Z", zone="(GMT+09:00) Asia/Tokyo")


def _segment(sid, label, start, end, date):
    return {"item_type": "segment", "segment": {
        "id": sid, "segment_type": "train", "label": label, "date": date,
        "route_mode": "great_circle",
        "start": {"lat": start[0], "lon": start[1], "source": "manual"},
        "end": {"lat": end[0], "lon": end[1], "source": "manual"}}}


def _trip_file(activities, segments=()):
    items = []
    for index, act in enumerate(activities):
        items.append({"item_type": "activity", "activity_id": act["id"]})
        if index < len(segments):
            items.append(segments[index])
    items.append({"item_type": "memory", "memory": {
        "name": "Lac Blanc", "date": "2024-06-01", "time": "12:30",
        "description": "Lunch by the lake", "photos": [],
        "geo_mode": "custom", "lat": 45.98, "lon": 6.89}})
    return json.dumps({
        "version": 1, "name": "x", "items": items, "activities": list(activities),
        "day_meta": {"2024-06-01": {"difficulty": "hard", "sleeping": "Hut",
                                    "weather": "sun", "journal": "Big day"}},
    }).encode("utf-8")


def _import(client, name, content):
    r = client.post("/api/projects/import", files={
        "file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})
    assert r.status_code == 201, r.text


def _export(client, name) -> bytes:
    r = client.get(f"/api/projects/{name}/export")
    assert r.status_code == 200, r.text
    return r.content


_SEGMENTS = (
    _segment("seg-1", "Lisbon - Tokyo", _RUN_TRACK[-1], _HIKE_TRACK[0], "2024-06-01"),
    _segment("seg-2", "Tokyo - Kamakura", _HIKE_TRACK[-1], _KAYAK_TRACK[0], "2024-06-02"),
)


@pytest.fixture
def exported(client):
    _import(client, "Journey", _trip_file([_RUN, _HIKE, _KAYAK], _SEGMENTS))
    return _export(client, "Journey")


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ── Tracks ────────────────────────────────────────────────────────────────────

def test_three_activities_and_two_segments_export_five_tracks(exported):
    gpx = gpxpy.parse(exported)

    assert len(gpx.tracks) == 5
    assert [len(t.segments) for t in gpx.tracks] == [1] * 5
    assert [t.type for t in gpx.tracks] == [
        "Run", CONNECTION_TRACK_TYPE, "hike", CONNECTION_TRACK_TYPE, "Kayaking"]
    # The file's identity is unchanged.
    assert gpx.name == "Journey"


def test_the_activity_tracks_read_back_as_the_activities(exported):
    found = [c for c in candidates(parse_gpx_bytes(exported))
             if c.activity_type is not None]

    assert len(found) == 3
    for candidate, act in zip(found, (_RUN, _HIKE, _KAYAK)):
        start = _utc(act["start_date"])
        assert candidate.name == act["name"]
        assert candidate.activity_type == map_activity_type(act["type"])
        # The true UTC instant, not the local wall clock written as UTC.
        assert candidate.started_at == start
        assert candidate.ended_at == start + timedelta(seconds=act["elapsed_time"])
        assert candidate.carried_moving_seconds == act["moving_time"]
        assert candidate.carried_distance_m == act["distance"]
    # Run and hike come back in the app's own spelling of the type.
    assert [c.activity_type for c in found] == ["run", "hike", "Kayaking"]


def test_a_simplified_strava_polyline_carries_its_stored_distance(exported):
    run = candidates(parse_gpx_bytes(exported))[0]

    # The line through the simplified polyline falls short of the recording;
    # the carried value is the stored one, exactly.
    assert run.distance_m < _RUN["distance"] * 0.8
    assert run.carried_distance_m == _RUN["distance"]
    assert run.carried_moving_seconds == 3000


def test_every_activity_point_is_timed_and_times_increase(exported):
    gpx = gpxpy.parse(exported)
    activity_tracks = [t for t in gpx.tracks if t.type != CONNECTION_TRACK_TYPE]

    for track in activity_tracks:
        times = [p.time for p in track.segments[0].points]
        assert all(t is not None for t in times)
        assert all(a < b for a, b in zip(times, times[1:])), track.name
        assert all(t.utcoffset() == timedelta(0) for t in times)


def test_times_are_spread_by_distance():
    track = [(0.0, 0.0), (0.0, 0.001), (0.0, 0.004)]   # 1 step, then 3
    start = datetime(2024, 6, 1, 7, 0, tzinfo=timezone.utc)

    times = spread_times(track, start, 400)

    assert times == [start, start + timedelta(seconds=100),
                     start + timedelta(seconds=400)]


def test_a_track_that_does_not_move_is_spread_by_point():
    start = datetime(2024, 6, 1, 7, 0, tzinfo=timezone.utc)

    times = spread_times([(1.0, 1.0)] * 3, start, 60)

    assert times == [start, start + timedelta(seconds=30),
                     start + timedelta(seconds=60)]


def test_connection_tracks_are_typed_and_untimed(exported):
    gpx = gpxpy.parse(exported)
    connections = [t for t in gpx.tracks if t.type == CONNECTION_TRACK_TYPE]

    assert [t.name for t in connections] == ["Lisbon - Tokyo", "Tokyo - Kamakura"]
    for track in connections:
        points = track.segments[0].points
        assert len(points) == project_transfer_mod._SEGMENT_GPX_POINTS
        assert all(p.time is None for p in points)
        assert not track.extensions


# ── Types ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["Kayaking", "Swim", "AlpineSki"])
def test_strava_types_read_back_as_themselves(client, kind):
    act = dict(_KAYAK, type=kind)
    _import(client, "Typed", _trip_file([act]))

    (candidate,) = candidates(parse_gpx_bytes(_export(client, "Typed")))

    assert candidate.activity_type == kind


@pytest.mark.parametrize("stored,expected", [
    ("Run", "run"), ("Ride", "ride"), ("Hike", "hike"), ("Walk", "walk"),
    ("run", "run"), ("ride", "ride"), ("hike", "hike"), ("walk", "walk"),
    ("Workout", "Workout"), ("NordicSki", "NordicSki"),
    ("StandUpPaddling", "StandUpPaddling"), ("TrailRun", "TrailRun"),
    ("MountainBikeRide", "MountainBikeRide"),
    # Already collapsed into their family before #367, and still are.
    ("VirtualRide", "ride"), ("EBikeRide", "ride"), ("VirtualRun", "run"),
])
def test_every_stored_type_reads_back_in_its_family(stored, expected):
    assert map_activity_type(stored) == expected


# ── Rows whose instant is unknown ─────────────────────────────────────────────

def test_a_gpx_row_labelled_utc_exports_without_times(client):
    legacy = dict(_HIKE, id=-201, name="Old import", timezone="UTC")
    _import(client, "Legacy", _trip_file([legacy, _KAYAK]))

    content = _export(client, "Legacy")

    root = ET.fromstring(content)
    first, second = root.findall(f"{_GPX_NS}trk")
    assert first.findtext(f"{_GPX_NS}name") == "Old import"
    assert first.find(f".//{_GPX_NS}time") is None
    # Only that row: the other activity keeps its times.
    assert second.find(f".//{_GPX_NS}time") is not None
    old, kayak = candidates(parse_gpx_bytes(content))
    assert not old.has_times
    assert old.started_at is None and old.time_span is None
    assert kayak.started_at == _utc(_KAYAK["start_date"])


def test_a_strava_row_is_never_taken_for_an_unknown_instant(client):
    strava = dict(_KAYAK, timezone="UTC")
    _import(client, "Strava", _trip_file([strava]))

    (candidate,) = candidates(parse_gpx_bytes(_export(client, "Strava")))

    assert candidate.started_at == _utc(_KAYAK["start_date"])


# ── Unchanged ─────────────────────────────────────────────────────────────────

def test_waypoints_are_unchanged(exported):
    gpx = gpxpy.parse(exported)

    memory, day = gpx.waypoints
    assert (memory.name, memory.description) == ("Lac Blanc", "Lunch by the lake")
    assert (memory.latitude, memory.longitude) == (45.98, 6.89)
    assert memory.time == datetime(2024, 6, 1, 12, 30)
    assert day.name == "Day meta 2024-06-01"
    assert day.description == ("Difficulty: hard | Sleeping: Hut | Weather: sun"
                               " | Journal: Big day")
    assert json.loads(day.comment) == {
        "date": "2024-06-01", "difficulty": "hard", "sleeping": "Hut",
        "weather": "sun", "journal": "Big day"}
    # Placed at the first activity of that local day, as before.
    assert (day.latitude, day.longitude) == _RUN_TRACK[0]


def test_an_activity_without_geometry_is_left_out(client):
    bare = dict(_KAYAK, id=104, name="No line", start_latlng=None,
                end_latlng=None, map={"summary_polyline": None})
    _import(client, "Bare", _trip_file([bare, _RUN]))

    gpx = gpxpy.parse(_export(client, "Bare"))

    assert [t.name for t in gpx.tracks] == ["Morning run"]


def test_an_encrypted_activity_still_refuses_the_export(client):
    _import(client, "Secret", _trip_file([_RUN, _KAYAK]))
    with Session(db_module.engine) as sess:
        row = sess.exec(select(DBActivity).where(DBActivity.id == 103)).one()
        row.summary_polyline = "v1.d2Vr.Y2lwaGVy"
        sess.add(row)
        sess.commit()

    r = client.get("/api/projects/Secret/export")

    assert r.status_code == 409
    assert r.json()["detail"] == \
        "Cannot export GPX for a project containing an encrypted activity"


# ── The extension ─────────────────────────────────────────────────────────────

def test_the_extension_is_in_its_own_namespace(exported):
    root = ET.fromstring(exported)
    extensions = root.find(f"{_GPX_NS}trk/{_GPX_NS}extensions")

    # The Strava run also carries its original identity (Q1).
    assert [e.tag for e in extensions] == [
        f"{{{EXTENSION_NAMESPACE}}}moving_time",
        f"{{{EXTENSION_NAMESPACE}}}distance",
        f"{{{EXTENSION_NAMESPACE}}}source",
        f"{{{EXTENSION_NAMESPACE}}}source_id"]


@pytest.mark.parametrize("moving,distance", [
    ("-5", "-1"), ("nan", "inf"), ("lots", ""), ("1e12", "1e12"),
])
def test_carried_values_out_of_bounds_count_as_absent(moving, distance):
    elements = activity_extensions(1, 1.0)
    elements[0].text, elements[1].text = moving, distance

    assert read_activity_extensions(elements) == (None, None)


def test_missing_figures_are_not_carried():
    assert activity_extensions(None, math.nan) == []
    assert read_activity_extensions([]) == (None, None)


def test_a_third_party_track_carries_nothing():
    content = (
        b'<?xml version="1.0"?><gpx version="1.1" creator="x" '
        b'xmlns="http://www.topografix.com/GPX/1/1"><trk><name>A</name>'
        b'<extensions><other xmlns="urn:x">7</other></extensions><trkseg>'
        b'<trkpt lat="1" lon="1"/><trkpt lat="1.1" lon="1.1"/></trkseg>'
        b'<trkseg><trkpt lat="1.2" lon="1.2"/></trkseg></trk></gpx>')

    (candidate,) = candidates(parse_gpx_bytes(content))

    assert candidate.carried_moving_seconds is None
    assert candidate.carried_distance_m is None
    # Several segments of one track are still one candidate.
    assert candidate.point_count == 3

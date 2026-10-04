"""Every track of a GPX file imports as its own activity, in one go (#367).

A TraxJourney GPX export writes one track per activity and one per connecting
segment. ``POST /{name}/activities/import-gpx-tracks`` reads it back: each
activity track becomes an activity, with its name, type, times and zone from
the file, and its distance and moving time from the values the export
carried. Connection tracks are left out, tracks the trip already holds are
skipped and listed, and the rest is written all or nothing.

Also here: ``inspect`` reports the carried figures and the connection mark,
and keeps its ``activity_type`` in the types installed clients offer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

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
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.gpx.importer import map_activity_type
from src.models.great_circle import haversine_km
from src.project.project_io import ProjectIO


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app (for its 402 handler) on an in-memory DB, as user 1, who
    owns the empty trips "Back" and "Single"."""
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, project_transfer_mod, storage_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_TRIP_DAYS", "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        sess.add(UserInfo(id=1, display_name="A", email="a@e.com"))
        sess.add(DBProject(user_info_id=1, name="Back"))
        sess.add(DBProject(user_info_id=1, name="Single"))
        sess.commit()
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": "1"}
    try:
        yield TestClient(router.app, raise_server_exceptions=False), engine
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


# ── A trip to export (as in tests/test_gpx_export_tracks.py) ──────────────────

def _line(lat, lon, count, step=0.002):
    return [(round(lat + i * step, 5), round(lon + i * step, 5)) for i in range(count)]


def _length_m(track):
    return sum(haversine_km(a[0], a[1], b[0], b[1]) * 1000.0
               for a, b in zip(track, track[1:]))


def _activity(aid, name, kind, track, start, elapsed, moving, distance,
              local, zone, source=None):
    return {
        "id": aid, "name": name, "type": kind, "distance": distance,
        "moving_time": moving, "elapsed_time": elapsed,
        "start_date": start, "start_date_local": local, "timezone": zone,
        "start_latlng": list(track[0]), "end_latlng": list(track[-1]),
        "map": {"summary_polyline": polyline.encode(track)},
        "source": source,
    }


#: A Strava run in Lisbon whose polyline is simplified: its stored distance is
#: longer than the line through its 6 points.
_RUN_TRACK = _line(38.70, -9.14, 6)
_RUN = _activity(
    101, "Morning run", "Run", _RUN_TRACK, "2024-06-01T07:00:00Z",
    elapsed=3600, moving=3000, distance=round(_length_m(_RUN_TRACK) * 1.3, 1),
    local="2024-06-01T08:00:00Z", zone="(GMT+00:00) Europe/Lisbon")

#: A GPX import in Tokyo, 09:00 local.
_HIKE_TRACK = _line(35.68, 139.76, 30, step=0.001)
_HIKE = _activity(
    -102, "Temple hike", "hike", _HIKE_TRACK, "2024-06-02T00:00:00Z",
    elapsed=7200, moving=6000, distance=round(_length_m(_HIKE_TRACK), 1),
    local="2024-06-02T09:00:00Z", zone="Asia/Tokyo", source="gpx")

#: A Strava activity of a type installed clients do not offer.
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


_SEGMENTS = (
    _segment("seg-1", "Lisbon - Tokyo", _RUN_TRACK[-1], _HIKE_TRACK[0], "2024-06-01"),
    _segment("seg-2", "Tokyo - Kamakura", _HIKE_TRACK[-1], _KAYAK_TRACK[0], "2024-06-02"),
)


def _trip_file(activities, segments=()):
    items = []
    for index, act in enumerate(activities):
        items.append({"item_type": "activity", "activity_id": act["id"]})
        if index < len(segments):
            items.append(segments[index])
    return json.dumps({"version": 1, "name": "x", "items": items,
                       "activities": list(activities)}).encode("utf-8")


def _export_of(client, activities, segments=(), name="Journey") -> bytes:
    r = client.post("/api/projects/import", files={
        "file": (f"{name}{ProjectIO.EXTENSION}", _trip_file(activities, segments),
                 "application/json")})
    assert r.status_code == 201, r.text
    r = client.get(f"/api/projects/{name}/export")
    assert r.status_code == 200, r.text
    return r.content


@pytest.fixture
def exported(env):
    client, _ = env
    return _export_of(client, [_RUN, _HIKE, _KAYAK], _SEGMENTS)


def _import_all(client, content, project="Back"):
    return client.post(
        f"/api/projects/{project}/activities/import-gpx-tracks",
        files={"file": ("Journey.gpx", content, "application/gpx+xml")})


def _import_one(client, content, project="Single", **fields):
    return client.post(
        f"/api/projects/{project}/activities/import-gpx",
        files={"file": ("Journey.gpx", content, "application/gpx+xml")},
        data={k: str(v) for k, v in fields.items()})


def _inspect(client, content, project="Back"):
    r = client.post(f"/api/projects/{project}/activities/gpx/inspect",
                    files={"file": ("Journey.gpx", content, "application/gpx+xml")})
    assert r.status_code == 200, r.text
    return r.json()["candidates"]


def _rows(engine, project):
    """The trip's activities, in timeline order."""
    with Session(engine) as sess:
        return list(sess.exec(
            select(DBActivity)
            .join(DBProjectItem, DBProjectItem.activity_id == DBActivity.id)
            .join(DBProject, DBProject.id == DBProjectItem.project_id)
            .where(DBProject.name == project)
            .order_by(DBActivity.start_date)
        ).all())


def _instant(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _wall(text):
    """A start_date_local, which is stored as the wall clock labelled UTC."""
    return _instant(text).replace(tzinfo=None)


# ── The round trip ────────────────────────────────────────────────────────────

def test_an_export_imports_back_as_its_activities(env, exported):
    client, engine = env

    r = _import_all(client, exported)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] == []
    assert [i["name"] for i in body["imported"]] == [
        "Morning run", "Temple hike", "Bay paddle"]
    rows = _rows(engine, "Back")
    assert [row.id for row in rows] == [i["activity_id"] for i in body["imported"]]
    for row, act in zip(rows, (_RUN, _HIKE, _KAYAK)):
        assert row.name == act["name"]
        assert row.type == map_activity_type(act["type"])
        assert row.source == "gpx"
        assert _instant(row.start_date) == _instant(act["start_date"])
        assert _wall(row.start_date_local) == _wall(act["start_date_local"])
        assert row.elapsed_time == act["elapsed_time"]
        assert row.moving_time == act["moving_time"]
        assert row.distance == act["distance"]
        assert row.average_speed == pytest.approx(
            act["distance"] / act["moving_time"])
    # Run and hike in the app's own spelling; the kayak keeps its own type.
    assert [row.type for row in rows] == ["run", "hike", "Kayaking"]


def test_the_strava_run_keeps_its_stored_distance(env, exported):
    """Its polyline is simplified: the length measured from it falls short."""
    client, engine = env
    assert _import_all(client, exported).status_code == 200

    run = _rows(engine, "Back")[0]

    assert _length_m(_RUN_TRACK) < run.distance * 0.8
    assert run.distance == _RUN["distance"]


def test_tracks_in_two_zones_each_get_their_own(env, exported):
    client, engine = env
    assert _import_all(client, exported).status_code == 200

    run, hike, _ = _rows(engine, "Back")

    assert (run.timezone, hike.timezone) == ("Europe/Lisbon", "Asia/Tokyo")
    assert _wall(run.start_date_local) == datetime(2024, 6, 1, 8, 0)
    assert _wall(hike.start_date_local) == datetime(2024, 6, 2, 9, 0)


def test_the_connection_tracks_are_not_imported(env, exported):
    client, engine = env
    assert _import_all(client, exported).status_code == 200

    names = [row.name for row in _rows(engine, "Back")]

    assert len(names) == 3
    assert "Lisbon - Tokyo" not in names and "Tokyo - Kamakura" not in names


def test_the_single_track_import_stores_the_same_figures(env, exported):
    client, engine = env
    assert _import_all(client, exported).status_code == 200
    r = _import_one(client, exported, track_index=0)
    assert r.status_code == 200, r.text

    (alone,) = _rows(engine, "Single")
    run = _rows(engine, "Back")[0]

    assert (alone.distance, alone.moving_time, alone.average_speed) == \
        (run.distance, run.moving_time, run.average_speed)
    assert (alone.distance, alone.moving_time) == (_RUN["distance"], 3000)


def test_inspect_reports_the_figures_the_import_stores(env, exported):
    client, engine = env

    candidates = _inspect(client, exported)
    assert _import_all(client, exported).status_code == 200
    run = _rows(engine, "Back")[0]

    assert candidates[0]["distance_m"] == run.distance == _RUN["distance"]
    assert candidates[0]["moving_seconds"] == run.moving_time == 3000


def test_inspect_marks_the_connection_tracks(env, exported):
    client, _ = env

    candidates = _inspect(client, exported)

    assert [c["is_connection"] for c in candidates] == [
        False, True, False, True, False]


# ── Refusals ──────────────────────────────────────────────────────────────────

def test_a_file_with_an_untimed_track_is_refused_naming_it(env):
    """A pre-release GPX row's instant is unknown, so its export has no times
    (Decision 5): its times must be typed, one track at a time."""
    client, engine = env
    legacy = dict(_HIKE, id=-201, name="Old import", timezone="UTC")
    content = _export_of(client, [legacy, _KAYAK], name="Legacy")

    r = _import_all(client, content)

    assert r.status_code == 400, r.text
    (message,) = r.json()["detail"]["errors"]
    assert '"Old import"' in message
    assert "Bay paddle" not in message
    assert _rows(engine, "Back") == []


def test_over_the_trip_days_limit_is_refused_and_imports_nothing(
        env, exported, monkeypatch):
    """The three activities fall on three local days; the limit is two."""
    client, engine = env
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_TRIP_DAYS", "2")

    r = _import_all(client, exported)

    assert r.status_code == 402, r.text
    assert r.json()["resource"] == "trip_days"
    assert _rows(engine, "Back") == []


def test_within_the_trip_days_limit_imports(env, exported, monkeypatch):
    client, engine = env
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_TRIP_DAYS", "3")

    assert _import_all(client, exported).status_code == 200
    assert len(_rows(engine, "Back")) == 3


def test_a_file_with_nothing_to_import_is_refused(env):
    client, _ = env
    content = (
        b'<?xml version="1.0"?><gpx version="1.1" creator="x" '
        b'xmlns="http://www.topografix.com/GPX/1/1"><trk><name>A</name>'
        b'<type>traxjourney-connection</type><trkseg>'
        b'<trkpt lat="1" lon="1"/><trkpt lat="1.1" lon="1.1"/></trkseg></trk></gpx>')

    r = _import_all(client, content)

    assert r.status_code == 422, r.text


# ── Duplicates ────────────────────────────────────────────────────────────────

def test_reimporting_skips_every_track_as_a_duplicate(env, exported):
    client, engine = env
    first = _import_all(client, exported).json()["imported"]

    r = _import_all(client, exported)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == []
    assert body["skipped"] == [
        {"track_index": index, "name": done["name"],
         "duplicate_of": {"activity_id": done["activity_id"],
                          "name": done["name"]}}
        for index, done in zip((0, 2, 4), first)]
    assert len(_rows(engine, "Back")) == 3


def test_a_track_already_in_the_trip_is_skipped_and_the_rest_imported(
        env, exported):
    client, engine = env
    single = _import_one(client, exported, project="Back", track_index=2)
    assert single.status_code == 200, single.text

    body = _import_all(client, exported).json()

    assert [i["name"] for i in body["imported"]] == ["Morning run", "Bay paddle"]
    assert [(s["track_index"], s["duplicate_of"]["activity_id"])
            for s in body["skipped"]] == [(2, single.json()["activity_id"])]
    assert len(_rows(engine, "Back")) == 3


def test_a_track_twice_in_one_file_is_imported_once(env, exported):
    client, engine = env
    content = _export_of(client, [_RUN, dict(_RUN, id=105)], name="Twice")

    body = _import_all(client, content).json()

    (done,) = body["imported"]
    assert body["skipped"] == [{
        "track_index": 1, "name": "Morning run",
        "duplicate_of": {"activity_id": done["activity_id"],
                         "name": "Morning run"}}]
    assert len(_rows(engine, "Back")) == 1


# ── Third-party files ─────────────────────────────────────────────────────────

def test_one_track_of_several_segments_is_one_activity(env):
    client, engine = env
    content = (
        b'<?xml version="1.0"?><gpx version="1.1" creator="x" '
        b'xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Loop</name>'
        b'<trkseg>'
        b'<trkpt lat="45.0" lon="6.0"><time>2024-06-01T07:00:00Z</time></trkpt>'
        b'<trkpt lat="45.001" lon="6.001"><time>2024-06-01T07:01:00Z</time></trkpt>'
        b'</trkseg><trkseg>'
        b'<trkpt lat="45.002" lon="6.002"><time>2024-06-01T07:30:00Z</time></trkpt>'
        b'<trkpt lat="45.003" lon="6.003"><time>2024-06-01T07:31:00Z</time></trkpt>'
        b'</trkseg></trk></gpx>')

    r = _import_all(client, content)

    assert r.status_code == 200, r.text
    (row,) = _rows(engine, "Back")
    assert row.name == "Loop"
    assert row.elapsed_time == 31 * 60
    assert row.timezone == "Europe/Paris"


# ── Types for installed clients (Decision 3, E5) ──────────────────────────────

def test_inspect_keeps_activity_type_in_the_installed_clients_types(
        env, exported):
    client, _ = env

    run, _, hike, _, kayak = _inspect(client, exported)

    assert (kayak["activity_type"], kayak["activity_type_exact"]) == \
        ("Workout", "Kayaking")
    assert (run["activity_type"], run["activity_type_exact"]) == ("run", "run")
    assert (hike["activity_type"], hike["activity_type_exact"]) == \
        ("hike", "hike")


def test_an_installed_client_sending_the_suggested_type_stores_the_exact_one(
        env, exported):
    """An installed client shows the kayak as Workout and sends it back."""
    client, engine = env

    r = _import_one(client, exported, track_index=4, activity_type="Workout")

    assert r.status_code == 200, r.text
    (row,) = _rows(engine, "Single")
    assert row.type == "Kayaking"


def test_a_type_the_user_picked_is_stored_as_picked(env, exported):
    client, engine = env

    r = _import_one(client, exported, track_index=4, activity_type="ride")

    assert r.status_code == 200, r.text
    (row,) = _rows(engine, "Single")
    assert row.type == "ride"


def test_the_exact_type_sent_back_is_stored(env, exported):
    """What the new client sends: activity_type_exact."""
    client, engine = env

    r = _import_one(client, exported, track_index=4, activity_type="Kayaking")

    assert r.status_code == 200, r.text
    (row,) = _rows(engine, "Single")
    assert row.type == "Kayaking"

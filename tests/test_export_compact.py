"""Trip exports are compact JSON, and a trip that fits the import cap comes back (#454).

Import is capped at ``MAX_IMPORT_BYTES`` (50 MB, #434). Export had no cap and
wrote indented JSON, over twice the size of the same content written compact,
so a long trip could export to a file the same server then refused to import.

Exports are now compact (no indentation, no spaces after separators). Import
takes any valid JSON, so a file exported before this, indented, still imports.

The near-cap round trip runs against a scaled-down cap rather than the real
50 MB: at the real size it would generate, hold and parse one to two million
points several times over (well past a gigabyte of test memory, and minutes),
yet exercise nothing the scaled version does not. What ties it to the real cap
is the per-point guard below, one bound per source of elevation profile:

* Strava-style (distances in 0.1 m, elevations in 0.1 m): measured ~18.5
  bytes per point, bound 25, so 50 MB holds at least ~2.1 million points;
* a GPX upload with 0.1 m elevations: its profile stores cumulative distances
  at full float precision (points_to_elevation_profile); measured ~27.8,
  bound 32, at least ~1.6 million points;
* a GPX upload with unrounded or interpolated elevations: measured ~39.6,
  bound 45, at least ~1.2 million points.

Every source stays over the million-point trip #434 sized the cap for.
"""

from __future__ import annotations

import io
import json
import random
import zipfile

import polyline
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.project_shared as project_shared_mod
import api.project_transfer as project_transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from api.project_transfer import MAX_IMPORT_BYTES
from models.user import UserInfo
from src.project.project_io import ProjectIO

#: Upper bounds on a compact export's size per GPS point (encoded polyline plus
#: the elevation profile's distance/elevation pair), per source of profile.
#: See the module docstring for what was measured.
_BYTES_PER_POINT = {"strava": 25, "gpx": 32, "gpx-unrounded": 45}


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


def _activity(aid: int, points: int, rng: random.Random) -> dict:
    """A recorded ride: a GPS point every few metres, as Strava streams it."""
    lat, lon, dist_m, ele = 45.0 + aid * 0.01, 6.0, 0.0, 800.0
    track, dist, elev = [], [], []
    for _ in range(points):
        lat += rng.uniform(-1, 1) * 0.00004 + 0.00003
        lon += rng.uniform(-1, 1) * 0.00004 + 0.00003
        dist_m += rng.uniform(3, 6)
        ele += rng.uniform(-0.6, 0.6)
        track.append((lat, lon))
        dist.append(round(dist_m, 1) / 1000)
        elev.append(round(ele, 1))
    return {
        "id": aid, "name": f"Ride {aid}", "type": "Ride", "distance": dist_m,
        "start_date": f"2024-06-{aid % 28 + 1:02d}T08:00:00Z",
        "start_date_local": f"2024-06-{aid % 28 + 1:02d}T08:00:00Z",
        "start_latlng": list(track[0]), "end_latlng": list(track[-1]),
        "map": {"summary_polyline": polyline.encode(track)},
        "elevation_profile": {"distances_km": dist, "elevations_m": elev},
    }


def _gpx_activity(aid: int, points: int, rng: random.Random, *, rounded: bool) -> dict:
    """An uploaded GPX track, its profile built the way the upload builds it."""
    from src.models.track_edit import (
        TrackPoint, points_to_elevation_profile, points_to_polyline)
    lat, lon, ele = 46.0 + aid * 0.01, 7.0, 1200.0
    track = []
    for n in range(points):
        lat += rng.uniform(-1, 1) * 0.00004 + 0.00003
        lon += rng.uniform(-1, 1) * 0.00004 + 0.00003
        ele += rng.uniform(-0.6, 0.6)
        # Some devices leave <ele> out of a point: interpolated on import.
        elev = None if not rounded and n % 7 == 3 else (round(ele, 1) if rounded else ele)
        track.append(TrackPoint(lat=lat, lng=lon, elev=elev))
    dist, elev = points_to_elevation_profile(track)
    return {
        "id": -aid, "name": f"Hike {aid}", "type": "Hike",
        "start_date": "2024-07-01T08:00:00Z", "start_date_local": "2024-07-01T08:00:00Z",
        "map": {"summary_polyline": points_to_polyline(track)},
        "elevation_profile": {"distances_km": dist, "elevations_m": elev},
        "source": "gpx",
    }


def _trip(activities: int, points: int, source: str = "strava") -> bytes:
    """A trip of *activities* recorded tracks, and one of everything else a
    trip file holds: memory, journal entry, group, person, encounter,
    segment, day notes and sleeping options."""
    rng = random.Random(454)
    if source == "strava":
        acts = [_activity(aid, points, rng) for aid in range(1, activities + 1)]
    else:
        acts = [_gpx_activity(aid, points, rng, rounded=source == "gpx")
                for aid in range(1, activities + 1)]
    items = [{"item_type": "activity", "activity_id": a["id"]} for a in acts]
    items += [
        {"item_type": "memory", "memory": {
            "name": "Lac Blanc", "date": "2024-06-01", "time": "12:30",
            "description": "Lunch by the lake", "public_id": "pub-lac-blanc",
            "photos": ["00000000-0000-4000-8000-000000000454"],
            "geo_mode": "custom", "lat": 45.98, "lon": 6.89}},
        {"item_type": "journal", "journal": {
            "date": "2024-06-01", "time": "21:00", "description": "Tired legs",
            "photos": [], "geo_mode": "end_of_day", "lat": None, "lon": None}},
        {"item_type": "encounter", "encounter": {
            "person_id": 11, "date": "2024-06-01", "description": "Shared the hut",
            "geo_mode": "custom", "lat": 45.9, "lon": 6.9}},
        {"item_type": "segment", "segment": {
            "id": "seg-454", "segment_type": "train", "label": "Chamonix - Geneva",
            "date": "2024-06-02", "route_mode": "great_circle",
            "start": {"lat": 45.92, "lon": 6.87, "source": "manual"},
            "end": {"lat": 46.21, "lon": 6.14, "source": "manual"}}},
    ]
    return json.dumps({
        "version": 1, "name": "x",
        "items": items,
        "activities": acts,
        "groups": [{"id": 21, "name": "Hut crew", "nationalities": ["FR"], "socials": []}],
        "people": [{"id": 11, "name": "Ann", "group_id": 21, "nationalities": ["CH"],
                    "socials": [], "residence": "Bern, Switzerland"}],
        "day_meta": {"2024-06-01": {"difficulty": "hard", "sleeping": "Hut",
                                    "weather": "sun", "journal": "Big day",
                                    "tags": ["alps"]}},
        "sleeping_options": ["Hut", "Tent", "Hotel"],
    }, separators=(",", ":")).encode("utf-8")


def _import(client, name: str, content: bytes, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    return client.post(
        "/api/projects/import", params=params,
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})


def _export(client, name: str) -> bytes:
    r = client.get(f"/api/projects/{name}/export-traxj")
    assert r.status_code == 200, r.text
    return r.content


def _compact(content: bytes) -> bytes:
    return json.dumps(json.loads(content), separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _content(export: bytes) -> dict:
    """The trip's content, less what names it or is minted per trip: its
    name, lock version, the database ids of its memories, journal entries,
    encounters, people and groups, and memory public ids (a copy of a trip
    that still exists gets new ones, #463). People and groups are renumbered
    by position, and the encounters' references with them."""
    data = json.loads(export)
    for key in ("name", "lock_version"):
        data.pop(key, None)
    groups = {g.pop("id"): n for n, g in enumerate(data.get("groups", []))}
    people = {p.pop("id"): n for n, p in enumerate(data.get("people", []))}
    for p in data.get("people", []):
        p["group_id"] = groups.get(p.get("group_id"))
    for item in data["items"]:
        content = item.get(item["item_type"])
        if not isinstance(content, dict) or item["item_type"] == "segment":
            continue
        content.pop("id", None)
        content.pop("public_id", None)
        if item["item_type"] == "encounter":
            content["person_id"] = people.get(content.get("person_id"))
            content["group_id"] = groups.get(content.get("group_id"))
    return data


# ── Exports are compact ─────────────────────────────────────────────────────

def test_the_trip_export_is_compact(client):
    assert _import(client, "Alps", _trip(2, 50)).status_code == 201

    exported = _export(client, "Alps")

    assert exported == _compact(exported)


def test_the_trip_file_inside_the_zip_export_is_compact(client):
    assert _import(client, "Alps", _trip(2, 50)).status_code == 201

    r = client.get("/api/projects/Alps/export-zip")

    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    (trip_file,) = [n for n in zf.namelist() if n.endswith(ProjectIO.EXTENSION)]
    body = zf.read(trip_file)
    assert body == _compact(body)


def test_projectio_save_writes_compact_json(tmp_path):
    project = ProjectIO.from_bytes(_trip(1, 20))
    path = tmp_path / f"t{ProjectIO.EXTENSION}"

    ProjectIO.save(project, str(path))

    body = path.read_bytes()
    assert body == _compact(body)
    assert ProjectIO.load(str(path)).activities[0].summary_polyline == \
        project.activities[0].summary_polyline


def test_non_ascii_text_is_written_as_is(client):
    """As before: UTF-8 characters, not \\u escapes."""
    doc = json.loads(_trip(1, 10))
    doc["activities"][0]["name"] = "Col de l’Iseran à vélo"
    assert _import(client, "Alps", json.dumps(doc).encode()).status_code == 201

    exported = _export(client, "Alps")

    assert "Col de l’Iseran à vélo".encode("utf-8") in exported


# ── Older, indented exports still import ────────────────────────────────────

def test_an_indented_export_from_before_still_imports(client):
    assert _import(client, "Alps", _trip(3, 200)).status_code == 201
    exported = _export(client, "Alps")
    indented = json.dumps(json.loads(exported), indent=2, ensure_ascii=False).encode()

    r = _import(client, "Restored", indented)

    assert r.status_code == 201, r.text
    assert _content(_export(client, "Restored")) == _content(exported)


# ── Size ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", ["strava", "gpx", "gpx-unrounded"])
def test_an_export_stays_under_the_per_point_bound(client, source):
    activities, points = 4, 5000
    assert _import(client, "Alps", _trip(activities, points, source)).status_code == 201

    exported = _export(client, "Alps")

    per_point = len(exported) / (activities * points)
    assert per_point <= _BYTES_PER_POINT[source], per_point
    # What the bound means for the real cap: over the million points #434
    # sized it for, whatever the source.
    assert MAX_IMPORT_BYTES / _BYTES_PER_POINT[source] >= 1_000_000


def test_the_whole_format_survives_a_round_trip(client):
    """Every kind of content a trip file holds, not only activities."""
    assert _import(client, "Alps", _trip(2, 100)).status_code == 201
    exported = _export(client, "Alps")
    data = json.loads(exported)
    assert {i["item_type"] for i in data["items"]} == {
        "activity", "memory", "journal", "encounter", "segment"}
    assert data["people"] and data["groups"] and data["day_meta"]

    r = _import(client, "Restored", exported)

    assert r.status_code == 201, r.text
    assert _content(_export(client, "Restored")) == _content(exported)


def test_an_export_just_under_the_cap_imports_back(client, monkeypatch):
    # Scaled down: see the module docstring.
    cap = 2 * 1024 * 1024
    monkeypatch.setattr(project_transfer_mod, "MAX_IMPORT_BYTES", cap)
    points = 2000
    # Size one activity's share of an export, then pick the count that lands
    # the whole export just under the cap.
    assert _import(client, "Probe", _trip(1, points)).status_code == 201
    per_activity = len(_export(client, "Probe"))
    activities = int(0.97 * cap / per_activity)
    while True:
        assert _import(client, "Big", _trip(activities, points),
                       on_conflict="replace").status_code == 201
        exported = _export(client, "Big")
        if len(exported) <= cap:
            break
        activities -= 1
    assert 0.9 * cap < len(exported) <= cap, len(exported)
    # The same content indented would not fit: compact is what makes it fit.
    indented = json.dumps(json.loads(exported), indent=2, ensure_ascii=False).encode()
    assert len(indented) > cap

    r = _import(client, "Restored", exported)

    assert r.status_code == 201, r.text
    assert _content(_export(client, "Restored")) == _content(exported)
    assert _import(client, "Too big", indented).status_code == 413

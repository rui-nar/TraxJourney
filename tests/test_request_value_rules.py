"""No request can store what the trip-file import refuses (issue #462).

Every trip the app holds must export to a file its own import accepts. The
import refuses a non-finite number, an integer past 64 bits, a lone surrogate
and an out-of-range coordinate anywhere, and a wrong-typed or implausible
figure where it reads one. Request bodies took all of these: pydantic allows
NaN and Infinity by default, Python's JSON parser reads them, and nothing
bounded a coordinate. So a client could store a trip that would not export.

* Any JSON request body holding a value no JSON document of the app may hold
  is refused, whatever the endpoint (a check in front of every route).
* The request models that write trip content take the same bounds and types
  as the import (src/models/value_bounds.py, src/project/traxj_schema.py).
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session, func, select

from models.project_db import DBActivity, DBMemory, DBProject
from src.project.project_io import ProjectIO
from tests.test_elevation_non_finite import _upload_gpx, app  # noqa: F401

_ENVELOPE = "v1.d3JhcHBlZA.Y2lwaGVy"


def _raw(client, method, url, text):
    """A request whose body is *text* as is: a JSON library would not write
    NaN, Infinity or a lone surrogate for us."""
    return client.request(method, url, content=text.encode("utf-8"),
                          headers={"Content-Type": "application/json"})


def _count(engine, model) -> int:
    with Session(engine) as sess:
        return sess.exec(select(func.count()).select_from(model)).one()


def _memory(**kw):
    body = {"project_name": "Trip", "date": "2024-06-01", "geo_mode": "custom",
            "name": "Lake", "lat": 45.9, "lon": 6.8}
    body.update(kw)
    return body


# ── Any JSON body ───────────────────────────────────────────────────────────

_TOKENS = {
    "NaN": '"lat": NaN',
    "Infinity": '"lat": Infinity',
    "-Infinity": '"lon": -Infinity',
    "a float too large": '"lat": 1e999',
    "an integer past 64 bits": f'"insert_after_index": {2 ** 70}',
    "a lone surrogate": '"name": "Lake \\ud800"',
}


@pytest.mark.parametrize("fragment", _TOKENS.values(), ids=_TOKENS.keys())
def test_a_body_holding_a_value_no_trip_may_hold_is_refused(app, fragment):  # noqa: F811
    client, engine = app
    body = json.dumps(_memory(lat=None, lon=None, name=None, insert_after_index=None))
    key = fragment.split(":")[0]
    body = body.replace(f"{key}: null", fragment)
    assert fragment in body

    r = _raw(client, "POST", "/api/memories/", body)

    assert r.status_code == 422, r.text
    assert '"' not in r.json()["detail"]
    assert _count(engine, DBMemory) == 0


def test_a_well_formed_body_still_goes_through(app):  # noqa: F811
    client, engine = app

    r = client.post("/api/memories/", json=_memory(name="Café \U0001F600"))

    assert r.status_code == 201, r.text
    assert _count(engine, DBMemory) == 1


def test_the_check_covers_every_route(app):  # noqa: F811
    """A body the models type loosely (a style's width, a day's counters)
    is checked too."""
    client, _ = app
    for url, body in [
        ("/api/projects/Trip/track-style", '{"track_width": NaN}'),
        ("/api/projects/Trip/day-meta",
         '{"day_meta": {"2024-06-01": {"counters": [{"name": "Coffee", "value": Infinity}]}}}'),
    ]:
        r = _raw(client, "PUT", url, body)
        assert r.status_code == 422, (url, r.text)


# ── Coordinates ─────────────────────────────────────────────────────────────

def _person(client) -> int:
    r = client.post("/api/people/", json={"project_name": "Trip", "name": "Ann"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _segment(client, **kw) -> dict:
    body = {"segment_type": "train", "start_lat": 45.9, "start_lon": 6.8,
            "end_lat": 46.2, "end_lon": 6.1, "date": "2024-06-02"}
    body.update(kw)
    return body


@pytest.mark.parametrize("lat, lon", [(90.5, 6.0), (45.0, -180.5), (-91.0, 0.0)])
def test_an_out_of_range_coordinate_is_refused_everywhere(app, lat, lon):  # noqa: F811
    client, engine = app
    pid = _person(client)
    assert client.post("/api/memories/", json=_memory(lat=lat, lon=lon)).status_code == 422
    assert client.post("/api/journal/", json={
        "project_name": "Trip", "date": "2024-06-01", "geo_mode": "custom",
        "lat": lat, "lon": lon}).status_code == 422
    assert client.post("/api/encounters/", json={
        "project_name": "Trip", "person_id": pid, "date": "2024-06-01",
        "geo_mode": "custom", "lat": lat, "lon": lon}).status_code == 422
    assert client.post("/api/projects/Trip/segments",
                       json=_segment(client, start_lat=lat, start_lon=lon)).status_code == 422
    assert client.post("/api/projects/Trip/segments",
                       json=_segment(client, end_lat=lat, end_lon=lon)).status_code == 422

    # And on update.
    mid = client.post("/api/memories/", json=_memory()).json()["id"]
    r = client.put(f"/api/memories/{mid}", json={
        "date": "2024-06-01", "geo_mode": "custom", "lat": lat, "lon": lon})
    assert r.status_code == 422, r.text
    seg = client.post("/api/projects/Trip/segments", json=_segment(client))
    assert seg.status_code == 201, seg.text
    seg_id = seg.json()["id"]
    r = client.put(f"/api/projects/Trip/segments/{seg_id}", json=_segment(client, end_lat=lat, end_lon=lon))
    assert r.status_code == 422, r.text
    r = client.put(f"/api/projects/Trip/segments/{seg_id}/track", json={"points": [
        {"lat": 45.9, "lng": 6.8}, {"lat": lat, "lng": lon}]})
    assert r.status_code == 422, r.text


# ── A day's notes ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("day", [
    {"journal": 5}, {"tags": "alps"}, {"tags": [1]}, {"sleeping": ["Hut"]},
    {"difficulty": {}}, {"weather": True},
], ids=["journal", "tags-text", "tag-number", "sleeping", "difficulty", "weather"])
def test_a_day_note_of_the_wrong_type_is_refused(app, day):  # noqa: F811
    client, engine = app

    r = client.put("/api/projects/Trip/day-meta", json={"day_meta": {"2024-06-01": day}})

    assert r.status_code == 422, r.text
    with Session(engine) as sess:
        assert "2024-06-01" not in (sess.exec(select(DBProject)).one().day_meta_json or "")


def test_a_well_formed_day_note_still_goes_through(app):  # noqa: F811
    client, _ = app

    r = client.put("/api/projects/Trip/day-meta", json={"day_meta": {"2024-06-01": {
        "journal": "Big day", "tags": ["alps"], "sleeping": "Hut",
        "difficulty": "hard", "weather": "clear", "counters": [{"name": "Coffee", "value": 2}]}}})

    assert r.status_code == 204, r.text


# ── Activities a client adds, and their encrypted fields ────────────────────

def _strava(**kw):
    act = {"id": 16900000001, "name": "Ride", "type": "Ride", "distance": 1000.0,
           "moving_time": 300, "elapsed_time": 320,
           "start_date": "2024-06-01T08:00:00Z", "start_date_local": "2024-06-01T10:00:00Z"}
    act.update(kw)
    return act


@pytest.mark.parametrize("bad", [
    {"distance": 1e300}, {"moving_time": -5}, {"start_latlng": [95.0, 6.0]},
    {"name": 5}, {"trainer": "yes"}, {"start_date": "yesterday"},
], ids=["distance", "time", "latlng", "name", "flag", "date"])
def test_an_activity_the_import_would_refuse_is_not_added(app, monkeypatch, caplog, bad):  # noqa: F811
    """Dropped and logged, as a malformed one always was (#205): the rest of
    the batch is still added."""
    import logging
    import api.activities as activities_module
    monkeypatch.setattr(activities_module, "_enrich_activities_background", lambda *a, **k: None)
    client, engine = app

    with caplog.at_level(logging.WARNING):
        r = client.post("/api/projects/Trip/activities", json={"activities": [
            _strava(**bad), _strava(id=16900000002)]})

    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1
    with Session(engine) as sess:
        assert [a.id for a in sess.exec(select(DBActivity)).all()] == [16900000002]
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_a_strava_activity_can_still_be_added(app, monkeypatch):  # noqa: F811
    import api.activities as activities_module
    monkeypatch.setattr(activities_module, "_enrich_activities_background", lambda *a, **k: None)
    client, engine = app

    r = client.post("/api/projects/Trip/activities", json={"activities": [
        _strava(start_latlng=[], end_latlng=[], max_heartrate=171.0, elev_high=65535.0)]})

    assert r.status_code == 200, r.text
    assert _count(engine, DBActivity) == 1


@pytest.mark.parametrize("field, value", [
    ("start_latlng_json", "[95.0, 6.0]"),
    ("end_latlng_json", "[45.0, NaN]"),
    ("start_latlng_json", "not json"),
    ("elevation_profile_json", '{"distances_km": [0, 1], "elevations_m": [1, Infinity]}'),
    ("elevation_profile_low_res_json", '{"distances_km": [0, 1]}'),
    ("original_elevation_profile_json", '{"distances_km": [0, 1], "elevations_m": [1, 1e300]}'),
])
def test_plaintext_written_to_an_encrypted_field_must_be_what_the_import_takes(app, field, value):  # noqa: F811
    client, engine = app
    activity_id = _upload_gpx(client, [100.0, 110.0, 120.0])

    r = client.put(f"/api/activities/{activity_id}", json={field: value})

    assert r.status_code == 422, r.text


def test_an_envelope_or_a_well_formed_plaintext_is_still_written(app):  # noqa: F811
    client, engine = app
    activity_id = _upload_gpx(client, [100.0, 110.0, 120.0])

    for body in ({"start_latlng_json": _ENVELOPE, "elevation_profile_json": _ENVELOPE},
                 {"start_latlng_json": "[48.0, 2.0]",
                  "elevation_profile_json": '{"distances_km": [0, 1], "elevations_m": [1, 2]}'}):
        r = client.put(f"/api/activities/{activity_id}", json=body)
        assert r.status_code == 200, r.text


# ── What the app holds exports and imports back ─────────────────────────────

def test_a_trip_written_through_the_api_exports_and_imports_back(app, monkeypatch):  # noqa: F811
    import api.activities as activities_module
    monkeypatch.setattr(activities_module, "_enrich_activities_background", lambda *a, **k: None)
    client, _ = app
    pid = _person(client)
    assert client.post("/api/memories/", json=_memory(lat=-90.0, lon=180.0)).status_code == 201
    assert client.post("/api/encounters/", json={
        "project_name": "Trip", "person_id": pid, "date": "2024-06-01",
        "geo_mode": "custom", "lat": 90.0, "lon": -180.0}).status_code == 201
    assert client.post("/api/projects/Trip/segments", json=_segment(client)).status_code == 201
    assert client.post("/api/projects/Trip/activities", json={"activities": [
        _strava(elev_high=65535.0)]}).status_code == 200
    _upload_gpx(client, [100.0, "NaN", 99999.0, 130.0])
    assert client.put("/api/projects/Trip/day-meta", json={"day_meta": {"2024-06-01": {
        "journal": "Big day", "tags": ["alps"]}}}).status_code == 204

    exported = client.get("/api/projects/Trip/export-traxj")
    assert exported.status_code == 200, exported.text
    r = client.post("/api/projects/import", files={
        "file": (f"Back{ProjectIO.EXTENSION}", exported.content, "application/json")})

    assert r.status_code == 201, r.text


def _store_day_meta(engine, day_meta: dict) -> None:
    with Session(engine) as sess:
        row = sess.exec(select(DBProject)).one()
        row.day_meta_json = json.dumps(day_meta)
        sess.add(row)
        sess.commit()


def test_a_stored_legacy_day_does_not_block_a_save(app):  # noqa: F811
    """A day stored before its notes were typed may hold anything. The client
    sends every day back on each save; one it did not touch must not make
    every save of the trip a 422."""
    client, engine = app
    _store_day_meta(engine, {"2024-05-01": {"journal": 5, "tags": "old", "sleeping": "Hut"}})

    r = client.put("/api/projects/Trip/day-meta", json={"day_meta": {
        "2024-05-01": {"journal": 5, "tags": "old", "sleeping": "Hut"},
        "2024-06-01": {"journal": "Big day", "tags": ["alps"]}}})

    assert r.status_code == 204, r.text


def test_a_changed_field_of_a_legacy_day_is_still_checked(app):  # noqa: F811
    client, engine = app
    _store_day_meta(engine, {"2024-05-01": {"journal": 5, "sleeping": "Hut"}})

    # The untouched field passes; the one being written is judged.
    r = client.put("/api/projects/Trip/day-meta", json={"day_meta": {
        "2024-05-01": {"journal": 5, "sleeping": ["Tent"]}}})
    assert r.status_code == 422, r.text

    r = client.put("/api/projects/Trip/day-meta", json={"day_meta": {
        "2024-05-01": {"journal": 5, "sleeping": "Tent"}}})
    assert r.status_code == 204, r.text


def test_a_changed_type_is_a_change(app):  # noqa: F811
    """1 == True in Python: a stored 1 rewritten as true is still a write,
    and is judged."""
    client, engine = app
    _store_day_meta(engine, {"2024-05-01": {"journal": 1}})

    r = client.put("/api/projects/Trip/day-meta", json={"day_meta": {
        "2024-05-01": {"journal": True}}})

    assert r.status_code == 422, r.text

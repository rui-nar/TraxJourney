"""An imported ``.traxj`` file is checked value by value (issue #462).

#451 made a file whose *structure* the import cannot walk a 400. Its leaf
values were still taken as they came, so a wrong-typed or out-of-range value in
an otherwise well-formed file either failed at ingest with a 500 or, worse,
imported and then broke the trip: every later load of it a 500, or a document
the app's JSON parser refuses.

A real export never holds such values, so the file is refused, with a 400
naming the offending field (never its content), and nothing is ingested.
Every shape of file a past version wrote still imports: see
test_import_historical_exports.py.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import (
    DBActivity, DBEncounter, DBJournalEntry, DBMemory, DBPerson, DBPersonGroup,
    DBProject, DBProjectItem,
)
from models.user import UserInfo
from src.brand import APP_NAME
from src.project.project_io import InvalidProjectFile, ProjectIO

_INVALID = f"This file isn't a valid {APP_NAME} trip"
_PHOTO = "00000000-0000-4000-8000-000000000462"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app, one signed-in user, an in-memory DB."""
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(project_shared_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        uid = user.id

    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app, raise_server_exceptions=False), engine
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


#: One of everything a trip file holds, each at a fixed place so a case can
#: point at it: items[0] activity, [1] memory, [2] journal entry,
#: [3] encounter, [4] segment.
_TRIP = {
    "version": 1,
    "name": "Trip",
    "trip_start": "2024-06-01",
    "filter_state": {"start_date": None, "end_date": None, "activity_types": None},
    "items": [
        {"item_type": "activity", "activity_id": 1},
        {"item_type": "memory", "memory": {
            "id": 5, "public_id": "a" * 32, "name": "Lake", "date": "2024-06-01",
            "time": "12:30", "description": "Lunch", "photos": [_PHOTO],
            "geo_mode": "custom", "lat": 45.9, "lon": 6.8,
            "comment_count": 0, "like_count": 0}},
        {"item_type": "journal", "journal": {
            "id": 6, "date": "2024-06-01", "time": None, "description": "Tired",
            "photos": [], "geo_mode": "end_of_day", "lat": None, "lon": None}},
        {"item_type": "encounter", "encounter": {
            "id": 7, "person_id": 11, "group_id": None, "date": "2024-06-01",
            "time": None, "description": "Hut", "geo_mode": "custom",
            "lat": 45.9, "lon": 6.9}},
        {"item_type": "segment", "segment": {
            "id": "seg-1", "segment_type": "train", "label": "Down", "date": "2024-06-02",
            "start": {"lat": 45.92, "lon": 6.87, "source": "manual"},
            "end": {"lat": 46.21, "lon": 6.14, "source": "auto"},
            "route_mode": "great_circle", "train_number": None, "hafas_provider": None,
            "route_polyline": None, "route_status": "idle", "route_error": None,
            "route_started_at": None, "route_degraded": False,
            "route_hafas_failed": False, "route_degrade_retries": 0,
            "route_edited": False, "route_resolver_version": 0, "route_strategy": None}},
    ],
    "activities": [{
        "id": 1, "name": "Ride", "type": "Ride", "distance": 1000.0,
        "moving_time": 300, "elapsed_time": 320, "total_elevation_gain": 10.0,
        "start_date": "2024-06-01T08:00:00Z", "start_date_local": "2024-06-01T10:00:00Z",
        "timezone": "(GMT+01:00) Europe/Paris", "achievement_count": 0,
        "kudos_count": 0, "comment_count": 0, "athlete_count": 1, "photo_count": 0,
        "trainer": False, "commute": False, "manual": False, "private": False,
        "flagged": False, "average_speed": 3.3, "max_speed": 9.1,
        "has_heartrate": False, "pr_count": 0, "total_photo_count": 0,
        "has_kudoed": False, "gear_id": None, "average_heartrate": None,
        "max_heartrate": None, "heartrate_opt_out": False,
        "display_hide_heartrate_option": False, "elev_high": 900.0, "elev_low": 800.0,
        "start_latlng": [45.9, 6.8], "end_latlng": [45.95, 6.85],
        "map": {"summary_polyline": "_p~iF~ps|U_ulLnnqC"},
        "elevation_profile": {"distances_km": [0.0, 1.0], "elevations_m": [800.0, 810.0]},
        "is_edited": False, "split_parent_id": None, "refresh_status": None,
        "refresh_started_at": None, "refresh_error": None, "source": None,
        "source_id": None, "start_latlng_enc": None, "end_latlng_enc": None,
        "elevation_profile_enc": None,
    }],
    "people": [{
        "id": 11, "name": "Ann", "email": None, "phone": None, "polarsteps": None,
        "notes": None, "avatar_photo": None,
        "socials": [{"network": "instagram", "handle": "ann"}],
        "nationalities": ["CH"], "residence": "Bern", "group_id": 21}],
    "groups": [{"id": 21, "name": "Hut crew", "nationalities": ["FR"], "socials": []}],
    "day_meta": {"2024-06-01": {"difficulty": "hard", "sleeping": "Hut",
                                "weather": "clear", "journal": "Big day",
                                "tags": ["alps"]}},
    "sleeping_options": ["Hut", "Tent"],
}


def _set(path, value):
    """A copy of the trip with the value at *path* (keys and indexes) replaced."""
    doc = copy.deepcopy(_TRIP)
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return doc


def _bytes(doc) -> bytes:
    # json.dumps writes NaN and Infinity as bare tokens, and escapes a lone
    # surrogate as \ud800: both parse back to what the case put in.
    return json.dumps(doc).encode("utf-8")


def _import(client, content: bytes, **params):
    return client.post(
        "/api/projects/import", params=params,
        files={"file": (f"Trip{ProjectIO.EXTENSION}", content, "application/json")},
    )


def _count(engine, model) -> int:
    with Session(engine) as sess:
        return sess.exec(select(func.count()).select_from(model)).one()


_NAN, _INF = float("nan"), float("inf")
_MEM = ("items", 1, "memory")
_JOURNAL = ("items", 2, "journal")
_ENC = ("items", 3, "encounter")
_SEG = ("items", 4, "segment")
_ACT = ("activities", 0)
_DAY = ("day_meta", "2024-06-01")

#: case id -> (file bytes, the field the refusal must name)
_BAD = {
    # ── Lone surrogates: a UnicodeEncodeError at the SQLite bind ──────────
    "a lone surrogate in a memory name": (_bytes(_set(_MEM + ("name",), "Lake \ud800")), "items[1].memory.name"),
    "a lone surrogate in an activity name": (_bytes(_set(_ACT + ("name",), "\udfff")), "activities[0].name"),
    "a lone surrogate in a person name": (_bytes(_set(("people", 0, "name"), "\ud83d")), "people[0].name"),
    "a lone surrogate in a day's tags": (_bytes(_set(_DAY + ("tags",), ["\ud800"])), "day_meta.<day>.tags[0]"),
    "a lone surrogate in a field the import ignores": (_bytes(_set(("track_color",), "\ud800")), "track_color"),
    "a lone surrogate in a field name": (_bytes(_set(("people", 0, "\ud800"), 1)), "people[0]"),
    # ── Non-finite numbers ───────────────────────────────────────────────
    "Infinity as a segment latitude": (_bytes(_set(_SEG + ("start", "lat"), _INF)), "items[4].segment.start.lat"),
    "-Infinity as a memory longitude": (_bytes(_set(_MEM + ("lon",), -_INF)), "items[1].memory.lon"),
    "NaN in filter_state": (_bytes(_set(("filter_state", "start_date"), _NAN)), "filter_state.start_date"),
    "NaN in an elevation profile": (
        _bytes(_set(_ACT + ("elevation_profile", "elevations_m"), [800.0, _NAN])),
        "activities[0].elevation_profile.elevations_m[1]"),
    "NaN in a field the import ignores": (_bytes(_set(("track_width",), _NAN)), "track_width"),
    "a number too large for a float": (
        _bytes(_set(_ACT + ("distance",), 1.5)).replace(b"1.5", b"1e999"), "activities[0].distance"),
    # ── Integers past 64 bits: an OverflowError at the bind ──────────────
    "an activity id of 2^63": (_bytes(_set(_ACT + ("id",), 2 ** 63)), "activities[0].id"),
    "an activity count past 64 bits": (_bytes(_set(_ACT + ("kudos_count",), 2 ** 64)), "activities[0].kudos_count"),
    "a version past 64 bits": (_bytes(_set(("version",), -(2 ** 63) - 1)), "version"),
    # ── Wrong types the database binds ──────────────────────────────────
    "a string memory latitude": (_bytes(_set(_MEM + ("lat",), "abc")), "items[1].memory.lat"),
    "a string segment latitude": (_bytes(_set(_SEG + ("start", "lat"), "45.9")), "items[4].segment.start.lat"),
    "a boolean journal longitude": (_bytes(_set(_JOURNAL + ("lon",), True)), "items[2].journal.lon"),
    "an object memory name": (_bytes(_set(_MEM + ("name",), {})), "items[1].memory.name"),
    "a number memory date": (_bytes(_set(_MEM + ("date",), 20240601)), "items[1].memory.date"),
    "a null memory date": (_bytes(_set(_MEM + ("date",), None)), "items[1].memory.date"),
    "a list segment id": (_bytes(_set(_SEG + ("id",), [1])), "items[4].segment.id"),
    "a null segment latitude": (_bytes(_set(_SEG + ("end", "lat"), None)), "items[4].segment.end.lat"),
    "a string activity distance": (_bytes(_set(_ACT + ("distance",), "1000")), "activities[0].distance"),
    "a null activity distance": (_bytes(_set(_ACT + ("distance",), None)), "activities[0].distance"),
    "a number activity flag": (_bytes(_set(_ACT + ("trainer",), 1)), "activities[0].trainer"),
    "a string activity start date that is not a date": (
        _bytes(_set(_ACT + ("start_date",), "yesterday")), "activities[0].start_date"),
    "a map that is not an object": (_bytes(_set(_ACT + ("map",), "x")), "activities[0].map"),
    "a number summary polyline": (_bytes(_set(_ACT + ("map", "summary_polyline"), 5)), "activities[0].map.summary_polyline"),
    "an elevation profile without elevations": (
        _bytes(_set(_ACT + ("elevation_profile",), {"distances_km": [0.0]})),
        "activities[0].elevation_profile.elevations_m"),
    "an elevation that is text": (
        _bytes(_set(_ACT + ("elevation_profile", "elevations_m"), [800.0, "810"])),
        "activities[0].elevation_profile.elevations_m"),
    "an elevation profile that is not lists": (
        _bytes(_set(_ACT + ("elevation_profile", "distances_km"), {"0": 0.0})),
        "activities[0].elevation_profile.distances_km"),
    "a string version": (_bytes(_set(("version",), "1")), "version"),
    "a person's socials that is text": (_bytes(_set(("people", 0, "socials"), "x")), "people[0].socials"),
    "a social entry that is text": (_bytes(_set(("people", 0, "socials"), ["x"])), "people[0].socials[0]"),
    "a social handle that is a number": (
        _bytes(_set(("people", 0, "socials"), [{"network": "x", "handle": 5}])), "people[0].socials[0].handle"),
    "a nationality that is a number": (_bytes(_set(("groups", 0, "nationalities"), [1])), "groups[0].nationalities[0]"),
    # ── Ids the import uses as keys: unhashable ones are a TypeError ─────
    "a list group id": (_bytes(_set(("groups", 0, "id"), [21])), "groups[0].id"),
    "an object person id": (_bytes(_set(("people", 0, "id"), {})), "people[0].id"),
    "a list person group_id": (_bytes(_set(("people", 0, "group_id"), [21])), "people[0].group_id"),
    "a list encounter person_id": (_bytes(_set(_ENC + ("person_id",), [11])), "items[3].encounter.person_id"),
    "a string journal id": (_bytes(_set(_JOURNAL + ("id",), "6")), "items[2].journal.id"),
    # ── Imported, then broke every load of the trip ─────────────────────
    "a sleeping option that is not text": (_bytes(_set(("sleeping_options",), ["Hut", 3])), "sleeping_options[1]"),
    "sleeping options that are not a list": (_bytes(_set(("sleeping_options",), "Hut")), "sleeping_options"),
    "a day's tags that are not a list": (_bytes(_set(_DAY + ("tags",), "alps")), "day_meta.<day>.tags"),
    "a day's journal that is a number": (_bytes(_set(_DAY + ("journal",), 1)), "day_meta.<day>.journal"),
    "activity_types that are not a list": (_bytes(_set(("filter_state", "activity_types"), "Ride")), "filter_state.activity_types"),
    "a routed segment whose polyline is not JSON": (
        _bytes(_set(_SEG, {**_TRIP["items"][4]["segment"], "route_mode": "rail",
                           "route_polyline": "not json"})), "items[4].segment.route_polyline"),
    "a routed segment whose polyline holds NaN": (
        _bytes(_set(_SEG, {**_TRIP["items"][4]["segment"], "route_mode": "rail",
                           "route_polyline": "[[6.8, 45.9], [NaN, 46.0]]"})),
        "items[4].segment.route_polyline"),
    "a routed segment whose polyline is not a list of points": (
        _bytes(_set(_SEG, {**_TRIP["items"][4]["segment"], "route_mode": "ferry",
                           "route_polyline": "[1, 2]"})), "items[4].segment.route_polyline"),
    # ── Coordinates out of range ─────────────────────────────────────────
    "a memory latitude past the pole": (_bytes(_set(_MEM + ("lat",), 90.5)), "items[1].memory.lat"),
    "an encounter longitude past 180": (_bytes(_set(_ENC + ("lon",), -180.5)), "items[3].encounter.lon"),
    "a segment latitude past the pole": (_bytes(_set(_SEG + ("end", "lat"), -91)), "items[4].segment.end.lat"),
    "an activity start latitude past the pole": (_bytes(_set(_ACT + ("start_latlng",), [91.0, 6.8])), "activities[0].start_latlng"),
    "an activity end longitude past 180": (_bytes(_set(_ACT + ("end_latlng",), [45.0, 200.0])), "activities[0].end_latlng"),
    "an activity start point of one number": (_bytes(_set(_ACT + ("start_latlng",), [45.0])), "activities[0].start_latlng"),
    "an activity start point of text": (_bytes(_set(_ACT + ("start_latlng",), ["45", "6"])), "activities[0].start_latlng[0]"),
}


@pytest.mark.parametrize("content, field", _BAD.values(), ids=_BAD.keys())
def test_a_bad_value_is_refused_by_name_and_nothing_is_ingested(env, caplog, content, field):
    client, engine = env

    with caplog.at_level(logging.DEBUG):
        r = _import(client, content)

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail.startswith(_INVALID), detail
    assert f": {field} " in detail, detail
    # The client lifts the detail out with a "no double quote" pattern.
    assert '"' not in detail, detail
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    for model in (DBProject, DBProjectItem, DBActivity, DBMemory, DBJournalEntry,
                  DBEncounter, DBPerson, DBPersonGroup):
        assert _count(engine, model) == 0, model


def test_the_refusal_never_echoes_the_file(env):
    """The day's key, and the value itself, are the file's content."""
    client, _ = env
    doc = copy.deepcopy(_TRIP)
    doc["day_meta"] = {"secret-day-key": {"journal": 12345}}

    r = _import(client, _bytes(doc))

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "day_meta.<day>.journal" in detail
    assert "secret-day-key" not in detail
    assert "12345" not in detail


def test_the_well_formed_trip_the_cases_start_from_imports(env):
    """Each case above breaks one value of this trip, and only that."""
    client, engine = env

    r = _import(client, _bytes(_TRIP))

    assert r.status_code == 201, r.text
    assert _count(engine, DBActivity) == 1
    assert _count(engine, DBMemory) == 1
    assert _count(engine, DBEncounter) == 1
    r = client.get("/api/projects/Trip")
    assert r.status_code == 200, r.text


def test_a_bad_file_does_not_replace_the_trip(env):
    client, engine = env
    assert _import(client, _bytes(_TRIP)).status_code == 201
    bad = _bytes(_set(_MEM + ("name",), "\ud800"))

    r = _import(client, bad, on_conflict="replace")

    assert r.status_code == 400, r.text
    trip = client.get("/api/projects/Trip").json()
    assert trip["items"][1]["memory"]["name"] == "Lake"


def test_projectio_refuses_it_too():
    """Every reader of a trip document goes through the same check."""
    with pytest.raises(InvalidProjectFile, match=r"items\[1\]\.memory\.lat"):
        ProjectIO.from_dict(_set(_MEM + ("lat",), "abc"))


def test_unknown_fields_are_still_ignored(env):
    client, _ = env
    doc = copy.deepcopy(_TRIP)
    doc["from_the_future"] = {"anything": [1, "two", None, {"three": 3.0}]}
    doc["items"][1]["memory"]["mood"] = "happy"
    doc["activities"][0]["suffer_score"] = 12
    doc["people"][0]["pronouns"] = ["they"]

    r = _import(client, _bytes(doc))

    assert r.status_code == 201, r.text


@pytest.mark.parametrize("container", ["[0]", '{"a":0}'])
def test_checking_a_file_costs_little_more_memory_than_reading_it(container):
    """The check walks every value of the file, and must not keep a record
    of where each one is while it does: a file of 300,000 tiny containers
    peaked at 67 times its size, where reading it alone peaks at 25."""
    import tracemalloc

    raw = (json.dumps(_TRIP)[:-1] + ',"junk":['
           + ",".join([container] * 300_000) + "]}").encode()
    tracemalloc.start()
    try:
        ProjectIO.from_bytes(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak / len(raw) < 30, peak / len(raw)

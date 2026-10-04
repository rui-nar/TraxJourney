"""A TraxJourney GPX export imported back into its own trip adds nothing (Q1).

The export writes each activity's original identity into the TraxJourney
extension: a GPX import's stored fingerprint, a Strava activity's id. The
track itself cannot be recognised by its fingerprint, because the export
re-times it and a Strava row has no fingerprint at all. On import, a track
whose identity names an activity already in the trip is a duplicate:
``import-gpx`` answers 409, ``import-gpx-tracks`` skips it, and ``inspect``
reports it.

The identity is untrusted input: it is checked for shape, and matched only
within the importer's own trip, never another trip or another user's.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

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
from src.gpx.export_format import (
    EXTENSION_NAMESPACE, activity_extensions, activity_identity,
    read_activity_identity,
)
from src.gpx.importer import candidates, parse_gpx_bytes
from src.project.project_io import ProjectIO

#: The stored fingerprint of the GPX import below.
_HIKE_FINGERPRINT = "ab" * 32


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app on an in-memory DB. User 1 owns the empty trip "Back";
    user 2 exists. ``as_user`` switches who is calling."""
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
        sess.add(UserInfo(id=2, display_name="B", email="b@e.com"))
        sess.add(DBProject(user_info_id=1, name="Back"))
        sess.commit()

    def as_user(uid):
        router.app.dependency_overrides[get_current_user] = \
            lambda: {"sub": str(uid)}

    as_user(1)
    try:
        yield TestClient(router.app, raise_server_exceptions=False), engine, as_user
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _line(lat, lon, count, step=0.002):
    return [(round(lat + i * step, 5), round(lon + i * step, 5)) for i in range(count)]


def _activity(aid, name, kind, track, start, local, zone, source=None,
              source_id=None):
    return {
        "id": aid, "name": name, "type": kind, "distance": 1234.5,
        "moving_time": 3000, "elapsed_time": 3600,
        "start_date": start, "start_date_local": local, "timezone": zone,
        "start_latlng": list(track[0]), "end_latlng": list(track[-1]),
        "map": {"summary_polyline": polyline.encode(track)},
        "source": source, "source_id": source_id,
    }


#: A Strava run.
_RUN = _activity(
    101, "Morning run", "Run", _line(38.70, -9.14, 6), "2024-06-01T07:00:00Z",
    "2024-06-01T08:00:00Z", "(GMT+00:00) Europe/Lisbon")

#: A GPX import, with its stored fingerprint.
_HIKE = _activity(
    -102, "Temple hike", "hike", _line(35.68, 139.76, 30, step=0.001),
    "2024-06-02T00:00:00Z", "2024-06-02T09:00:00Z", "Asia/Tokyo",
    source="gpx", source_id=_HIKE_FINGERPRINT)


def _trip_file(activities):
    items = [{"item_type": "activity", "activity_id": a["id"]} for a in activities]
    return json.dumps({"version": 1, "name": "x", "items": items,
                       "activities": list(activities)}).encode("utf-8")


def _create_trip(client, name, activities):
    r = client.post("/api/projects/import", files={
        "file": (f"{name}{ProjectIO.EXTENSION}", _trip_file(activities),
                 "application/json")})
    assert r.status_code == 201, r.text


def _export(client, name) -> bytes:
    r = client.get(f"/api/projects/{name}/export")
    assert r.status_code == 200, r.text
    return r.content


def _import_all(client, content, project):
    return client.post(
        f"/api/projects/{project}/activities/import-gpx-tracks",
        files={"file": ("Journey.gpx", content, "application/gpx+xml")})


def _import_one(client, content, project, **fields):
    return client.post(
        f"/api/projects/{project}/activities/import-gpx",
        files={"file": ("Journey.gpx", content, "application/gpx+xml")},
        data={k: str(v) for k, v in fields.items()})


def _inspect(client, content, project):
    r = client.post(f"/api/projects/{project}/activities/gpx/inspect",
                    files={"file": ("Journey.gpx", content, "application/gpx+xml")})
    assert r.status_code == 200, r.text
    return r.json()


def _rows(engine, project):
    with Session(engine) as sess:
        return list(sess.exec(
            select(DBActivity)
            .join(DBProjectItem, DBProjectItem.activity_id == DBActivity.id)
            .join(DBProject, DBProject.id == DBProjectItem.project_id)
            .where(DBProject.name == project)
            .order_by(DBActivity.start_date)
        ).all())


def _crafted(*identities) -> bytes:
    """A GPX file of one timed track per ``(source, source_id)``."""
    ns = EXTENSION_NAMESPACE
    tracks = []
    for index, (source, source_id) in enumerate(identities):
        lat = 45.0 + index * 0.1
        tracks.append(
            f'<trk><name>Track {index}</name><extensions>'
            f'<traxj:source>{source}</traxj:source>'
            f'<traxj:source_id>{source_id}</traxj:source_id></extensions><trkseg>'
            f'<trkpt lat="{lat}" lon="6.0"><time>2024-06-0{index + 1}T07:00:00Z</time></trkpt>'
            f'<trkpt lat="{lat + 0.001}" lon="6.001"><time>2024-06-0{index + 1}T07:01:00Z</time></trkpt>'
            f'<trkpt lat="{lat + 0.002}" lon="6.002"><time>2024-06-0{index + 1}T07:02:00Z</time></trkpt>'
            f'</trkseg></trk>')
    return (f'<?xml version="1.0"?><gpx version="1.1" creator="x" '
            f'xmlns="http://www.topografix.com/GPX/1/1" xmlns:traxj="{ns}">'
            + "".join(tracks) + '</gpx>').encode("utf-8")


# ── The export ────────────────────────────────────────────────────────────────

def test_the_export_carries_each_activitys_identity(env):
    client, _, _ = env
    _create_trip(client, "Journey", [_RUN, _HIKE])

    run, hike = candidates(parse_gpx_bytes(_export(client, "Journey")))

    assert run.carried_identity == ("strava", "101")
    assert hike.carried_identity == ("gpx", _HIKE_FINGERPRINT)


def test_an_activity_without_a_fingerprint_or_strava_id_carries_its_local_id():
    """PIR2-1: a split tail (GPX or Strava), or a GPX import from before
    fingerprints, is known by its own local id."""
    assert activity_identity(None, None, -5) == ("local", "-5")
    assert activity_identity("gpx", None, -6) == ("local", "-6")
    assert activity_identity("gpx", "xyz", -6) == ("local", "-6")
    # Old GPX imports drew 62-bit ids; the whole id column is accepted.
    assert activity_identity(None, None, -(2 ** 62)) ==         ("local", str(-(2 ** 62)))
    assert activity_identity(None, None, -(2 ** 63)) is None


def test_an_activity_without_an_identity_carries_none():
    assert activity_identity(None, None, None) is None
    assert [e.tag for e in activity_extensions(1, 1.0, None)] == [
        f"{{{EXTENSION_NAMESPACE}}}moving_time",
        f"{{{EXTENSION_NAMESPACE}}}distance"]


# ── Back into the trip it came from ───────────────────────────────────────────

def test_import_all_into_its_own_trip_skips_every_activity(env):
    client, engine, _ = env
    _create_trip(client, "Journey", [_RUN, _HIKE])
    before = [(row.id, row.name) for row in _rows(engine, "Journey")]

    r = _import_all(client, _export(client, "Journey"), "Journey")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == []
    assert body["skipped"] == [
        {"track_index": index, "name": name,
         "duplicate_of": {"activity_id": aid, "name": name}}
        for index, (aid, name) in enumerate(before)]
    assert [(row.id, row.name) for row in _rows(engine, "Journey")] == before


def test_import_gpx_into_its_own_trip_is_a_conflict(env):
    client, engine, _ = env
    _create_trip(client, "Solo", [_RUN])

    r = _import_one(client, _export(client, "Solo"), "Solo")

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["activity_id"] == 101
    assert len(_rows(engine, "Solo")) == 1


def test_import_gpx_with_typed_times_is_still_a_conflict(env):
    """The identity does not depend on the times the form sends."""
    client, _, _ = env
    _create_trip(client, "Solo", [_HIKE])

    r = _import_one(client, _export(client, "Solo"), "Solo",
                    date="2024-07-01", start_time="10:00", end_time="11:00")

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["activity_id"] == -102


def test_inspect_reports_the_activity_already_in_its_own_trip(env):
    client, _, _ = env
    _create_trip(client, "Solo", [_RUN])

    body = _inspect(client, _export(client, "Solo"), "Solo")

    assert body["duplicate_of"] == {"activity_id": 101, "name": "Morning run"}


def _split(client, trip, activity_id, at=3):
    r = client.post(f"/api/projects/{trip}/activities/{activity_id}/split",
                    json={"split_index": at})
    assert r.status_code == 200, r.text


def test_split_pieces_imported_back_into_their_own_trip_are_skipped(env):
    """PIR2-1: a tail has a fresh local id and no fingerprint, so only its
    "local" identity recognises it."""
    client, engine, _ = env
    _create_trip(client, "Split", [_RUN, _HIKE])
    _split(client, "Split", 101)
    _split(client, "Split", -102, at=10)
    before = _rows(engine, "Split")
    assert len(before) == 4
    tails = [row for row in before if row.split_parent_id is not None]
    assert len(tails) == 2 and all(row.id < 0 for row in tails)
    assert all(row.source_id is None for row in tails)

    r = _import_all(client, _export(client, "Split"), "Split")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == []
    assert sorted(s["duplicate_of"]["activity_id"] for s in body["skipped"])         == sorted(row.id for row in before)
    assert len(_rows(engine, "Split")) == 4


def test_a_gpx_import_from_before_fingerprints_is_skipped(env):
    """A pre-#462 GPX row: source NULL, a negative id, no fingerprint."""
    client, engine, _ = env
    early = dict(_HIKE, id=-300, name="Early import", source=None,
                 source_id=None)
    _create_trip(client, "Early", [early])
    content = _export(client, "Early")
    (candidate,) = candidates(parse_gpx_bytes(content))
    assert candidate.carried_identity == ("local", "-300")

    r = _import_all(client, content, "Early")

    assert r.status_code == 200, r.text
    assert r.json()["imported"] == []
    assert r.json()["skipped"][0]["duplicate_of"] == {
        "activity_id": -300, "name": "Early import"}
    single = _import_one(client, content, "Early")
    assert single.status_code == 409, single.text
    assert _inspect(client, content, "Early")["duplicate_of"] == {
        "activity_id": -300, "name": "Early import"}
    assert len(_rows(engine, "Early")) == 1


def test_a_gpx_import_with_a_62_bit_id_is_skipped(env):
    """GPX import drew 62-bit ids before local ids were capped at 53 bits."""
    client, engine, _ = env
    old_id = -(2 ** 62 - 12345)
    early = dict(_HIKE, id=old_id, name="Wide id", source=None,
                 source_id=None)
    _create_trip(client, "Wide", [early])
    content = _export(client, "Wide")
    (candidate,) = candidates(parse_gpx_bytes(content))
    assert candidate.carried_identity == ("local", str(old_id))

    r = _import_all(client, content, "Wide")

    assert r.status_code == 200, r.text
    assert r.json()["imported"] == []
    assert r.json()["skipped"][0]["duplicate_of"] == {
        "activity_id": old_id, "name": "Wide id"}
    assert len(_rows(engine, "Wide")) == 1


def test_split_pieces_import_into_an_empty_trip(env):
    client, engine, _ = env
    _create_trip(client, "Split", [_RUN, _HIKE])
    _split(client, "Split", 101)
    _split(client, "Split", -102, at=10)

    r = _import_all(client, _export(client, "Split"), "Back")

    assert r.status_code == 200, r.text
    assert len(r.json()["imported"]) == 4
    assert r.json()["skipped"] == []
    assert len(_rows(engine, "Back")) == 4


def test_a_local_identity_in_another_users_trip_matches_nothing(env):
    client, engine, as_user = env
    as_user(2)
    _create_trip(client, "Theirs", [dict(_HIKE, id=-777, source=None,
                                         source_id=None)])
    as_user(1)
    with Session(engine) as sess:
        sess.add(DBProject(user_info_id=1, name="Absent"))
        sess.add(DBProject(user_info_id=1, name="Fresh"))
        sess.commit()

    assert _inspect(client, _crafted(("local", "-777")), "Back") ==         _inspect(client, _crafted(("local", "-776")), "Back")
    assert _inspect(client, _crafted(("local", "-777")),
                    "Back")["duplicate_of"] is None
    real = _import_all(client, _crafted(("local", "-777")), "Back")
    absent = _import_all(client, _crafted(("local", "-776")), "Absent")
    assert real.status_code == absent.status_code == 200
    assert [i["name"] for i in real.json()["imported"]] ==         [i["name"] for i in absent.json()["imported"]] == ["Track 0"]
    assert real.json()["skipped"] == absent.json()["skipped"] == []
    single = _import_one(client, _crafted(("local", "-777")), "Fresh")
    assert single.status_code == 200, single.text


# ── Into another trip ─────────────────────────────────────────────────────────

def test_import_all_into_an_empty_trip_imports_as_today(env):
    """The importer's other trip holding the activities does not count."""
    client, engine, _ = env
    _create_trip(client, "Journey", [_RUN, _HIKE])

    r = _import_all(client, _export(client, "Journey"), "Back")

    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["name"] for i in body["imported"]] == ["Morning run", "Temple hike"]
    assert body["skipped"] == []
    rows = _rows(engine, "Back")
    assert [row.source for row in rows] == ["gpx", "gpx"]
    # Each new row keeps its own fingerprint, as before.
    assert all(row.source_id != _HIKE_FINGERPRINT for row in rows)


def test_an_identity_in_another_users_trip_matches_nothing(env):
    """A crafted file naming user 2's activities imports as if they did not
    exist: same answer as for ids that exist nowhere."""
    client, engine, as_user = env
    theirs = dict(_RUN, id=777, name="Their run")
    their_hike = dict(_HIKE, id=-778, name="Their hike", source_id="cd" * 32)
    as_user(2)
    _create_trip(client, "Theirs", [theirs, their_hike])
    as_user(1)

    with Session(engine) as sess:
        sess.add(DBProject(user_info_id=1, name="Absent"))
        sess.commit()
    real = _crafted(("strava", "777"), ("gpx", "cd" * 32))
    absent = _crafted(("strava", "778"), ("gpx", "ef" * 32))

    assert _inspect(client, _crafted(("strava", "777")), "Back") ==         _inspect(client, _crafted(("strava", "778")), "Back")
    assert _inspect(client, _crafted(("strava", "777")),
                    "Back")["duplicate_of"] is None
    answers = []
    for project, content in (("Back", real), ("Absent", absent)):
        r = _import_all(client, content, project)
        assert r.status_code == 200, r.text
        body = r.json()
        answers.append(([i["name"] for i in body["imported"]], body["skipped"]))

    assert answers[0] == answers[1] == (["Track 0", "Track 1"], [])
    # User 2's trip is untouched.
    assert [row.name for row in _rows(engine, "Theirs")] == [
        "Their run", "Their hike"]


def test_import_gpx_naming_another_users_activity_is_no_conflict(env):
    client, engine, as_user = env
    as_user(2)
    _create_trip(client, "Theirs", [dict(_RUN, id=777)])
    as_user(1)

    r = _import_one(client, _crafted(("strava", "777")), "Back")

    assert r.status_code == 200, r.text
    assert len(_rows(engine, "Back")) == 1


# ── The identity is untrusted ─────────────────────────────────────────────────

def _identity_elements(source, source_id):
    elements = []
    for tag, text in (("source", source), ("source_id", source_id)):
        if text is not None:
            element = ET.Element(f"{{{EXTENSION_NAMESPACE}}}{tag}")
            element.text = text
            elements.append(element)
    return elements


@pytest.mark.parametrize("source,source_id", [
    ("strava", "0"), ("strava", "-5"), ("strava", "0101"), ("strava", "1.5"),
    ("strava", str(2 ** 63)), ("strava", "9" * 40), ("strava", ""),
    ("gpx", "AB" * 32), ("gpx", "ab" * 31), ("gpx", "ab" * 33),
    ("gpx", "zz" * 32), ("gpx", "x" * 10_000),
    ("local", "101"), ("local", "0"), ("local", "-0"), ("local", "-05"),
    ("local", str(-(2 ** 63))), ("local", "-" + "9" * 40), ("local", "-1.5"),
    ("local", "--5"), ("local", "+5"), ("local", "-+5"), ("local", "-5.0"),
    ("local", " -5x"), ("other", "-5"), ("", "101"),
    ("strava", None), (None, "101"),
])
def test_a_malformed_identity_counts_as_absent(source, source_id):
    assert read_activity_identity(_identity_elements(source, source_id)) is None


@pytest.mark.parametrize("source,source_id", [
    ("strava", "1"), ("strava", str(2 ** 63 - 1)), ("gpx", "0f" * 32),
    ("local", "-1"), ("local", str(-(2 ** 53 - 1))),
    ("local", str(-(2 ** 62))), ("local", str(-(2 ** 63 - 1))),
])
def test_a_well_formed_identity_is_read(source, source_id):
    assert read_activity_identity(_identity_elements(source, source_id)) == \
        (source, source_id)


def test_a_file_without_an_identity_carries_none():
    content = (
        b'<?xml version="1.0"?><gpx version="1.1" creator="x" '
        b'xmlns="http://www.topografix.com/GPX/1/1"><trk><name>A</name><trkseg>'
        b'<trkpt lat="1" lon="1"/><trkpt lat="1.1" lon="1.1"/></trkseg></trk></gpx>')

    (candidate,) = candidates(parse_gpx_bytes(content))

    assert candidate.carried_identity is None

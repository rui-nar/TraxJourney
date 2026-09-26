"""An implausible elevation or elapsed time in a trip file is normalised (#462).

Before #462 the app stored what it was given: a GPX ``<ele>NaN</ele>``,
``inf`` or a device's ``65535`` sentinel in an activity's profile and
high/low, and a gain measured from them; an elapsed time decades long from
one stray timestamp. Its exports, and any backup made with them, hold those
values. Every file a past version wrote must still import, so the import
treats them as the writers now do (``repair_elevations``, ``repair_elapsed``
in src/models/track_edit.py) instead of refusing the file: an implausible
elevation becomes a missing one, a figure the app measured from it is
measured again, and the elapsed time falls back to the moving time.

Values no past writer could produce (a distance past 100,000 km, a count past
a billion, a coordinate off the globe) are still refused: see
test_import_value_schema.py.
"""
from __future__ import annotations

import copy
import json
import math

import pytest
from sqlmodel import Session, select

from models.project_db import DBActivity
from src.models.track_edit import clean_elevation_profile, elevation_gain
from src.project.project_io import ProjectIO
from tests.test_elevation_non_finite import _upload_gpx, app  # noqa: F401
from tests.test_import_value_schema import _TRIP, _import, env  # noqa: F401

_NAN, _INF = float("nan"), float("inf")
_DISTS = [0.0, 0.5, 1.0, 1.5, 2.0]
_CLEAN = [800.0, 810.0, 820.0, 830.0, 840.0]


def _trip(**activity) -> dict:
    doc = copy.deepcopy(_TRIP)
    doc["activities"][0].update(activity)
    return doc


def _bytes(doc) -> bytes:
    return json.dumps(doc).encode("utf-8")


def _row(engine) -> DBActivity:
    with Session(engine) as sess:
        return sess.exec(select(DBActivity)).one()


def _profile(elevs):
    # Copies: a case that edits a profile in place must not edit the next.
    return {"distances_km": list(_DISTS), "elevations_m": list(elevs)}


_SENTINELS = {
    "65535": 65535.0, "NaN": _NAN, "Infinity": _INF, "-Infinity": -_INF,
    "past 20 km": 20_001.0, "null": None,
}


@pytest.mark.parametrize("bad", _SENTINELS.values(), ids=_SENTINELS.keys())
def test_a_gpx_upload_with_an_implausible_reading_imports_remeasured(env, bad):
    client, engine = env
    broken = _CLEAN[:2] + [bad] + _CLEAN[3:]
    doc = _trip(source="gpx", elevation_profile=_profile(broken),
                total_elevation_gain=1e300 if bad is not None else 40.0,
                elev_high=65535.0 if bad is not None else 840.0, elev_low=800.0)

    r = _import(client, _bytes(doc))

    assert r.status_code == 201, r.text
    row = _row(engine)
    dists, elevs = clean_elevation_profile(_DISTS, broken)
    assert json.loads(row.elevation_profile_json) == {"distances_km": dists, "elevations_m": elevs}
    assert row.total_elevation_gain == pytest.approx(elevation_gain(elevs, dists))
    assert (row.elev_high, row.elev_low) == (840.0, 800.0)


def test_a_strava_activity_keeps_its_own_plausible_figures(env):
    client, engine = env
    doc = _trip(source=None, elevation_profile=_profile(_CLEAN[:2] + [65535] + _CLEAN[3:]),
                total_elevation_gain=55.0, elev_high=900.0, elev_low=790.0)

    r = _import(client, _bytes(doc))

    assert r.status_code == 201, r.text
    row = _row(engine)
    assert max(json.loads(row.elevation_profile_json)["elevations_m"]) == 840.0
    assert (row.total_elevation_gain, row.elev_high, row.elev_low) == (55.0, 900.0, 790.0)


def test_implausible_figures_without_a_profile_are_cleared(env):
    client, engine = env
    doc = _trip(elevation_profile=None, total_elevation_gain=_INF,
                elev_high=65535.0, elev_low=-30000.0)

    r = _import(client, _bytes(doc))

    assert r.status_code == 201, r.text
    row = _row(engine)
    assert (row.total_elevation_gain, row.elev_high, row.elev_low) == (0.0, None, None)


def test_an_elapsed_time_of_decades_falls_back_to_the_moving_time(env):
    """One stamp from 1970 in a GPX file made the span 54 years (#462)."""
    client, engine = env
    doc = _trip(source="gpx", moving_time=3600, elapsed_time=1_700_000_000)

    r = _import(client, _bytes(doc))

    assert r.status_code == 201, r.text
    assert (_row(engine).moving_time, _row(engine).elapsed_time) == (3600, 3600)


def test_the_repaired_trip_opens_and_exports_again(env):
    client, engine = env
    doc = _trip(source="gpx", elevation_profile=_profile([800.0, _NAN, 65535.0, 830.0, 840.0]),
                total_elevation_gain=_INF, elev_high=65535.0, elapsed_time=10 ** 12)
    assert _import(client, _bytes(doc)).status_code == 201

    exported = client.get("/api/projects/Trip/export-traxj")
    assert exported.status_code == 200, exported.text

    def _refuse(token):
        raise ValueError(token)
    json.loads(exported.content, parse_constant=_refuse)
    r = client.post("/api/projects/import", files={
        "file": (f"Again{ProjectIO.EXTENSION}", exported.content, "application/json")})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("profile", [
    {"distances_km": _DISTS, "elevations_m": [800.0, "high", 820.0, 830.0, 840.0]},
    {"distances_km": [0.0, -1.0, 1.0, 1.5, 2.0], "elevations_m": _CLEAN},
], ids=["text", "negative distance"])
def test_what_no_writer_made_is_still_refused(env, profile):
    client, _ = env

    r = _import(client, _bytes(_trip(elevation_profile=profile)))

    assert r.status_code == 400, r.text


def test_normalising_leaves_a_plausible_activity_untouched():
    doc = _trip(source="gpx", elevation_profile=_profile(_CLEAN))
    before = copy.deepcopy(doc)

    project = ProjectIO.from_dict(doc)

    assert doc == before
    assert project.activities[0].elevation_profile == (_DISTS, _CLEAN)
    assert math.isfinite(project.activities[0].total_elevation_gain)


def test_a_trip_stored_before_the_repair_exports_to_a_file_that_imports(env):
    """A row holding the sentinel (as one did until the repair migration ran)
    exports it as it is; the import makes it missing, as the writers would."""
    client, engine = env
    assert _import(client, _bytes(_trip(source="gpx"))).status_code == 201
    with Session(engine) as sess:
        row = sess.exec(select(DBActivity)).one()
        row.elev_high = 65535.0
        row.elevation_profile_json = json.dumps(_profile([800.0, 810.0, 65535.0, 830.0, 840.0]))
        sess.add(row)
        sess.commit()
    exported = client.get("/api/projects/Trip/export-traxj")
    assert b"65535" in exported.content

    r = client.post("/api/projects/import", files={
        "file": (f"Again{ProjectIO.EXTENSION}", exported.content, "application/json")})

    assert r.status_code == 201, r.text
    # The copy names the same activity, which is the importer's own and so is
    # kept as stored (the repair migration fixes the row itself); what the
    # file was read as is what a new owner's copy would store.
    act = ProjectIO.from_bytes(exported.content).activities[0]
    assert act.elev_high == 840.0
    assert max(act.elevation_profile[1]) == 840.0


_HUGE = "1" + "0" * 400      # json.loads reads it as an int past any float


@pytest.mark.parametrize("path, field", [
    (("elevation_profile", "elevations_m", 2), "activities[0].elevation_profile.elevations_m[2]"),
    (("elevation_profile", "distances_km", 2), "activities[0].elevation_profile.distances_km[2]"),
    (("total_elevation_gain",), "activities[0].total_elevation_gain"),
    (("elev_high",), "activities[0].elev_high"),
    (("elapsed_time",), "activities[0].elapsed_time"),
    (("moving_time",), "activities[0].moving_time"),
], ids=["profile elevation", "profile distance", "gain", "high", "elapsed", "moving"])
def test_an_integer_too_large_for_a_float_is_refused_not_a_500(env, path, field):
    """Normalising compares such a number with a float bound, which raised
    OverflowError before the file was ever judged."""
    client, _ = env
    doc = _trip(source="gpx", elevation_profile=_profile(list(_CLEAN)))
    node = doc["activities"][0]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = 7777777
    raw = _bytes(doc).replace(b"7777777", _HUGE.encode())

    r = _import(client, raw)

    assert r.status_code == 400, r.text
    assert f": {field} " in r.json()["detail"], r.json()["detail"]


# ── A split piece of a GPX upload (issue #462) ──────────────────────────────

def test_splitting_a_gpx_upload_keeps_the_source_on_the_tail(app):  # noqa: F811
    """The app measured both pieces' figures, so both say where they came
    from; a tail without it read as a Strava activity."""
    client, engine = app
    activity_id = _upload_gpx(client, [100.0, 110.0, 120.0, 130.0, 125.0, 118.0])

    r = client.post(f"/api/projects/Trip/activities/{activity_id}/split",
                    json={"split_index": 2})

    assert r.status_code == 200, r.text
    with Session(engine) as sess:
        rows = sess.exec(select(DBActivity)).all()
    assert len(rows) == 2
    assert {row.source for row in rows} == {"gpx"}


def test_a_tail_exported_without_its_source_is_measured_as_its_parent(env):
    """Tails split before the fix have no source; their parent in the file
    says whether the app measured them."""
    client, engine = env
    broken = _profile([800.0, 810.0, 65535.0, 830.0, 840.0])
    doc = _trip(source="gpx", elevation_profile=_profile(_CLEAN))
    tail = copy.deepcopy(doc["activities"][0])
    tail.update(id=-5, source=None, split_parent_id=1, elevation_profile=broken,
                total_elevation_gain=4e6, elev_high=9999.0, elev_low=800.0)
    doc["activities"].append(tail)
    doc["items"].append({"item_type": "activity", "activity_id": -5})

    r = _import(client, _bytes(doc))

    assert r.status_code == 201, r.text
    with Session(engine) as sess:
        row = sess.get(DBActivity, -5)
    dists, elevs = clean_elevation_profile(_DISTS, broken["elevations_m"])
    assert row.total_elevation_gain == pytest.approx(elevation_gain(elevs, dists))
    assert (row.elev_high, row.elev_low) == (840.0, 800.0)


# ── Legacy day notes (issue #462) ───────────────────────────────────────────

def _day_meta(engine):
    from models.project_db import DBProject
    with Session(engine) as sess:
        rows = sess.exec(select(DBProject)).all()
        return {r.name: json.loads(r.day_meta_json or "{}") for r in rows}


def test_a_day_note_stored_as_another_type_imports_as_text(env):
    """A day saved before its notes were typed may hold a number or a bare
    tag. The client reads each as text, so that is what they become; a
    value text cannot stand for is dropped."""
    client, engine = env
    doc = copy.deepcopy(_TRIP)
    doc["day_meta"] = {"2024-06-01": {"journal": 5, "sleeping": True, "difficulty": 2.5,
                                      "weather": ["clear"], "tags": "old"},
                       "2024-06-02": {"tags": ["a", 7, {"x": 1}]}}

    r = _import(client, _bytes(doc))

    assert r.status_code == 201, r.text
    days = _day_meta(engine)["Trip"]
    assert days["2024-06-01"]["journal"] == "5"
    assert days["2024-06-01"]["sleeping"] == "true"
    assert days["2024-06-01"]["difficulty"] == "2.5"
    assert days["2024-06-01"]["weather"] is None
    assert days["2024-06-01"]["tags"] == ["old"]
    assert days["2024-06-02"]["tags"] == ["a", "7"]


def test_a_trip_holding_a_legacy_day_exports_to_a_file_that_imports(env):
    client, engine = env
    assert _import(client, _bytes(_TRIP)).status_code == 201
    from models.project_db import DBProject
    with Session(engine) as sess:
        row = sess.exec(select(DBProject)).one()
        row.day_meta_json = json.dumps({"2024-05-01": {"journal": 5, "tags": "old"}})
        sess.add(row)
        sess.commit()
    exported = client.get("/api/projects/Trip/export-traxj")

    r = client.post("/api/projects/import", files={
        "file": (f"Again{ProjectIO.EXTENSION}", exported.content, "application/json")})

    assert r.status_code == 201, r.text


def test_a_day_note_that_is_not_finite_is_still_refused(env):
    client, _ = env
    doc = copy.deepcopy(_TRIP)
    doc["day_meta"] = {"2024-06-01": {"journal": float("nan")}}

    assert _import(client, _bytes(doc)).status_code == 400

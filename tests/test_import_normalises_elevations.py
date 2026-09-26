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
    return {"distances_km": _DISTS, "elevations_m": elevs}


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

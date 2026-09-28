"""No figure the app stores is past what the trip file allows (issue #462).

The trip-file import bounds every figure by physical plausibility with a large
margin (src/models/value_bounds.py): 100,000 km of distance, 31 years of time,
10,000 km of climbing, elevations within ±20 km. Two activities of 1e308 m
imported fine before, and then every load of the trip's totals was a 500: the
sum overflowed to Infinity.

So the app's own writers stay inside the same bounds, or no trip it holds
could be exported and imported back: a GPX upload or an edited track past
them is refused as implausible (a span of decades is repaired instead: see
test_gpx_stray_stamps.py), and an elevation outside them is no reading,
wherever it comes from. The totals never overflow whatever they sum.
"""
from __future__ import annotations

import json
import math
import pytest

from src.models.activity import Activity
from src.models.project import Project
import src.models.track_edit as track_edit
from src.models.track_edit import TrackPoint
from src.project.repo_core import _compute_stats
from tests.test_elevation_non_finite import _activity_row, _upload_gpx, app  # noqa: F401
from tests.test_gpx_import_api import _gpx_xml


def _all_finite(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_all_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_all_finite(v) for v in value)
    return True


# ── The totals ──────────────────────────────────────────────────────────────

def test_the_totals_never_overflow():
    def huge(aid):
        return Activity.from_strava_api({
            "id": aid, "name": "Ride", "type": "Ride", "distance": 1e308,
            "moving_time": 10 ** 308, "total_elevation_gain": 1e308,
            "start_date_local": "2024-06-01T10:00:00Z"})
    project = Project(name="Trip", activities=[huge(1), huge(2)])

    stats = _compute_stats(project)

    assert _all_finite(stats), stats
    json.dumps(stats, allow_nan=False)


# ── Elevation outside ±20 km is no reading ──────────────────────────────────

@pytest.mark.parametrize("elev", [20_000.5, -25_000.0, 1e300])
def test_a_track_point_holds_an_implausible_elevation_as_missing(elev):
    assert TrackPoint(lat=45.0, lng=6.0, elev=elev).elev is None
    assert TrackPoint(lat=45.0, lng=6.0, elev=-420.0).elev == -420.0   # the Dead Sea


def test_a_stream_altitude_outside_the_bounds_is_filled():
    dists, elevs = track_edit.elevation_profile_from_streams(
        [0.0, 100.0, 200.0], [100.0, 65535.0, 120.0])

    assert elevs == pytest.approx([100.0, 110.0, 120.0])


def test_a_strava_summary_keeps_no_implausible_elevation():
    act = Activity.from_strava_api({
        "id": 1, "name": "Ride", "type": "Ride",
        "total_elevation_gain": 2e7, "elev_high": 65535.0, "elev_low": -30000.0})

    assert act.total_elevation_gain == 0.0
    assert act.elev_high is None and act.elev_low is None


def test_a_gpx_upload_takes_an_implausible_ele_as_missing(app):  # noqa: F811
    client, engine = app
    activity_id = _upload_gpx(client, [100.0, 110.0, 99999.0, 130.0, 125.0])

    row = _activity_row(engine, activity_id)
    assert row.elev_high == 130.0
    assert max(json.loads(row.elevation_profile_json)["elevations_m"]) == 130.0


# ── A track past the bounds is implausible ──────────────────────────────────

#: Six hops between the Greenwich meridian and the antimeridian: 120,000 km.
_ZIGZAG = [(0.0, 0.0 if i % 2 == 0 else 179.9) for i in range(7)]


def _post_gpx(client, xml: str, **form):
    return client.post(
        "/api/projects/Trip/activities/import-gpx",
        files={"file": ("t.gpx", xml.encode(), "application/gpx+xml")}, data=form)


def test_a_gpx_track_past_100000_km_is_refused(app):  # noqa: F811
    client, engine = app
    xml = _gpx_xml([[[(lat, lng, 100.0) for lat, lng in _ZIGZAG]]])

    r = _post_gpx(client, xml, date="2024-06-01", start_time="09:00",
                  end_time="10:00", activity_type="Ride")

    assert r.status_code == 422, r.text
    assert "100,000 km" in json.dumps(r.json())
    # The preview says so before the upload.
    r = client.post("/api/projects/Trip/activities/gpx/inspect",
                    files={"file": ("t.gpx", xml.encode(), "application/gpx+xml")})
    assert r.status_code == 200, r.text
    assert any("100,000 km" in e for c in r.json()["candidates"] for e in c["errors"])


def test_an_edited_track_past_100000_km_is_refused(app):  # noqa: F811
    client, engine = app
    activity_id = _upload_gpx(client, [100.0, 110.0, 120.0])
    before = _activity_row(engine, activity_id).summary_polyline

    r = client.put(f"/api/projects/Trip/activities/{activity_id}/track", json={
        "points": [{"lat": lat, "lng": lng} for lat, lng in _ZIGZAG]})

    assert r.status_code == 422, r.text
    assert "100,000 km" in r.text
    assert _activity_row(engine, activity_id).summary_polyline == before


def test_a_track_inside_the_bounds_is_not_implausible():
    metrics = track_edit.recompute_track_metrics(
        [TrackPoint(45.0, 6.0, 500.0), TrackPoint(45.1, 6.1, 900.0)])

    assert track_edit.implausible_track(metrics) is None


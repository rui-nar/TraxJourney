"""A GPX clock that looks wrong is said, never refused or cut (issue #462).

A track's span is its earliest and latest stamps, as it always was, and
moving time comes from consecutive stamps, which already skips a gap longer
than MAX_SAMPLE_GAP_S. No stamp is left out of either: three attempts at
telling a clock error from a real stamp each broke a class of real files.

What is done instead:

* the preview WARNS when a stamp is before 2000-01-01 (1970, the 1980 GPS
  epoch) or more than a day in the future, or when the span passes the
  31-year bound, naming the odd stamps' dates. Never an error: the user
  reaches review and can set the date and times there;
* at import, an elapsed time past the bound is repaired as the trip-file
  import and the repair migration repair one (repair_elapsed: it becomes the
  moving time), unless the user set the times, which win.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from models.project_db import DBActivity
from src.gpx.importer import GpxCandidate, candidates, parse_gpx_bytes
from src.models.track_edit import TrackPoint
from tests.test_gpx_inspect_api import _HEADER, _import, _inspect, env  # noqa: F401

_START = datetime(2024, 8, 12, 7, 33, 0, tzinfo=timezone.utc)
_1970 = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: 14 m every 5 s: 2.8 m/s, moving throughout.
_STEP_M, _STEP_S = 14.0, 5


def _track(stamps):
    parts = [_HEADER, "<trk><name>Ride</name><type>cycling</type><trkseg>"]
    for i, when in enumerate(stamps):
        lat = 46.0 + i * (_STEP_M / 111320.0)
        parts.append(f'<trkpt lat="{lat}" lon="6.0"><ele>{500 + i % 7}</ele>'
                     f'<time>{when.strftime("%Y-%m-%dT%H:%M:%S")}Z</time></trkpt>')
    parts += ["</trkseg></trk></gpx>"]
    return "".join(parts).encode("utf-8")


def _ride(count=40, start=_START):
    return [start + timedelta(seconds=i * _STEP_S) for i in range(count)]


def _candidate(stamps):
    (candidate,) = candidates(parse_gpx_bytes(_track(stamps)))
    return candidate


def _preview(client, stamps):
    r = _inspect(client, _track(stamps))
    assert r.status_code == 200, r.text
    (candidate,) = r.json()["candidates"]
    assert candidate["errors"] == []
    return candidate


def _stored(engine) -> DBActivity:
    with Session(engine) as sess:
        return sess.exec(select(DBActivity)).one()


def test_an_archive_ride_from_1998_keeps_its_moving_time_and_speed(env):
    client, engine, *_ = env
    ride = _ride(start=datetime(1998, 5, 3, 9, tzinfo=timezone.utc))

    candidate = _preview(client, ride)
    assert candidate["moving_seconds"] == 39 * _STEP_S
    assert any("1998-05-03" in w for w in candidate["warnings"])

    r = _import(client, _track(ride), activity_type="Ride")
    assert r.status_code == 200, r.text
    act = _stored(engine)
    assert act.start_date.startswith("1998-05-03")
    assert (act.moving_time, act.elapsed_time) == (39 * _STEP_S, 39 * _STEP_S)
    assert act.average_speed > 2.0


def test_a_ride_with_one_stamp_from_2015_loses_no_stamp(env):
    """Nothing is dropped: its span runs from 2015, and its moving time is
    the ride's own, less the steps into and out of the stray stamp."""
    client, *_ = env
    stamps = _ride(20)
    stamps[10] = datetime(2015, 3, 1, tzinfo=timezone.utc)
    candidate = _candidate(stamps)

    assert candidate.time_span[0].year == 2015
    assert 15 * _STEP_S <= candidate.moving_seconds <= 17 * _STEP_S
    assert _preview(client, stamps)["errors"] == []


def test_a_1970_first_stamp_warns_and_moving_time_is_intact(env):
    client, engine, *_ = env
    stamps = [_1970] + _ride()

    candidate = _preview(client, stamps)
    assert candidate["moving_seconds"] == 39 * _STEP_S
    (warning,) = candidate["warnings"]
    assert "1970-01-01" in warning and "31 years" in warning

    r = _import(client, _track(stamps), activity_type="Ride")
    assert r.status_code == 200, r.text
    act = _stored(engine)
    # The 54-year span is past the bound: repaired as a stored one is.
    assert (act.moving_time, act.elapsed_time) == (39 * _STEP_S, 39 * _STEP_S)


def test_times_the_user_sets_win(env):
    client, engine, *_ = env

    r = _import(client, _track([_1970] + _ride()), activity_type="Ride",
                date="2024-08-12", start_time="07:33", end_time="09:00")

    assert r.status_code == 200, r.text
    act = _stored(engine)
    assert act.start_date.startswith("2024-08-12")
    assert act.elapsed_time == 87 * 60


def test_a_stamp_in_the_future_warns(env):
    client, *_ = env
    stamps = _ride()
    stamps[-1] = datetime(2099, 12, 31, tzinfo=timezone.utc)

    assert any("2099-12-31" in w for w in _preview(client, stamps)["warnings"])


def test_a_track_pausing_for_45_days_keeps_its_whole_clock(env):
    client, *_ = env
    first = _ride(300, start=datetime(2024, 6, 1, 8, tzinfo=timezone.utc))
    second = _ride(400, start=datetime(2024, 7, 16, 8, tzinfo=timezone.utc))
    candidate = _candidate(first + second)

    assert candidate.time_span == (first[0], second[-1])
    assert candidate.moving_seconds == (299 + 399) * _STEP_S
    preview = _preview(client, first + second)
    assert preview["warnings"] == []
    assert preview["started_at"].startswith("2024-06-01")


def _crafted(n):
    """The review's file, scaled: runs of 4 stamps 31 days apart among a long
    ride's points, which made the dropped clock rule quadratic."""
    times, points = [], []
    stray = datetime(2001, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        if i % 100 < 4:
            times.append(stray + timedelta(days=31 * (i // 100), seconds=i % 100))
        else:
            times.append(_START + timedelta(seconds=i))
        points.append(TrackPoint(lat=46.0 + i * 1e-5, lng=6.0, elev=500.0))
    return GpxCandidate(index=0, name=None, activity_type=None, points=points,
                        times=times, is_route=False)


def test_moving_time_takes_linear_time():
    def cost(n):
        candidate = _crafted(n)
        began = time.perf_counter()
        candidate.moving_seconds
        return time.perf_counter() - began

    cost(5_000)                                  # warm up
    small, large = min(cost(20_000) for _ in range(3)), min(cost(40_000) for _ in range(3))
    assert large / small < 3.0, (small, large)


def _track_with_times(times):
    parts = [_HEADER, "<trk><name>Ride</name><type>cycling</type><trkseg>"]
    for i, when in enumerate(times):
        lat = 46.0 + i * (_STEP_M / 111320.0)
        parts.append(f'<trkpt lat="{lat}" lon="6.0"><ele>500</ele><time>{when}</time></trkpt>')
    parts += ["</trkseg></trk></gpx>"]
    return "".join(parts).encode("utf-8")


def test_a_stamp_at_the_edge_of_the_calendar_is_a_clear_refusal_not_a_500(env):
    """A time zone can push a stamp near year 1 or 9999 past what a date
    holds once it is turned into UTC. That used to be an unhandled error."""
    client, engine, *_ = env
    ride = [(_START + timedelta(seconds=i * _STEP_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for i in range(20)]
    for times in (["0001-01-01T00:30:00+02:00"] + ride, ride + ["9999-12-31T23:59:59-02:00"]):
        r = _import(client, _track_with_times(times), activity_type="Ride")
        assert r.status_code == 422, r.text
        assert "date and times" in r.text
    with Session(engine) as sess:
        assert sess.exec(select(DBActivity)).first() is None

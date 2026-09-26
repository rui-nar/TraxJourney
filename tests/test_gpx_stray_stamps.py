"""A clock error in a GPX file does not decide the track's dates (issue #462).

A track's span was its earliest and latest stamps, so one point stamped
1970-01-01 (a device whose clock had not synced yet) made a morning ride 54
years long, which is implausible, and the preview listed it as an error that
kept the user from the form where the times can be corrected.

Only stamps that are clearly a clock error are left out now: any before
2000-01-01 (1970, the GPS epoch of 1980) or more than a day in the future, and
a tiny run of stamps (at most 3, under 5% of them) more than 30 days from the
rest. Everything else is the track's clock, however long it pauses. Whenever
a stamp is left out, the preview says so, with how many and their dates, and
moving time is counted over the stamps kept.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from models.project_db import DBActivity
from src.gpx.importer import candidates, parse_gpx_bytes
from tests.test_gpx_inspect_api import _HEADER, _import, _inspect, env  # noqa: F401

_START = datetime(2024, 8, 12, 7, 33, 0, tzinfo=timezone.utc)
_1970 = datetime(1970, 1, 1, tzinfo=timezone.utc)
_1980 = datetime(1980, 1, 6, tzinfo=timezone.utc)


def _track(stamps):
    parts = [_HEADER, "<trk><name>Ride</name><type>cycling</type><trkseg>"]
    for i, when in enumerate(stamps):
        lat = 46.0 + i * (14.0 / 111320.0)
        parts.append(f'<trkpt lat="{lat}" lon="6.0"><ele>{500 + i % 7}</ele>'
                     f'<time>{when.strftime("%Y-%m-%dT%H:%M:%S")}Z</time></trkpt>')
    parts += ["</trkseg></trk></gpx>"]
    return "".join(parts).encode("utf-8")


def _ride(count=40, start=_START, step=1):
    return [start + timedelta(seconds=i * step) for i in range(count)]


def _candidate(stamps):
    (candidate,) = candidates(parse_gpx_bytes(_track(stamps)))
    return candidate


def test_a_lone_1970_stamp_is_left_out_and_said_so(env):
    client, *_ = env
    assert _candidate([_1970] + _ride()).time_span == (_START, _START + timedelta(seconds=39))

    r = _inspect(client, _track([_1970] + _ride()))

    assert r.status_code == 200, r.text
    (candidate,) = r.json()["candidates"]
    assert candidate["errors"] == []
    assert candidate["started_at"].startswith("2024-08-12")
    assert candidate["elapsed_seconds"] == 39
    (warning,) = candidate["warnings"]
    assert "1 timestamp" in warning and "1970-01-01" in warning


def test_stray_stamps_anywhere_are_left_out():
    stamps = _ride()
    stamps[20] = _1980
    stamps[30] = datetime(2099, 12, 31, tzinfo=timezone.utc)
    stamps[35] = datetime(2015, 3, 1, tzinfo=timezone.utc)   # plausible year, lone
    candidate = _candidate(stamps)

    assert candidate.time_span == (_START, _START + timedelta(seconds=39))
    count, first, last = candidate.left_out_stamps
    assert (count, first.year, last.year) == (3, 1980, 2099)


def test_a_track_pausing_for_45_days_keeps_its_whole_clock(env):
    """Two real stretches a month and a half apart are one track: neither is
    a clock error, and neither may be cut, nor the moving time with it."""
    client, *_ = env
    first = _ride(300, start=datetime(2024, 6, 1, 8, tzinfo=timezone.utc), step=5)
    second = _ride(400, start=datetime(2024, 7, 16, 8, tzinfo=timezone.utc), step=5)
    candidate = _candidate(first + second)

    assert candidate.time_span == (first[0], second[-1])
    assert candidate.left_out_stamps is None
    assert candidate.moving_seconds == (299 + 399) * 5

    (inspected,) = _inspect(client, _track(first + second)).json()["candidates"]
    assert inspected["warnings"] == []
    assert inspected["started_at"].startswith("2024-06-01")


def test_an_unsynced_majority_does_not_date_the_track(env):
    """500 points stamped 1980 before the clock synced, then 300 real ones."""
    client, engine, *_ = env
    stamps = _ride(500, start=_1980) + _ride(300)
    candidate = _candidate(stamps)

    assert candidate.time_span == (_START, _START + timedelta(seconds=299))
    assert candidate.moving_seconds <= 299
    (inspected,) = _inspect(client, _track(stamps)).json()["candidates"]
    assert any("500 timestamps" in w and "1980-01-06" in w for w in inspected["warnings"])

    r = _import(client, _track(stamps), activity_type="Ride")
    assert r.status_code == 200, r.text
    with Session(engine) as sess:
        act = sess.exec(select(DBActivity)).one()
    assert act.start_date.startswith("2024-08-12")
    assert act.elapsed_time == 299


def test_two_equal_stretches_are_both_kept_whatever_their_order():
    """No tie to break: runs of real size are never left out, so the file's
    point order cannot change the answer."""
    a = _ride(3, start=datetime(2024, 1, 10, tzinfo=timezone.utc))
    b = _ride(3, start=datetime(2024, 3, 10, tzinfo=timezone.utc))

    assert _candidate(a + b).time_span == (a[0], b[-1])
    assert _candidate(b + a).time_span == (a[0], b[-1])
    assert _candidate(a + b).left_out_stamps is None


def test_a_file_whose_every_stamp_is_a_clock_error_has_no_clock(env):
    """Then the user is asked for the date and times, as for a route."""
    client, *_ = env
    candidate = _candidate(_ride(40, start=_1980))

    assert candidate.time_span is None
    (inspected,) = _inspect(client, _track(_ride(40, start=_1980))).json()["candidates"]
    assert inspected["started_at"] is None
    assert any("40 timestamps" in w for w in inspected["warnings"])

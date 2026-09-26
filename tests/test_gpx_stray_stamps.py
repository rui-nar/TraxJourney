"""A stray timestamp does not make a GPX track decades long (issue #462).

A track's span was its earliest and latest stamps, so one point stamped
1970-01-01 (a device whose clock had not yet synced) made a morning ride 54
years long. Since #462 an activity longer than 31 years is implausible, and
the preview listed that as an error, which the import dialog treats as "this
track cannot be imported": the user never reached the form where the date and
times can be corrected.

Now the span is taken from the track's clock proper: stamps more than
MAX_CLOCK_GAP_S from any other are a separate clock, and the one holding the
most stamps is the track's. A span that is still implausible is a warning in
the preview, not an error, and the import itself judges the times it is
finally given, the user's own when they typed them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from models.project_db import DBActivity
from src.gpx.importer import candidates, parse_gpx_bytes
from tests.test_gpx_inspect_api import _HEADER, _import, _inspect, env  # noqa: F401

_START = datetime(2024, 8, 12, 7, 33, 0, tzinfo=timezone.utc)
_1970 = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _track(stamps):
    parts = [_HEADER, "<trk><name>Ride</name><type>cycling</type><trkseg>"]
    for i, when in enumerate(stamps):
        lat = 46.0 + i * (14.0 / 111320.0)
        parts.append(f'<trkpt lat="{lat}" lon="6.0"><ele>{500 + i % 7}</ele>'
                     f'<time>{when.strftime("%Y-%m-%dT%H:%M:%S")}Z</time></trkpt>')
    parts += ["</trkseg></trk></gpx>"]
    return "".join(parts).encode("utf-8")


def _ride(count=40):
    return [_START + timedelta(seconds=i) for i in range(count)]


def _span(content):
    (candidate,) = candidates(parse_gpx_bytes(content))
    return candidate.time_span


def test_a_stray_1970_stamp_is_not_the_start():
    assert _span(_track([_1970] + _ride())) == (_START, _START + timedelta(seconds=39))


def test_a_stray_stamp_anywhere_is_left_out():
    stamps = _ride()
    stamps[20] = datetime(1980, 1, 6, tzinfo=timezone.utc)   # the GPS epoch
    stamps[30] = datetime(2099, 12, 31, tzinfo=timezone.utc)
    assert _span(_track(stamps)) == (_START, _START + timedelta(seconds=39))


def test_a_long_trip_with_nights_between_keeps_its_whole_span():
    """Days apart is one trip; only a gap past MAX_CLOCK_GAP_S splits it."""
    stamps = [_START + timedelta(days=d, seconds=s) for d in range(10) for s in range(4)]
    assert _span(_track(stamps)) == (stamps[0], stamps[-1])


def test_the_preview_of_a_track_with_a_stray_stamp_is_importable(env):
    client, *_ = env

    r = _inspect(client, _track([_1970] + _ride()))

    assert r.status_code == 200, r.text
    (candidate,) = r.json()["candidates"]
    assert candidate["errors"] == []
    assert candidate["warnings"] == []
    assert candidate["started_at"].startswith("2024-08-12")
    assert candidate["elapsed_seconds"] == 39


def test_it_imports_on_its_own_clock(env):
    client, engine, *_ = env

    r = _import(client, _track([_1970] + _ride()), activity_type="Ride")

    assert r.status_code == 200, r.text
    with Session(engine) as sess:
        act = sess.exec(select(DBActivity)).one()
    assert act.start_date.startswith("2024-08-12")
    assert act.elapsed_time == 39


def _decades():
    """A clock that really does run for decades: a stamp every 20 days."""
    return [_START - timedelta(days=20 * i) for i in range(600)][::-1]


def test_an_implausible_span_is_a_warning_in_the_preview(env):
    client, *_ = env

    r = _inspect(client, _track(_decades()))

    assert r.status_code == 200, r.text
    (candidate,) = r.json()["candidates"]
    assert candidate["errors"] == []
    assert any("31 years" in w for w in candidate["warnings"])


def test_the_import_judges_the_times_it_is_given(env):
    client, engine, *_ = env

    r = _import(client, _track(_decades()), activity_type="Ride")
    assert r.status_code == 422, r.text
    assert "31 years" in r.text

    r = _import(client, _track(_decades()), activity_type="Ride",
                date="2024-08-12", start_time="07:33", end_time="09:00")
    assert r.status_code == 200, r.text
    with Session(engine) as sess:
        assert sess.exec(select(DBActivity)).one().elapsed_time == 87 * 60

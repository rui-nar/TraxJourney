"""The zone a GPX track was recorded in, and its times there (issue #365).

The import used to store a track's UTC instant as its local time, labelled
"UTC": a ride in Tokyo showed nine hours off, and a time typed for it was read
as UTC. These tests cover the zone helper (src/gpx/timezone.py) and what the
inspect and import endpoints now do with it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.billing.entitlements import trip_days_used
from src.gpx.importer import candidates, parse_gpx_bytes
from src.gpx.timezone import UTC_ZONE, local_to_utc, to_local, zone_at
from src.project.local_ids import track_fingerprint

TOKYO = (35.68, 139.69)
LISBON = (38.72, -9.14)
AT_SEA = (40.0, -30.0)          # mid-Atlantic, west of the Azores
NULL_ISLAND = (0.0, 0.0)        # Gulf of Guinea, on the prime meridian


# ── The helper ────────────────────────────────────────────────────────────────

class TestZoneAt:
    def test_tokyo(self):
        assert zone_at(*TOKYO) == "Asia/Tokyo"

    def test_lisbon(self):
        assert zone_at(*LISBON) == "Europe/Lisbon"

    def test_at_sea_is_a_nautical_zone(self):
        assert zone_at(*AT_SEA) == "Etc/GMT+2"

    def test_at_sea_on_utc_is_etc_utc(self):
        """The lookup says Etc/GMT there; a zone that is UTC is written one
        way, and never as the bare "UTC" kept for pre-release rows."""
        assert zone_at(*NULL_ISLAND) == UTC_ZONE == "Etc/UTC"

    @pytest.mark.parametrize("lat, lon", [(None, None), (None, 2.0), (48.0, None)])
    def test_a_missing_coordinate(self, lat, lon):
        assert zone_at(lat, lon) == "Etc/UTC"

    def test_a_coordinate_off_the_globe(self):
        assert zone_at(10.0, 200.0) == "Etc/UTC"


class TestToLocal:
    def test_tokyo(self):
        instant = datetime(2024, 8, 12, 0, 0, tzinfo=timezone.utc)
        assert to_local(instant, "Asia/Tokyo") == datetime(2024, 8, 12, 9, 0)

    def test_lisbon_in_summer(self):
        instant = datetime(2024, 7, 1, 12, 0, tzinfo=timezone.utc)
        assert to_local(instant, "Europe/Lisbon") == datetime(2024, 7, 1, 13, 0)

    def test_lisbon_in_winter(self):
        instant = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
        assert to_local(instant, "Europe/Lisbon") == datetime(2024, 1, 15, 12, 0)

    def test_a_naive_instant_is_utc(self):
        assert to_local(datetime(2024, 8, 12, 0, 0), "Asia/Tokyo") == \
            datetime(2024, 8, 12, 9, 0)


class TestLocalToUtc:
    def test_tokyo(self):
        assert local_to_utc(datetime(2024, 8, 12, 9, 0), "Asia/Tokyo") == \
            datetime(2024, 8, 12, 0, 0, tzinfo=timezone.utc)

    def test_lisbon_in_summer_and_winter(self):
        assert local_to_utc(datetime(2024, 7, 1, 13, 0), "Europe/Lisbon") == \
            datetime(2024, 7, 1, 12, 0, tzinfo=timezone.utc)
        assert local_to_utc(datetime(2024, 1, 15, 12, 0), "Europe/Lisbon") == \
            datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)

    def test_a_time_in_the_dst_gap_moves_forward_by_the_gap(self):
        """Lisbon jumps from 01:00 to 02:00 on 2024-03-31: 01:30 never shows,
        and is read as 02:30 local, which is 01:30Z."""
        instant = local_to_utc(datetime(2024, 3, 31, 1, 30), "Europe/Lisbon")
        assert instant == datetime(2024, 3, 31, 1, 30, tzinfo=timezone.utc)
        assert to_local(instant, "Europe/Lisbon") == datetime(2024, 3, 31, 2, 30)

    def test_a_time_in_the_gap_ignores_fold(self):
        assert local_to_utc(datetime(2024, 3, 31, 1, 30), "Europe/Lisbon",
                            fold=1) == \
            datetime(2024, 3, 31, 1, 30, tzinfo=timezone.utc)

    def test_a_repeated_time_takes_the_first_occurrence(self):
        """Lisbon goes back from 02:00 to 01:00 on 2024-10-27: 01:30 shows
        twice, at 00:30Z (summer time) and 01:30Z (winter time)."""
        assert local_to_utc(datetime(2024, 10, 27, 1, 30), "Europe/Lisbon") == \
            datetime(2024, 10, 27, 0, 30, tzinfo=timezone.utc)

    def test_the_second_occurrence_on_request(self):
        assert local_to_utc(datetime(2024, 10, 27, 1, 30), "Europe/Lisbon",
                            fold=1) == \
            datetime(2024, 10, 27, 1, 30, tzinfo=timezone.utc)


# ── The endpoints ─────────────────────────────────────────────────────────────

_HEADER = ('<?xml version="1.0"?>'
           '<gpx version="1.1" creator="test" '
           'xmlns="http://www.topografix.com/GPX/1/1">')


def _track(where, start=None, count=40, seconds=10, name="Walk"):
    """A walk of *count* points north from *where*, stamped from *start*
    every *seconds*, or unstamped when *start* is None."""
    lat0, lon0 = where
    parts = [_HEADER, "<trk>", f"<name>{name}</name>",
             "<type>walking</type>", "<trkseg>"]
    when = start
    for i in range(count):
        stamp = (f"<time>{when.strftime('%Y-%m-%dT%H:%M:%SZ')}</time>"
                 if when is not None else "")
        parts.append(f'<trkpt lat="{lat0 + i * 0.0001}" lon="{lon0}">'
                     f"<ele>10</ele>{stamp}</trkpt>")
        if when is not None:
            when += timedelta(seconds=seconds)
    parts += ["</trkseg>", "</trk>", "</gpx>"]
    return "".join(parts).encode("utf-8")


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def env(monkeypatch):
    """In-memory DB + the real app (for its 402 handler), as the owner of
    "Trip"."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", engine)
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_TRIP_DAYS", "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        sess.add(UserInfo(id=1, email="owner@example.com", display_name="Owner"))
        sess.add(DBProject(id=1, user_info_id=1, name="Trip"))
        sess.commit()
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "1", "email": "owner@example.com"}
    try:
        yield TestClient(app), engine
    finally:
        app.dependency_overrides.clear()


def _inspect(client, content):
    return client.post("/api/projects/Trip/activities/gpx/inspect",
                       files={"file": ("t.gpx", content, "application/gpx+xml")})


def _import(client, content, **fields):
    return client.post(
        "/api/projects/Trip/activities/import-gpx",
        files={"file": ("t.gpx", content, "application/gpx+xml")},
        data={k: v for k, v in fields.items() if v is not None})


def _row(engine, resp):
    assert resp.status_code == 200, resp.text
    with Session(engine) as sess:
        row = sess.get(DBActivity, resp.json()["activity_id"])
    # No import writes the bare label kept for pre-release rows (Decision 5).
    assert row.timezone != "UTC"
    return row


class TestInspect:
    def test_started_at_is_unchanged_and_local_times_are_added(self, env):
        client, _ = env
        content = _track(TOKYO, start=_utc(2024, 8, 12, 0, 0))
        today = candidates(parse_gpx_bytes(content))[0].time_span

        only = _inspect(client, content).json()["candidates"][0]

        # Byte-identical to what installed clients read before #365.
        assert only["started_at"] == today[0].isoformat() == \
            "2024-08-12T00:00:00+00:00"
        assert only["ended_at"] == today[1].isoformat()
        assert only["timezone"] == "Asia/Tokyo"
        assert only["start_local"] == "2024-08-12T09:00:00"
        assert only["end_local"] == "2024-08-12T09:06:30"

    def test_an_untimed_track_has_a_zone_but_no_local_times(self, env):
        client, _ = env
        only = _inspect(client, _track(TOKYO)).json()["candidates"][0]

        assert only["started_at"] is None
        assert only["timezone"] == "Asia/Tokyo"
        assert only["start_local"] is None and only["end_local"] is None

    def test_each_track_has_its_own_zone(self, env):
        client, _ = env
        lisbon = _track(LISBON, start=_utc(2024, 8, 12, 10, 0)).decode()
        tokyo = _track(TOKYO, start=_utc(2024, 8, 12, 0, 0)).decode()
        both = lisbon.replace("</gpx>", tokyo[tokyo.index("<trk>"):])

        found = _inspect(client, both.encode()).json()["candidates"]

        assert [c["timezone"] for c in found] == ["Europe/Lisbon", "Asia/Tokyo"]
        assert [c["start_local"] for c in found] == \
            ["2024-08-12T11:00:00", "2024-08-12T09:00:00"]


class TestImportStampedTrack:
    def test_the_file_gives_the_instant_the_wall_clock_and_the_zone(self, env):
        client, engine = env
        row = _row(engine, _import(client, _track(TOKYO, start=_utc(2024, 8, 12, 0, 0))))

        assert row.start_date == "2024-08-12T00:00:00Z"
        assert row.start_date_local == "2024-08-12T09:00:00Z"
        assert row.timezone == "Asia/Tokyo"

    def test_typed_local_times_are_wall_clock_in_the_zone(self, env):
        client, engine = env
        content = _track(TOKYO, start=_utc(2024, 8, 12, 3, 0))

        row = _row(engine, _import(client, content, date="2024-08-12",
                                   start_time="09:00", end_time="10:00",
                                   times_local="true"))

        assert row.start_date == "2024-08-12T00:00:00Z"
        assert row.start_date_local == "2024-08-12T09:00:00Z"
        assert row.elapsed_time == 3600

    def test_the_installed_client_request_keeps_the_instant_it_showed(self, env):
        """Installed clients show started_at in UTC and send what they show,
        without times_local. That has to stay the instant they showed (R1-1),
        now with the right local time beside it."""
        client, engine = env
        content = _track(TOKYO, start=_utc(2024, 8, 12, 3, 0))

        row = _row(engine, _import(client, content, date="2024-08-12",
                                   start_time="00:00", end_time="01:00"))

        assert row.start_date == "2024-08-12T00:00:00Z"
        assert row.start_date_local == "2024-08-12T09:00:00Z"
        assert row.timezone == "Asia/Tokyo"

    def test_a_typed_start_equal_to_the_files_keeps_its_exact_instant(self, env):
        """The track starts at 01:30:20Z on 2024-10-27, in Lisbon's second
        01:30 (clocks went back at 02:00). The client sends the start it
        prefilled and a new end. Read afresh, 01:30 would be the FIRST
        occurrence, an hour earlier (R2-2)."""
        client, engine = env
        content = _track(LISBON, start=_utc(2024, 10, 27, 1, 30, 20))

        row = _row(engine, _import(client, content, date="2024-10-27",
                                   start_time="01:30", end_time="02:00",
                                   times_local="true"))

        assert row.start_date == "2024-10-27T01:30:20Z"
        assert row.start_date_local == "2024-10-27T01:30:20Z"
        assert row.elapsed_time == 29 * 60 + 40
        assert row.timezone == "Europe/Lisbon"

    def test_an_end_in_the_repeated_hour_lands_after_the_start(self, env):
        client, engine = env
        content = _track(LISBON, start=_utc(2024, 10, 27, 1, 30, 20))

        row = _row(engine, _import(client, content, date="2024-10-27",
                                   start_time="01:30", end_time="01:45",
                                   times_local="true"))

        assert row.start_date == "2024-10-27T01:30:20Z"
        assert row.elapsed_time == 14 * 60 + 40

    def test_times_typed_over_a_clock_at_the_calendars_edge(self, env):
        """Tokyo's wall clock at these stamps is past year 9999. The preview
        asks for times instead; the ones typed must import, not trip over the
        file's own."""
        client, engine = env
        content = _track(TOKYO, start=_utc(9999, 12, 31, 20, 0))

        only = _inspect(client, content).json()["candidates"][0]
        assert only["start_local"] is None

        row = _row(engine, _import(client, content, date="2024-08-12",
                                   start_time="09:00", end_time="10:00",
                                   times_local="true"))
        assert row.start_date == "2024-08-12T00:00:00Z"

    def test_tracks_in_two_zones_each_keep_their_own(self, env):
        """10:00Z is the 12th in Lisbon; 20:00Z the same day is the 13th in
        Tokyo. The trip spans two local days."""
        client, engine = env
        lisbon = _row(engine, _import(client, _track(LISBON, start=_utc(2024, 8, 12, 10, 0))))
        tokyo = _row(engine, _import(client, _track(TOKYO, start=_utc(2024, 8, 12, 20, 0))))

        assert (lisbon.timezone, tokyo.timezone) == ("Europe/Lisbon", "Asia/Tokyo")
        assert lisbon.start_date_local.startswith("2024-08-12T11:00")
        assert tokyo.start_date_local.startswith("2024-08-13T05:00")
        with Session(engine) as sess:
            assert trip_days_used(sess, 1) == 2

    def test_the_trip_days_quota_sees_local_dates(self, env, monkeypatch):
        """With a one-day limit, the Tokyo track is refused: in UTC it falls
        on the Lisbon track's day, but its local date is the next one."""
        client, engine = env
        monkeypatch.setenv("BILLING_ENABLED", "1")
        monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
        monkeypatch.setenv("FREE_MAX_TRIP_DAYS", "1")
        _row(engine, _import(client, _track(LISBON, start=_utc(2024, 8, 12, 10, 0))))

        resp = _import(client, _track(TOKYO, start=_utc(2024, 8, 12, 20, 0)))

        assert resp.status_code == 402, resp.text
        assert resp.json()["resource"] == "trip_days"

    def test_a_track_on_utc_is_labelled_etc_utc(self, env):
        client, engine = env
        row = _row(engine, _import(client, _track(NULL_ISLAND, start=_utc(2024, 8, 12, 10, 0))))

        assert row.timezone == "Etc/UTC"
        assert row.start_date_local == row.start_date == "2024-08-12T10:00:00Z"


class TestImportUntimedTrack:
    @pytest.mark.parametrize("times_local", [None, "true", "false"])
    def test_typed_times_are_wall_clock_in_the_zone(self, env, times_local):
        """Installed clients prefill nothing for a track without stamps and
        send the clock the user picked, so with or without times_local it is
        the wall clock where the track is (R2-1, R3-1)."""
        client, engine = env

        row = _row(engine, _import(client, _track(TOKYO), date="2024-08-12",
                                   start_time="20:00", end_time="21:00",
                                   times_local=times_local))

        assert row.start_date == "2024-08-12T11:00:00Z"
        assert row.start_date_local == "2024-08-12T20:00:00Z"
        assert row.timezone == "Asia/Tokyo"
        assert row.elapsed_time == 3600

    def test_a_typed_time_in_the_dst_gap_is_kept_as_typed(self, env):
        """start_date_local shows what was typed; start_date is the instant
        the clocks moved to."""
        client, engine = env

        row = _row(engine, _import(client, _track(LISBON), date="2024-03-31",
                                   start_time="01:30", end_time="03:00"))

        assert row.start_date == "2024-03-31T01:30:00Z"
        assert row.start_date_local == "2024-03-31T01:30:00Z"
        assert row.elapsed_time == 30 * 60

    @pytest.mark.parametrize("times_local", [None, "true"])
    def test_a_pre_release_import_is_still_a_duplicate(self, env, times_local):
        """Before #365 an untimed track was fingerprinted on its typed wall
        clock labelled UTC. Re-importing it with the same typed times, from
        either client, must still be recognised (R4-1)."""
        client, engine = env
        content = _track(TOKYO)
        points = candidates(parse_gpx_bytes(content))[0].points
        old = track_fingerprint(((p.lat, p.lng) for p in points),
                                _utc(2024, 8, 12, 20, 0).isoformat())
        with Session(engine) as sess:
            sess.add(DBActivity(
                id=-4242, user_info_id=1, name="Old import",
                start_date="2024-08-12T20:00:00Z",
                start_date_local="2024-08-12T20:00:00Z",
                timezone="UTC", source="gpx", source_id=old))
            sess.add(DBProjectItem(project_id=1, position=0,
                                   item_type="activity", activity_id=-4242))
            sess.commit()

        resp = _import(client, content, date="2024-08-12", start_time="20:00",
                       end_time="21:00", times_local=times_local)

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["activity_id"] == -4242

    def test_the_same_route_reimported_is_a_duplicate(self, env):
        client, engine = env
        first = _import(client, _track(TOKYO), date="2024-08-12",
                        start_time="20:00", end_time="21:00")
        _row(engine, first)

        again = _import(client, _track(TOKYO), date="2024-08-12",
                        start_time="20:00", end_time="21:00",
                        times_local="true")

        assert again.status_code == 409, again.text


def test_no_import_writes_the_bare_utc_label(env):
    """Every row any test above wrote went through _row's check; this one
    sweeps the table after one import of each kind."""
    client, engine = env
    _row(engine, _import(client, _track(NULL_ISLAND, start=_utc(2024, 8, 12, 10, 0))))
    _row(engine, _import(client, _track(AT_SEA), date="2024-08-12",
                         start_time="10:00", end_time="11:00"))
    with Session(engine) as sess:
        zones = sess.exec(select(DBActivity.timezone)).all()
    assert zones and "UTC" not in zones

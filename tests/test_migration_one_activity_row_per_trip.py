"""Data-migration test for b3e9d71c4a58 — one activity row per trip (#592).

Builds a throwaway SQLite DB up to the revision before it, seeds trips sharing
activity rows the ways production data does (a Strava ride in two trips, a
"Keep both" import, a split made in one of two trips, a companion's activity),
runs the migration and checks the plan's Decision 10 end state: every
referenced row held by one trip, no split link crossing trips, the trip's
owner owning its rows, and leaked splits repaired. Hermetic — never touches
the developer's real db.
"""
from __future__ import annotations

import json
import logging
import logging.config
import math
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, insert, text

from models.project_db import DBActivity, DBProject, DBProjectItem
from src.project.elevation_downsample import downsample_elevation

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BEFORE = "a8d3f5c2e917"
_REVISION = "b3e9d71c4a58"

_OWNER, _COMPANION, _OTHER = 1, 2, 3
_FULL_TRACK = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
_CUT_TRACK = "_p~iF~ps|U_ulLnnqC"
_ENVELOPE = "v1.d3JhcHBlZA==.Y2lwaGVy"
_LOG = "alembic.runtime.migration"


def _profile(n: int) -> str:
    return json.dumps({
        "distances_km": [i * 0.1 for i in range(n)],
        "elevations_m": [500 + 100 * math.sin(i / 7) for i in range(n)],
    })


_FULL_PROFILE = _profile(800)
_CUT_PROFILE = _profile(400)


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "one_row_per_trip.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["url"] = url
    command.upgrade(config, _BEFORE)
    return config


@pytest.fixture()
def engine(cfg):
    eng = create_engine(cfg.attributes["url"])
    yield eng
    eng.dispose()


@pytest.fixture()
def log(caplog, monkeypatch):
    # alembic/env.py runs fileConfig on every command, which strips the root
    # handler caplog listens on; the migration's logging is what is tested.
    monkeypatch.setattr(logging.config, "fileConfig", lambda *a, **k: None)
    caplog.set_level(logging.INFO, logger=_LOG)
    return caplog


def _insert(engine, table_name: str, obj) -> None:
    """Insert *obj* with only the columns the table has AT _BEFORE, as
    test_migration_prune_orphaned_tails does: the ORM instance supplies every
    NOT NULL default, and strava_id / strava_user_id do not exist yet."""
    data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_sa_")}
    tbl = Table(table_name, MetaData(), autoload_with=engine)
    data = {k: v for k, v in data.items() if k in tbl.columns}
    with engine.begin() as conn:
        conn.execute(insert(tbl), data)


def _trip(engine, trip_id: int, owner: int = _OWNER, *, shared: bool = False) -> None:
    _insert(engine, "project", DBProject(
        id=trip_id, user_info_id=owner, name=f"trip {trip_id}", lock_version=5,
        stats_json='{"cached": true}', share_token="tok" if shared else None))


def _activity(engine, row_id: int, owner: int = _OWNER, **kwargs) -> None:
    kwargs.setdefault("name", f"activity {row_id}")
    kwargs.setdefault("type", "Ride")
    _insert(engine, "activity", DBActivity(id=row_id, user_info_id=owner, **kwargs))


def _prepared(engine, row_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO activity_geo_prepared (activity_id, version, blob) "
            "VALUES (:a, 1, :b)"), {"a": row_id, "b": b"cut"})


def _item(engine, item_id: int, trip: int, row_id: int) -> None:
    _insert(engine, "projectitem", DBProjectItem(
        id=item_id, project_id=trip, position=item_id, uid=f"u{item_id}",
        item_type="activity", activity_id=row_id))


def _split_head(engine, row_id: int, owner: int = _OWNER, **kwargs) -> None:
    """A head cut by a split: the cut track, a first-edit snapshot of the full
    one, and the base name its pieces are numbered from."""
    fields = dict(
        name="Alps (1/2)", split_base_name="Alps", is_edited=True,
        summary_polyline=_CUT_TRACK, elevation_profile_json=_CUT_PROFILE,
        elevation_profile_low_res_json=_CUT_PROFILE,
        distance=40_000.0, moving_time=4_000, elapsed_time=5_000,
        average_speed=10.0, total_elevation_gain=300.0,
        original_polyline=_FULL_TRACK, original_elevation_profile_json=_FULL_PROFILE,
        original_total_elevation_gain=900.0, original_distance=80_000.0,
        original_moving_time=8_000, original_elapsed_time=10_000,
        original_average_speed=10.0, original_elev_high=600.0,
        original_elev_low=400.0, original_start_latlng_json="[38.5, -120.2]",
        original_end_latlng_json="[43.2, -126.4]")
    fields.update(kwargs)
    _activity(engine, row_id, owner, **fields)
    _prepared(engine, row_id)


def _tail(engine, row_id: int, head: int, parent: int | None = None, **kwargs) -> None:
    _activity(engine, row_id, name="Alps (2/2)", split_root_id=head,
              split_parent_id=head if parent is None else parent, **kwargs)


def _upgrade(cfg) -> None:
    command.upgrade(cfg, _REVISION)


def _row(engine, row_id: int) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(
            text("SELECT * FROM activity WHERE id = :id"), {"id": row_id}
        ).mappings().one())


def _row_of(engine, trip: int, item_id: int) -> dict:
    with engine.connect() as conn:
        row_id = conn.execute(text(
            "SELECT activity_id FROM projectitem WHERE id = :i AND project_id = :t"),
            {"i": item_id, "t": trip}).scalar_one()
    return _row(engine, row_id)


def _has_prepared(engine, row_id: int) -> bool:
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT 1 FROM activity_geo_prepared WHERE activity_id = :a"),
            {"a": row_id}).first() is not None


def _trips(engine) -> dict:
    with engine.connect() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(text(
            "SELECT id, lock_version, stats_json FROM project"))}


def _assert_invariants(engine) -> None:
    """Decision 10's end state: every referenced row held by one trip, no
    split link naming a row of another trip, no item on a missing row."""
    with engine.connect() as conn:
        holders = {}
        for row_id, trip in conn.execute(text(
                "SELECT activity_id, project_id FROM projectitem "
                "WHERE item_type = 'activity'")):
            holders.setdefault(row_id, set()).add(trip)
        assert all(len(t) == 1 for t in holders.values()), holders
        for row_id, (trip,) in holders.items():
            row = conn.execute(text(
                "SELECT split_root_id, split_parent_id FROM activity WHERE id = :id"),
                {"id": row_id}).first()
            assert row is not None, f"item on missing row {row_id}"
            for link in row:
                assert link is None or holders.get(link) == {trip}, (
                    f"row {row_id} of trip {trip} links to {link}, held by "
                    f"{holders.get(link)}")


# -- revision chain -----------------------------------------------------------

def test_it_follows_the_strava_code_binding_and_leaves_one_head():
    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1
    assert script.get_revision(_REVISION).down_revision == _BEFORE


# -- backfill -----------------------------------------------------------------

def test_backfill_sets_strava_identity_on_strava_rows_only(cfg, engine):
    _trip(engine, 10)
    _activity(engine, 1001, _COMPANION)                       # Strava, NULL source
    _activity(engine, 1002, source="strava")
    _activity(engine, -7, source="gpx")                       # GPX import
    _activity(engine, -8)                                     # another account's file
    for item, row_id in enumerate((1001, 1002, -7, -8), start=1):
        _item(engine, item, 10, row_id)

    _upgrade(cfg)

    assert (_row(engine, 1001)["strava_id"], _row(engine, 1001)["strava_user_id"]) \
        == (1001, _COMPANION)
    assert _row(engine, 1002)["strava_id"] == 1002
    for local in (-7, -8):
        assert (_row(engine, local)["strava_id"], _row(engine, local)["strava_user_id"]) \
            == (None, None)


# -- copy ---------------------------------------------------------------------

def test_a_strava_row_shared_by_two_trips_becomes_two_rows(cfg, engine):
    _trip(engine, 10)                       # the owner's trip
    _trip(engine, 20, _COMPANION)           # the companion's own trip
    # The companion's ride: added to their own trip first, then to the owner's.
    _activity(engine, 5001, _COMPANION, name="Col du Galibier",
              summary_polyline=_FULL_TRACK)
    _prepared(engine, 5001)
    _item(engine, 1, 20, 5001)
    _item(engine, 2, 10, 5001)

    _upgrade(cfg)

    kept = _row_of(engine, 20, 1)
    copy = _row_of(engine, 10, 2)
    assert kept["id"] == 5001, "the lowest item id keeps the original"
    assert copy["id"] != 5001 and copy["id"] > 0
    assert copy["strava_id"] == 5001
    assert copy["strava_user_id"] == _COMPANION
    assert copy["user_info_id"] == _OWNER
    assert (copy["name"], copy["summary_polyline"]) == ("Col du Galibier", _FULL_TRACK)
    assert _has_prepared(engine, copy["id"])
    assert (kept["strava_id"], kept["user_info_id"]) == (5001, _COMPANION)
    _assert_invariants(engine)


def test_a_gpx_row_shared_by_two_trips_gets_a_negative_copy(cfg, engine):
    _trip(engine, 10)
    _trip(engine, 20)
    _activity(engine, -42, source="gpx", source_id="fp")
    _item(engine, 1, 10, -42)
    _item(engine, 2, 20, -42)

    _upgrade(cfg)

    copy = _row_of(engine, 20, 2)
    assert copy["id"] < 0 and copy["id"] != -42
    assert (copy["source"], copy["source_id"], copy["strava_id"]) == ("gpx", "fp", None)
    _assert_invariants(engine)


def test_a_split_family_held_whole_by_two_trips_is_remapped_per_trip(cfg, engine):
    # A "Keep both" import of a split trip: both trips hold head and tails.
    _trip(engine, 10)
    _trip(engine, 20)
    _split_head(engine, 900)
    _tail(engine, -1, 900)
    _tail(engine, -2, 900, parent=-1)
    for item, row_id in enumerate((900, -1, -2), start=1):
        _item(engine, item, 10, row_id)
        _item(engine, item + 10, 20, row_id)

    _upgrade(cfg)

    head_b, tail1_b, tail2_b = (_row_of(engine, 20, i) for i in (11, 12, 13))
    assert {head_b["id"], tail1_b["id"], tail2_b["id"]}.isdisjoint({900, -1, -2})
    assert (tail1_b["split_root_id"], tail1_b["split_parent_id"]) == (head_b["id"], head_b["id"])
    assert (tail2_b["split_root_id"], tail2_b["split_parent_id"]) == (head_b["id"], tail1_b["id"])
    assert _row(engine, -2)["split_parent_id"] == -1
    # Both trips split it: neither head is restored.
    assert head_b["summary_polyline"] == _CUT_TRACK
    assert _row(engine, 900)["summary_polyline"] == _CUT_TRACK
    _assert_invariants(engine)


# -- leaked splits ------------------------------------------------------------

def test_the_split_made_in_the_newer_trip_stays_and_the_other_head_is_restored(
        cfg, engine, log):
    _trip(engine, 10, shared=True)  # B: added the ride first, never split it
    _trip(engine, 20)               # A: added it later and split it
    _split_head(engine, 900)
    _tail(engine, -1, 900)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, 900)
    _item(engine, 3, 20, -1)

    _upgrade(cfg)

    # A keeps its cut head and its tail, on the copy it got.
    head_a = _row_of(engine, 20, 2)
    assert head_a["id"] != 900
    assert (head_a["summary_polyline"], head_a["name"]) == (_CUT_TRACK, "Alps (1/2)")
    assert (head_a["is_edited"], head_a["split_base_name"]) == (1, "Alps")
    assert _has_prepared(engine, head_a["id"])
    tail = _row(engine, -1)
    assert (tail["split_root_id"], tail["split_parent_id"]) == (head_a["id"], head_a["id"])

    # B, which kept the original row, gets the full track back.
    head_b = _row(engine, 900)
    assert head_b["summary_polyline"] == _FULL_TRACK
    assert head_b["elevation_profile_json"] == _FULL_PROFILE
    assert head_b["name"] == "Alps"
    assert (head_b["split_root_id"], head_b["split_parent_id"],
            head_b["split_base_name"]) == (None, None, None)
    assert (head_b["distance"], head_b["moving_time"], head_b["elapsed_time"]) \
        == (80_000.0, 8_000, 10_000)
    assert (head_b["total_elevation_gain"], head_b["elev_high"]) == (900.0, 600.0)
    assert head_b["start_latlng_json"] == "[38.5, -120.2]"
    assert head_b["is_edited"] == 0
    assert head_b["original_polyline"] is None
    assert head_b["original_distance"] is None
    # R1-3 and R3-2: no stale prepared blob, a low-res profile of the full one.
    assert not _has_prepared(engine, 900)
    full = json.loads(_FULL_PROFILE)
    dd, ee = downsample_elevation(full["distances_km"], full["elevations_m"])
    assert json.loads(head_b["elevation_profile_low_res_json"]) \
        == {"distances_km": dd, "elevations_m": ee}
    assert any("restored split head 900 in trip 10" in m and "share link" in m
               for m in log.messages)
    _assert_invariants(engine)


def test_the_split_made_in_the_older_trip_stays_and_the_copy_is_restored(cfg, engine):
    _trip(engine, 10)   # A: split it, and keeps the original row
    _trip(engine, 20)   # B: never split it, gets the copy
    _split_head(engine, 900)
    _tail(engine, -1, 900)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 10, -1)
    _item(engine, 3, 20, 900)

    _upgrade(cfg)

    head_a = _row(engine, 900)
    assert (head_a["summary_polyline"], head_a["name"]) == (_CUT_TRACK, "Alps (1/2)")
    assert _row(engine, -1)["split_root_id"] == 900
    head_b = _row_of(engine, 20, 3)
    assert head_b["id"] != 900
    assert (head_b["summary_polyline"], head_b["name"]) == (_FULL_TRACK, "Alps")
    assert not _has_prepared(engine, head_b["id"])
    assert _has_prepared(engine, 900)
    _assert_invariants(engine)


def test_a_restore_without_a_gain_snapshot_recomputes_the_gain_as_reset_does(
        cfg, engine):
    from src.models.track_edit import align_points, recompute_track_metrics

    _trip(engine, 10)
    _trip(engine, 20)
    _split_head(engine, 900, original_total_elevation_gain=None)
    _tail(engine, -1, 900)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, 900)
    _item(engine, 3, 20, -1)

    _upgrade(cfg)

    full = json.loads(_FULL_PROFILE)
    expected = recompute_track_metrics(align_points(
        _FULL_TRACK, (full["distances_km"], full["elevations_m"]))).total_elevation_gain
    assert _row(engine, 900)["total_elevation_gain"] == pytest.approx(expected)


def test_a_head_left_only_in_b_whose_tails_only_a_holds_is_restored(cfg, engine):
    # Split in A, then A's head removed: its row survived because B holds it.
    _trip(engine, 10)   # A
    _trip(engine, 20)   # B
    _split_head(engine, 900)
    _tail(engine, -1, 900)
    _item(engine, 1, 10, -1)
    _item(engine, 2, 20, 900)

    _upgrade(cfg)

    assert _row(engine, 900)["summary_polyline"] == _FULL_TRACK
    tail = _row(engine, -1)
    assert (tail["split_root_id"], tail["split_parent_id"],
            tail["split_base_name"]) == (None, None, None)
    _assert_invariants(engine)


def test_a_head_in_a_and_b_whose_tails_only_c_holds_is_restored_in_both(cfg, engine):
    for trip in (10, 20, 30):
        _trip(engine, trip)
    _split_head(engine, 900)
    _tail(engine, -1, 900)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, 900)
    _item(engine, 3, 30, -1)

    _upgrade(cfg)

    for trip, item in ((10, 1), (20, 2)):
        head = _row_of(engine, trip, item)
        assert head["summary_polyline"] == _FULL_TRACK
        assert head["name"] == "Alps"
    assert _row(engine, -1)["split_root_id"] is None
    _assert_invariants(engine)


def test_an_encrypted_head_is_left_unrestored_and_logged(cfg, engine, log):
    _trip(engine, 10)
    _trip(engine, 20)
    _split_head(engine, 900, name=_ENVELOPE, split_base_name=None,
                summary_polyline=_ENVELOPE, original_polyline=_ENVELOPE,
                original_elevation_profile_json=_ENVELOPE)
    _tail(engine, -1, 900)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, 900)
    _item(engine, 3, 20, -1)

    _upgrade(cfg)

    head = _row(engine, 900)
    assert (head["name"], head["summary_polyline"]) == (_ENVELOPE, _ENVELOPE)
    assert head["original_polyline"] == _ENVELOPE
    assert any("split head 900 in trip 10 left as it is: its original is encrypted"
               in m for m in log.messages)
    _assert_invariants(engine)


def test_a_users_own_split_whose_tail_they_deleted_is_untouched(cfg, engine):
    _trip(engine, 10)
    _split_head(engine, 900)
    _tail(engine, -1, 900)          # its item removed: the row is left behind
    _item(engine, 1, 10, 900)
    before = _row(engine, 900)

    _upgrade(cfg)

    after = _row(engine, 900)
    assert {k: v for k, v in after.items() if not k.startswith("strava_")} == before
    assert _has_prepared(engine, 900)
    assert _trips(engine)[10] == (5, '{"cached": true}')


def test_a_shared_head_no_trip_holds_a_tail_of_is_left_in_every_trip(cfg, engine, log):
    _trip(engine, 10)
    _trip(engine, 20)
    _split_head(engine, 900)
    _tail(engine, -1, 900)          # held by nobody
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, 900)

    _upgrade(cfg)

    for trip, item in ((10, 1), (20, 2)):
        head = _row_of(engine, trip, item)
        assert (head["summary_polyline"], head["name"]) == (_CUT_TRACK, "Alps (1/2)")
    assert sum("no trip holds a tail" in m for m in log.messages) == 2
    _assert_invariants(engine)


def test_a_shared_head_split_then_reset_is_left_without_aborting(cfg, engine, log):
    _trip(engine, 10)
    _trip(engine, 20)
    # Reset nulled the snapshot but kept split_base_name; a later split's tail
    # is held by trip 20.
    _activity(engine, 900, name="Alps", split_base_name="Alps", is_edited=False,
              summary_polyline=_CUT_TRACK)
    _tail(engine, -1, 900)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, 900)
    _item(engine, 3, 20, -1)

    _upgrade(cfg)

    assert _row(engine, 900)["summary_polyline"] == _CUT_TRACK
    assert any("split head 900 in trip 10 left as it is: no snapshot" in m
               for m in log.messages)
    _assert_invariants(engine)


def test_keep_both_then_a_reset_deletes_the_dangling_items_first(cfg, engine, log):
    # "Keep both" shared head and tails; a reset in trip 10 deleted the tail
    # rows, so trip 20's items on them point at nothing.
    _trip(engine, 10)
    _trip(engine, 20)
    _activity(engine, 900, name="Alps", summary_polyline=_FULL_TRACK)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, 900)
    _item(engine, 3, 20, -1)
    _item(engine, 4, 20, -2)

    _upgrade(cfg)

    with engine.connect() as conn:
        items = {r[0] for r in conn.execute(text("SELECT id FROM projectitem"))}
    assert items == {1, 2}
    assert _row_of(engine, 20, 2)["id"] != 900
    assert any("trip 20: 2 item(s) on a missing activity deleted" in m
               for m in log.messages)
    _assert_invariants(engine)


def test_a_tail_whose_root_another_trip_holds_is_re_rooted_on_its_parent(cfg, engine):
    # Trip 20 holds the middle piece and its child, not the head.
    _trip(engine, 10)
    _trip(engine, 20)
    _split_head(engine, 900)
    _tail(engine, -1, 900)
    _tail(engine, -2, 900, parent=-1)
    _tail(engine, -3, 900, parent=-2)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, -1)
    _item(engine, 3, 20, -3)

    _upgrade(cfg)

    assert (_row(engine, -1)["split_root_id"], _row(engine, -1)["split_parent_id"]) \
        == (None, None)
    # -3's parent -2 is held by nobody: its nearest held ancestor is -1.
    assert (_row(engine, -3)["split_root_id"], _row(engine, -3)["split_parent_id"]) \
        == (-1, -1)
    _assert_invariants(engine)


def test_a_piece_whose_parent_chain_dead_ends_while_its_root_is_held_warns(
        cfg, engine, log):
    # Guard for U1R1-1: no known path leaves this state; the migration still
    # detaches the piece, but says the trip held its root.
    _trip(engine, 10)
    _split_head(engine, 900)
    _tail(engine, -1, 900, parent=-99)      # -99 is no row at all
    _item(engine, 1, 10, 900)
    _item(engine, 2, 10, -1)

    _upgrade(cfg)

    assert (_row(engine, -1)["split_root_id"], _row(engine, -1)["split_parent_id"]) \
        == (None, None)
    assert [r.getMessage() for r in log.records
            if r.name == _LOG and r.levelno == logging.WARNING] == [
        "one row per trip: split piece -1 in trip 10 detached although the trip "
        "holds its root 900 (parent chain dead-ends)"]
    assert any("detached_with_held_root=1" in m for m in log.messages)


# -- a piece split again (IR1-1) ----------------------------------------------

def _sub_split(engine, *, piece_original: str = _FULL_TRACK) -> None:
    """ "Keep both" of a split trip (head 900, piece -1, both shared by trips
    10 and 20), then -1 split again in trip 20 only: -1 is cut and every
    name was renumbered over the whole family, trip 10's rows included."""
    _trip(engine, 10)
    _trip(engine, 20)
    _split_head(engine, 900, name="Alps (1/3)", start_date="2026-05-01T08:00:00Z")
    _activity(engine, -1, name="Alps (2/3)", split_root_id=900, split_parent_id=900,
              start_date="2026-05-01T10:00:00Z", is_edited=True,
              summary_polyline=_CUT_TRACK, elevation_profile_json=_CUT_PROFILE,
              elevation_profile_low_res_json=_CUT_PROFILE, distance=20_000.0,
              moving_time=2_000, total_elevation_gain=100.0,
              original_polyline=piece_original,
              original_elevation_profile_json=_FULL_PROFILE,
              original_total_elevation_gain=400.0, original_distance=40_000.0,
              original_moving_time=4_000, original_elapsed_time=5_000,
              original_average_speed=10.0, original_elev_high=600.0,
              original_elev_low=400.0)
    _prepared(engine, -1)
    _activity(engine, -2, name="Alps (3/3)", split_root_id=900, split_parent_id=-1,
              start_date="2026-05-01T12:00:00Z")
    _item(engine, 1, 10, 900)
    _item(engine, 2, 10, -1)
    _item(engine, 3, 20, 900)
    _item(engine, 4, 20, -1)
    _item(engine, 5, 20, -2)


def test_a_piece_split_again_in_one_trip_is_restored_in_the_other(cfg, engine, log):
    _sub_split(engine)

    _upgrade(cfg)

    # Trip 10 never split -1: restored, still a piece of its own family, and
    # the family it holds renumbered without trip 20's piece.
    piece_a = _row(engine, -1)
    assert (piece_a["summary_polyline"], piece_a["elevation_profile_json"]) \
        == (_FULL_TRACK, _FULL_PROFILE)
    assert (piece_a["distance"], piece_a["moving_time"], piece_a["total_elevation_gain"]) \
        == (40_000.0, 4_000, 400.0)
    assert (piece_a["split_root_id"], piece_a["split_parent_id"]) == (900, 900)
    assert (piece_a["is_edited"], piece_a["original_polyline"]) == (0, None)
    assert not _has_prepared(engine, -1)
    full = json.loads(_FULL_PROFILE)
    dd, ee = downsample_elevation(full["distances_km"], full["elevations_m"])
    assert json.loads(piece_a["elevation_profile_low_res_json"]) \
        == {"distances_km": dd, "elevations_m": ee}
    assert (_row(engine, 900)["name"], piece_a["name"]) == ("Alps (1/2)", "Alps (2/2)")
    assert _row(engine, 900)["summary_polyline"] == _CUT_TRACK

    # Trip 20 made the split: its copies keep it, linked among themselves.
    head_b, piece_b, cut_b = (_row_of(engine, 20, i) for i in (3, 4, 5))
    assert (piece_b["summary_polyline"], piece_b["name"]) == (_CUT_TRACK, "Alps (2/3)")
    assert head_b["name"] == "Alps (1/3)"
    assert (piece_b["split_root_id"], piece_b["split_parent_id"]) \
        == (head_b["id"], head_b["id"])
    assert (cut_b["split_root_id"], cut_b["split_parent_id"]) \
        == (head_b["id"], piece_b["id"])
    assert any("restored split piece -1 in trip 10, which never split it" in m
               for m in log.messages)
    _assert_invariants(engine)


def test_a_piece_split_again_with_an_encrypted_original_is_left_and_logged(
        cfg, engine, log):
    _sub_split(engine, piece_original=_ENVELOPE)

    _upgrade(cfg)

    piece_a = _row(engine, -1)
    assert (piece_a["summary_polyline"], piece_a["name"]) == (_CUT_TRACK, "Alps (2/3)")
    assert piece_a["original_polyline"] == _ENVELOPE
    assert _row(engine, 900)["name"] == "Alps (1/3)"
    assert any("shared split piece -1 in trip 10 left as it is: its original "
               "is encrypted" in m for m in log.messages)
    _assert_invariants(engine)


def test_a_piece_split_again_held_by_one_trip_is_untouched(cfg, engine):
    _trip(engine, 10)
    _split_head(engine, 900, name="Alps (1/3)")
    _activity(engine, -1, name="Alps (2/3)", split_root_id=900, split_parent_id=900,
              is_edited=True, summary_polyline=_CUT_TRACK,
              original_polyline=_FULL_TRACK, original_distance=40_000.0)
    _activity(engine, -2, name="Alps (3/3)", split_root_id=900, split_parent_id=-1)
    for item, row_id in enumerate((900, -1, -2), start=1):
        _item(engine, item, 10, row_id)
    before = {r: _row(engine, r) for r in (900, -1, -2)}

    _upgrade(cfg)

    for row_id, row in before.items():
        assert {k: v for k, v in _row(engine, row_id).items()
                if not k.startswith("strava_")} == row
    assert _trips(engine)[10] == (5, '{"cached": true}')


def test_a_second_run_after_a_piece_repair_changes_nothing(cfg, engine):
    _sub_split(engine)
    _upgrade(cfg)
    first = _dump(engine)

    command.stamp(cfg, _BEFORE)
    _upgrade(cfg)

    assert _dump(engine) == first


# -- a shared head cut again (IR2-1) ------------------------------------------

_CUT_AGAIN = "head cut again by another trip's split"


def _keep_both_split(engine) -> None:
    """ "Keep both" of a split trip: head 900 and piece -1 shared by trips 10
    and 20."""
    _trip(engine, 10)
    _trip(engine, 20)
    _split_head(engine, 900, name="Alps (1/3)")
    _tail(engine, -1, 900)
    _item(engine, 1, 10, 900)
    _item(engine, 2, 10, -1)
    _item(engine, 3, 20, 900)
    _item(engine, 4, 20, -1)


def test_a_shared_head_cut_again_by_another_trip_is_left_and_logged(cfg, engine, log):
    _keep_both_split(engine)
    # Trip 20 split the shared head again: -2 was cut out of 900.
    _tail(engine, -2, 900)
    _item(engine, 5, 20, -2)
    before = _row(engine, 900)

    _upgrade(cfg)

    after = _row(engine, 900)
    assert {k: v for k, v in after.items() if not k.startswith("strava_")} == before
    assert [m for m in log.messages if _CUT_AGAIN in m] == [
        "one row per trip: shared split head 900 in trip 10 left as it is: head "
        "cut again by another trip's split; no snapshot of the state before "
        "that cut"]
    _assert_invariants(engine)


def test_a_shared_head_both_trips_cut_alike_logs_no_second_cut(cfg, engine, log):
    _keep_both_split(engine)

    _upgrade(cfg)

    assert not [m for m in log.messages if _CUT_AGAIN in m]
    _assert_invariants(engine)


# -- ownership ----------------------------------------------------------------

def test_a_companions_plaintext_row_goes_to_the_trip_owner(cfg, engine):
    _trip(engine, 10)
    _activity(engine, 5001, _COMPANION)
    _item(engine, 1, 10, 5001)

    _upgrade(cfg)

    row = _row(engine, 5001)
    assert (row["user_info_id"], row["strava_user_id"]) == (_OWNER, _COMPANION)
    assert _trips(engine)[10] == (6, None)


def test_a_companions_encrypted_row_keeps_its_owner_and_is_logged(cfg, engine, log):
    _trip(engine, 10)
    _trip(engine, 20, _COMPANION)
    _activity(engine, 5001, _COMPANION, name=_ENVELOPE)
    _activity(engine, 5002, _COMPANION, summary_polyline=_ENVELOPE)
    _item(engine, 1, 20, 5001)
    _item(engine, 2, 10, 5001)      # copied into trip 10
    _item(engine, 3, 10, 5002)

    _upgrade(cfg)

    copy = _row_of(engine, 10, 2)
    assert copy["user_info_id"] == _COMPANION
    assert _row(engine, 5002)["user_info_id"] == _COMPANION
    for row_id in (copy["id"], 5002):
        assert any(f"activity {row_id} in trip 10 holds encrypted fields and "
                   f"stays with its owner {_COMPANION}" in m for m in log.messages)


# -- bookkeeping --------------------------------------------------------------

def test_only_touched_trips_are_bumped_and_marked_stale(cfg, engine):
    _trip(engine, 10)               # gets a copy
    _trip(engine, 20)               # keeps the original: untouched
    _trip(engine, 30)               # a dangling item
    _trip(engine, 40)               # nothing shared
    _trip(engine, 50, _OTHER)       # a row to take over
    _activity(engine, 5001)
    _activity(engine, 5002)
    _activity(engine, 5003, _COMPANION)
    _item(engine, 1, 20, 5001)
    _item(engine, 2, 10, 5001)
    _item(engine, 3, 30, 777)
    _item(engine, 4, 40, 5002)
    _item(engine, 5, 50, 5003)

    _upgrade(cfg)

    trips = _trips(engine)
    for touched in (10, 30, 50):
        assert trips[touched] == (6, None)
    for untouched in (20, 40):
        assert trips[untouched] == (5, '{"cached": true}')


def _dump(engine) -> dict:
    with engine.connect() as conn:
        return {
            table: sorted(tuple(r) for r in conn.execute(text(f"SELECT * FROM {table}")))
            for table in ("activity", "activity_geo_prepared", "projectitem", "project")
        }


def _seed_everything(engine) -> None:
    for trip in (10, 20, 30):
        _trip(engine, trip)
    _split_head(engine, 900)
    _tail(engine, -1, 900)
    _activity(engine, 5001, _COMPANION)
    _activity(engine, 5002, _COMPANION, name=_ENVELOPE)
    _activity(engine, -42, source="gpx")
    _item(engine, 1, 10, 900)
    _item(engine, 2, 20, 900)
    _item(engine, 3, 20, -1)
    _item(engine, 4, 10, 5001)
    _item(engine, 5, 30, 5001)
    _item(engine, 6, 10, 5002)
    _item(engine, 7, 20, -42)
    _item(engine, 8, 30, -42)
    _item(engine, 9, 30, 4040)      # dangling


def test_a_second_run_changes_nothing(cfg, engine):
    _seed_everything(engine)
    _upgrade(cfg)
    _assert_invariants(engine)
    first = _dump(engine)

    command.stamp(cfg, _BEFORE)     # downgrade raises; rewind bookkeeping only
    _upgrade(cfg)

    assert _dump(engine) == first


def test_a_run_interrupted_after_adding_the_columns_can_be_run_again(cfg, engine):
    _seed_everything(engine)
    # pysqlite autocommits the ADD COLUMN before the data transaction opens.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE activity ADD COLUMN strava_id BIGINT"))
        conn.execute(text("ALTER TABLE activity ADD COLUMN strava_user_id INTEGER"))

    _upgrade(cfg)

    _assert_invariants(engine)
    assert _row(engine, 5001)["strava_id"] == 5001


def test_downgrade_raises_naming_the_backup(cfg):
    _upgrade(cfg)
    with pytest.raises(RuntimeError, match="backup"):
        command.downgrade(cfg, _BEFORE)

"""Data-migration test for a442d0e1f2b3 — drop heart-rate data (issue #442).

Builds a throwaway SQLite DB at a revision two steps BEFORE the migration's
own parent (so the intermediate migrations run in the same pass, as they do on
a real install that skipped a release), seeds activity rows carrying heart
rate and a raw Strava cache blob carrying it, upgrades to head, and asserts
that nothing heart-rate-shaped survives while everything else does. Hermetic
— never touches the developer's real db.
"""
import json
import tracemalloc
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, inspect, MetaData, Table, text

from models.project_db import DBActivity, DBProject, DBProjectItem

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OLD_REV = "d2b7e4c81f59"       # well before the drop's down_revision
_PREV_REV = "b8c4e2f19a37"      # down_revision of the drop
_DROP_REV = "a442d0e1f2b3"

_HR_COLUMNS = {
    "has_heartrate", "heartrate_opt_out", "display_hide_heartrate_option",
    "average_heartrate", "max_heartrate",
}


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "drop_hr_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    return db_path


def _seed_row(engine, table_name: str, obj, **extra) -> None:
    """Insert *obj* using only the columns the table has at the seeded revision.

    The ORM instance supplies every NOT NULL column its model default; the
    reflected historical table filters the dict to the columns that exist
    there, and *extra* adds values for columns only that schema has.
    """
    data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_sa_")}
    data.update(extra)
    tbl = Table(table_name, MetaData(), autoload_with=engine)
    data = {k: v for k, v in data.items() if k in tbl.columns}
    with engine.begin() as conn:
        conn.execute(insert(tbl), data)


def _seed_activity(engine, **hr) -> None:
    """Insert an activity row with heart-rate values the CURRENT model no longer has."""
    assert _HR_COLUMNS <= set(hr), "seed must exercise every heart-rate column"
    _seed_row(engine, "activity", DBActivity(
        id=hr.pop("id"), user_info_id=1, name="Ride with HR", type="Ride",
        distance=5000.0), **hr)


def _seed_trip(engine, project_id: int, *activity_ids: int) -> None:
    """A trip at lock_version 4 holding *activity_ids* on its timeline."""
    _seed_row(engine, "project", DBProject(
        id=project_id, user_info_id=1, name=f"Trip {project_id}", lock_version=4))
    for pos, aid in enumerate(activity_ids):
        _seed_row(engine, "projectitem", DBProjectItem(
            project_id=project_id, position=pos, item_type="activity", activity_id=aid))


def _lock_versions(engine) -> dict:
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(
            text("SELECT id, lock_version FROM project"))}


def _seed_cache(engine, user_info_id: int, blob: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO stravacache (user_info_id, fetched_at, activities_json) "
            "VALUES (:u, 1.0, :b)"
        ), {"u": user_info_id, "b": blob})


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


_RAW_WITH_HR = {
    "id": 7, "name": "Ride", "type": "Ride", "distance": 5000.0,
    "map": {"summary_polyline": "abc"},
    "has_heartrate": True, "average_heartrate": 142.5, "max_heartrate": 181,
    "heartrate_opt_out": False, "display_hide_heartrate_option": True,
}


@pytest.fixture()
def seeded(db):
    cfg = _cfg(db)
    command.upgrade(cfg, _OLD_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    assert _HR_COLUMNS <= _columns(engine, "activity")

    _seed_activity(engine, id=1, has_heartrate=True, heartrate_opt_out=False,
                   display_hide_heartrate_option=True,
                   average_heartrate=142.5, max_heartrate=181)
    _seed_activity(engine, id=2, has_heartrate=False, heartrate_opt_out=False,
                   display_hide_heartrate_option=False,
                   average_heartrate=None, max_heartrate=None)
    # No values, but one of Strava's flags: still heart-rate data.
    _seed_activity(engine, id=3, has_heartrate=False, heartrate_opt_out=True,
                   display_hide_heartrate_option=False,
                   average_heartrate=None, max_heartrate=None)
    # Trip 1 holds the activity with values, trip 2 the clean one, trip 3 the
    # flagged one; trip 4 holds nothing.
    _seed_trip(engine, 1, 1)
    _seed_trip(engine, 2, 2)
    _seed_trip(engine, 3, 3)
    _seed_trip(engine, 4)
    # User 1: a real cached page carrying heart rate. User 2: a blob that does
    # not parse — nothing can be scrubbed from it, so it must go.
    _seed_cache(engine, 1, json.dumps([_RAW_WITH_HR]))
    _seed_cache(engine, 2, "{not json")
    return cfg, engine


def test_upgrade_drops_columns_and_scrubs_cache(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, "head")

    assert not (_HR_COLUMNS & _columns(engine, "activity"))
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, name, distance FROM activity ORDER BY id")).fetchall()
        assert [tuple(r) for r in rows] == [
            (1, "Ride with HR", 5000.0), (2, "Ride with HR", 5000.0),
            (3, "Ride with HR", 5000.0)]
        # Indexes survive the SQLite table rebuild.
        assert {ix["name"] for ix in inspect(engine).get_indexes("activity")} >= {
            "ix_activity_start_date", "ix_activity_type", "ix_activity_user_info_id"}

        cache = {r[0]: r[1] for r in conn.execute(
            text("SELECT user_info_id, activities_json FROM stravacache"))}
    assert set(cache) == {1}, "the unparseable blob is deleted, not kept"
    assert "heartrate" not in cache[1]
    assert json.loads(cache[1]) == [
        {k: v for k, v in _RAW_WITH_HR.items() if "heartrate" not in k}]


def test_upgrade_bumps_lock_version_only_of_trips_holding_heart_rate(seeded):
    """A native client keeps a trip's detail JSON until its lock_version moves
    (issue #173): the trips whose cached copy carries a value must be told."""
    cfg, engine = seeded
    assert _lock_versions(engine) == {1: 4, 2: 4, 3: 4, 4: 4}
    command.upgrade(cfg, "head")
    assert _lock_versions(engine) == {1: 5, 2: 4, 3: 5, 4: 4}, (
        "a value or a Strava flag bumps its trip; a clean or empty trip is left")


def test_cache_rows_are_read_one_at_a_time(db):
    """The scrub runs at API startup, in a process with little memory to
    spare, and a blob is a user's whole Strava history: it must not hold
    every blob at once."""
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    page = [dict(_RAW_WITH_HR, id=i, description="x" * 500) for i in range(2000)]
    blob = json.dumps(page)
    users = 40
    for uid in range(1, users + 1):
        _seed_cache(engine, uid, blob)
    engine.dispose()
    stored = users * len(blob)

    tracemalloc.start()
    try:
        command.upgrade(cfg, _DROP_REV)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < stored / 4, (peak, stored)
    check = create_engine(f"sqlite:///{db.as_posix()}")
    with check.connect() as conn:
        left = conn.execute(text(
            "SELECT count(*) FROM stravacache WHERE activities_json GLOB '*heartrate*'"
        )).scalar()
        kept = conn.execute(text("SELECT count(*) FROM stravacache")).scalar()
    assert (left, kept) == (0, users)


def test_upgrade_is_noop_on_clean_db(db):
    """Idempotent on the data side: re-running the scrub changes nothing."""
    cfg = _cfg(db)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    clean = json.dumps([{"id": 7, "name": "Ride"}])
    _seed_cache(engine, 1, clean)
    command.downgrade(cfg, _PREV_REV)  # bring the columns back for the re-run
    command.upgrade(cfg, _DROP_REV)
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT activities_json FROM stravacache")).scalar() == clean


def test_downgrade_restores_empty_columns(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PREV_REV)
    assert _HR_COLUMNS <= _columns(engine, "activity")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT has_heartrate, average_heartrate, max_heartrate FROM activity"
        )).fetchall()
    assert [tuple(r) for r in rows] == [(0, None, None)] * 3, (
        "the values are gone by design; a downgrade only restores the shape")

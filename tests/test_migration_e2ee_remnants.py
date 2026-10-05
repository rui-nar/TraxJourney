"""Data-migration test for c4e2a9f1b7d3 — E2EE remnants (docs/E2EE_REMNANTS_PLAN.md U4).

Builds a throwaway SQLite DB at the migration's parent revision, seeds the
cases it distinguishes, upgrades to it, and asserts:

* encrypted users' Strava cache rows go, everyone else's stay;
* ``split_base_name`` is nulled only where the name is an envelope;
* ``project.low_res_geo_json`` is gone;
* the eight scalar snapshot columns of an edited row with plaintext originals
  hold exactly what ``reset_activity_track`` restores — checked by running
  the reset itself on the migrated row — and stay NULL for null or enveloped
  originals and unedited rows;
* downgrade restores the shape.

Hermetic — never touches the developer's real db.
"""
import json
from pathlib import Path

import polyline as polyline_lib
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, inspect, MetaData, Table, text
from sqlmodel import Session

from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.models.track_edit import TrackPoint, recompute_track_metrics
from src.project.project_repo import ProjectRepo

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PREV_REV = "87200bcb9342"      # down_revision of the migration
_REV = "c4e2a9f1b7d3"

ENV = "v1.a2V5.Y2lwaGVy"

_SNAPSHOT = (
    "original_distance", "original_moving_time", "original_elapsed_time",
    "original_average_speed", "original_elev_high", "original_elev_low",
    "original_start_latlng_json", "original_end_latlng_json",
)
#: The live column each snapshot column records.
_LIVE = tuple(c[len("original_"):] for c in _SNAPSHOT)


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "e2ee_remnants_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    return db_path


def _seed_row(engine, table_name: str, obj, **extra) -> None:
    """Insert *obj* using only the columns the table has at the seeded revision
    (test_migration_elevation_gain_backfill's idiom); *extra* sets columns the
    current model no longer has."""
    data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_sa_")}
    data.update(extra)
    tbl = Table(table_name, MetaData(), autoload_with=engine)
    data = {k: v for k, v in data.items() if k in tbl.columns}
    with engine.begin() as conn:
        conn.execute(insert(tbl), data)


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


# ── Geometry ────────────────────────────────────────────────────────────────

def _track(n: int) -> list[TrackPoint]:
    """A climbing line north from (45, 6), ~110 m between points."""
    return [TrackPoint(lat=45.0 + i * 0.001, lng=6.0, elev=500.0 + 3.0 * i)
            for i in range(n)]


def _poly(points) -> str:
    return polyline_lib.encode([(p.lat, p.lng) for p in points])


def _profile_json(points) -> str:
    from src.models.track_edit import points_to_elevation_profile
    dist, elev = points_to_elevation_profile(points)
    return json.dumps({"distances_km": dist, "elevations_m": elev})


def _edited(aid: int, *, original_polyline, original_profile=None,
            name="Ride") -> DBActivity:
    """An activity trimmed to its first 20 of 60 points; the snapshot columns
    hold whatever *original_* says (plaintext, envelope or NULL)."""
    trimmed = _track(20)
    m = recompute_track_metrics(trimmed)
    return DBActivity(
        id=aid, user_info_id=1, name=name, type="Ride",
        distance=m.distance, moving_time=1200, elapsed_time=1500,
        average_speed=m.distance / 1200, total_elevation_gain=57.0,
        elev_high=m.elev_high, elev_low=m.elev_low,
        start_latlng_json=json.dumps(m.start_latlng),
        end_latlng_json=json.dumps(m.end_latlng),
        summary_polyline=_poly(trimmed), elevation_profile_json=_profile_json(trimmed),
        is_edited=True,
        original_polyline=original_polyline,
        original_elevation_profile_json=original_profile,
        original_total_elevation_gain=177.0,
    )


_FULL = _track(60)

#: id → (DBActivity, whether the backfill must fill its snapshot)
_ACTIVITIES = {
    # Plaintext originals with a profile, and without one.
    1: (_edited(1, original_polyline=_poly(_FULL),
                original_profile=_profile_json(_FULL)), True),
    2: (_edited(2, original_polyline=_poly(_FULL)), True),
    # The shipped encryption migration nulled the original polyline.
    3: (_edited(3, original_polyline=None), False),
    # Encrypted originals.
    4: (_edited(4, original_polyline=ENV, original_profile=ENV), False),
    # Never edited.
    5: (DBActivity(id=5, user_info_id=1, name="Plain", type="Ride",
                   distance=1000.0, moving_time=300, elapsed_time=300), False),
}


@pytest.fixture()
def seeded(db):
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")

    _seed_row(engine, "userinfo", UserInfo(id=1, encryption_enabled=True))
    _seed_row(engine, "userinfo", UserInfo(id=2, encryption_enabled=False))
    with engine.begin() as conn:
        for uid in (1, 2):
            conn.execute(text(
                "INSERT INTO stravacache (user_info_id, fetched_at, activities_json) "
                "VALUES (:u, 1.0, '[{\"id\": 7, \"name\": \"Morning ride\"}]')"
            ), {"u": uid})

    for row, _ in _ACTIVITIES.values():
        _seed_row(engine, "activity", row)
    # Split families: an enveloped name, a plaintext one, and a name that only
    # starts like an envelope.
    for aid, name in ((10, ENV), (11, "Alps (1/2)"), (12, "v1.5 ride (1/2)")):
        _seed_row(engine, "activity", DBActivity(
            id=aid, user_info_id=1, name=name, type="Ride",
            split_base_name="Alps"))

    _seed_row(engine, "project", DBProject(id=1, user_info_id=1, name="Trip"),
              low_res_geo_json='{"type": "FeatureCollection", "features": []}')
    for pos, aid in enumerate(_ACTIVITIES):
        _seed_row(engine, "projectitem", DBProjectItem(
            project_id=1, position=pos, item_type="activity", activity_id=aid,
            uid=f"a{aid}"))
    return cfg, engine


def _values(engine, aid: int, columns) -> dict:
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT {', '.join(columns)} FROM activity WHERE id = :id"),
            {"id": aid}).mappings().one()
    return dict(row)


def test_deletes_only_encrypted_users_strava_cache(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REV)
    with engine.connect() as conn:
        users = [r[0] for r in conn.execute(text(
            "SELECT user_info_id FROM stravacache ORDER BY user_info_id"))]
    assert users == [2]


def test_nulls_split_base_name_only_where_name_is_an_envelope(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REV)
    assert {aid: _values(engine, aid, ["split_base_name"])["split_base_name"]
            for aid in (10, 11, 12)} == {10: None, 11: "Alps", 12: "Alps"}


def test_drops_low_res_geo_json(seeded):
    cfg, engine = seeded
    assert "low_res_geo_json" in _columns(engine, "project")
    command.upgrade(cfg, _REV)
    assert "low_res_geo_json" not in _columns(engine, "project")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM project")).scalar() == "Trip"
    assert "uq_project_user_name" in {
        ix["name"] for ix in inspect(engine).get_indexes("project")}


@pytest.mark.parametrize("aid", [1, 2])
def test_backfills_exactly_what_reset_restores(seeded, aid):
    """The snapshot of a plaintext edited row equals what the reset writes
    into the live columns when it is run on that same row."""
    cfg, engine = seeded
    command.upgrade(cfg, _REV)
    snapshot = _values(engine, aid, _SNAPSHOT)
    edited = _values(engine, aid, _LIVE)
    # Elevation stays NULL only where the original had no profile to take it from.
    expect_filled = _SNAPSHOT if aid == 1 else tuple(
        c for c in _SNAPSHOT if c not in ("original_elev_high", "original_elev_low"))
    assert all(snapshot[c] is not None for c in expect_filled), snapshot
    # Not a copy of the edited values: the original track is three times as long.
    assert snapshot["original_distance"] > 2.5 * edited["distance"]
    assert snapshot["original_moving_time"] > 2.5 * edited["moving_time"]

    with Session(engine) as sess:
        assert ProjectRepo().reset_activity_track(sess, 1, aid)
    restored = _values(engine, aid, _LIVE)
    assert {f"original_{k}": v for k, v in restored.items()} == snapshot


@pytest.mark.parametrize("aid", [3, 4, 5], ids=["null original", "enveloped original", "unedited"])
def test_leaves_snapshot_null_without_plaintext_originals(seeded, aid):
    cfg, engine = seeded
    command.upgrade(cfg, _REV)
    assert set(_values(engine, aid, _SNAPSHOT).values()) == {None}


def test_downgrade_restores_the_shape(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REV)
    command.downgrade(cfg, _PREV_REV)
    assert "low_res_geo_json" in _columns(engine, "project")
    assert not set(_SNAPSHOT) & _columns(engine, "activity")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM activity")).scalar() == 8
    command.upgrade(cfg, _REV)   # and back up again
    assert set(_SNAPSHOT) <= _columns(engine, "activity")

"""Data-migration test for 6c1f0e9a2b47: repair stored NaN/Infinity elevations (#462).

Until #462, a GPX upload whose ``<ele>`` read NaN or inf stored NaN or Infinity
in the activity's elevation profile (JSON text: ``NaN``, ``Infinity``) and
Infinity in its gain and high/low. The trip's JSON then held tokens the
client's parser refuses, and its export was a file the import refuses. The
writers now treat such a value as missing; this migration repairs what they
already stored, to exactly what they would store now.

Builds a throwaway SQLite DB up to the revision before the repair, seeds one
row per case, runs it, and checks who was repaired and who was left alone.
"""
import json
import math
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, insert, text

from models.project_db import DBActivity, DBProject, DBProjectItem
from src.models.track_edit import clean_elevation_profile, elevation_gain
from src.project.elevation_downsample import downsample_elevation

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PREV_REV = "5e2b7c1d9a40"
_REPAIR_REV = "6c1f0e9a2b47"

_INF, _NAN = float("inf"), float("nan")
_DISTANCES = [i * 0.01 for i in range(40)]
_CLEAN = [500.0 + (i % 10) * 2.0 for i in range(40)]
_ENVELOPE = "v1.NaNInfinity.Y2lwaGVyTmFO"   # ciphertext may spell anything


def _broken(*positions, value=_NAN):
    return [value if i in positions else e for i, e in enumerate(_CLEAN)]


def _profile(elevations, distances=None) -> str:
    # json.dumps writes the NaN and Infinity tokens, as the old writers did.
    return json.dumps({"distances_km": distances or _DISTANCES, "elevations_m": elevations})


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def _seed_row(engine, table_name: str, obj) -> None:
    """Insert using only the columns that exist in *table_name* at _PREV_REV."""
    data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_sa_")}
    tbl = Table(table_name, MetaData(), autoload_with=engine)
    data = {k: v for k, v in data.items() if k in tbl.columns}
    with engine.begin() as conn:
        conn.execute(insert(tbl), data)


_COLUMNS = ("total_elevation_gain", "elev_high", "elev_low", "elevation_profile_json",
            "elevation_profile_low_res_json", "original_elevation_profile_json",
            "original_total_elevation_gain")


def _rows(engine) -> dict:
    with engine.connect() as conn:
        return {r[0]: dict(zip(_COLUMNS, r[1:])) for r in conn.execute(text(
            f"SELECT id, {', '.join(_COLUMNS)} FROM activity"))}


def _projects(engine) -> dict:
    with engine.connect() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(text(
            "SELECT id, stats_json, lock_version FROM project"))}


def _expected(elevations, distances=None):
    """What the writers now store for these samples."""
    return clean_elevation_profile(distances or _DISTANCES, elevations)


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    db_path = tmp_path / "elev_non_finite_repair.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    cfg = _cfg(db_path)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    def act(id, **kw):
        base = dict(id=id, user_info_id=1, name="x", type="Hike",
                    total_elevation_gain=40.0, elev_high=518.0, elev_low=500.0)
        base.update(kw)
        _seed_row(engine, "activity", DBActivity(**base))

    # 1: a GPX upload with a NaN and an Infinity reading: profile, low-res
    #    copy, gain and high all hold what the old writer stored.
    gpx = _profile(_broken(5)[:9] + [_INF] + _broken(5)[10:])
    act(1, source="gpx", elevation_profile_json=gpx, elevation_profile_low_res_json=gpx,
        total_elevation_gain=_INF, elev_high=_INF)
    # 2: an edited piece whose own profile is clean, but whose undo snapshot
    #    (restored verbatim by a reset) holds -Infinity and an infinite gain.
    act(-2, is_edited=True, elevation_profile_json=_profile(_CLEAN),
        original_elevation_profile_json=_profile(_broken(3, value=-_INF)),
        original_total_elevation_gain=_INF)
    # 3: a Strava row with a NaN in its stream profile: the gain and high/low
    #    are Strava's own figures, finite, and stay as they are.
    act(3, source=None, elevation_profile_json=_profile(_broken(7)),
        total_elevation_gain=55.0, elev_high=520.0, elev_low=499.0)
    # 4: a row with infinite scalars and no profile to recompute them from.
    act(4, total_elevation_gain=_INF, elev_high=_INF, elev_low=-_INF)
    # 5: an E2EE envelope the server cannot read, whatever letters it holds.
    act(5, is_edited=True, elevation_profile_json=_ENVELOPE,
        elevation_profile_low_res_json=_ENVELOPE)
    # 6: a clean row, byte for byte what it was.
    act(6, source="gpx", elevation_profile_json=_profile(_CLEAN),
        elevation_profile_low_res_json=_profile(_CLEAN))
    # 7: a GPX upload whose every reading was NaN.
    act(7, source="gpx", elevation_profile_json=_profile([_NAN] * 40),
        elevation_profile_low_res_json=_profile([_NAN] * 40), total_elevation_gain=0.0,
        elev_high=None, elev_low=None)

    for pid, aid in ((1, 1), (2, 6)):
        _seed_row(engine, "project", DBProject(
            id=pid, user_info_id=1, name=f"trip{pid}", lock_version=5,
            stats_json=json.dumps({"total_elev_m": 1.0})))
        _seed_row(engine, "projectitem", DBProjectItem(
            project_id=pid, position=0, item_type="activity", activity_id=aid))
    return cfg, engine


def test_it_is_the_single_head():
    """It composes with the rest of the release: one line of history."""
    script = ScriptDirectory.from_config(_cfg(Path("unused.db")))
    assert script.get_heads() == [_REPAIR_REV]
    assert script.get_revision(_REPAIR_REV).down_revision == _PREV_REV


def test_a_gpx_upload_is_repaired_and_recomputed(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    row = _rows(engine)[1]

    dists, elevs = _expected(_broken(5)[:9] + [_INF] + _broken(5)[10:])
    assert json.loads(row["elevation_profile_json"]) == {
        "distances_km": dists, "elevations_m": elevs}
    low_d, low_e = downsample_elevation(dists, elevs)
    assert json.loads(row["elevation_profile_low_res_json"]) == {
        "distances_km": low_d, "elevations_m": low_e}
    assert row["total_elevation_gain"] == pytest.approx(elevation_gain(elevs, dists))
    assert row["elev_high"] == max(elevs) and row["elev_low"] == min(elevs)


def test_the_undo_snapshot_is_repaired_too(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    row = _rows(engine)[-2]

    dists, elevs = _expected(_broken(3, value=-_INF))
    assert json.loads(row["original_elevation_profile_json"]) == {
        "distances_km": dists, "elevations_m": elevs}
    assert row["original_total_elevation_gain"] == pytest.approx(elevation_gain(elevs, dists))
    # The piece's own figures were never broken.
    assert json.loads(row["elevation_profile_json"])["elevations_m"] == _CLEAN
    assert row["total_elevation_gain"] == 40.0


def test_strava_figures_that_are_finite_are_kept(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    row = _rows(engine)[3]

    assert json.loads(row["elevation_profile_json"])["elevations_m"] == _expected(_broken(7))[1]
    assert (row["total_elevation_gain"], row["elev_high"], row["elev_low"]) == (55.0, 520.0, 499.0)


def test_figures_with_nothing_to_recompute_from_are_cleared(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    rows = _rows(engine)

    # Gain is NOT NULL: 0.0 is its column default, what an activity without
    # elevation has always stored.
    assert (rows[4]["total_elevation_gain"], rows[4]["elev_high"], rows[4]["elev_low"]) == (0.0, None, None)
    assert rows[7]["elevation_profile_json"] is None
    assert rows[7]["elevation_profile_low_res_json"] is None
    assert (rows[7]["total_elevation_gain"], rows[7]["elev_high"], rows[7]["elev_low"]) == (0.0, None, None)


def test_encrypted_and_clean_rows_are_untouched(seeded):
    cfg, engine = seeded
    before = _rows(engine)
    command.upgrade(cfg, _REPAIR_REV)
    after = _rows(engine)

    assert after[5] == before[5]
    assert after[6] == before[6]


def test_no_stored_value_is_left_non_finite(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)

    def _refuse(token):
        raise ValueError(token)
    for aid, row in _rows(engine).items():
        for col in ("total_elevation_gain", "elev_high", "elev_low",
                    "original_total_elevation_gain"):
            assert row[col] is None or math.isfinite(row[col]), (aid, col)
        for col in ("elevation_profile_json", "elevation_profile_low_res_json",
                    "original_elevation_profile_json"):
            if row[col] and not row[col].startswith("v1."):
                json.loads(row[col], parse_constant=_refuse)


def test_the_trips_holding_a_repaired_row_are_told(seeded):
    """Their cached totals are dropped, and their lock_version advances so a
    client's on-device copy is refetched (issue #173)."""
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    projects = _projects(engine)

    assert projects[1] == (None, 6)
    assert projects[2] == (json.dumps({"total_elev_m": 1.0}), 5)


def test_it_is_idempotent_and_its_downgrade_keeps_the_repair(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    repaired, projects = _rows(engine), _projects(engine)

    command.downgrade(cfg, _PREV_REV)
    assert _rows(engine) == repaired, "the NaN it replaced is not worth restoring"
    command.upgrade(cfg, "head")

    assert _rows(engine) == repaired
    assert _projects(engine) == projects

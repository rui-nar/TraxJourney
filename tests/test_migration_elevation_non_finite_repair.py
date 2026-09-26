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
import importlib.util
import json
import math
import tracemalloc
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
#: Ciphertext spelling "nan" and "infinity": an envelope never starts with "{",
#: so the scan passes it by whatever letters it holds.
_LOWER_ENVELOPE = "v1.bmFuYW5hbmFu.aW5maW5pdHluYW4"


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

    # 8: a GPX upload whose totals are finite but wrong: measured from the
    #    NaN reading (comparisons with NaN are all false). Only the recompute
    #    of app-measured rows can fix them; nothing about them is non-finite.
    act(8, source="gpx", elevation_profile_json=_profile(_broken(4)),
        total_elevation_gain=0.0, elev_high=9999.0, elev_low=-5.0)
    # 9: an edited Strava activity: since #386 its gain is its share of
    #    Strava's own, and a NaN in its profile does not make that wrong.
    act(9, source=None, is_edited=True, elevation_profile_json=_profile(_broken(6)),
        total_elevation_gain=33.0, elev_high=518.0, elev_low=500.0)
    # 10: an edited GPX upload whose undo snapshot holds a NaN: the gain the
    #     snapshot keeps was measured from it too, so a reset would restore it.
    act(10, source="gpx", is_edited=True, elevation_profile_json=_profile(_CLEAN),
        original_elevation_profile_json=_profile(_broken(8)),
        original_total_elevation_gain=77.0)
    # 11: ciphertext whose letters spell "nan" and "infinity" in lower case.
    act(11, is_edited=True, elevation_profile_json=_LOWER_ENVELOPE,
        elevation_profile_low_res_json=_LOWER_ENVELOPE)
    # 12: a GPX upload from a device writing <ele>65535</ele> for no reading:
    #     finite, but no elevation, and the gain measured through it.
    act(12, source="gpx", elevation_profile_json=_profile(_broken(5, value=65535)),
        elevation_profile_low_res_json=_profile(_broken(5, value=65535)),
        total_elevation_gain=5e6, elev_high=65535.0, elev_low=500.0)
    # 13: a Strava activity whose summary high is the sentinel.
    act(13, source=None, elevation_profile_json=_profile(_CLEAN),
        total_elevation_gain=55.0, elev_high=65535.0, elev_low=500.0)
    # 14: a GPX upload whose one stray 1970 stamp made it 54 years long.
    act(14, source="gpx", elevation_profile_json=_profile(_CLEAN),
        moving_time=3600, elapsed_time=1_700_000_000)
    # 15: an edited GPX upload whose undo snapshot holds the sentinel.
    act(15, source="gpx", is_edited=True, elevation_profile_json=_profile(_CLEAN),
        original_elevation_profile_json=_profile(_broken(2, value=65535)),
        original_total_elevation_gain=9e6)
    # 16: a clean profile that merely holds a five-digit number (12 km up is
    #     in range): selected by the scan, left as it is.
    act(16, source="gpx", elevation_profile_json=_profile(_broken(1, value=12345.0)))

    for pid, aid in ((1, 1), (2, 6)):
        _seed_row(engine, "project", DBProject(
            id=pid, user_info_id=1, name=f"trip{pid}", lock_version=5,
            stats_json=json.dumps({"total_elev_m": 1.0})))
        _seed_row(engine, "projectitem", DBProjectItem(
            project_id=pid, position=0, item_type="activity", activity_id=aid))
    # 3: a trip whose cached totals overflowed (two huge but finite
    #    distances), with no activity to repair.
    _seed_row(engine, "project", DBProject(
        id=3, user_info_id=1, name="trip3", lock_version=5,
        stats_json='{"total_distance_m": Infinity}'))
    return cfg, engine


def _migration():
    path = _PROJECT_ROOT / "alembic" / "versions" / f"{_REPAIR_REV}_repair_non_finite_elevations.py"
    spec = importlib.util.spec_from_file_location("repair_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_plaintext_rows_are_candidates(seeded):
    """Ciphertext is never selected, whatever letters it holds, so the
    startup scan does not load every encrypted profile into memory."""
    _, engine = seeded
    with engine.connect() as conn:
        ids = set(_migration()._candidate_ids(conn))

    assert ids == {1, -2, 3, 4, 7, 8, 9, 10, 12, 13, 14, 15, 16}


def test_an_app_measured_row_with_finite_but_wrong_totals_is_recomputed(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    row = _rows(engine)[8]

    dists, elevs = _expected(_broken(4))
    assert row["total_elevation_gain"] == pytest.approx(elevation_gain(elevs, dists))
    assert (row["elev_high"], row["elev_low"]) == (max(elevs), min(elevs))


def test_an_edited_strava_activity_keeps_its_share_of_stravas_gain(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    row = _rows(engine)[9]

    assert json.loads(row["elevation_profile_json"])["elevations_m"] == _expected(_broken(6))[1]
    assert (row["total_elevation_gain"], row["elev_high"], row["elev_low"]) == (33.0, 518.0, 500.0)


def test_an_edited_gpx_upload_gets_its_snapshot_gain_remeasured(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    row = _rows(engine)[10]

    dists, elevs = _expected(_broken(8))
    assert row["original_total_elevation_gain"] == pytest.approx(elevation_gain(elevs, dists))
    assert row["total_elevation_gain"] == 40.0   # its own profile was clean


def test_cached_totals_holding_a_non_finite_number_are_dropped(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)

    assert _projects(engine)[3] == (None, 5)


def test_rows_are_read_one_at_a_time(tmp_path, monkeypatch):
    """The repair runs at startup, in a process with little memory to spare:
    it must not hold every candidate row at once."""
    db_path = tmp_path / "many.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    cfg = _cfg(db_path)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    n = 10_000
    dists = [i * 0.01 for i in range(n)]
    elevs = [500.0 + (i % 50) for i in range(n)]
    elevs[n // 2] = _NAN
    big = _profile(elevs, dists)
    tbl = Table("activity", MetaData(), autoload_with=engine)
    rows = 80
    with engine.begin() as conn:
        for aid in range(1, rows + 1):
            data = {k: v for k, v in DBActivity(
                id=aid, user_info_id=1, name="x", type="Ride", source="gpx",
                elevation_profile_json=big, elevation_profile_low_res_json=big,
            ).__dict__.items() if not k.startswith("_sa_") and k in tbl.columns}
            conn.execute(insert(tbl), data)
    engine.dispose()
    stored = rows * 2 * len(big)

    tracemalloc.start()
    try:
        command.upgrade(cfg, _REPAIR_REV)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < stored / 4, (peak, stored)
    check = create_engine(f"sqlite:///{db_path.as_posix()}")
    with check.connect() as conn:
        left = conn.execute(text(
            "SELECT count(*) FROM activity WHERE elevation_profile_json GLOB '*NaN*'")).scalar()
    assert left == 0


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
    assert after[11] == before[11]
    assert after[16] == before[16]


def test_a_finite_sentinel_is_repaired_like_a_non_finite_one(seeded):
    """Pre-#462 writers stored <ele>65535</ele> as it came, and the import now
    treats it as no reading: a stored one must go too, or the trip it is in
    exports to a file its own import normalises differently."""
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    rows = _rows(engine)

    dists, elevs = _expected(_broken(5, value=65535))
    assert json.loads(rows[12]["elevation_profile_json"])["elevations_m"] == elevs
    assert rows[12]["total_elevation_gain"] == pytest.approx(elevation_gain(elevs, dists))
    assert (rows[12]["elev_high"], rows[12]["elev_low"]) == (max(elevs), min(elevs))
    # Strava's gain kept; its impossible high measured from its profile.
    assert (rows[13]["total_elevation_gain"], rows[13]["elev_high"]) == (55.0, max(_CLEAN))
    # The snapshot a reset restores, and the gain it keeps.
    o_dists, o_elevs = _expected(_broken(2, value=65535))
    assert json.loads(rows[15]["original_elevation_profile_json"])["elevations_m"] == o_elevs
    assert rows[15]["original_total_elevation_gain"] == pytest.approx(elevation_gain(o_elevs, o_dists))


def test_an_elapsed_time_of_decades_falls_back_to_the_moving_time(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT moving_time, elapsed_time FROM activity WHERE id = 14")).one() == (3600, 3600)


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

"""Data-migration test for c4a9e1f70b38 — repair the 0.0 missing-<ele> sentinel.

Builds a throwaway SQLite DB up to the revision *before* the repair, seeds the
cases it distinguishes, runs it, and asserts who was repaired and who was left
alone. Hermetic — never touches the developer's real db.

The case that matters most is the genuine sea-level one: the sentinel is
detected by shape, not by a flag, so a coastal track whose real readings include
0.0 must come through untouched. The E2EE case matters for the same reason it
did in test_migration_elevation_gain_backfill: the profile these rows must be
repaired FROM is ciphertext the server holds no key for (issue #374, #366).
"""
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, MetaData, Table, text

from models.project_db import DBActivity, DBProject, DBProjectItem
from src.models.track_edit import elevation_gain, interpolate_elevation_gaps

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PREV_REV = "b7f1a3c9d204"      # down_revision of the repair
_REPAIR_REV = "c4a9e1f70b38"

#: Figure the live import path computed, which b7f1a3c9d204 left in place
#: because it could not safely recompute a sentinel-bearing profile.
_STORED_LIVE = 14.4

_N = 300
_DISTANCES = [i * 0.01 for i in range(_N)]      # 10 m spacing, ~3 km
_MISSING = (100, 101, 102)


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "elev_dropout_repair_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    return db_path


def _alpine_elevations(missing=_MISSING):
    """A ~3 km out-and-back at ~500 m, with *missing* samples carrying 0.0."""
    return [
        0.0 if i in missing
        else 500.0 + (i * 0.1 if i < _N // 2 else (_N - i) * 0.1)
        for i in range(_N)
    ]


def _profile_json(elevations, distances=None) -> str:
    return json.dumps({"distances_km": distances or _DISTANCES,
                       "elevations_m": elevations})


def _sea_level_elevations():
    """A genuine coastal track: median near zero, real 0.0 readings in it."""
    return [0.0 if i % 3 == 0 else float(i % 7) for i in range(_N)]


def _lowland_elevations(missing=_MISSING):
    """A Dutch-flat ~3 km at ~30 m, with *missing* samples carrying 0.0.

    Its median sits below any sea-level margin, so the altitude test that
    `b7f1a3c9d204` used never saw the dropout at all.
    """
    return [
        0.0 if i in missing
        else 30.0 + (i * 0.01 if i < _N // 2 else (_N - i) * 0.01)
        for i in range(_N)
    ]


def _beach_then_cliff_elevations():
    """A route along 2 km of DEM beach at exactly 0.0, then a 60 m cliff climb.

    The zeros are real (DEM sea level is exactly 0) and one neighbour is far
    above any sea-level margin, so a neighbour-altitude test condemns the whole
    beach. The step to the cliff is 60 m over 100 m of travel — steep, but a
    grade terrain can actually have.
    """
    return [3.0, 2.0] + [0.0] * 20 + [60.0, 90.0]


def _descent_to_the_sea_elevations():
    """A real descent from 800 m to a beach — high median, genuine 0.0 readings.

    The zeros here are walked down to: the samples either side of them are a few
    metres up, not hundreds. That is what separates a coastline from a dropout,
    and a whole-series median test cannot see the difference.
    """
    elevations = []
    for i in range(_N):
        if i < _N - 12:
            elevations.append(800.0 - (800.0 * i / (_N - 12)))
        else:
            elevations.append(0.0)             # arrived, and stayed at sea level
    return elevations


def _seed_row(engine, table_name: str, obj) -> None:
    """Insert using only the columns that exist in *table_name* AT _PREV_REV.

    Same idiom as test_migration_elevation_gain_backfill: the ORM class always
    reflects the CURRENT model, so filtering against the reflected table keeps
    this test immune to columns added after this migration.
    """
    data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_sa_")}
    tbl = Table(table_name, MetaData(), autoload_with=engine)
    data = {k: v for k, v in data.items() if k in tbl.columns}
    # The reverse drift: a NOT NULL column this historical schema still has but
    # the current model has since dropped (#442's heart-rate flags) gets its
    # type's zero value, since no ORM default exists for it any more.
    for col in tbl.columns:
        if (col.name not in data and not col.nullable and not col.primary_key
                and col.server_default is None):
            data[col.name] = col.type.python_type()
    with engine.begin() as conn:
        conn.execute(insert(tbl), data)


def _seed_activity(engine, **kwargs) -> None:
    _seed_row(engine, "activity", DBActivity(**kwargs))


def _rows(engine) -> dict:
    with engine.connect() as conn:
        return {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(text(
                "SELECT id, total_elevation_gain, elevation_profile_json, "
                "elevation_profile_low_res_json FROM activity"))
        }


@pytest.fixture()
def seeded(db):
    """Four activities at _PREV_REV, one per case the migration distinguishes."""
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")

    dropout = _profile_json(_alpine_elevations())
    common = dict(user_info_id=1, name="x", type="Ride",
                  total_elevation_gain=_STORED_LIVE)

    # 1 — GPX import whose track had three points with no <ele>. The one to fix.
    _seed_activity(engine, id=1, is_edited=False, source="gpx",
                   elevation_profile_json=dropout,
                   elevation_profile_low_res_json=dropout, **common)
    # 2 — hand-edited activity carrying the same sentinel: also ours.
    _seed_activity(engine, id=-2, is_edited=True,
                   elevation_profile_json=dropout,
                   elevation_profile_low_res_json=dropout, **common)
    # 3 — a genuine sea-level track. Its 0.0 samples are real readings, and
    #     "repairing" them would invent a hill on flat coastal ground.
    _seed_activity(engine, id=3, is_edited=True,
                   elevation_profile_json=_profile_json(_sea_level_elevations()),
                   **common)
    # 4 — hand-edited, but the profile is an E2EE envelope the server cannot
    #     decrypt (`v1.<wrapped DEK>.<ciphertext>`).
    _seed_activity(engine, id=4, is_edited=True,
                   elevation_profile_json="v1.d3JhcHBlZA==.Y2lwaGVydGV4dA==",
                   **common)
    # 5 — an untouched Strava row: its profile and gain are Strava's own.
    _seed_activity(engine, id=5, is_edited=False,
                   elevation_profile_json=dropout, **common)
    # 7 — a lowland track at ~30 m with the same three missing samples.
    #     b7f1a3c9d204 ran first and, seeing a median under its sea-level
    #     margin, recomputed this row FROM the sentinel — so it arrives here
    #     already inflated, and this pass is its only chance to be corrected.
    _seed_activity(engine, id=7, is_edited=False, source="gpx",
                   elevation_profile_json=_profile_json(_lowland_elevations()),
                   **common)
    # 8 — a DEM beach ending in a coastal cliff. Real zeros beside a big step.
    _seed_activity(engine, id=8, is_edited=True,
                   elevation_profile_json=_profile_json(
                       _beach_then_cliff_elevations(),
                       distances=[i * 0.1 for i in range(24)]), **common)
    # 6 — a real descent from 800 m to the sea. High median AND real 0.0s, so a
    #     median-based test would call it a dropout and interpolate the coast
    #     away. The zeros are reached gradually, which is what marks them real.
    _seed_activity(engine, id=6, is_edited=True,
                   elevation_profile_json=_profile_json(
                       _descent_to_the_sea_elevations()), **common)

    # A trip holding the GPX import, with its totals already cached.
    _seed_row(engine, "project", DBProject(
        id=1, user_info_id=1, name="trip",
        stats_json=json.dumps({"total_elev_m": _STORED_LIVE})))
    _seed_row(engine, "projectitem", DBProjectItem(
        project_id=1, position=0, item_type="activity", activity_id=1))
    return cfg, engine


def test_repair_fills_the_sentinel_and_corrects_the_gain(seeded):
    cfg, engine = seeded
    holed = [None if i in _MISSING else e
             for i, e in enumerate(_alpine_elevations())]
    expected_series = interpolate_elevation_gaps(_DISTANCES, holed)
    expected_gain = elevation_gain(expected_series, _DISTANCES)

    command.upgrade(cfg, _REPAIR_REV)
    rows = _rows(engine)

    for row_id in (1, -2):
        gain, ep_json, _ = rows[row_id]
        series = json.loads(ep_json)["elevations_m"]
        assert 0.0 not in series, "the fabricated sea-level dive must be gone"
        assert min(series) >= 500.0, "the track never leaves 500 m"
        assert series == pytest.approx(expected_series)
        assert gain == pytest.approx(expected_gain)
        # And the corrected figure agrees with what the live path computed —
        # the point of the whole exercise (issue #374 criterion 2).
        assert gain == pytest.approx(_STORED_LIVE, abs=1.0)


def test_repair_rebuilds_the_low_res_copy(seeded):
    """The chart loads the low-res profile first, so leaving it behind would
    keep drawing the dive even with the full profile repaired."""
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)

    _, _, low_json = _rows(engine)[1]
    low = json.loads(low_json)
    assert 0.0 not in low["elevations_m"]
    assert min(low["elevations_m"]) >= 500.0
    assert len(low["distances_km"]) == len(low["elevations_m"])


def test_genuine_sea_level_track_is_not_mangled(seeded):
    """A real 0.0 reading on a coastal track is data, not a sentinel."""
    cfg, engine = seeded
    before = _rows(engine)[3]
    command.upgrade(cfg, _REPAIR_REV)
    assert _rows(engine)[3] == before, (
        "a series whose median sits at sea level must come through untouched — "
        "its zeros are readings, and filling them would invent terrain"
    )


def test_real_descent_to_sea_level_keeps_its_coastline(seeded):
    """The narrow case the per-run test exists for: a track whose median is high
    but whose 0.0 samples are genuine. Filling those would invent altitude over
    the last stretch of a real ride, irreversibly."""
    cfg, engine = seeded
    before = json.loads(_rows(engine)[6][1])["elevations_m"]

    command.upgrade(cfg, _REPAIR_REV)

    after = json.loads(_rows(engine)[6][1])["elevations_m"]
    assert after == before, (
        "zeros approached gradually are a coastline, not a missing reading — "
        "a whole-series median test cannot tell them apart, so detection is "
        "per run of zeros, by its own neighbours"
    )


def test_lowland_dropout_is_repaired_too(seeded):
    """The altitude test missed these entirely: a track at 30 m has a median
    below any sea-level margin, so its dropout went unrepaired while
    b7f1a3c9d204 — shipping in the same release — recomputed its gain from the
    fabricated dive. Grade sees it: 30 m of step over 10 m of travel."""
    cfg, engine = seeded

    command.upgrade(cfg, _REPAIR_REV)

    gain, ep_json, _ = _rows(engine)[7]
    assert 0.0 not in json.loads(ep_json)["elevations_m"], (
        "the fabricated dive to sea level must be filled in"
    )
    assert gain < 5.0, (
        f"gain must come back to the real ascent of this flat track, got {gain}"
    )


def test_beach_beside_a_cliff_keeps_its_zeros(seeded):
    """Real DEM sea level is exactly 0.0, and a coastal route can climb a cliff
    right after it. Judging the run by its neighbours' ALTITUDE condemned the
    whole beach; judging it by grade does not — 60 m over 100 m is terrain."""
    cfg, engine = seeded
    before = json.loads(_rows(engine)[8][1])["elevations_m"]

    command.upgrade(cfg, _REPAIR_REV)

    assert json.loads(_rows(engine)[8][1])["elevations_m"] == before


def test_encrypted_profile_is_left_alone(seeded):
    """The server holds no key, so it cannot read — let alone repair — these."""
    cfg, engine = seeded
    before = _rows(engine)[4]
    command.upgrade(cfg, _REPAIR_REV)
    assert _rows(engine)[4] == before, (
        "an E2EE-enveloped profile must survive the migration untouched — not "
        "rewritten, not zeroed, not crashed on (see issue #366)"
    )


def test_untouched_strava_row_is_left_alone(seeded):
    """Its profile came from Strava; a 0.0 in it is not our sentinel to fix."""
    cfg, engine = seeded
    before = _rows(engine)[5]
    command.upgrade(cfg, _REPAIR_REV)
    assert _rows(engine)[5] == before


def test_repair_clears_cached_trip_totals(seeded):
    """A repaired activity inside an unrepaired trip total is still wrong to
    the user — on their stats screen and on any public share page."""
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    with engine.connect() as conn:
        stats = conn.execute(
            text("SELECT stats_json FROM project WHERE id = 1")).scalar()
    assert stats is None, (
        "project.stats_json caches summed elevation and is only recomputed when "
        "NULL, so the migration must invalidate every trip it touched"
    )


def test_repair_is_idempotent(seeded):
    """A second pass finds no sentinel left and changes nothing."""
    cfg, engine = seeded
    command.upgrade(cfg, _REPAIR_REV)
    before = _rows(engine)

    command.stamp(cfg, _PREV_REV)           # rewind bookkeeping only
    command.upgrade(cfg, _REPAIR_REV)       # run the repair again

    assert _rows(engine) == before


def test_unusable_profile_is_skipped(db):
    """Malformed, too-short, or length-mismatched profiles are left alone."""
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")

    common = dict(user_info_id=1, name="x", type="Ride", is_edited=True,
                  total_elevation_gain=_STORED_LIVE)
    _seed_activity(engine, id=10, elevation_profile_json="{not json", **common)
    _seed_activity(engine, id=11, elevation_profile_json=json.dumps(
        {"distances_km": [0.0], "elevations_m": [100.0]}), **common)
    # Length mismatch: without trustworthy distances the gap cannot be placed.
    _seed_activity(engine, id=12, elevation_profile_json=json.dumps(
        {"distances_km": [0.0, 0.01], "elevations_m": _alpine_elevations()}),
        **common)
    before = _rows(engine)

    command.upgrade(cfg, _REPAIR_REV)

    assert _rows(engine) == before

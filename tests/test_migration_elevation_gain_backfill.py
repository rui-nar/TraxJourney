"""Data-migration test for b7f1a3c9d204 — recompute inflated elevation gain.

Builds a throwaway SQLite DB up to the revision *before* the backfill, seeds
activities covering every case the migration distinguishes, runs it, and asserts
who was corrected and who was left alone. Hermetic — never touches the
developer's real db.

The case that matters most is the encrypted one: `total_elevation_gain` is a
plain column, but the profile it must be recomputed from can be a client-side
E2EE envelope the server cannot decrypt. Those rows must come through untouched
rather than be zeroed or crash the migration (issue #260, follow-up #366).
"""
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, MetaData, Table, text

from models.project_db import DBActivity, DBProject, DBProjectItem
from src.models.track_edit import elevation_gain

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PREV_REV = "43f9efcb0207"      # down_revision of the backfill
_BACKFILL_REV = "b7f1a3c9d204"

#: Inflated figure the old raw-delta-sum code would have stored.
_STORED_INFLATED = 4094.0


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "elev_backfill_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    return db_path


def _noisy_profile_json() -> str:
    """A 600 m climb under ±1.2 m of noise, stored the way the app stores it."""
    import random

    random.seed(7)
    n = 6000
    elevations = [
        200.0 + 600.0 * (1 - abs(2 * (i / (n - 1)) - 1)) + random.gauss(0, 1.2)
        for i in range(n)
    ]
    distances = [i * 0.01 for i in range(n)]
    return json.dumps({"distances_km": distances, "elevations_m": elevations})


def _seed_row(engine, table_name: str, obj) -> None:
    """Insert using only the columns that exist in *table_name* AT _PREV_REV.

    Same idiom as test_migration_prune_orphaned_tails: the ORM class always
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


def _dropout_profile_json() -> str:
    """A ~500 m track with three points that carried no elevation."""
    elevations = [
        0.0 if i in (100, 101, 102) else 500.0 + (i * 0.1 if i < 150 else (300 - i) * 0.1)
        for i in range(300)
    ]
    return json.dumps({"distances_km": [i * 0.01 for i in range(300)],
                       "elevations_m": elevations})


def _seed_activity(engine, **kwargs) -> None:
    """Insert using only the columns that exist in `activity` AT _PREV_REV.

    Same idiom as test_migration_prune_orphaned_tails: the ORM class always
    reflects the CURRENT model, so filtering against the reflected table keeps
    this test immune to columns added after this migration.
    """
    _seed_row(engine, "activity", DBActivity(**kwargs))


def _gains(engine) -> dict[int, float]:
    with engine.connect() as conn:
        return {
            r[0]: r[1]
            for r in conn.execute(text("SELECT id, total_elevation_gain FROM activity"))
        }


@pytest.fixture()
def seeded(db):
    """Four activities at _PREV_REV, one per case the migration distinguishes."""
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")

    profile = _noisy_profile_json()
    common = dict(user_info_id=1, name="x", type="Ride",
                  total_elevation_gain=_STORED_INFLATED)

    # 1 — hand-edited Strava activity: ours to recompute.
    _seed_activity(engine, id=1, is_edited=True,
                   elevation_profile_json=profile, **common)
    # 2 — GPX import: also ours, even though it was never edited.
    _seed_activity(engine, id=-2, is_edited=False, source="gpx",
                   elevation_profile_json=profile, **common)
    # 3 — untouched Strava activity: its gain is Strava's, not ours.
    _seed_activity(engine, id=3, is_edited=False,
                   elevation_profile_json=profile, **common)
    # 4 — hand-edited, but the profile is an E2EE envelope the server cannot
    #     decrypt (`v1.<wrapped DEK>.<ciphertext>`).
    _seed_activity(engine, id=4, is_edited=True,
                   elevation_profile_json="v1.d3JhcHBlZA==.Y2lwaGVydGV4dA==",
                   **common)
    # 5 — a GPX import whose track had points without <ele>. The stored profile
    #     carries 0.0 at those samples (points_to_elevation_profile's sentinel),
    #     so recomputing from it verbatim would invent a plunge to sea level and
    #     a climb back out — the stored figure is the honest one.
    _seed_activity(engine, id=-5, is_edited=False, source="gpx",
                   elevation_profile_json=_dropout_profile_json(), **common)

    # A trip holding the edited activity, with its totals already cached.
    _seed_row(engine, "project", DBProject(
        id=1, user_info_id=1, name="trip",
        stats_json=json.dumps({"total_elev_m": _STORED_INFLATED})))
    _seed_row(engine, "projectitem", DBProjectItem(
        project_id=1, position=0, item_type="activity", activity_id=1))
    return cfg, engine, profile


def test_backfill_corrects_derived_gains_and_spares_the_rest(seeded):
    cfg, engine, profile = seeded
    # Distances included, exactly as the migration reads them: the smoothing
    # window spans metres of travel, so scoring the same series without them
    # would produce a different number.
    stored = json.loads(profile)
    expected = elevation_gain(stored["elevations_m"], stored["distances_km"])

    command.upgrade(cfg, _BACKFILL_REV)
    gains = _gains(engine)

    assert gains[1] == pytest.approx(expected), "edited activity must be corrected"
    assert gains[-2] == pytest.approx(expected), "GPX import must be corrected"
    assert gains[1] == pytest.approx(600.0, abs=25.0), "and land on the real climb"
    assert gains[3] == pytest.approx(_STORED_INFLATED), (
        "an untouched Strava activity keeps the figure Strava gave us"
    )
    assert gains[4] == pytest.approx(_STORED_INFLATED), (
        "an E2EE-enveloped profile cannot be decrypted server-side, so the row "
        "must survive the migration untouched — not zeroed (see issue #366)"
    )
    assert gains[-5] == pytest.approx(_STORED_INFLATED), (
        "a profile carrying the 0.0 missing-elevation sentinel must be left "
        "alone: recomputing from it reads each gap as a dive to sea level and "
        "reports roughly ten times the real climb (see issue #374)"
    )


def test_backfill_clears_cached_trip_totals(seeded):
    """A corrected activity inside an uncorrected trip total is still wrong to
    the user — on their stats screen and on any public share page."""
    cfg, engine, _ = seeded
    command.upgrade(cfg, _BACKFILL_REV)
    with engine.connect() as conn:
        stats = conn.execute(
            text("SELECT stats_json FROM project WHERE id = 1")).scalar()
    assert stats is None, (
        "project.stats_json caches summed elevation and is only recomputed when "
        "NULL, so the migration must invalidate every trip it touched"
    )


def test_backfill_is_idempotent(seeded):
    """Re-running recomputes the same values from the same stored profiles."""
    cfg, engine, _ = seeded
    command.upgrade(cfg, _BACKFILL_REV)
    before = _gains(engine)

    command.stamp(cfg, _PREV_REV)           # rewind bookkeeping only
    command.upgrade(cfg, _BACKFILL_REV)     # run the backfill again

    assert _gains(engine) == before


def test_unusable_profile_is_skipped_not_zeroed(db):
    """Malformed or too-short profiles leave the stored figure alone."""
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")

    common = dict(user_info_id=1, name="x", type="Ride", is_edited=True,
                  total_elevation_gain=_STORED_INFLATED)
    _seed_activity(engine, id=10, elevation_profile_json="{not json", **common)
    _seed_activity(engine, id=11, elevation_profile_json=json.dumps(
        {"distances_km": [0.0], "elevations_m": [100.0]}), **common)

    command.upgrade(cfg, _BACKFILL_REV)

    gains = _gains(engine)
    assert gains[10] == pytest.approx(_STORED_INFLATED)
    assert gains[11] == pytest.approx(_STORED_INFLATED)

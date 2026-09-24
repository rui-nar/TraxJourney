"""Data-migration test for d5b1c0a2e3f4 — prune orphaned split-tail activities.

Builds a throwaway SQLite DB up to the revision *before* the prune, seeds a mix
of activity rows (orphaned local, referenced local, referenced Strava, orphaned
Strava), runs the prune migration, and asserts only the orphaned LOCAL row is
gone. Hermetic — never touches the developer's real db.
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, MetaData, Table, text

from models.project_db import DBActivity, DBProjectItem

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PREV_REV = "f40e0c0de001"   # down_revision of the prune migration
_PRUNE_REV = "d5b1c0a2e3f4"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "prune_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    return db_path


def _activity_ids(engine) -> set[int]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT id FROM activity"))}


def _seed_row(engine, table_name: str, obj) -> None:
    """Insert *obj* using only the columns that exist in the table AT _PREV_REV.

    Building the values from the ORM instance is what gets every NOT NULL
    column (activity has ~20: distance/moving_time/trainer/...) its correct
    model default for free. But the ORM classes always reflect the CURRENT
    model — inserting all of their columns directly would include any added
    since _PREV_REV (#45's split_root_id/split_base_name, #173's
    projectitem.uid/segment_id) that don't exist yet in this deliberately
    frozen historical schema. Reflecting the real table and filtering to its
    actual columns keeps this test immune to that drift.
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


def _seed_activity_row(engine, **kwargs) -> None:
    _seed_row(engine, "activity", DBActivity(**kwargs))


def test_prune_removes_only_orphaned_local_rows(db):
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)

    engine = create_engine(f"sqlite:///{db.as_posix()}")
    # SQLite leaves FK enforcement off, so activity/projectitem rows stand alone
    # without userinfo/project parents — keeps the fixture focused on the two
    # tables the prune actually reads.
    # Four activities: only the orphaned LOCAL one (-1) must be pruned.
    for aid in (-1, -2, 100, 200):
        _seed_activity_row(engine, id=aid, user_info_id=1, name="x", type="Ride")
    # -2 (local) and 100 (Strava) are referenced by a timeline item; -1 and
    # 200 are orphaned. The prune is scoped to id < 0, so 200 survives too.
    for pos, aid in enumerate((-2, 100)):
        _seed_row(engine, "projectitem", DBProjectItem(
            project_id=1, position=pos, item_type="activity", activity_id=aid))

    command.upgrade(cfg, _PRUNE_REV)

    assert _activity_ids(engine) == {-2, 100, 200}, (
        "prune must delete only the orphaned negative-id row (-1), never a "
        "referenced row (-2, 100) nor any positive id (200)"
    )


def test_prune_is_noop_on_clean_db(db):
    """Idempotent: re-running the prune on an already-clean DB changes nothing."""
    cfg = _cfg(db)
    command.upgrade(cfg, "head")            # includes the prune
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    before = _activity_ids(engine)
    command.stamp(cfg, _PREV_REV)           # rewind bookkeeping only
    command.upgrade(cfg, _PRUNE_REV)        # run the prune again
    assert _activity_ids(engine) == before

"""Data-migration test for 90b8faeea0f3 — backfill split_parent_id (issue #143).

Builds a throwaway SQLite DB up to the revision *before* the backfill, seeds a
split family plus an unrelated activity, runs the migration and asserts each row
inherits split_root_id as its parent — the conservative choice that reproduces
the pre-#143 behaviour for legacy data without ever over-deleting. Hermetic —
never touches the developer's real db.
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, MetaData, Table, text

from models.project_db import DBActivity

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PREV_REV = "c8f1a2b3d4e5"      # down_revision of the backfill migration
_BACKFILL_REV = "90b8faeea0f3"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "split_parent_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    return db_path


def _seed_activity_row(engine, **kwargs) -> None:
    """Insert an activity row using only the columns that exist AT _PREV_REV.

    Same trick as test_migration_prune_orphaned_tails: DBActivity(**kwargs) gets
    every NOT NULL column its model default, then the values are filtered to the
    frozen historical table so columns added later (split_parent_id itself) are
    not inserted before the migration creates them.
    """
    obj = DBActivity(**kwargs)
    data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_sa_")}
    md = MetaData()
    tbl = Table("activity", md, autoload_with=engine)
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


def _parents(engine) -> dict[int, int | None]:
    with engine.connect() as conn:
        return {
            r[0]: r[1]
            for r in conn.execute(text("SELECT id, split_parent_id FROM activity"))
        }


def test_backfill_seeds_the_parent_from_the_root(db):
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)

    engine = create_engine(f"sqlite:///{db.as_posix()}")
    # A family rooted at 100 (two pieces, one cut out of the other in reality —
    # unknowable from the data) plus an unrelated activity that was never split.
    _seed_activity_row(engine, id=100, user_info_id=1, name="Ride", type="Ride")
    _seed_activity_row(engine, id=-1, user_info_id=1, name="Ride (2/3)",
                       type="Ride", split_root_id=100)
    _seed_activity_row(engine, id=-2, user_info_id=1, name="Ride (3/3)",
                       type="Ride", split_root_id=100)
    _seed_activity_row(engine, id=200, user_info_id=1, name="Run", type="Run")

    command.upgrade(cfg, _BACKFILL_REV)

    assert _parents(engine) == {100: None, -1: 100, -2: 100, 200: None}, (
        "every piece adopts its root as parent — flattening the chain rather "
        "than guessing it. The root and a never-split activity stay NULL."
    )


def test_backfill_leaves_a_fresh_db_empty(db):
    """Nothing to backfill on a DB with no split families — the column is added
    and every row keeps a NULL parent."""
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    _seed_activity_row(engine, id=100, user_info_id=1, name="Ride", type="Ride")

    command.upgrade(cfg, _BACKFILL_REV)

    assert _parents(engine) == {100: None}

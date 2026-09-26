"""Data-migration test for 5e2b7c1d9a40: trip names unique per owner (#452).

The app has only ever checked a name was free before inserting, so two
requests racing could leave one owner with two trips of the same name, one of
them unreachable by name. The migration keeps the oldest under its name and
renames the others to the first free ``"<name> (n)"`` before adding the unique
index. Hermetic: a throwaway SQLite DB built from the migrations alone.
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PREV_REV = "04a606ace483"
_REV = "5e2b7c1d9a40"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "unique_name_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    return db_path


def _add(engine, pid: int, uid: int, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO project (id, user_info_id, name, version, lock_version,"
            " filter_state_json, created_at, updated_at, track_color, track_width,"
            " alternating_track_colors, elevation_chart_show_line, color_by_type,"
            " type_styles_json)"
            " VALUES (:id, :uid, :name, 1, 0, '{}', 0, 0, '#F97316', 2.5, 0, 1, 0, '{}')"
        ), {"id": pid, "uid": uid, "name": name})


def _names(engine) -> dict[int, tuple[int, str]]:
    with engine.connect() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            text("SELECT id, user_info_id, name FROM project"))}


def test_duplicates_are_renamed_and_the_oldest_keeps_its_name(db):
    cfg = _cfg(db)
    command.upgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    _add(engine, 1, 1, "Alps")
    _add(engine, 2, 1, "Alps")
    _add(engine, 3, 1, "Alps (2)")    # already taken: the rename skips it
    _add(engine, 4, 1, "Alps")
    _add(engine, 5, 2, "Alps")        # another owner: not a duplicate
    _add(engine, 6, 1, "Coast")

    command.upgrade(cfg, _REV)

    assert _names(engine) == {
        1: (1, "Alps"),
        2: (1, "Alps (3)"),
        3: (1, "Alps (2)"),
        4: (1, "Alps (4)"),
        5: (2, "Alps"),
        6: (1, "Coast"),
    }


def test_after_the_migration_a_duplicate_is_refused(db):
    cfg = _cfg(db)
    command.upgrade(cfg, _REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    _add(engine, 1, 1, "Alps")

    with pytest.raises(IntegrityError):
        _add(engine, 2, 1, "Alps")
    _add(engine, 3, 2, "Alps")


def test_downgrade_drops_the_index(db):
    cfg = _cfg(db)
    command.upgrade(cfg, _REV)
    command.downgrade(cfg, _PREV_REV)
    engine = create_engine(f"sqlite:///{db.as_posix()}")

    _add(engine, 1, 1, "Alps")
    _add(engine, 2, 1, "Alps")

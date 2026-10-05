"""Photo order state, dense photo lists, ids never reused (#237, #511).

Revision 4b9d2e7a1c63 adds ``photo_order_json`` to ``memory`` and
``journalentry``, removes the ``null`` placeholders the positional writer left
in ``photos_json``, and rebuilds both tables as AUTOINCREMENT so a deleted
memory's or journal entry's id is never handed to the next one — a photo write
still in flight for the deleted row would otherwise land in a stranger
(docs/PHOTOS_ORDER_ORIENTATION_PLAN.md, D3 and D4c). These tests run it on a
database that already holds rows.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from models.project_db import DBJournalEntry, DBMemory

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BEFORE = "87200bcb9342"
_REVISION = "4b9d2e7a1c63"
_TABLES = ("memory", "journalentry")


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "photo_order.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["db_path"] = db_path
    return config


def _connect(cfg):
    """Closed on exit: ``with sqlite3.connect()`` alone only ends the
    transaction, and a connection left open holds the file under the next
    migration step."""
    return closing(sqlite3.connect(cfg.attributes["db_path"]))


def _insert_memory(conn, memory_id: int | None, photos) -> int:
    cur = conn.execute(
        "INSERT INTO memory (id, project_id, date, photos_json, geo_mode, public_id) "
        "VALUES (?, 1, '2026-01-01', ?, 'start_of_day', lower(hex(randomblob(16))))",
        (memory_id, json.dumps(photos)),
    )
    conn.commit()
    return cur.lastrowid


def _insert_entry(conn, entry_id: int | None, photos) -> int:
    cur = conn.execute(
        "INSERT INTO journalentry (id, project_id, date, photos_json, geo_mode) "
        "VALUES (?, 1, '2026-01-01', ?, 'start_of_day')",
        (entry_id, json.dumps(photos)),
    )
    conn.commit()
    return cur.lastrowid


def _table_sql(conn, table: str) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]


def _shape(conn, table: str) -> dict:
    """Everything the rebuild must keep: indexes (with their SQL, so a partial
    index keeps its WHERE), foreign keys, and each column's type, nullability
    and default."""
    return {
        "indexes": sorted(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall()),
        "foreign_keys": sorted(
            (r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA foreign_key_list({table})")
        ),
        "columns": sorted(
            (r[1], r[2], r[3], r[4], r[5]) for r in conn.execute(f"PRAGMA table_info({table})")
            if r[1] != "photo_order_json"
        ),
    }


def _photos(conn, table: str) -> list[tuple[int, list]]:
    return [
        (row_id, json.loads(raw)) for row_id, raw in conn.execute(
            f"SELECT id, photos_json FROM {table} ORDER BY id"
        )
    ]


def test_upgrade_compacts_nulls_and_keeps_rows_ids_and_schema(cfg):
    command.upgrade(cfg, _BEFORE)
    with _connect(cfg) as conn:
        _insert_memory(conn, 3, ["a", None, "b"])
        _insert_memory(conn, 8, ["c", "d"])
        _insert_entry(conn, 5, [None])
        _insert_entry(conn, 6, ["e"])
        shapes_before = {t: _shape(conn, t) for t in _TABLES}
        for table in _TABLES:
            assert "AUTOINCREMENT" not in _table_sql(conn, table).upper()

    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        assert _photos(conn, "memory") == [(3, ["a", "b"]), (8, ["c", "d"])]
        assert _photos(conn, "journalentry") == [(5, []), (6, ["e"])]
        for table in _TABLES:
            assert "AUTOINCREMENT" in _table_sql(conn, table).upper()
            assert _shape(conn, table) == shapes_before[table]
            assert conn.execute(
                f"SELECT photo_order_json FROM {table}"
            ).fetchall() == [(None,), (None,)]
        # The shape comparison above covers the partial unique indexes too:
        # make sure they were there to compare, WHERE clause included.
        for table, index in (
            ("memory", "uq_memory_project_polarsteps_step_id"),
            ("journalentry", "uq_journalentry_project_client_token"),
        ):
            assert "IS NOT NULL" in dict(shapes_before[table]["indexes"])[index]


def test_a_clean_list_is_left_byte_for_byte(cfg):
    command.upgrade(cfg, _BEFORE)
    with _connect(cfg) as conn:
        conn.execute(
            "INSERT INTO memory (id, project_id, date, photos_json, geo_mode, public_id) "
            "VALUES (1, 1, '2026-01-01', '[\"a\",\"b\"]', 'start_of_day', 'p1'), "
            "(2, 1, '2026-01-01', 'not json', 'start_of_day', 'p2')"
        )
        conn.commit()

    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        assert conn.execute(
            "SELECT photos_json FROM memory ORDER BY id"
        ).fetchall() == [('["a","b"]',), ("not json",)]


def test_the_sequence_starts_above_the_current_max(cfg):
    command.upgrade(cfg, _BEFORE)
    with _connect(cfg) as conn:
        _insert_memory(conn, 4, [])
        _insert_memory(conn, 9, [])
        _insert_entry(conn, 12, [])

    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        sequences = dict(conn.execute("SELECT name, seq FROM sqlite_sequence"))
        assert sequences["memory"] == 9
        assert sequences["journalentry"] == 12
        assert _insert_memory(conn, None, []) == 10
        assert _insert_entry(conn, None, []) == 13


@pytest.mark.parametrize("insert", [_insert_memory, _insert_entry])
def test_a_deleted_id_is_not_handed_out_again_after_the_migration(cfg, insert):
    table = "memory" if insert is _insert_memory else "journalentry"
    command.upgrade(cfg, _BEFORE)
    with _connect(cfg) as conn:
        insert(conn, 1, [])
    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        newest = insert(conn, None, [])
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (newest,))
        conn.commit()
        assert insert(conn, None, []) == newest + 1


def test_downgrade_drops_the_column_and_autoincrement_and_keeps_rows(cfg):
    command.upgrade(cfg, _BEFORE)
    with _connect(cfg) as conn:
        shapes_before = {t: _shape(conn, t) for t in _TABLES}
    command.upgrade(cfg, _REVISION)
    with _connect(cfg) as conn:
        _insert_memory(conn, 5, ["a"])
        _insert_entry(conn, 7, ["b"])

    command.downgrade(cfg, _BEFORE)

    with _connect(cfg) as conn:
        for table in _TABLES:
            assert "AUTOINCREMENT" not in _table_sql(conn, table).upper()
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert "photo_order_json" not in columns
            assert _shape(conn, table) == shapes_before[table]
        assert _photos(conn, "memory") == [(5, ["a"])]
        assert _photos(conn, "journalentry") == [(7, ["b"])]


def test_the_models_themselves_never_reuse_an_id():
    """Tables built from metadata (tests, a fresh install) behave the same."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        for table in _TABLES:
            sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).scalar_one()
            assert "AUTOINCREMENT" in sql.upper()
            assert "photo_order_json" in sql

    with Session(engine) as sess:
        for make in (
            lambda: DBMemory(project_id=1, date="2026-01-01"),
            lambda: DBJournalEntry(project_id=1, date="2026-01-01"),
        ):
            first = make()
            sess.add(first)
            sess.commit()
            old_id = first.id
            sess.delete(first)
            sess.commit()

            second = make()
            sess.add(second)
            sess.commit()
            assert second.id != old_id
            assert second.photo_order_json is None

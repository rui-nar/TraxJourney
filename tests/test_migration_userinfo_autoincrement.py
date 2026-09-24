"""Account ids are never reused (issue #429).

SQLite without AUTOINCREMENT gives a new row ``max(id) + 1``, so deleting the
newest account handed its id to the next one registered. The id lives on in
Stripe metadata, where a late event would then name a stranger. The migration
rebuilds ``userinfo`` as AUTOINCREMENT on a database that already has users;
these tests run it on one.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from models.user import UserInfo

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BEFORE = "04a606ace483"
_REVISION = "6abe17b5d61f"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "autoinc.db"
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


def _insert_user(conn, user_id: int | None, email: str) -> int:
    cur = conn.execute(
        "INSERT INTO userinfo (id, google_sub, display_name, email, avatar_url, "
        "auth_provider, is_admin, email_verified, created_at, encryption_enabled) "
        "VALUES (?, '', ?, ?, '', 'local', 0, 0, 1.0, 0)",
        (user_id, email.upper(), email),
    )
    conn.commit()
    return cur.lastrowid


def _table_sql(conn) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='userinfo'"
    ).fetchone()[0]


def _indexes(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='userinfo'"
    )}


def test_existing_rows_and_indexes_survive_the_rebuild(cfg):
    command.upgrade(cfg, _BEFORE)
    with _connect(cfg) as conn:
        _insert_user(conn, 1, "a@x.io")
        _insert_user(conn, 7, "b@x.io")
        indexes_before = _indexes(conn)
        assert "AUTOINCREMENT" not in _table_sql(conn).upper()

    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        assert "AUTOINCREMENT" in _table_sql(conn).upper()
        assert conn.execute(
            "SELECT id, email, display_name FROM userinfo ORDER BY id"
        ).fetchall() == [(1, "a@x.io", "A@X.IO"), (7, "b@x.io", "B@X.IO")]
        assert _indexes(conn) == indexes_before
        assert "ix_userinfo_email" in indexes_before


def test_the_sequence_starts_above_the_current_max(cfg):
    command.upgrade(cfg, _BEFORE)
    with _connect(cfg) as conn:
        _insert_user(conn, 3, "a@x.io")
        _insert_user(conn, 9, "b@x.io")

    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        assert conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='userinfo'"
        ).fetchone() == (9,)
        assert _insert_user(conn, None, "c@x.io") == 10


def test_a_deleted_id_is_not_handed_out_again_after_the_migration(cfg):
    command.upgrade(cfg, _BEFORE)
    with _connect(cfg) as conn:
        _insert_user(conn, 1, "a@x.io")
    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        newest = _insert_user(conn, None, "b@x.io")
        conn.execute("DELETE FROM userinfo WHERE id = ?", (newest,))
        conn.commit()
        assert _insert_user(conn, None, "c@x.io") == newest + 1


def test_downgrade_restores_the_plain_table_and_keeps_rows(cfg):
    command.upgrade(cfg, _REVISION)
    with _connect(cfg) as conn:
        _insert_user(conn, 5, "a@x.io")

    command.downgrade(cfg, _BEFORE)

    with _connect(cfg) as conn:
        assert "AUTOINCREMENT" not in _table_sql(conn).upper()
        assert conn.execute("SELECT id FROM userinfo").fetchall() == [(5,)]


def test_the_model_itself_never_reuses_an_id():
    """Tables built from metadata (tests, a fresh install) behave the same."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        first = UserInfo(email="a@x.io")
        sess.add(first)
        sess.commit()
        old_id = first.id
        sess.delete(first)
        sess.commit()

        second = UserInfo(email="b@x.io")
        sess.add(second)
        sess.commit()
        assert second.id != old_id

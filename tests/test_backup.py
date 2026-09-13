"""Tests for the backup service — create, list, restore, and prune."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import src.backup.backup_service as svc


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Point the backup service at a real temporary SQLite database."""
    db_file = tmp_path / "traxjourney.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE kv (key TEXT, value TEXT)")
    conn.execute("INSERT INTO kv VALUES ('hello', 'world')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    return tmp_path


# ── backup_db ─────────────────────────────────────────────────────────────────


def test_backup_creates_file(db_env):
    dest = svc.backup_db()
    assert dest.exists()
    assert dest.suffix == ".db"


def test_backup_file_contains_data(db_env):
    dest = svc.backup_db()
    conn = sqlite3.connect(str(dest))
    rows = conn.execute("SELECT value FROM kv WHERE key='hello'").fetchall()
    conn.close()
    assert rows == [("world",)]


def test_backup_stored_in_backups_subdir(db_env):
    dest = svc.backup_db()
    assert dest.parent.name == "backups"


# ── list_backups ──────────────────────────────────────────────────────────────


def test_list_backups_empty_before_first_backup(db_env):
    assert svc.list_backups() == []


def test_list_backups_returns_entry_after_backup(db_env):
    svc.backup_db()
    entries = svc.list_backups()
    assert len(entries) == 1
    assert "date" in entries[0]
    assert "size_bytes" in entries[0]
    assert entries[0]["size_bytes"] > 0


def test_list_backups_newest_first(db_env, monkeypatch):
    backup_dir = svc._backup_dir()
    stem = svc._stem()
    # Create two fake backup files with different dates
    for date in ("2026-01-01", "2026-06-01"):
        f = backup_dir / f"{stem}_{date}.db"
        f.write_bytes(b"x")
    entries = svc.list_backups()
    assert entries[0]["date"] == "2026-06-01"
    assert entries[1]["date"] == "2026-01-01"


# ── restore_db ────────────────────────────────────────────────────────────────


def test_restore_raises_for_missing_date(db_env):
    with pytest.raises(FileNotFoundError):
        svc.restore_db("2000-01-01")


def test_restore_replaces_db_content(db_env, monkeypatch):
    # Take a backup of the original DB (has 'world')
    dest = svc.backup_db()
    date_str = dest.stem.split("_", 1)[1]

    # Mutate the live DB
    db_path = svc._db_path()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE kv SET value='mutated' WHERE key='hello'")
    conn.commit()
    conn.close()

    # Stub engine.dispose so it doesn't import the real SQLAlchemy engine
    import models.db as db_module
    monkeypatch.setattr(db_module.engine, "dispose", lambda: None)

    svc.restore_db(date_str)

    # Live DB should be back to 'world'
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT value FROM kv WHERE key='hello'").fetchall()
    conn.close()
    assert rows == [("world",)]


# ── prune ─────────────────────────────────────────────────────────────────────


def test_prune_keeps_at_most_30(db_env):
    backup_dir = svc._backup_dir()
    stem = svc._stem()
    # Create 35 fake backup files
    for i in range(35):
        f = backup_dir / f"{stem}_2026-{i:02d}-01.db"
        f.write_bytes(b"x")

    svc._prune_old_backups()

    remaining = list(backup_dir.glob(f"{stem}_*.db"))
    assert len(remaining) == 30

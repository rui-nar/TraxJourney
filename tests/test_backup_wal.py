"""Backup/restore must remain correct now that the app runs SQLite in WAL mode.

The online .backup() API captures committed WAL data, and restore must fold the
restored frames into the main file (truncating the live WAL) so nothing stale
replays over them. Backups must be single self-contained files (no orphan -wal).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import create_engine

import models.db as db_module
from src.backup import backup_service


def _make_engine(monkeypatch, tmp_path):
    dbfile = tmp_path / "traxjourney.db"
    url = f"sqlite:///{dbfile}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    db_module._configure_sqlite(eng)
    monkeypatch.setattr(db_module, "engine", eng)
    return eng


def test_backup_restore_roundtrip_under_wal(tmp_path, monkeypatch):
    eng = _make_engine(monkeypatch, tmp_path)
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
        c.exec_driver_sql("INSERT INTO kv VALUES ('a', 'original')")

    # The engine really is in WAL mode (sanity-check the precondition).
    with eng.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"

    dest = backup_service.backup_db()
    assert dest.exists()
    # Self-contained: the checkpoint(TRUNCATE) left no sidecar beside the backup.
    assert not Path(str(dest) + "-wal").exists()

    # Mutate AFTER the backup, then restore — the change must be rolled back.
    with eng.begin() as c:
        c.exec_driver_sql("UPDATE kv SET v='changed' WHERE k='a'")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_service.restore_db(date_str)

    with eng.connect() as c:
        assert c.exec_driver_sql("SELECT v FROM kv WHERE k='a'").scalar() == "original"


def test_restore_missing_backup_raises(tmp_path, monkeypatch):
    _make_engine(monkeypatch, tmp_path)
    try:
        backup_service.restore_db("1999-01-01")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass

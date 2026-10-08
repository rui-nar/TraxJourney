"""Migration a8d3f5c2e917: the Strava code-binding table
(docs/STRAVA_CONNECT_BINDING_PLAN.md D9).

One new table keyed by the code's hash; nothing existing is migrated, and the
downgrade drops it.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BEFORE = "c4e2a9f1b7d3"
_REVISION = "a8d3f5c2e917"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "strava_code.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["db_path"] = db_path
    return config


def _connect(cfg):
    return closing(sqlite3.connect(cfg.attributes["db_path"]))


def _tables(cfg) -> set[str]:
    with _connect(cfg) as conn:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


def test_it_follows_the_e2ee_remnants_migration():
    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    assert script.get_revision(_REVISION).down_revision == _BEFORE


def test_upgrade_creates_the_binding_table_keyed_by_code_hash(cfg):
    command.upgrade(cfg, _BEFORE)
    assert "strava_oauth_code" not in _tables(cfg)

    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        columns = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(strava_oauth_code)")}
    assert columns == {"code_hash": 1, "state_jti": 0, "expires_at": 0}  # name → pk


def test_downgrade_drops_the_binding_table(cfg):
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _BEFORE)
    assert "strava_oauth_code" not in _tables(cfg)

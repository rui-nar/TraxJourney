"""Migration e3bb990551f8: the refund ledger (issue #441, review round 2).

The ledger's rows must survive account deletion — no foreign key to
``userinfo`` — and ``withdrawal_requested_at``, which kept the window open
after a failed cancellation, goes.
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
_BEFORE = "3828d92db32c"
_REVISION = "e3bb990551f8"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger.db"
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


def _subscription_columns(cfg) -> set[str]:
    with _connect(cfg) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(subscription)")}


def test_it_follows_the_contract_migration():
    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    # The single head is ed0f801e164c; see test_migration_refund_states.py.
    assert script.get_revision(_REVISION).down_revision == _BEFORE


def test_upgrade_creates_the_ledger_without_a_link_to_accounts(cfg):
    command.upgrade(cfg, _BEFORE)
    assert "withdrawal_requested_at" in _subscription_columns(cfg)

    command.upgrade(cfg, _REVISION)

    assert "subscription_refund" in _tables(cfg)
    assert "withdrawal_requested_at" not in _subscription_columns(cfg)
    with _connect(cfg) as conn:
        assert conn.execute(
            "PRAGMA foreign_key_list(subscription_refund)").fetchall() == []
        columns = {r[1] for r in conn.execute("PRAGMA table_info(subscription_refund)")}
    assert {"subscription_id", "customer_id", "state", "attempt", "amount",
            "refunded", "invoice_id", "credit_note_id", "reason",
            "lease_until"} <= columns
    assert "user_info_id" not in columns


def test_downgrade_restores_the_column_and_drops_the_ledger(cfg):
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _BEFORE)
    assert "subscription_refund" not in _tables(cfg)
    assert "withdrawal_requested_at" in _subscription_columns(cfg)

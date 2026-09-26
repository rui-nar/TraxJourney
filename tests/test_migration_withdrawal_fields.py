"""Migration 2d5c660f9f6e: the withdrawal fields on ``subscription`` (#441).

Run on a database that already has subscribers, because the backfill is the
point: an account that reached Stripe before the purchase date was tracked has
no date to go on, and its window must come out closed, not open.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.billing.refunds import withdrawal_window_open

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BEFORE = "6abe17b5d61f"
_REVISION = "2d5c660f9f6e"
_NEW_COLUMNS = {"initial_paid_at", "terms_accepted_at", "terms_version", "withdrawn_at"}


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "withdrawal.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["db_path"] = db_path
    return config


def _connect(cfg):
    return closing(sqlite3.connect(cfg.attributes["db_path"]))


def _columns(cfg) -> set[str]:
    with _connect(cfg) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(subscription)")}


def _seed(cfg) -> None:
    """Two users: one who subscribed, one who only opened a checkout."""
    with _connect(cfg) as conn:
        for uid in (1, 2):
            conn.execute(
                "INSERT INTO userinfo (id, google_sub, display_name, email, "
                "avatar_url, auth_provider, is_admin, email_verified, created_at, "
                "encryption_enabled) VALUES (?, '', 'U', ?, '', 'local', 0, 0, 1.0, 0)",
                (uid, f"u{uid}@x.io"),
            )
        conn.execute(
            "INSERT INTO subscription (user_info_id, plan, status, provider, "
            "provider_customer_id, provider_subscription_id, current_period_end, "
            "cancel_at_period_end, pending_plan, pending_plan_at, "
            "admin_override_plan, last_event_id, last_event_at, updated_at) "
            "VALUES (1, 'tier_2', 'active', 'stripe', 'cus_1', 'sub_1', 0, 0, '', "
            "0, '', '', 0, 1.0)"
        )
        conn.execute(
            "INSERT INTO subscription (user_info_id, plan, status, provider, "
            "provider_customer_id, provider_subscription_id, current_period_end, "
            "cancel_at_period_end, pending_plan, pending_plan_at, "
            "admin_override_plan, last_event_id, last_event_at, updated_at) "
            "VALUES (2, 'free', 'none', 'stripe', 'cus_2', '', 0, 0, '', "
            "0, '', '', 0, 1.0)"
        )
        conn.commit()


def test_it_follows_the_account_id_migration():
    """The single head is 3828d92db32c, which follows this one; see
    test_migration_withdrawal_per_contract.py."""
    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    assert script.get_revision(_REVISION).down_revision == _BEFORE


def test_upgrade_adds_the_columns_and_closes_existing_subscribers_windows(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed(cfg)
    assert not (_columns(cfg) & _NEW_COLUMNS)

    command.upgrade(cfg, _REVISION)

    assert _NEW_COLUMNS <= _columns(cfg)
    with _connect(cfg) as conn:
        rows = dict(conn.execute(
            "SELECT user_info_id, initial_paid_at FROM subscription").fetchall())
        consent = conn.execute(
            "SELECT terms_accepted_at, terms_version, withdrawn_at "
            "FROM subscription WHERE user_info_id = 1").fetchone()
    # A subscriber from before: a purchase date, and a closed window.
    assert rows[1] == 1.0
    assert withdrawal_window_open(rows[1], 1_790_000_000.0) is False
    # Never subscribed: no date, so their first purchase will set it.
    assert rows[2] == 0
    assert consent == (0, "", 0)


def test_downgrade_removes_them_and_keeps_the_rows(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed(cfg)
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _BEFORE)

    assert not (_columns(cfg) & _NEW_COLUMNS)
    with _connect(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM subscription").fetchone()[0] == 2

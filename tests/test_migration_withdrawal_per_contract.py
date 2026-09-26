"""Migration 3828d92db32c: the withdrawal window per contract (issue #441).

Owner decision of 2026-09-26: each new subscription after the previous one
ended opens its own window. The backfill must therefore close the window only
for subscriptions still in force when this shipped, and leave every other row
free to open one with its next subscription — including one that only ever
held a subscription that was never paid (finding 10).
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
_BEFORE = "2d5c660f9f6e"
_REVISION = "3828d92db32c"
_ADDED = {"contract_subscription_id", "withdrawal_requested_at",
          "withdrawn_subscription_id"}

#: user id -> (status, subscription id)
_ROWS = {
    1: ("active", "sub_a"),
    2: ("trialing", "sub_t"),
    3: ("past_due", "sub_p"),
    4: ("unpaid", "sub_u"),
    5: ("paused", "sub_z"),
    6: ("canceled", "sub_c"),
    7: ("incomplete", "sub_i"),
    8: ("incomplete_expired", "sub_x"),
    9: ("none", ""),
}
_IN_FORCE = {1, 2, 3, 4, 5}


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "contract.db"
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
    with _connect(cfg) as conn:
        for uid, (status, sub_id) in _ROWS.items():
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
                "admin_override_plan, last_event_id, last_event_at, updated_at, "
                "initial_paid_at, terms_accepted_at, terms_version, withdrawn_at) "
                "VALUES (?, 'tier_2', ?, 'stripe', ?, ?, 0, 0, '', 0, '', '', 0, "
                "1.0, ?, 0, '', 0)",
                # What 2d5c660f9f6e's backfill left: 1.0 on every row that
                # named a subscription. This migration must undo it where the
                # subscription is not in force.
                (uid, status, f"cus_{uid}", sub_id, 1.0 if sub_id else 0.0),
            )
        conn.commit()


def _contracts(cfg) -> dict[int, tuple]:
    with _connect(cfg) as conn:
        return {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT user_info_id, contract_started_at, contract_subscription_id, "
                "withdrawal_requested_at, withdrawn_subscription_id FROM subscription"
            )
        }


def test_it_follows_the_withdrawal_fields_migration():
    """The single head is e3bb990551f8, which follows this one; see
    test_migration_refund_ledger.py."""
    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    assert script.get_revision(_REVISION).down_revision == _BEFORE


def test_upgrade_renames_the_start_and_adds_the_columns(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed(cfg)

    command.upgrade(cfg, _REVISION)

    columns = _columns(cfg)
    assert _ADDED <= columns
    assert "contract_started_at" in columns
    assert "initial_paid_at" not in columns
    with _connect(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM subscription").fetchone()[0] == len(_ROWS)


def test_only_subscriptions_still_in_force_are_closed(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed(cfg)
    command.upgrade(cfg, _REVISION)

    contracts = _contracts(cfg)
    for uid, (status, sub_id) in _ROWS.items():
        if uid in _IN_FORCE:
            # That subscription is the contract, from before tracking: closed,
            # and its renewals (same id) cannot open a window.
            assert contracts[uid] == (1.0, sub_id, 0.0, ""), status
        else:
            # Ended, never paid, or nothing at all: the next subscription it
            # starts opens a window.
            assert contracts[uid] == (0.0, "", 0.0, ""), status


def test_downgrade_restores_the_first_purchase_column(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed(cfg)
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _BEFORE)

    columns = _columns(cfg)
    assert not (_ADDED & columns)
    assert "initial_paid_at" in columns and "contract_started_at" not in columns
    with _connect(cfg) as conn:
        rows = dict(conn.execute(
            "SELECT user_info_id, initial_paid_at FROM subscription").fetchall())
    # 2d5c660f9f6e's rule again: anyone who ever named a subscription had one.
    assert all(rows[uid] == 1.0 for uid, (_s, sub) in _ROWS.items() if sub)
    assert rows[9] == 0

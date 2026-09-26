"""Migration ed0f801e164c: owed is final, settled tombstones, fencing (#441).

On a database that already has ledger rows and subscribers: a refund Stripe
refused becomes ``owed`` with what is owed frozen; every existing subscription
counts as already judged for a contract; the unread withdrawal columns go.
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
_BEFORE = "e3bb990551f8"
_REVISION = "ed0f801e164c"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "states.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["db_path"] = db_path
    return config


def _connect(cfg):
    return closing(sqlite3.connect(cfg.attributes["db_path"]))


def _columns(cfg, table) -> set[str]:
    with _connect(cfg) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _seed(cfg) -> None:
    with _connect(cfg) as conn:
        conn.execute(
            "INSERT INTO userinfo (id, google_sub, display_name, email, avatar_url, "
            "auth_provider, is_admin, email_verified, created_at, encryption_enabled) "
            "VALUES (1, '', 'U', 'u@x.io', '', 'local', 0, 0, 1.0, 0)")
        conn.execute(
            "INSERT INTO subscription (user_info_id, plan, status, provider, "
            "provider_customer_id, provider_subscription_id, current_period_end, "
            "cancel_at_period_end, pending_plan, pending_plan_at, admin_override_plan, "
            "last_event_id, last_event_at, updated_at, contract_started_at, "
            "contract_subscription_id, terms_accepted_at, terms_version, "
            "withdrawn_at, withdrawn_subscription_id) VALUES (1, 'tier_2', 'active', "
            "'stripe', 'cus_1', 'sub_b', 0, 0, '', 0, '', '', 0, 1.0, 0, '', 0, '', "
            "5.0, 'sub_a')")
        for sub, state, amount, refunded in (("sub_a", "failed_permanent", 266, 100),
                                             ("sub_c", "done", 50, 50)):
            conn.execute(
                "INSERT INTO subscription_refund (subscription_id, customer_id, state, "
                "attempt, amount, refunded, currency, invoice_id, credit_note_id, "
                "reason, requested_at, lease_until, created_at, updated_at) VALUES "
                "(?, 'cus_1', ?, 0, ?, ?, 'eur', 'in_1', '', 'x', 0, 0, 0, 0)",
                (sub, state, amount, refunded))
        conn.commit()


def test_it_is_the_single_head():
    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    assert script.get_heads() == [_REVISION]
    assert script.get_revision(_REVISION).down_revision == _BEFORE


def test_upgrade(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed(cfg)

    command.upgrade(cfg, _REVISION)

    assert {"withdrawn_at", "withdrawn_subscription_id"}.isdisjoint(
        _columns(cfg, "subscription"))
    assert {"contract_started_at", "to_refund", "owed", "claim_token",
            "settled_at"} <= _columns(cfg, "subscription_refund")
    with _connect(cfg) as conn:
        ledger = {r[0]: r[1:] for r in conn.execute(
            "SELECT subscription_id, state, owed, to_refund FROM subscription_refund")}
        checked = conn.execute(
            "SELECT contract_checked_subscription_id FROM subscription").fetchone()[0]
    assert ledger["sub_a"] == ("owed", 166, 100)
    assert ledger["sub_c"] == ("done", 0, 50)
    # Already judged: an existing subscription never becomes a contract late.
    assert checked == "sub_b"


def test_downgrade(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed(cfg)
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _BEFORE)

    assert {"withdrawn_at", "withdrawn_subscription_id"} <= _columns(cfg, "subscription")
    assert "claim_token" not in _columns(cfg, "subscription_refund")
    with _connect(cfg) as conn:
        state = conn.execute("SELECT state FROM subscription_refund "
                             "WHERE subscription_id = 'sub_a'").fetchone()[0]
    assert state == "failed_permanent"

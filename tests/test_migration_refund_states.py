"""Migration ed0f801e164c: owed is final, settled tombstones, fencing (#441).

On a database that already has ledger rows and subscribers:
* a refund Stripe refused becomes ``owed`` with what is owed frozen;
* only subscriptions in force count as already judged for a contract — one
  still ``incomplete`` has not had its first paid event (review round 4);
* a pending refund gets its contract's start (review round 4);
* the unread withdrawal columns go;
* downgrading never turns a settled refund into one the older code retries.
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
        for uid, status, sub, contract, start in (
            (1, "active", "sub_b", "sub_p", 777.0),
            (2, "incomplete", "sub_i", "", 0.0),
        ):
            conn.execute(
                "INSERT INTO userinfo (id, google_sub, display_name, email, avatar_url, "
                "auth_provider, is_admin, email_verified, created_at, encryption_enabled) "
                "VALUES (?, '', 'U', ?, '', 'local', 0, 0, 1.0, 0)", (uid, f"u{uid}@x.io"))
            conn.execute(
                "INSERT INTO subscription (user_info_id, plan, status, provider, "
                "provider_customer_id, provider_subscription_id, current_period_end, "
                "cancel_at_period_end, pending_plan, pending_plan_at, admin_override_plan, "
                "last_event_id, last_event_at, updated_at, contract_started_at, "
                "contract_subscription_id, terms_accepted_at, terms_version, "
                "withdrawn_at, withdrawn_subscription_id) VALUES (?, 'tier_2', ?, "
                "'stripe', ?, ?, 0, 0, '', 0, '', '', 0, 1.0, ?, ?, 0, '', 5.0, 'sub_a')",
                (uid, status, f"cus_{uid}", sub, start, contract))
        for sub, state, amount, refunded in (("sub_a", "failed_permanent", 266, 100),
                                             ("sub_c", "done", 50, 50),
                                             ("sub_p", "pending", -1, 0)):
            conn.execute(
                "INSERT INTO subscription_refund (subscription_id, customer_id, state, "
                "attempt, amount, refunded, currency, invoice_id, credit_note_id, "
                "reason, requested_at, lease_until, created_at, updated_at) VALUES "
                "(?, 'cus_1', ?, 0, ?, ?, 'eur', 'in_1', '', 'x', 0, 0, 0, 0)",
                (sub, state, amount, refunded))
        conn.commit()


def test_it_follows_the_ledger_migration():
    """The single head is ca17b22c22d5, which follows this one."""
    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    assert script.get_revision(_REVISION).down_revision == _BEFORE
    assert script.get_heads() == ["ca17b22c22d5"]
    assert script.get_revision("ca17b22c22d5").down_revision == _REVISION


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
            "SELECT subscription_id, state, owed, to_refund, contract_started_at "
            "FROM subscription_refund")}
        checked = dict(conn.execute(
            "SELECT user_info_id, contract_checked_subscription_id FROM subscription"))
    assert ledger["sub_a"][:3] == ("owed", 166, 100)
    assert ledger["sub_c"][:3] == ("done", 0, 50)
    # A pending refund knows its contract's start, to be completed late.
    assert ledger["sub_p"][0] == "pending" and ledger["sub_p"][3] == 777.0
    # In force: judged already. Incomplete: not yet — its first paid event
    # must still be able to start a contract.
    assert checked == {1: "sub_b", 2: ""}


def test_downgrade_never_hands_a_settled_refund_back_for_retrying(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed(cfg)
    command.upgrade(cfg, _REVISION)
    with _connect(cfg) as conn:
        conn.execute("UPDATE subscription_refund SET state = 'settled' "
                     "WHERE subscription_id = 'sub_c'")
        conn.commit()

    command.downgrade(cfg, _BEFORE)

    assert {"withdrawn_at", "withdrawn_subscription_id"} <= _columns(cfg, "subscription")
    assert "claim_token" not in _columns(cfg, "subscription_refund")
    with _connect(cfg) as conn:
        states = dict(conn.execute("SELECT subscription_id, state FROM subscription_refund"))
    assert states["sub_a"] == "failed_permanent"
    assert states["sub_c"] == "done"  # final in the older code, never retried


def test_the_refund_id_migration_up_and_down(cfg):
    command.upgrade(cfg, "ca17b22c22d5")
    assert "refund_id" in _columns(cfg, "subscription_refund")
    command.downgrade(cfg, _REVISION)
    assert "refund_id" not in _columns(cfg, "subscription_refund")

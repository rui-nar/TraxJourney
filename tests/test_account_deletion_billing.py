"""Deleting an account cancels its paid plan first (issue #429).

Before this, both deletion routes removed the local ``Subscription`` row and
left the subscription running at Stripe — charging someone who no longer had an
account to cancel it from.

Drives the real ``api.router.app`` so the ``AccountDeletionRefused`` handler is
the one production uses. The payment provider is a fake gateway; each test that
must *not* reach it installs one anyway and asserts it stayed untouched.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from api.router import app
from models.billing import Subscription, UserUsage
from models.project_db import DBMemory, DBProject
from models.user import LocalUser, UserInfo
from src.billing.gateway import GatewayError, set_gateway


class FakeGateway:
    """Records cancellations, and what the DB looked like when each happened."""

    def __init__(self, engine, *, fail=False, event=None):
        self.engine = engine
        self.fail = fail
        self.event = event or {}
        self.cancel_calls: list[str] = []
        self.rows_at_cancel: list[bool] = []

    def cancel_subscription(self, subscription_id):
        self.cancel_calls.append(subscription_id)
        # The cancel must come before anything is deleted — look from a
        # separate session, as another request would.
        with Session(self.engine) as sess:
            self.rows_at_cancel.append(
                sess.exec(select(Subscription)).first() is not None
                and sess.exec(select(UserInfo)).first() is not None
            )
        if self.fail:
            raise GatewayError("provider down")

    def parse_webhook(self, payload, signature):
        return self.event


@pytest.fixture
def engine(monkeypatch, tmp_path):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(test_engine)
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield test_engine
    app.dependency_overrides.clear()
    set_gateway(None)


def _seed(engine, *, status="active", sub_id="sub_live", admin=False,
          with_subscription=True) -> int:
    """A user with a trip, a memory, usage and (optionally) a subscription."""
    with Session(engine) as sess:
        local = LocalUser(username=f"u{time.time_ns()}@x.io")
        sess.add(local)
        sess.commit()
        sess.refresh(local)
        user = UserInfo(local_auth_id=local.id, email="u@x.io", is_admin=admin)
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id
        proj = DBProject(user_info_id=uid, name="Trip")
        sess.add(proj)
        sess.commit()
        sess.refresh(proj)
        sess.add(DBMemory(project_id=proj.id, date="2026-01-01"))
        sess.add(UserUsage(user_info_id=uid, storage_bytes=10))
        if with_subscription:
            sess.add(Subscription(
                user_info_id=uid, plan="tier_2", status=status,
                provider_customer_id="cus_1", provider_subscription_id=sub_id,
            ))
        sess.commit()
    return uid


def _everything_present(engine, uid) -> bool:
    with Session(engine) as sess:
        return (
            sess.get(UserInfo, uid) is not None
            and sess.exec(select(Subscription).where(
                Subscription.user_info_id == uid)).first() is not None
            and sess.exec(select(UserUsage).where(
                UserUsage.user_info_id == uid)).first() is not None
            and sess.exec(select(DBProject).where(
                DBProject.user_info_id == uid)).first() is not None
            and sess.exec(select(DBMemory)).first() is not None
        )


def _everything_gone(engine, uid) -> bool:
    with Session(engine) as sess:
        return (
            sess.get(UserInfo, uid) is None
            and sess.exec(select(Subscription)).first() is None
            and sess.exec(select(UserUsage)).first() is None
            and sess.exec(select(DBProject)).first() is None
            and sess.exec(select(DBMemory)).first() is None
        )


def _as(uid):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uid), "email": "u@x.io", "auth_provider": "local",
    }
    return TestClient(app)


def _delete_me(uid):
    return _as(uid).delete("/api/auth/me")


def _admin_delete(engine, target):
    admin = _seed(engine, admin=True, with_subscription=False)
    return _as(admin).delete(f"/api/admin/users/{target}")


# ── Self-service DELETE /api/auth/me ─────────────────────────────────────────

class TestSelfDelete:
    @pytest.mark.parametrize(
        "status", ["active", "trialing", "past_due", "unpaid", "incomplete", "paused"]
    )
    def test_a_subscription_that_may_still_bill_is_cancelled_first(self, engine, status):
        uid = _seed(engine, status=status)
        gw = FakeGateway(engine)
        set_gateway(gw)

        res = _delete_me(uid)

        assert res.status_code == 200, res.text
        assert gw.cancel_calls == ["sub_live"]
        assert gw.rows_at_cancel == [True], "rows were deleted before the cancel"
        assert _everything_gone(engine, uid)

    def test_a_gateway_failure_refuses_and_deletes_nothing(self, engine):
        uid = _seed(engine)
        set_gateway(FakeGateway(engine, fail=True))

        res = _delete_me(uid)

        assert res.status_code == 502
        body = res.json()
        assert body["code"] == "subscription_cancel_failed"
        assert "not deleted" in body["detail"]
        assert _everything_present(engine, uid)

    def test_no_gateway_with_a_live_subscription_refuses(self, engine):
        """A self-hosted instance without Stripe keys cannot cancel it, so
        deleting would orphan a paying customer."""
        uid = _seed(engine)
        set_gateway(None)

        res = _delete_me(uid)

        assert res.status_code == 409
        body = res.json()
        assert body["code"] == "billing_unavailable"
        assert "not deleted" in body["detail"]
        assert _everything_present(engine, uid)

    @pytest.mark.parametrize("gateway_installed", [False, True])
    @pytest.mark.parametrize("status", [None, "none", "canceled", "incomplete_expired"])
    def test_nothing_that_can_bill_never_touches_the_gateway(
        self, engine, status, gateway_installed,
    ):
        """A free user (no row, or an admin comp's "none" row) and an ended
        subscription delete fine with or without a gateway."""
        uid = _seed(engine, status=status or "none",
                    with_subscription=status is not None)
        gw = FakeGateway(engine)
        set_gateway(gw if gateway_installed else None)

        res = _delete_me(uid)

        assert res.status_code == 200, res.text
        assert gw.cancel_calls == []
        with Session(engine) as sess:
            assert sess.get(UserInfo, uid) is None
            assert sess.exec(select(Subscription)).first() is None

    def test_a_retry_after_a_refusal_succeeds(self, engine):
        uid = _seed(engine)
        gw = FakeGateway(engine, fail=True)
        set_gateway(gw)
        assert _delete_me(uid).status_code == 502

        gw.fail = False
        assert _delete_me(uid).status_code == 200
        assert gw.cancel_calls == ["sub_live", "sub_live"]
        assert _everything_gone(engine, uid)


# ── Admin DELETE /api/admin/users/{id} ───────────────────────────────────────

class TestAdminDelete:
    def test_a_live_subscription_is_cancelled_first(self, engine):
        uid = _seed(engine)
        gw = FakeGateway(engine)
        set_gateway(gw)

        res = _admin_delete(engine, uid)

        assert res.status_code == 200, res.text
        assert gw.cancel_calls == ["sub_live"]
        assert gw.rows_at_cancel == [True]
        with Session(engine) as sess:
            assert sess.get(UserInfo, uid) is None
            assert sess.exec(select(Subscription).where(
                Subscription.user_info_id == uid)).first() is None

    def test_a_gateway_failure_refuses_and_deletes_nothing(self, engine):
        uid = _seed(engine)
        set_gateway(FakeGateway(engine, fail=True))

        res = _admin_delete(engine, uid)

        assert res.status_code == 502
        assert res.json()["code"] == "subscription_cancel_failed"
        assert _everything_present(engine, uid)

    def test_no_gateway_with_a_live_subscription_refuses(self, engine):
        uid = _seed(engine)
        set_gateway(None)

        res = _admin_delete(engine, uid)

        assert res.status_code == 409
        assert _everything_present(engine, uid)


# ── The provider's events after the account is gone ──────────────────────────

def _subscription_deleted_event(uid, *, created) -> dict:
    """What Stripe sends once the deletion's cancel has gone through."""
    return {
        "id": "evt_late", "type": "customer.subscription.deleted",
        "created": created + 5,
        "data": {"object": {
            "id": "sub_live", "customer": "cus_1", "status": "canceled",
            "created": created,
            "metadata": {"user_info_id": str(uid), "plan": "tier_2"},
            "items": {"data": [{"price": {"id": "price_t2"}}]},
        }},
    }


def _post_webhook(gw):
    set_gateway(gw)
    app.dependency_overrides.clear()
    return TestClient(app).post("/api/billing/webhook", content=b"{}",
                                headers={"stripe-signature": "good"})


class TestLateWebhook:
    def test_a_late_cancellation_event_recreates_nothing(self, engine):
        uid = _seed(engine)
        set_gateway(FakeGateway(engine))
        assert _delete_me(uid).status_code == 200

        res = _post_webhook(FakeGateway(
            engine, event=_subscription_deleted_event(uid, created=time.time() - 3600)))

        assert res.status_code == 200
        assert res.json() == {"received": True, "applied": False}
        with Session(engine) as sess:
            assert sess.exec(select(Subscription)).first() is None
            assert sess.exec(select(UserInfo)).first() is None

    def test_a_late_event_does_not_land_on_an_account_that_reused_the_id(self, engine):
        """SQLite hands the deleted account's id to the next one registered, and
        the event's metadata still names that id. Attaching it would give a
        stranger the old customer — and their next checkout would bill it."""
        bought_at = time.time() - 3600
        uid = _seed(engine)
        set_gateway(FakeGateway(engine))
        assert _delete_me(uid).status_code == 200

        with Session(engine) as sess:
            newcomer = UserInfo(email="new@x.io")
            sess.add(newcomer)
            sess.commit()
            sess.refresh(newcomer)
            assert newcomer.id == uid, "precondition: SQLite reused the id"

        res = _post_webhook(FakeGateway(
            engine, event=_subscription_deleted_event(uid, created=bought_at)))

        assert res.status_code == 200
        assert res.json()["applied"] is False
        with Session(engine) as sess:
            assert sess.exec(select(Subscription)).first() is None

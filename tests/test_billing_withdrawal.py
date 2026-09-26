"""Withdrawing inside the 14-day window, and deleting inside it (issue #441).

Drives the real ``api.router.app``. The provider is a fake that behaves like
Stripe where it matters here: cancelling records when the subscription ended,
and a refund made again under a key already used returns the first one instead
of paying twice. So "a retry does not refund twice" is checked by the money
the fake paid out, not by which helper was called.
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
from models.billing import Subscription
from models.user import LocalUser, UserInfo
from src.billing.gateway import GatewayError, RefundBasis, set_gateway

DAY = 24 * 60 * 60
#: The paid period on the fake invoice, and when the fake's cancellation lands:
#: ten days into a thirty-day period (now), so two thirds are unused.
PERIOD_START = time.time() - 10 * DAY  # the first period began ten days ago
PERIOD_END = PERIOD_START + 30 * DAY
CANCEL_AT = PERIOD_START + 10 * DAY
PAID = 400
EXPECTED = 266  # floor(400 * 20/30)
KEY = "traxjourney-unused-period-refund-sub_1"


class StripeLikeGateway:
    """Subscriptions that end when cancelled, and refunds deduplicated by key."""

    def __init__(self, *, amount_paid=PAID, fail_cancel=False, fail_refund=False,
                 subscriptions=("sub_1",), cancel_at=CANCEL_AT):
        self.amount_paid = amount_paid
        self.cancel_at = cancel_at
        self.fail_cancel = fail_cancel
        self.fail_refund = fail_refund
        self.ended: dict[str, float] = {sid: 0.0 for sid in subscriptions}
        self.paid_out: dict[str, int] = {}  # idempotency key -> cents
        self.log: list[tuple] = []

    # cancellation
    def cancel_subscription(self, subscription_id, customer_id=""):
        self.log.append(("cancel", subscription_id))
        if self.fail_cancel:
            raise GatewayError("provider down")
        if not self.ended.get(subscription_id):
            self.ended[subscription_id] = self.cancel_at

    def cancel_all_for_customer(self, customer_id):
        self.log.append(("cancel_all", customer_id))
        if self.fail_cancel:
            raise GatewayError("provider down")
        running = [sid for sid, at in self.ended.items() if not at]
        for sid in running:
            self.ended[sid] = self.cancel_at
        return running

    def subscriptions_ended_since(self, customer_id, since):
        self.log.append(("ended_since", customer_id, since))
        return [sid for sid, at in self.ended.items() if at and at >= since]

    # refunds
    def refund_basis(self, subscription_id):
        self.log.append(("basis", subscription_id))
        return RefundBasis(subscription_id, self.ended.get(subscription_id, 0.0),
                           "in_1", self.amount_paid, "eur", PERIOD_START, PERIOD_END)

    def refund_unused(self, subscription_id, amount_cents, idempotency_key):
        self.log.append(("refund", subscription_id, amount_cents, idempotency_key))
        if self.fail_refund:
            raise GatewayError("card network down")
        if idempotency_key in self.paid_out:
            return self.paid_out[idempotency_key]
        self.paid_out[idempotency_key] = amount_cents
        return amount_cents

    def expire_checkout_session(self, session_id):
        pass

    def calls(self, name):
        return [c for c in self.log if c[0] == name]

    @property
    def total_refunded(self) -> int:
        return sum(self.paid_out.values())


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
    for var in ("BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BILLING_ENABLED", "1")
    yield test_engine
    app.dependency_overrides.clear()
    set_gateway(None)


_seq = iter(range(1, 10_000))


def _seed(engine, *, bought_days_ago: float | None = 10, status="active",
          sub_id="sub_1", customer="cus_1", withdrawn_at=0.0) -> int:
    with Session(engine) as sess:
        local = LocalUser(username=f"w{next(_seq)}@x.io")
        sess.add(local)
        sess.commit()
        sess.refresh(local)
        user = UserInfo(local_auth_id=local.id, email="w@x.io")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id
        sess.add(Subscription(
            user_info_id=uid, plan="tier_2", status=status,
            provider_customer_id=customer, provider_subscription_id=sub_id,
            initial_paid_at=(time.time() - bought_days_ago * DAY
                             if bought_days_ago is not None else 0.0),
            withdrawn_at=withdrawn_at,
        ))
        sess.commit()
    return uid


def _as(uid) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uid), "email": "w@x.io", "auth_provider": "local",
    }
    return TestClient(app)


def _row(engine, uid) -> Subscription | None:
    with Session(engine) as sess:
        return sess.exec(
            select(Subscription).where(Subscription.user_info_id == uid)
        ).first()


def _account_exists(engine, uid) -> bool:
    with Session(engine) as sess:
        return sess.get(UserInfo, uid) is not None


# ── POST /api/billing/withdraw ────────────────────────────────────────────────

class TestWithdraw:
    def test_inside_the_window_cancels_then_refunds_the_unused_part(self, engine):
        uid = _seed(engine, bought_days_ago=10)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json() == {"refunded_cents": EXPECTED, "currency": "eur"}
        kinds = [c[0] for c in gw.log]
        assert kinds.index("cancel") < kinds.index("refund"), "refund before cancel"
        assert gw.calls("refund") == [("refund", "sub_1", EXPECTED, KEY)]
        assert gw.total_refunded == EXPECTED
        assert _row(engine, uid).withdrawn_at > 0

    def test_just_inside_the_window_is_honoured(self, engine):
        uid = _seed(engine, bought_days_ago=14 - 1 / DAY * 60)  # a minute left
        set_gateway(StripeLikeGateway())
        assert _as(uid).post("/api/billing/withdraw").status_code == 200

    @pytest.mark.parametrize("days", [14, 15, 90])
    def test_outside_the_window_is_refused_and_nothing_is_touched(self, engine, days):
        uid = _seed(engine, bought_days_ago=days)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 409
        assert res.json()["code"] == "withdrawal_window_closed"
        assert "14-day" in res.json()["detail"]
        assert gw.log == []
        assert _row(engine, uid).withdrawn_at == 0

    def test_no_purchase_date_on_record_is_refused(self, engine):
        """Pre-existing subscribers (backfilled 1.0) and unknown dates alike."""
        uid = _seed(engine, bought_days_ago=None)
        gw = StripeLikeGateway()
        set_gateway(gw)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 409
        assert res.json()["code"] == "withdrawal_window_closed"
        assert gw.log == []

    def test_no_subscription_is_refused(self, engine):
        uid = _seed(engine, sub_id="", status="none")
        gw = StripeLikeGateway()
        set_gateway(gw)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 409
        assert res.json()["code"] == "not_subscribed"
        assert gw.log == []

    def test_a_failed_cancel_refunds_nothing(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 502
        assert "nothing was refunded" in res.json()["detail"]
        assert gw.calls("refund") == [] and gw.calls("basis") == []
        assert _row(engine, uid).withdrawn_at == 0

    def test_a_failed_refund_leaves_it_cancelled_and_retryable(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_refund=True)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 502
        assert res.json()["code"] == "refund_failed"
        assert gw.ended["sub_1"] == CANCEL_AT  # cancelled, as the message says
        assert gw.total_refunded == 0
        assert _row(engine, uid).withdrawn_at == 0

        # Stripe's cancellation webhook lands meanwhile; the app must still
        # offer the retry, and the retry must finish the job.
        with Session(engine) as sess:
            row = sess.exec(select(Subscription)).first()
            row.status = "canceled"
            sess.add(row)
            sess.commit()
        assert _as(uid).get("/api/billing/me").json()["withdrawal_open"] is True

        gw.fail_refund = False
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 200, res.text
        assert res.json()["refunded_cents"] == EXPECTED
        assert gw.total_refunded == EXPECTED
        assert _row(engine, uid).withdrawn_at > 0

    def test_a_repeated_withdrawal_does_not_refund_twice(self, engine):
        """A double tap, or a retry after a lost response."""
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        first = _as(uid).post("/api/billing/withdraw")
        second = _as(uid).post("/api/billing/withdraw")

        assert first.status_code == second.status_code == 200
        assert second.json()["refunded_cents"] == EXPECTED
        assert gw.total_refunded == EXPECTED
        refunds = gw.calls("refund")
        assert len(refunds) == 2 and refunds[0] == refunds[1], (
            "a retry must reuse the same amount and key, or the provider "
            "rejects the key / pays twice")

    def test_a_free_period_refunds_nothing_and_does_not_ask(self, engine):
        """A 100%-off promotion code: nothing was paid, nothing to refund."""
        uid = _seed(engine)
        gw = StripeLikeGateway(amount_paid=0)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200
        assert res.json()["refunded_cents"] == 0
        assert gw.calls("cancel") and not gw.calls("refund")

    def test_the_plan_ends_at_once_without_waiting_for_the_webhook(self, engine):
        """The app reloads /me straight after; it must not show the paid plan,
        or offer the withdrawal again, until Stripe's event arrives."""
        uid = _seed(engine)
        set_gateway(StripeLikeGateway())

        assert _as(uid).post("/api/billing/withdraw").status_code == 200
        me = _as(uid).get("/api/billing/me").json()

        assert me["plan"] == "free"
        assert me["status"] == "canceled"
        assert me["withdrawal_open"] is False

    def test_the_refund_is_measured_when_the_subscription_ended(self, engine):
        """Not when the request (or a retry of it) runs: the provider's
        recorded end is what makes every attempt compute the same amount."""
        uid = _seed(engine)
        gw = StripeLikeGateway(cancel_at=PERIOD_START + 5 * DAY, fail_refund=True)
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502

        gw.fail_refund = False
        res = _as(uid).post("/api/billing/withdraw")

        assert res.json()["refunded_cents"] == 333  # floor(400 * 25/30)
        assert {c[2] for c in gw.calls("refund")} == {333}

    def test_without_billing_it_does_not_exist(self, engine, monkeypatch):
        monkeypatch.setenv("BILLING_ENABLED", "0")
        uid = _seed(engine)
        assert _as(uid).post("/api/billing/withdraw").status_code == 404


class TestNoRefundWithoutCancellation:
    def test_a_running_subscription_is_never_refunded(self):
        from src.billing.withdrawal import refund_unused_period

        gw = StripeLikeGateway()  # sub_1 still running
        with pytest.raises(GatewayError):
            refund_unused_period(gw, "sub_1")
        assert gw.calls("refund") == []
        assert gw.total_refunded == 0


# ── GET /api/billing/withdraw (the dialog's estimate) ─────────────────────────

class TestQuote:
    def test_estimates_from_the_provider_invoice_without_changing_anything(
        self, engine, monkeypatch
    ):
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)
        # A live subscription has not ended: the estimate is measured at now.
        monkeypatch.setattr("api.billing.time.time", lambda: CANCEL_AT)
        with Session(engine) as sess:
            row = sess.exec(select(Subscription)).first()
            row.initial_paid_at = CANCEL_AT - 3 * DAY
            sess.add(row)
            sess.commit()

        res = _as(uid).get("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["amount_cents"] == EXPECTED
        assert body["currency"] == "eur"
        assert body["closes_at"] == CANCEL_AT - 3 * DAY + 14 * DAY
        assert not gw.calls("cancel") and not gw.calls("refund")

    def test_outside_the_window_is_refused(self, engine):
        uid = _seed(engine, bought_days_ago=20)
        gw = StripeLikeGateway()
        set_gateway(gw)
        res = _as(uid).get("/api/billing/withdraw")
        assert res.status_code == 409
        assert res.json()["code"] == "withdrawal_window_closed"
        assert gw.log == []


# ── /api/billing/me exposes the window ────────────────────────────────────────

class TestMe:
    def test_open_inside_the_window(self, engine):
        uid = _seed(engine, bought_days_ago=10)
        set_gateway(StripeLikeGateway())
        body = _as(uid).get("/api/billing/me").json()
        assert body["withdrawal_open"] is True
        row = _row(engine, uid)
        assert body["withdrawal_closes_at"] == row.initial_paid_at + 14 * DAY

    def test_closed_outside_it(self, engine):
        uid = _seed(engine, bought_days_ago=15)
        body = _as(uid).get("/api/billing/me").json()
        assert body["withdrawal_open"] is False

    def test_closed_without_a_purchase_date(self, engine):
        uid = _seed(engine, bought_days_ago=None)
        body = _as(uid).get("/api/billing/me").json()
        assert body["withdrawal_open"] is False
        assert body["withdrawal_closes_at"] == 0

    def test_closed_once_withdrawn(self, engine):
        uid = _seed(engine, status="canceled", withdrawn_at=time.time())
        assert _as(uid).get("/api/billing/me").json()["withdrawal_open"] is False

    def test_a_new_live_subscription_after_withdrawing_is_offered_again(self, engine):
        """Still inside the window, a re-subscription can be withdrawn from."""
        uid = _seed(engine, status="active", withdrawn_at=time.time() - DAY)
        assert _as(uid).get("/api/billing/me").json()["withdrawal_open"] is True

    def test_never_on_a_self_hosted_instance(self, engine, monkeypatch):
        monkeypatch.setenv("BILLING_ENABLED", "0")
        uid = _seed(engine)
        assert _as(uid).get("/api/billing/me").json()["withdrawal_open"] is False


# ── Checkout links the terms the consent box refers to ────────────────────────

class TestCheckoutTermsLink:
    def test_the_terms_page_is_passed_to_the_provider(self, engine, monkeypatch):
        import api.billing as billing_mod

        calls: list[dict] = []

        class RecordingGateway(StripeLikeGateway):
            def create_checkout_session(self, **kwargs):
                calls.append(kwargs)
                return {"url": "https://pay.test", "customer_id": "cus_1",
                        "session_id": "cs_1"}

        monkeypatch.setattr(billing_mod, "_FRONTEND_ORIGIN", "https://app.test")
        uid = _seed(engine, status="none", sub_id="")
        set_gateway(RecordingGateway())

        res = _as(uid).post("/api/billing/checkout", json={"plan": "tier_2"})

        assert res.status_code == 200, res.text
        assert calls[0]["terms_url"] == "https://app.test/terms"


# ── Account deletion inside the window ────────────────────────────────────────

class TestDeletion:
    def test_inside_the_window_refunds_the_unused_part(self, engine):
        uid = _seed(engine, bought_days_ago=10)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert gw.calls("refund") == [("refund", "sub_1", EXPECTED, KEY)]
        assert gw.total_refunded == EXPECTED
        kinds = [c[0] for c in gw.log]
        assert kinds.index("cancel_all") < kinds.index("refund")
        assert not _account_exists(engine, uid)

    @pytest.mark.parametrize("days", [14, 30])
    def test_outside_the_window_only_cancels(self, engine, days):
        uid = _seed(engine, bought_days_ago=days)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert gw.calls("cancel_all") and not gw.calls("refund")
        assert gw.total_refunded == 0
        assert not _account_exists(engine, uid)

    def test_a_failed_refund_refuses_and_keeps_the_account(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_refund=True)
        set_gateway(gw)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 502
        assert res.json()["code"] == "refund_failed"
        assert "not deleted" in res.json()["detail"]
        assert _account_exists(engine, uid)
        assert _row(engine, uid) is not None

    def test_the_retry_refunds_exactly_once(self, engine):
        """The first attempt cancelled; the retry has nothing left to cancel
        and must still find, and refund, that subscription — once."""
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_refund=True)
        set_gateway(gw)
        assert _as(uid).delete("/api/auth/me").status_code == 502

        gw.fail_refund = False
        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert gw.total_refunded == EXPECTED
        assert not _account_exists(engine, uid)

    def test_withdrawing_then_deleting_refunds_once(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        assert _as(uid).post("/api/billing/withdraw").status_code == 200
        assert _as(uid).delete("/api/auth/me").status_code == 200

        assert gw.total_refunded == EXPECTED
        assert not _account_exists(engine, uid)

    def test_admin_deletion_inside_the_window_refunds_too(self, engine):
        uid = _seed(engine)
        with Session(engine) as sess:
            local = LocalUser(username="admin@x.io")
            sess.add(local)
            sess.commit()
            sess.refresh(local)
            admin = UserInfo(local_auth_id=local.id, email="admin@x.io", is_admin=True)
            sess.add(admin)
            sess.commit()
            sess.refresh(admin)
            admin_id = admin.id
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(admin_id).delete(f"/api/admin/users/{uid}")

        assert res.status_code in (200, 204), res.text
        assert gw.total_refunded == EXPECTED

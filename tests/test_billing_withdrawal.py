"""Withdrawing inside the 14-day window, and deleting inside it (issue #441).

Drives the real ``api.router.app``. The provider is a fake that behaves like
Stripe where it matters here: cancelling records when the subscription ended,
a refund under a key already used returns the first one instead of paying
twice, and refunds already made on the payment count towards what is owed. So
"a retry does not refund twice" is checked by the money the fake paid out, not
by which helper was called.

Time is a fixed clock the tests move, because the window closes at a calendar
boundary (the end of the 14th day after the contract's day, in UTC).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

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


def _utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


#: The contract started on 10 September; its window closes at the first
#: instant of 25 September, UTC.
START = _utc(2026, 9, 10, 15, 0)
CLOSES = _utc(2026, 9, 25)
LAST_MOMENT = CLOSES - 0.001
#: The paid period on the fake invoice, and when the fake's cancellation lands:
#: ten days into a thirty-day period, so two thirds are unused.
PERIOD_START = START
PERIOD_END = START + 30 * DAY
CANCEL_AT = START + 10 * DAY
PAID = 400
EXPECTED = 266  # floor(400 * 20/30)
KEY = "traxjourney-unused-period-refund-sub_1"


class StripeLikeGateway:
    """Subscriptions that end when cancelled; refunds that dedupe and add up."""

    def __init__(self, *, amount_paid=PAID, fail_cancel=False, fail_refund=False,
                 fail_discard=False, subscriptions=("sub_1",), cancel_at=CANCEL_AT,
                 latest=None, manual_refunds=0):
        self.amount_paid = amount_paid
        self.cancel_at = cancel_at
        self.fail_cancel = fail_cancel
        self.fail_refund = fail_refund
        self.fail_discard = fail_discard
        self.ended: dict[str, float] = {sid: 0.0 for sid in subscriptions}
        self.paid_out: dict[str, int] = {}  # refund key -> cents
        self.manual_refunds = manual_refunds
        self.pending: dict[str, int] = {sid: 1 for sid in subscriptions}
        self.latest = latest
        self.on_cancel = None
        self.log: list[tuple] = []

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
        if self.on_cancel:
            self.on_cancel()
        running = [sid for sid, at in self.ended.items() if not at]
        for sid in running:
            self.ended[sid] = self.cancel_at
        return running

    def latest_subscription(self, customer_id):
        self.log.append(("latest", customer_id))
        return self.latest

    def discard_pending_items(self, customer_id, subscription_id):
        self.log.append(("discard", customer_id, subscription_id))
        if self.fail_discard:
            raise GatewayError("provider down")
        return self.pending.pop(subscription_id, 0)

    def refund_basis(self, subscription_id):
        self.log.append(("basis", subscription_id))
        return RefundBasis(subscription_id, self.ended.get(subscription_id, 0.0),
                           "in_1", self.amount_paid, "eur", PERIOD_START, PERIOD_END)

    def refund_unused(self, subscription_id, amount_cents, refund_key):
        self.log.append(("refund", subscription_id, amount_cents, refund_key))
        if self.fail_refund:
            raise GatewayError("card network down")
        if refund_key in self.paid_out:
            return self.paid_out[refund_key]
        already = self.manual_refunds + sum(self.paid_out.values())
        amount = max(0, min(amount_cents, self.amount_paid) - already)
        if amount:
            self.paid_out[refund_key] = amount
        return amount

    def expire_checkout_session(self, session_id):
        pass

    def calls(self, name):
        return [c for c in self.log if c[0] == name]

    @property
    def total_refunded(self) -> int:
        return sum(self.paid_out.values())


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """Ten days into the contract, unless a test moves it."""
    c = Clock(START + 10 * DAY)
    monkeypatch.setattr(time, "time", c)
    return c


@pytest.fixture
def engine(monkeypatch, tmp_path, clock):
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


def _seed(engine, *, started=START, status="active", sub_id="sub_1",
          contract="sub_1", customer="cus_1", withdrawn_subscription="",
          requested=0.0) -> int:
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
            contract_started_at=started, contract_subscription_id=contract,
            withdrawal_requested_at=requested,
            withdrawn_subscription_id=withdrawn_subscription,
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
    def test_inside_the_window_cancels_then_refunds_the_unused_part(self, engine, clock):
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json() == {"refunded_cents": EXPECTED, "currency": "eur"}
        kinds = [c[0] for c in gw.log]
        assert kinds.index("cancel") < kinds.index("discard") < kinds.index("refund")
        assert gw.calls("refund") == [("refund", "sub_1", EXPECTED, KEY)]
        assert gw.total_refunded == EXPECTED
        row = _row(engine, uid)
        assert row.withdrawn_subscription_id == "sub_1"
        assert row.withdrawal_requested_at == clock.now

    def test_the_last_moment_of_the_fourteenth_day_is_honoured(self, engine, clock):
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        set_gateway(StripeLikeGateway())
        assert _as(uid).post("/api/billing/withdraw").status_code == 200

    @pytest.mark.parametrize("at", [CLOSES, CLOSES + 1, CLOSES + 60 * DAY])
    def test_from_the_next_midnight_it_is_refused_and_nothing_is_touched(
        self, engine, clock, at
    ):
        clock.now = at
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 409
        assert res.json()["code"] == "withdrawal_window_closed"
        assert "14-day" in res.json()["detail"]
        assert gw.log == []
        assert _row(engine, uid).withdrawal_requested_at == 0

    @pytest.mark.parametrize("started", [0.0, 1.0])
    def test_no_contract_start_or_one_from_before_tracking_is_refused(
        self, engine, started
    ):
        uid = _seed(engine, started=started)
        gw = StripeLikeGateway()
        set_gateway(gw)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 409
        assert res.json()["code"] == "withdrawal_window_closed"
        assert gw.log == []

    def test_no_subscription_is_refused(self, engine):
        uid = _seed(engine, sub_id="", contract="", status="none", started=0.0)
        gw = StripeLikeGateway()
        set_gateway(gw)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 409
        assert res.json()["code"] == "not_subscribed"
        assert gw.log == []

    def test_a_failed_cancel_refunds_nothing_but_records_the_request(self, engine, clock):
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 502
        assert "nothing was refunded" in res.json()["detail"]
        assert gw.calls("refund") == [] and gw.calls("basis") == []
        assert _row(engine, uid).withdrawal_requested_at == clock.now

    def test_the_request_is_recorded_before_stripe_is_asked(self, engine, clock):
        """If the server died mid-call, the request must already be on file."""
        uid = _seed(engine)
        seen = []

        class Watching(StripeLikeGateway):
            def cancel_subscription(self, subscription_id, customer_id=""):
                seen.append(_row(engine, uid).withdrawal_requested_at)
                super().cancel_subscription(subscription_id, customer_id)

        set_gateway(Watching())
        _as(uid).post("/api/billing/withdraw")
        assert seen == [clock.now]

    def test_a_failed_refund_can_be_completed_after_the_deadline(self, engine, clock):
        """Finding 1: withdrawn at the last moment, refund fails, retried the
        next day — the request was in time, so the refund is still owed."""
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_refund=True)
        set_gateway(gw)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 502
        assert res.json()["code"] == "refund_failed"
        assert gw.ended["sub_1"] == CANCEL_AT  # cancelled, as the message says
        assert gw.total_refunded == 0

        clock.now = CLOSES + DAY
        # Stripe's cancellation webhook lands meanwhile; the app must still
        # offer the retry.
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
        assert _row(engine, uid).withdrawn_subscription_id == "sub_1"
        assert _as(uid).get("/api/billing/me").json()["withdrawal_open"] is False

    def test_a_failed_cancel_can_be_completed_after_the_deadline(self, engine, clock):
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502

        clock.now = CLOSES + 3 * DAY
        gw.fail_cancel = False
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 200, res.text
        assert gw.total_refunded == EXPECTED

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
            "a retry must ask for the same amount under the same key")

    def test_a_free_period_refunds_nothing_and_does_not_ask(self, engine):
        """A 100%-off promotion code: nothing was paid, nothing to refund."""
        uid = _seed(engine)
        gw = StripeLikeGateway(amount_paid=0)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200
        assert res.json()["refunded_cents"] == 0
        assert gw.calls("cancel") and not gw.calls("refund")

    def test_pending_upgrade_items_are_removed(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)
        _as(uid).post("/api/billing/withdraw")
        assert gw.calls("discard") == [("discard", "cus_1", "sub_1")]
        assert gw.pending == {}

    def test_a_failed_removal_of_pending_items_is_retryable(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_discard=True)
        set_gateway(gw)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 502
        assert res.json()["code"] == "refund_failed"
        assert gw.total_refunded == 0

        gw.fail_discard = False
        assert _as(uid).post("/api/billing/withdraw").status_code == 200
        assert gw.total_refunded == EXPECTED and gw.pending == {}

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

    def test_the_contracts_subscription_is_the_one_withdrawn(self, engine):
        """The window belongs to the contract. A second subscription bought
        while it ran (and now tracked on the row) is not what is refunded."""
        uid = _seed(engine, sub_id="sub_2", contract="sub_1")
        gw = StripeLikeGateway(subscriptions=("sub_1", "sub_2"))
        set_gateway(gw)

        assert _as(uid).post("/api/billing/withdraw").status_code == 200

        assert gw.calls("cancel") == [("cancel", "sub_1")]
        assert gw.calls("refund")[0][1] == "sub_1"
        assert _row(engine, uid).status == "active"  # sub_2 still runs

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
        self, engine, clock
    ):
        clock.now = CANCEL_AT
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).get("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json() == {"amount_cents": EXPECTED, "currency": "eur",
                              "closes_at": CLOSES}
        assert not gw.calls("cancel") and not gw.calls("refund")
        assert _row(engine, uid).withdrawal_requested_at == 0  # nothing recorded

    def test_outside_the_window_is_refused(self, engine, clock):
        clock.now = CLOSES
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)
        res = _as(uid).get("/api/billing/withdraw")
        assert res.status_code == 409
        assert res.json()["code"] == "withdrawal_window_closed"
        assert gw.log == []


# ── /api/billing/me exposes the window ────────────────────────────────────────

class TestMe:
    def test_open_inside_the_window(self, engine):
        uid = _seed(engine)
        body = _as(uid).get("/api/billing/me").json()
        assert body["withdrawal_open"] is True
        assert body["withdrawal_closes_at"] == CLOSES

    def test_closed_from_the_next_midnight(self, engine, clock):
        clock.now = CLOSES
        uid = _seed(engine)
        assert _as(uid).get("/api/billing/me").json()["withdrawal_open"] is False

    def test_a_request_made_in_time_keeps_it_open(self, engine, clock):
        clock.now = CLOSES + DAY
        uid = _seed(engine, requested=LAST_MOMENT, status="canceled")
        assert _as(uid).get("/api/billing/me").json()["withdrawal_open"] is True

    def test_closed_without_a_contract(self, engine):
        uid = _seed(engine, started=0.0, contract="")
        body = _as(uid).get("/api/billing/me").json()
        assert body["withdrawal_open"] is False
        assert body["withdrawal_closes_at"] == 0

    def test_closed_once_this_contract_was_withdrawn(self, engine):
        uid = _seed(engine, status="canceled", withdrawn_subscription="sub_1")
        assert _as(uid).get("/api/billing/me").json()["withdrawal_open"] is False

    def test_an_earlier_contracts_withdrawal_does_not_hide_this_one(self, engine):
        """Finding 8: sub_A was withdrawn from; sub_B's refund then failed.
        The retry for sub_B must still be offered."""
        uid = _seed(engine, status="canceled", sub_id="sub_B", contract="sub_B",
                    withdrawn_subscription="sub_A")
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
        uid = _seed(engine, status="none", sub_id="", contract="", started=0.0)
        set_gateway(RecordingGateway())

        res = _as(uid).post("/api/billing/checkout", json={"plan": "tier_2"})

        assert res.status_code == 200, res.text
        assert calls[0]["terms_url"] == "https://app.test/terms"


# ── Account deletion inside the window ────────────────────────────────────────

class TestDeletion:
    def test_inside_the_window_refunds_the_unused_part(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert gw.calls("refund") == [("refund", "sub_1", EXPECTED, KEY)]
        assert gw.total_refunded == EXPECTED
        assert gw.pending == {}
        kinds = [c[0] for c in gw.log]
        assert kinds.index("cancel_all") < kinds.index("discard") < kinds.index("refund")
        assert not _account_exists(engine, uid)

    @pytest.mark.parametrize("at", [CLOSES, CLOSES + 30 * DAY])
    def test_outside_the_window_only_cancels(self, engine, clock, at):
        clock.now = at
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert gw.calls("cancel_all") and not gw.calls("refund")
        assert gw.total_refunded == 0
        assert not _account_exists(engine, uid)

    def test_a_failed_refund_refuses_and_keeps_the_account(self, engine, clock):
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_refund=True)
        set_gateway(gw)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 502
        assert res.json()["code"] == "refund_failed"
        assert "not deleted" in res.json()["detail"]
        assert _account_exists(engine, uid)
        # The deletion was a withdrawal request, recorded before cancelling.
        assert _row(engine, uid).withdrawal_requested_at == clock.now

    def test_the_request_is_recorded_before_cancelling(self, engine, clock):
        uid = _seed(engine)
        seen = []
        gw = StripeLikeGateway()
        gw.on_cancel = lambda: seen.append(_row(engine, uid).withdrawal_requested_at)
        set_gateway(gw)
        _as(uid).delete("/api/auth/me")
        assert seen == [clock.now]

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

    def test_a_retry_after_the_deadline_still_refunds(self, engine, clock):
        """Finding 1: deleted at the last moment, refund failed, retried the
        next day. Without the recorded request it would delete unrefunded."""
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_refund=True)
        set_gateway(gw)
        assert _as(uid).delete("/api/auth/me").status_code == 502

        clock.now = CLOSES + DAY
        gw.fail_refund = False
        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert gw.total_refunded == EXPECTED

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

    def test_a_contract_recorded_while_cancelling_is_read_fresh(self, engine):
        """Finding 9: the paid webhook lands while deletion is cancelling at
        Stripe. What was read before the call is stale; the window must be
        decided from the row as it is now."""
        uid = _seed(engine, started=0.0, contract="")
        gw = StripeLikeGateway()

        def webhook_lands():
            with Session(engine) as sess:
                row = sess.exec(select(Subscription)).first()
                row.contract_started_at = START
                row.contract_subscription_id = "sub_1"
                sess.add(row)
                sess.commit()
        gw.on_cancel = webhook_lands
        set_gateway(gw)

        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert gw.total_refunded == EXPECTED

    def test_an_unknown_start_is_taken_from_stripe(self, engine):
        """The paid webhook has not arrived at all: ask Stripe when the
        customer's latest subscription started."""
        uid = _seed(engine, started=0.0, contract="")
        gw = StripeLikeGateway(latest=("sub_1", START))
        set_gateway(gw)

        assert _as(uid).delete("/api/auth/me").status_code == 200

        assert gw.calls("latest") == [("latest", "cus_1")]
        assert gw.total_refunded == EXPECTED

    def test_an_old_start_from_stripe_refunds_nothing(self, engine):
        uid = _seed(engine, started=0.0, contract="")
        gw = StripeLikeGateway(latest=("sub_1", START - 60 * DAY))
        set_gateway(gw)
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert gw.total_refunded == 0

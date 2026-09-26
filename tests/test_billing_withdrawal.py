"""Withdrawing inside the 14-day window, and deleting inside it (issue #441).

Drives the real ``api.router.app``. The provider is a fake that behaves like
Stripe where it matters here:
* cancelling records when the subscription ended;
* a refund made again under a provider key already used returns the first one
  (Stripe's idempotency), and one whose refund key is already on a credit note
  is not made again (the metadata check);
* refunds already made on the payment count towards what is owed.

So "a retry does not refund twice" is checked by the money the fake paid out,
not by which helper was called.

Time is a fixed clock the tests move, because the window closes at a calendar
boundary (the end of the 14th day after the contract's day, in UTC).
"""
from __future__ import annotations

import logging
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
from models.billing import Subscription, SubscriptionRefund
from models.user import LocalUser, UserInfo
from src.billing.gateway import (
    GatewayError,
    PermanentGatewayError,
    RefundBasis,
    RefundResult,
    set_gateway,
)

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
    """Subscriptions that end when cancelled; refunds that dedupe and add up.

    ``refund_mode``:
    * ``ok`` — refunds;
    * ``transient`` — fails before doing anything (a 5xx);
    * ``timeout`` — the credit note IS created, but the answer is lost;
    * ``permanent`` — a definite refusal (a 4xx), once, then ``ok``;
    * ``no_payment`` — nothing to refund against (paid from the balance).
    """

    def __init__(self, *, amount_paid=PAID, total=None, fail_cancel=False,
                 refund_mode="ok", fail_discard=False, subscriptions=("sub_1",),
                 cancel_at=CANCEL_AT, latest=None, in_force=None):
        self.amount_paid = amount_paid
        self.total = amount_paid if total is None else total
        self.cancel_at = cancel_at
        self.fail_cancel = fail_cancel
        self.refund_mode = refund_mode
        self.fail_discard = fail_discard
        self.ended: dict[str, float] = {sid: 0.0 for sid in subscriptions}
        self.notes: dict[str, int] = {}          # refund key -> cents (metadata)
        self.provider_keys: dict[str, int] = {}  # provider idempotency key -> cents
        self.pending: dict[str, int] = {sid: 1 for sid in subscriptions}
        self.latest = latest
        self.in_force = in_force
        self.on_cancel = None
        self.during_refund = None
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

    def subscriptions_in_force(self, customer_id):
        return [sid for sid, at in self.ended.items() if not at] \
            if self.in_force is None else list(self.in_force)

    def discard_pending_items(self, customer_id, subscription_id):
        self.log.append(("discard", customer_id, subscription_id))
        if self.fail_discard:
            raise GatewayError("provider down")
        return self.pending.pop(subscription_id, 0)

    def refund_basis(self, subscription_id):
        self.log.append(("basis", subscription_id))
        return RefundBasis(subscription_id, self.ended.get(subscription_id, 0.0),
                           "in_1", self.amount_paid, "eur", PERIOD_START, PERIOD_END,
                           total=self.total)

    def refund_unused(self, subscription_id, amount_cents, refund_key, *,
                      invoice_id, attempt=0):
        self.log.append(("refund", subscription_id, amount_cents, refund_key,
                         invoice_id, attempt))
        if self.during_refund:
            hook, self.during_refund = self.during_refund, None
            hook()
        if refund_key in self.notes:
            return RefundResult(self.notes[refund_key], "cn_1")
        provider_key = f"{refund_key}:{attempt}"
        if provider_key in self.provider_keys:
            return RefundResult(self.provider_keys[provider_key], "cn_1")
        mode = self.refund_mode
        if mode == "transient":
            raise GatewayError("503 from Stripe")
        if mode == "permanent":
            self.refund_mode = "ok"
            raise PermanentGatewayError("charge_disputed")
        if mode == "no_payment":
            return RefundResult(0, unrefunded_cents=amount_cents,
                                reason="paid without a refundable payment")
        amount = max(0, min(amount_cents, self.amount_paid) - sum(self.notes.values()))
        self.notes[refund_key] = amount
        self.provider_keys[provider_key] = amount
        if mode == "timeout":
            self.refund_mode = "ok"
            raise GatewayError("read timed out")
        return RefundResult(amount, "cn_1")

    def expire_checkout_session(self, session_id):
        pass

    def calls(self, name):
        return [c for c in self.log if c[0] == name]

    @property
    def total_refunded(self) -> int:
        return sum(self.notes.values())


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
          contract="sub_1", customer="cus_1", withdrawn_subscription="") -> int:
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


def _ledger(engine, sub="sub_1") -> SubscriptionRefund | None:
    with Session(engine) as sess:
        return sess.get(SubscriptionRefund, sub)


def _account_exists(engine, uid) -> bool:
    with Session(engine) as sess:
        return sess.get(UserInfo, uid) is not None


def _open(uid) -> bool:
    return _as(uid).get("/api/billing/me").json()["withdrawal_open"]


# ── POST /api/billing/withdraw ────────────────────────────────────────────────

class TestWithdraw:
    def test_inside_the_window_cancels_then_refunds_the_unused_part(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json() == {"refunded_cents": EXPECTED, "currency": "eur",
                              "owed_cents": 0}
        kinds = [c[0] for c in gw.log]
        assert kinds.index("cancel") < kinds.index("discard") < kinds.index("refund")
        assert gw.calls("refund") == [("refund", "sub_1", EXPECTED, KEY, "in_1", 0)]
        assert gw.total_refunded == EXPECTED
        entry = _ledger(engine)
        assert (entry.state, entry.amount, entry.refunded, entry.invoice_id) == (
            "done", EXPECTED, EXPECTED, "in_1")
        assert _row(engine, uid).withdrawn_subscription_id == "sub_1"

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
        assert gw.log == []
        assert _ledger(engine) is None

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

    def test_a_failed_cancel_refunds_nothing_and_records_nothing(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 502
        assert "nothing was refunded" in res.json()["detail"]
        assert gw.calls("refund") == [] and gw.calls("basis") == []
        assert _ledger(engine) is None

    def test_a_failed_cancel_cannot_be_completed_after_the_deadline(self, engine, clock):
        """F1: the cancel never landed, so the request did not happen. After
        the deadline there is nothing to complete — or the running, renewing
        subscription would be refunded against a later invoice."""
        clock.now = START + 13 * DAY
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502

        clock.now = START + 200 * DAY
        gw.fail_cancel = False
        assert _open(uid) is False
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 409
        assert res.json()["code"] == "withdrawal_window_closed"
        assert gw.total_refunded == 0

    def test_a_refused_deletion_does_not_keep_the_window_open(self, engine, clock):
        """F1: a deletion refused at the cancel leaves nothing behind that a
        later withdrawal could complete."""
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        assert _as(uid).delete("/api/auth/me").status_code == 502

        clock.now = CLOSES + DAY
        gw.fail_cancel = False
        assert _open(uid) is False
        assert _as(uid).post("/api/billing/withdraw").status_code == 409
        assert gw.total_refunded == 0

    def test_a_failed_refund_after_the_cancel_can_be_completed_late(self, engine, clock):
        """F1: cancelled in time, refund failed, retried the next day — the
        refund is still owed, and made."""
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="transient")
        set_gateway(gw)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 502
        assert res.json()["code"] == "refund_failed"
        assert gw.ended["sub_1"] == CANCEL_AT
        assert _ledger(engine).state == "pending"

        clock.now = CLOSES + DAY
        assert _open(uid) is True

        gw.refund_mode = "ok"
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 200, res.text
        assert res.json()["refunded_cents"] == EXPECTED
        assert gw.total_refunded == EXPECTED
        assert _open(uid) is False

    def test_a_late_completion_needs_the_subscription_to_have_ended(self, engine, clock):
        """A pending row whose subscription somehow still runs is not refunded."""
        uid = _seed(engine)
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(subscription_id="sub_1", customer_id="cus_1",
                                        requested_at=START + DAY))
            sess.commit()
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        clock.now = CLOSES + DAY
        # The cancel fails, so settle is never reached; and a running
        # subscription's basis says it has not ended:
        assert _as(uid).post("/api/billing/withdraw").status_code == 502
        from src.billing.withdrawal import NotEligible, settle
        with pytest.raises(NotEligible):
            settle(gw, user_info_id=uid, customer_id="cus_1", subscription_id="sub_1",
                   contract_start=START, requested_at=clock.now, now=clock.now)
        assert gw.total_refunded == 0
        # Nothing frozen, nothing owed: the row goes, so it cannot keep a late
        # path (or the app's action) open.
        assert _ledger(engine) is None
        assert _open(uid) is False

    def test_a_repeated_withdrawal_does_not_refund_twice(self, engine):
        """A retry after a lost response: the ledger answers, Stripe is not
        asked again."""
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        responses = [_as(uid).post("/api/billing/withdraw") for _ in range(3)]

        assert [r.status_code for r in responses] == [200, 200, 200]
        assert {r.json()["refunded_cents"] for r in responses} == {EXPECTED}
        assert gw.total_refunded == EXPECTED
        assert len(gw.calls("refund")) == 1
        # A done row is answered, never claimed: no lease left behind.
        assert _ledger(engine).lease_until == 0

    def test_a_double_tap_while_refunding_is_told_to_wait(self, engine):
        """F2: the second request finds the claim held and refunds nothing."""
        uid = _seed(engine)
        gw = StripeLikeGateway()
        seen = []
        gw.during_refund = lambda: seen.append(_as(uid).post("/api/billing/withdraw"))
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200
        assert seen[0].status_code == 409
        assert seen[0].json()["code"] == "refund_in_progress"
        assert gw.total_refunded == EXPECTED
        assert len(gw.calls("refund")) == 1

    def test_a_deletion_during_the_withdrawal_refund_waits(self, engine):
        """F2: withdraw and delete at once — one refund, and the deletion is
        refused (retryable) rather than refunding too."""
        uid = _seed(engine)
        gw = StripeLikeGateway()
        seen = []
        gw.during_refund = lambda: seen.append(_as(uid).delete("/api/auth/me"))
        set_gateway(gw)

        assert _as(uid).post("/api/billing/withdraw").status_code == 200

        assert seen[0].status_code == 409
        assert seen[0].json()["code"] == "refund_in_progress"
        assert _account_exists(engine, uid)
        assert gw.total_refunded == EXPECTED
        # And the deletion, retried, goes through without refunding again.
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert gw.total_refunded == EXPECTED

    def test_a_timeout_is_retried_under_the_same_provider_key(self, engine):
        """F2: the credit note was made but the answer lost. The retry must
        reuse the key, so Stripe answers with the same credit note."""
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="timeout")
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502
        gw.notes.clear()  # as if the metadata were not visible yet: only the key saves us

        assert _as(uid).post("/api/billing/withdraw").status_code == 200

        attempts = [c[5] for c in gw.calls("refund")]
        assert attempts == [0, 0]
        assert sum(gw.provider_keys.values()) == EXPECTED
        assert _ledger(engine).attempt == 0

    def test_a_definite_refusal_is_owed_and_retried_under_a_new_key(self, engine, caplog):
        """F2/F3: a 4xx records the refund as owed (ERROR logged) and bumps the
        attempt; the next try uses a new provider key."""
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="permanent")
        set_gateway(gw)

        with caplog.at_level(logging.ERROR):
            res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json() == {"refunded_cents": 0, "currency": "eur",
                              "owed_cents": EXPECTED}
        entry = _ledger(engine)
        assert (entry.state, entry.attempt, entry.amount) == ("failed_permanent", 1, EXPECTED)
        assert "charge_disputed" in entry.reason
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)
        assert _row(engine, uid).withdrawn_subscription_id == ""  # F4: still owed
        assert _open(uid) is False  # the owner settles it

        # The deletion tries once more, under attempt 1, and it goes through.
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert [c[5] for c in gw.calls("refund")] == [0, 1]
        assert gw.total_refunded == EXPECTED

    def test_nothing_to_refund_against_is_owed_not_forgotten(self, engine, caplog):
        """F4: paid without a refundable payment — ERROR, owed, and the
        subscription is not marked withdrawn."""
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="no_payment")
        set_gateway(gw)
        with caplog.at_level(logging.ERROR):
            res = _as(uid).post("/api/billing/withdraw")
        assert res.json()["owed_cents"] == EXPECTED
        assert _ledger(engine).state == "failed_permanent"
        assert _row(engine, uid).withdrawn_subscription_id == ""
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)

    def test_an_invoice_paid_from_the_balance_is_owed(self, engine, caplog):
        """F4: amount_paid 0, total 400 — the card took nothing, but the unused
        period was paid for."""
        uid = _seed(engine)
        gw = StripeLikeGateway(amount_paid=0, total=PAID)
        set_gateway(gw)
        with caplog.at_level(logging.ERROR):
            res = _as(uid).post("/api/billing/withdraw")
        assert res.json()["owed_cents"] == EXPECTED
        assert not gw.calls("refund")
        assert "balance" in _ledger(engine).reason
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)

    def test_a_free_period_refunds_nothing_and_does_not_ask(self, engine):
        """A 100%-off promotion code: nothing was paid, nothing is owed."""
        uid = _seed(engine)
        gw = StripeLikeGateway(amount_paid=0)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200
        assert res.json()["refunded_cents"] == 0 and res.json()["owed_cents"] == 0
        assert gw.calls("cancel") and not gw.calls("refund")
        assert _ledger(engine).state == "done"

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
        uid = _seed(engine)
        set_gateway(StripeLikeGateway())

        assert _as(uid).post("/api/billing/withdraw").status_code == 200
        me = _as(uid).get("/api/billing/me").json()

        assert me["plan"] == "free"
        assert me["status"] == "canceled"
        assert me["withdrawal_open"] is False

    def test_the_amount_is_frozen_at_the_first_computation(self, engine):
        """Measured when the subscription ended, and kept: a retry asks for
        the same amount of the same invoice."""
        uid = _seed(engine)
        gw = StripeLikeGateway(cancel_at=PERIOD_START + 5 * DAY, refund_mode="transient")
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502

        gw.refund_mode = "ok"
        gw.amount_paid = 999  # whatever Stripe says later, the frozen amount stands
        res = _as(uid).post("/api/billing/withdraw")

        assert res.json()["refunded_cents"] == 333  # floor(400 * 25/30)
        assert {c[2] for c in gw.calls("refund")} == {333}
        assert len(gw.calls("basis")) == 1

    def test_the_contracts_subscription_is_the_one_withdrawn(self, engine):
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
        assert _ledger(engine) is None

    def test_outside_the_window_is_refused(self, engine, clock):
        clock.now = CLOSES
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)
        res = _as(uid).get("/api/billing/withdraw")
        assert res.status_code == 409
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
        assert _open(uid) is False

    def test_closed_without_a_contract(self, engine):
        uid = _seed(engine, started=0.0, contract="")
        body = _as(uid).get("/api/billing/me").json()
        assert body["withdrawal_open"] is False
        assert body["withdrawal_closes_at"] == 0

    @pytest.mark.parametrize("state", ["done", "failed_permanent"])
    def test_closed_once_the_contracts_refund_is_done_or_owed(self, engine, state):
        """F7: whoever refunded it — including a deletion later refused with
        billing_changed, which leaves the account and never sets
        withdrawn_subscription_id."""
        uid = _seed(engine)
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(subscription_id="sub_1", customer_id="cus_1",
                                        state=state, amount=EXPECTED))
            sess.commit()
        assert _open(uid) is False

    def test_an_earlier_contracts_refund_does_not_hide_this_one(self, engine):
        uid = _seed(engine, sub_id="sub_B", contract="sub_B",
                    withdrawn_subscription="sub_A")
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(subscription_id="sub_A", customer_id="cus_1",
                                        state="done", amount=10))
            sess.commit()
        assert _open(uid) is True

    def test_never_on_a_self_hosted_instance(self, engine, monkeypatch):
        monkeypatch.setenv("BILLING_ENABLED", "0")
        uid = _seed(engine)
        assert _open(uid) is False


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
        assert gw.calls("refund") == [("refund", "sub_1", EXPECTED, KEY, "in_1", 0)]
        assert gw.total_refunded == EXPECTED
        assert gw.pending == {}
        kinds = [c[0] for c in gw.log]
        assert kinds.index("cancel_all") < kinds.index("discard") < kinds.index("refund")
        assert not _account_exists(engine, uid)
        # A settled refund goes with the account.
        assert _ledger(engine) is None

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

    def test_a_transient_refund_failure_refuses_and_keeps_the_account(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="transient")
        set_gateway(gw)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 502
        assert res.json()["code"] == "refund_failed"
        assert "not deleted" in res.json()["detail"]
        assert _account_exists(engine, uid)
        assert _ledger(engine).state == "pending"

    def test_a_permanent_refund_failure_does_not_trap_the_account(self, engine, caplog):
        """F3 (owner decision): the cancel landed, Stripe refused the refund
        for good — the account is deleted, and the owed refund is recorded,
        outliving it, with nothing but Stripe ids, amount and reason."""
        uid = _seed(engine)
        gw = StripeLikeGateway()

        def always_refuse(*args, **kwargs):
            gw.log.append(("refund",) + args)
            raise PermanentGatewayError("amount exceeds creditable")
        gw.refund_unused = always_refuse
        set_gateway(gw)

        with caplog.at_level(logging.ERROR):
            res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert not _account_exists(engine, uid)
        entry = _ledger(engine)
        assert entry is not None, "the owed refund must survive the account"
        assert (entry.state, entry.customer_id, entry.amount) == (
            "failed_permanent", "cus_1", EXPECTED)
        assert "creditable" in entry.reason
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)

    def test_the_retry_refunds_exactly_once(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="transient")
        set_gateway(gw)
        assert _as(uid).delete("/api/auth/me").status_code == 502

        gw.refund_mode = "ok"
        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert gw.total_refunded == EXPECTED
        assert not _account_exists(engine, uid)

    def test_a_retry_after_the_deadline_still_refunds(self, engine, clock):
        """F1: the cancel landed at the last moment and the refund failed; the
        retry the next day completes it."""
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="transient")
        set_gateway(gw)
        assert _as(uid).delete("/api/auth/me").status_code == 502

        clock.now = CLOSES + DAY
        gw.refund_mode = "ok"
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
        assert len(gw.calls("refund")) == 1
        assert not _account_exists(engine, uid)

    def test_admin_deletion_inside_the_window_refunds_too(self, engine):
        uid = _seed(engine)
        admin_id = _admin(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        res = _as(admin_id).delete(f"/api/admin/users/{uid}")

        assert res.status_code in (200, 204), res.text
        assert gw.total_refunded == EXPECTED

    def test_a_contract_recorded_while_cancelling_is_read_fresh(self, engine):
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


def _admin(engine) -> int:
    with Session(engine) as sess:
        local = LocalUser(username=f"admin{next(_seq)}@x.io")
        sess.add(local)
        sess.commit()
        sess.refresh(local)
        admin = UserInfo(local_auth_id=local.id, email="admin@x.io", is_admin=True)
        sess.add(admin)
        sess.commit()
        sess.refresh(admin)
        return admin.id


# ── Owed refunds, for the owner ──────────────────────────────────────────────

class TestOwedRefunds:
    def _owe(self, engine, sub="sub_9", state="failed_permanent"):
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(subscription_id=sub, customer_id="cus_9",
                                        state=state, amount=300, refunded=100,
                                        currency="eur", reason="charge_disputed",
                                        invoice_id="in_9"))
            sess.commit()

    def test_the_admin_lists_what_is_owed(self, engine):
        self._owe(engine)
        self._owe(engine, sub="sub_done", state="done")
        res = _as(_admin(engine)).get("/api/admin/billing/owed-refunds")
        assert res.status_code == 200
        (entry,) = res.json()
        assert entry["subscription_id"] == "sub_9"
        assert entry["owed_cents"] == 200
        assert entry["reason"] == "charge_disputed"

    def test_marking_it_settled_deletes_the_record(self, engine):
        self._owe(engine)
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_9/settle")
        assert res.status_code == 200
        assert _ledger(engine, "sub_9") is None

    def test_only_an_owed_refund_can_be_settled(self, engine):
        self._owe(engine, state="pending")
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_9/settle")
        assert res.status_code == 404
        assert _ledger(engine, "sub_9") is not None

    def test_not_for_users(self, engine):
        uid = _seed(engine)
        assert _as(uid).get("/api/admin/billing/owed-refunds").status_code == 403

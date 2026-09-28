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
from src.billing.refunds import prorated_refund_amount
from src.billing.gateway import (
    GatewayError,
    PermanentGatewayError,
    RefundBasis,
    IdempotencyConflict,
    IssuedRefund,
    RefundPlan,
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


def _unused_at(at: float) -> int:
    """The unused part of the fake invoice ``in_1`` at ``at``."""
    return prorated_refund_amount(PERIOD_START, PERIOD_END, PAID, at)


class StripeLikeGateway:
    """Subscriptions that end when cancelled; refunds that dedupe and add up.

    The refund arithmetic mirrors ``StripeGateway.refund_plan`` — refunds
    already made count towards what is owed, the card gets back at most what
    it paid, and whatever cannot go back is owed — so the ledger is tested
    against what the real gateway would answer, not a friendlier one.

    ``refund_mode``:
    * ``ok`` — refunds;
    * ``transient`` — the credit note fails before anything happens (a 5xx);
    * ``timeout`` — the credit note IS created, but the answer is lost;
    * ``permanent`` — a definite refusal, every time;
    * ``conflict`` — the key was seen with other parameters, once;
    * ``no_payment`` — nothing to refund against (paid from the balance).

    ``renewed``: the subscription renewed — its latest invoice is ``in_2``,
    for the next period; ``in_1`` is still there when asked for by id.
    """

    def __init__(self, *, amount_paid=PAID, total=None, fail_cancel=False,
                 refund_mode="ok", fail_discard=False, subscriptions=("sub_1",),
                 cancel_at=CANCEL_AT, latest=None, in_force=None, manual_refunds=0,
                 credited_elsewhere=0):
        self.amount_paid = amount_paid
        self.total = amount_paid if total is None else total
        self.cancel_at = cancel_at
        self.fail_cancel = fail_cancel
        self.refund_mode = refund_mode
        self.fail_discard = fail_discard
        self.ended: dict[str, float] = {sid: 0.0 for sid in subscriptions}
        self.notes: dict[str, int] = {}          # refund key -> cents (metadata)
        self.provider_keys: dict[str, int] = {}  # provider idempotency key -> cents
        self.manual_refunds = manual_refunds     # refunded by hand on the payment
        self.credited_elsewhere = credited_elsewhere
        self.pending: dict[str, int] = {sid: 1 for sid in subscriptions}
        self.latest = latest
        self.in_force = in_force
        self.renewed = False
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

    def refund_basis(self, subscription_id, *, invoice_id=""):
        self.log.append(("basis", subscription_id, invoice_id))
        ended = self.ended.get(subscription_id, 0.0)
        if invoice_id == "in_2" or (not invoice_id and self.renewed):
            return RefundBasis(subscription_id, ended, "in_2", self.amount_paid, "eur",
                               PERIOD_END, PERIOD_END + 30 * DAY, total=self.total)
        return RefundBasis(subscription_id, ended, "in_1", self.amount_paid, "eur",
                           PERIOD_START, PERIOD_END, total=self.total)

    def refund_plan(self, subscription_id, amount_cents, refund_key, *, invoice_id):
        self.log.append(("plan", subscription_id, amount_cents))
        others = sum(v for k, v in self.notes.items() if k != refund_key)
        already = self.manual_refunds + others
        if refund_key in self.notes:
            mine = self.notes[refund_key]
            still = max(0, amount_cents - mine - already)
            return RefundPlan(0, still, "part could not be made" if still else "",
                              existing_note_id="cn_1", existing_cents=mine,
                              existing_refund_id="re_1")
        owed = max(0, amount_cents - already)
        if self.refund_mode == "no_payment":
            return RefundPlan(0, owed, "paid without a refundable payment" if owed else "")
        creditable = max(0, self.total - self.credited_elsewhere - others)
        send = max(0, min(owed, self.amount_paid - already, creditable))
        short = owed - send
        return RefundPlan(send, short, "paid partly from the customer's balance"
                          if short else "")

    def issue_refund(self, subscription_id, send_cents, refund_key, *, invoice_id,
                     attempt=0):
        self.log.append(("refund", subscription_id, send_cents, refund_key,
                         invoice_id, attempt))
        if self.during_refund:
            hook, self.during_refund = self.during_refund, None
            hook()
        if refund_key in self.notes:
            return IssuedRefund("cn_1", "re_1")
        provider_key = f"{refund_key}:{attempt}"
        if provider_key in self.provider_keys:
            if self.provider_keys[provider_key] != send_cents:
                raise IdempotencyConflict("key reused with other parameters")
            return IssuedRefund("cn_1", "re_1")
        mode = self.refund_mode
        if mode == "transient":
            raise GatewayError("503 from Stripe")
        if mode == "permanent":
            raise PermanentGatewayError("charge_disputed")
        if mode == "conflict":
            self.refund_mode = "ok"
            raise IdempotencyConflict("key reused with other parameters")
        self.notes[refund_key] = send_cents
        self.provider_keys[provider_key] = send_cents
        if mode == "timeout":
            self.refund_mode = "ok"
            raise GatewayError("read timed out")
        return IssuedRefund("cn_1", "re_1")

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
          contract="sub_1", customer="cus_1") -> int:
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
        assert (entry.state, entry.amount, entry.to_refund, entry.refunded,
                entry.owed, entry.invoice_id) == ("done", EXPECTED, EXPECTED, EXPECTED,
                                                  0, "in_1")
        assert (entry.claim_token, entry.lease_until) == ("", 0)

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

    def test_a_failed_cancel_records_the_request_and_refunds_nothing(
        self, engine, clock
    ):
        """Review round 5 (R5-1) — a policy change, superseding round 2's
        F1, under which a failed cancel recorded nothing and the request "did
        not happen". The terms make the request the withdrawal, so it is
        written down before the cancel is tried: the request time, the
        contract's invoice (read before any renewal) and when the cancel was
        tried, all frozen."""
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 502
        assert "withdrawal is recorded" in res.json()["detail"]
        assert "nothing was refunded" in res.json()["detail"]
        assert gw.calls("refund") == []
        kinds = [c[0] for c in gw.log]
        assert kinds.index("basis") < kinds.index("cancel")
        entry = _ledger(engine)
        assert (entry.state, entry.requested_at, entry.cancel_attempted_at,
                entry.invoice_id, entry.amount) == (
            "pending", clock.now, clock.now, "in_1", -1)
        assert (entry.claim_token, entry.lease_until) == ("", 0)

    def test_a_failed_cancel_is_completed_after_the_deadline(self, engine, clock):
        """R5-1 (supersedes F1's refusal): asked in time, the cancel failed;
        retried the next day, it is the same withdrawal — cancelled now, and
        refunded on the frozen invoice for the part unused when it ended. The
        late-landing bound runs from the retry's attempt, not the request."""
        requested = LAST_MOMENT - 60
        clock.now = requested
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502

        clock.now = CLOSES + DAY
        assert _open(uid) is True  # the retry is offered
        gw.fail_cancel = False
        gw.cancel_at = clock.now
        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json() == {"refunded_cents": _unused_at(CLOSES + DAY),
                              "currency": "eur", "owed_cents": 0}
        assert gw.total_refunded == _unused_at(CLOSES + DAY)
        entry = _ledger(engine)
        assert (entry.state, entry.requested_at, entry.cancel_attempted_at) == (
            "done", requested, CLOSES + DAY)
        assert _open(uid) is False

    def test_a_cancel_whose_answer_was_lost_is_completed_after_the_deadline(
        self, engine, clock
    ):
        """R5-1: Stripe cancelled, the answer timed out, the user retried
        after midnight. Cancelled and refunded — never cancelled and not."""
        clock.now = LAST_MOMENT - 30
        uid = _seed(engine)
        gw = StripeLikeGateway(cancel_at=LAST_MOMENT - 29)
        real = gw.cancel_subscription

        def lost(subscription_id, customer_id=""):
            real(subscription_id, customer_id)
            raise GatewayError("read timed out")
        gw.cancel_subscription = lost
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502
        assert gw.ended["sub_1"]

        gw.cancel_subscription = real
        clock.now = CLOSES + 60
        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert gw.total_refunded == _unused_at(LAST_MOMENT - 29)

    def test_a_subscription_that_renewed_meanwhile_is_owed_not_refunded(
        self, engine, clock, caplog
    ):
        """R5-1, the renewal case — why round 2 (F1) refused late
        completions. Asked on day 13; the cancel kept failing; the
        subscription renewed (invoice in_2); the user retried weeks later.
        The retry cancels (the request time stays frozen), but nothing is
        refunded automatically, on either invoice: the renewal was charged
        after the withdrawal was asked for. The refund is recorded as owed —
        the unused part of the frozen invoice, measured at the request — for
        the owner to refund by hand with the renewal. A request never opens a
        window on a renewal."""
        requested = START + 13 * DAY
        clock.now = requested
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502

        gw.renewed = True
        clock.now = PERIOD_END + 10 * DAY
        gw.fail_cancel = False
        gw.cancel_at = clock.now
        with caplog.at_level(logging.ERROR):
            res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert gw.calls("cancel")[-1] == ("cancel", "sub_1")
        assert not gw.calls("refund") and gw.total_refunded == 0
        assert res.json() == {"refunded_cents": 0, "currency": "eur",
                              "owed_cents": _unused_at(requested)}
        entry = _ledger(engine)
        assert (entry.state, entry.invoice_id, entry.owed, entry.requested_at) == (
            "owed", "in_1", _unused_at(requested), requested)
        assert "renewed" in entry.reason
        # The invoice was frozen at the request, before the renewal; the retry
        # asks for that one by id, not for the latest.
        assert [c[2] for c in gw.calls("basis")] == ["", "in_1"]
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)

    def test_a_renewal_is_caught_when_no_invoice_could_be_frozen(self, engine, clock):
        """R5-1: Stripe could not be read at the request, so no invoice was
        frozen and nothing was cancelled. The retry after a renewal finds the
        renewal as the latest invoice — begun after the request — and does
        not refund it."""
        clock.now = START + 13 * DAY
        uid = _seed(engine)
        gw = StripeLikeGateway()
        real = gw.refund_basis

        def down(*args, **kwargs):
            raise GatewayError("503 from Stripe")
        gw.refund_basis = down
        set_gateway(gw)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 502
        assert "nothing was cancelled" in res.json()["detail"]
        assert not gw.calls("cancel")
        assert _ledger(engine).invoice_id == ""

        gw.refund_basis = real
        gw.renewed = True
        clock.now = PERIOD_END + 10 * DAY
        gw.cancel_at = clock.now
        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert gw.total_refunded == 0
        entry = _ledger(engine)
        assert entry.state == "owed"
        assert "renewed (in_2)" in entry.reason

    def test_a_deletion_refused_at_the_cancel_is_completed_later(self, engine, clock):
        """R5-1 (supersedes F1): a deletion refused at the cancel, at the
        last moment, recorded the withdrawal; retried after the deadline, the
        deletion refunds it."""
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        assert _as(uid).delete("/api/auth/me").status_code == 502
        entry = _ledger(engine)
        assert (entry.state, entry.requested_at) == ("pending", LAST_MOMENT)

        clock.now = CLOSES + DAY
        gw.fail_cancel = False
        gw.cancel_at = clock.now
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert gw.total_refunded == _unused_at(CLOSES + DAY)
        assert not _account_exists(engine, uid)

    def test_a_pending_withdrawal_whose_subscription_runs_is_cancelled_again(
        self, engine, clock
    ):
        """R5-1: a pending row means the withdrawal was asked for in time —
        not, as until round 4, that the cancel landed — so every retry cancels
        again (idempotent) before refunding; while it fails, nothing is
        refunded and the row stays pending."""
        uid = _seed(engine)
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(subscription_id="sub_1", customer_id="cus_1",
                                        contract_started_at=START, invoice_id="in_1",
                                        requested_at=START + DAY,
                                        cancel_attempted_at=START + DAY))
            sess.commit()
        gw = StripeLikeGateway(fail_cancel=True)  # sub_1 still running at Stripe
        set_gateway(gw)
        clock.now = CLOSES + DAY
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 502
        assert "withdrawal is recorded" in res.json()["detail"]
        assert gw.calls("cancel") and gw.total_refunded == 0
        assert _ledger(engine).state == "pending"

        gw.fail_cancel = False
        gw.cancel_at = clock.now
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 200, res.text
        assert gw.total_refunded == _unused_at(CLOSES + DAY)

    def test_an_end_stripe_does_not_show_yet_is_retried_never_refunded(self, engine):
        """The cancel was accepted but Stripe does not show the end yet: a
        failure to retry, never a refusal after cancelling — and nothing is
        refunded against a running subscription."""
        uid = _seed(engine)
        gw = StripeLikeGateway()
        gw.cancel_subscription = lambda subscription_id, customer_id="": None
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 502
        assert res.json()["code"] == "refund_failed"
        assert gw.total_refunded == 0
        assert _ledger(engine).state == "pending"

    def test_a_request_outside_the_window_leaves_no_row(self, engine, clock):
        """A pending row whose request was made after the deadline is not
        eligible: nothing frozen, nothing owed, so the row goes."""
        from src.billing.withdrawal import NotEligible, settle

        uid = _seed(engine)
        gw = StripeLikeGateway()
        gw.ended["sub_1"] = CLOSES + 1
        set_gateway(gw)
        with pytest.raises(NotEligible):
            settle(gw, user_info_id=uid, customer_id="cus_1", subscription_id="sub_1",
                   contract_start=START, requested_at=CLOSES + 1)
        assert _ledger(engine) is None
        assert gw.total_refunded == 0

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

    def test_a_definite_refusal_is_owed_and_final(self, engine, caplog):
        """F3 / review round 3: a definite refusal records the refund as owed
        (ERROR logged). Owed is final for the app: a later deletion does not
        send it to Stripe again — the owner may already have paid it by hand —
        and the record outlives the account. (Until round 3 the deletion
        retried it under a new key; that was removed on purpose.)"""
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="permanent")
        set_gateway(gw)

        with caplog.at_level(logging.ERROR):
            res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json() == {"refunded_cents": 0, "currency": "eur",
                              "owed_cents": EXPECTED}
        entry = _ledger(engine)
        assert (entry.state, entry.owed, entry.attempt) == ("owed", EXPECTED, 0)
        assert "charge_disputed" in entry.reason
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)
        assert _open(uid) is False  # the owner settles it

        gw.refund_mode = "ok"
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert len(gw.calls("refund")) == 1
        assert gw.total_refunded == 0
        assert _ledger(engine).state == "owed"  # outlives the account

    def test_a_withdrawal_after_an_owed_refund_does_not_ask_stripe(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="permanent")
        set_gateway(gw)
        _as(uid).post("/api/billing/withdraw")
        gw.refund_mode = "ok"

        res = _as(uid).post("/api/billing/withdraw")

        assert res.json()["owed_cents"] == EXPECTED
        assert len(gw.calls("refund")) == 1 and gw.total_refunded == 0

    def test_nothing_to_refund_against_is_owed_not_forgotten(self, engine, caplog):
        """F4: paid without a refundable payment — ERROR, owed."""
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="no_payment")
        set_gateway(gw)
        with caplog.at_level(logging.ERROR):
            res = _as(uid).post("/api/billing/withdraw")
        assert res.json()["owed_cents"] == EXPECTED
        assert _ledger(engine).state == "owed"
        assert not gw.calls("refund")
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)

    def test_an_invoice_paid_from_the_balance_is_owed(self, engine, caplog):
        """F4: amount_paid 0, total 400 — the card took nothing, but the unused
        period was paid for, on the total."""
        uid = _seed(engine)
        gw = StripeLikeGateway(amount_paid=0, total=PAID)
        set_gateway(gw)
        with caplog.at_level(logging.ERROR):
            res = _as(uid).post("/api/billing/withdraw")
        assert res.json()["owed_cents"] == EXPECTED
        assert not gw.calls("refund")
        assert "balance" in _ledger(engine).reason
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)

    def test_an_invoice_paid_partly_from_the_balance(self, engine):
        """F-D: total 400, card 200. The unused period is owed on the total
        (266): 200 goes back to the card, 66 is owed by hand."""
        uid = _seed(engine)
        gw = StripeLikeGateway(amount_paid=200, total=PAID)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.json() == {"refunded_cents": 200, "currency": "eur",
                              "owed_cents": 66}
        entry = _ledger(engine)
        assert (entry.state, entry.amount, entry.refunded, entry.owed) == (
            "owed", EXPECTED, 200, 66)

    def test_a_key_conflict_is_retried_under_a_new_key_not_owed(self, engine):
        """F-E: Stripe saw the key with other parameters — once more, under
        the next attempt, in the same request."""
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="conflict")
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json()["refunded_cents"] == EXPECTED
        assert [c[5] for c in gw.calls("refund")] == [0, 1]
        entry = _ledger(engine)
        assert (entry.state, entry.attempt) == ("done", 1)

    def test_two_key_conflicts_are_transient_never_owed(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)

        def always_conflict(*args, **kwargs):
            gw.log.append(("refund",) + args + (kwargs.get("attempt"),))
            raise IdempotencyConflict("key reused")
        gw.issue_refund = always_conflict

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 502
        assert _ledger(engine).state == "pending"

    def test_a_replay_sends_the_frozen_amount(self, engine):
        """F-E: a refund made by hand between a lost answer and the retry must
        not change what is sent under the same key — or Stripe answers with
        a key conflict, and a new key would refund twice."""
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="timeout")
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502
        gw.notes.clear()          # the metadata not visible yet: only the key saves us
        gw.manual_refunds = 100   # and the owner refunded something meanwhile

        assert _as(uid).post("/api/billing/withdraw").status_code == 200

        assert [(c[2], c[5]) for c in gw.calls("refund")] == [
            (EXPECTED, 0), (EXPECTED, 0)]
        assert sum(gw.provider_keys.values()) == EXPECTED

    def test_a_request_in_time_is_refunded_though_the_cancel_lands_after(
        self, engine, clock
    ):
        """Review round 4 — a policy correction, not a bent assertion. Round
        3 refused this (then test_a_cancel_landing_after_the_deadline_is_not_
        refunded): asked at the last moment, Stripe recorded the end just after
        midnight, and the user was left cancelled with nothing refunded. The
        terms make the *request* the withdrawal, so it is refunded."""
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(cancel_at=CLOSES + 2)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert res.json()["owed_cents"] == 0
        assert gw.total_refunded > 0
        entry = _ledger(engine)
        assert (entry.state, entry.requested_at) == ("done", LAST_MOMENT)

    def test_a_cancel_landing_past_the_bound_is_not_refunded_automatically(
        self, engine, clock
    ):
        """The request was in time, but Stripe recorded the end more than
        CANCEL_BOUND_SECONDS later: something went wrong. Still no refusal
        after cancelling — the refund is recorded as owed, for the owner."""
        from src.billing.withdrawal import CANCEL_BOUND_SECONDS

        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(cancel_at=LAST_MOMENT + CANCEL_BOUND_SECONDS + 1)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200
        assert gw.total_refunded == 0
        entry = _ledger(engine)
        assert entry.state == "owed"
        assert "after it was attempted" in entry.reason

    def test_the_request_is_taken_before_the_cancel(self, engine, clock):
        """Frozen on the row as when the user asked, not when Stripe answered."""
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway()

        def slow_cancel(subscription_id, customer_id=""):
            clock.now = CLOSES + 30  # the call took a while
            gw.ended[subscription_id] = clock.now
        gw.cancel_subscription = slow_cancel
        set_gateway(gw)

        assert _as(uid).post("/api/billing/withdraw").status_code == 200
        assert _ledger(engine).requested_at == LAST_MOMENT
        assert gw.total_refunded > 0

    def test_a_deletion_in_time_is_refunded_though_the_cancel_lands_after(
        self, engine, clock
    ):
        clock.now = LAST_MOMENT
        uid = _seed(engine)
        gw = StripeLikeGateway(cancel_at=CLOSES + 2)
        set_gateway(gw)

        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert gw.total_refunded > 0

    def test_a_stale_claim_cannot_overwrite_the_result(self, engine, clock):
        """F-F: a request whose lease ran out while it was still working must
        not write over what the next holder recorded."""
        from src.billing.withdrawal import LEASE_SECONDS, LostClaim, _claim, _save

        uid = _seed(engine)
        _row_, stale_token = _claim(uid, "cus_1", "sub_1", START, clock.now)
        clock.now += LEASE_SECONDS + 1
        gw = StripeLikeGateway()
        gw.ended["sub_1"] = CANCEL_AT
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 200
        assert _ledger(engine).state == "done"

        with pytest.raises(LostClaim):
            _save("sub_1", uid, stale_token, state="owed", owed=EXPECTED)
        assert _ledger(engine).state == "done"

    def test_the_lease_outlasts_the_slowest_refund(self):
        """F-F: ~14 SDK calls, each at most (retries + 1) x timeout."""
        from src.billing.stripe_gateway import HTTP_TIMEOUT_SECONDS, MAX_NETWORK_RETRIES
        from src.billing.withdrawal import LEASE_SECONDS
        assert LEASE_SECONDS > 14 * (MAX_NETWORK_RETRIES + 1) * HTTP_TIMEOUT_SECONDS

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
        # Read at the request (the invoice) and at the first computation (the
        # amount); the retry reads neither again.
        assert len(gw.calls("basis")) == 2

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
        assert res.json() == {"amount_cents": EXPECTED, "owed_cents": 0,
                              "currency": "eur", "closes_at": CLOSES}
        assert not gw.calls("cancel") and not gw.calls("refund")
        assert _ledger(engine) is None

    def test_the_quote_is_computed_as_the_refund_is(self, engine, clock):
        """Item 10: on the invoice total, split between the card and what would
        be owed — total 400, card 200: 200 back, 66 owed."""
        clock.now = CANCEL_AT
        uid = _seed(engine)
        set_gateway(StripeLikeGateway(amount_paid=200, total=PAID))

        body = _as(uid).get("/api/billing/withdraw").json()

        assert (body["amount_cents"], body["owed_cents"]) == (200, 66)

    def test_outside_the_window_is_refused(self, engine, clock):
        clock.now = CLOSES
        uid = _seed(engine)
        gw = StripeLikeGateway()
        set_gateway(gw)
        res = _as(uid).get("/api/billing/withdraw")
        assert res.status_code == 409
        assert gw.log == []

    def test_a_pending_withdrawal_that_renewed_is_quoted_as_owed(self, engine, clock):
        """Round 5 (R5-1): the retry's dialog must not promise a refund to
        the card that settle will not make — the subscription renewed since
        the request, so the refund is owed, measured at the request."""
        requested = START + 13 * DAY
        clock.now = requested
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502
        gw.renewed = True
        clock.now = PERIOD_END + 10 * DAY

        body = _as(uid).get("/api/billing/withdraw").json()

        assert (body["amount_cents"], body["owed_cents"]) == (0, _unused_at(requested))
        assert gw.calls("basis")[-1][2] == "in_1"


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

    @pytest.mark.parametrize("state", ["done", "owed", "settled"])
    def test_closed_once_the_contracts_refund_is_final(self, engine, state):
        """F7 / F-B: whoever refunded it — including a deletion later refused
        with billing_changed — and whoever settled it."""
        uid = _seed(engine)
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(subscription_id="sub_1", customer_id="cus_1",
                                        state=state, amount=EXPECTED))
            sess.commit()
        assert _open(uid) is False

    def test_an_earlier_contracts_refund_does_not_hide_this_one(self, engine):
        uid = _seed(engine, sub_id="sub_B", contract="sub_B")
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
        gw.issue_refund = always_refuse
        set_gateway(gw)

        with caplog.at_level(logging.ERROR):
            res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert not _account_exists(engine, uid)
        entry = _ledger(engine)
        assert entry is not None, "the owed refund must survive the account"
        assert (entry.state, entry.customer_id, entry.owed, entry.invoice_id) == (
            "owed", "cus_1", EXPECTED, "in_1")
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
    def _owe(self, engine, sub="sub_9", state="owed", customer="cus_9", **extra):
        fields = dict(subscription_id=sub, customer_id=customer, state=state,
                      amount=300, to_refund=100, refunded=100, owed=200,
                      currency="eur", reason="charge_disputed", invoice_id="in_9")
        fields.update(extra)
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(**fields))
            sess.commit()

    def test_the_admin_lists_what_is_owed(self, engine):
        self._owe(engine)
        self._owe(engine, sub="sub_done", state="done")
        self._owe(engine, sub="sub_settled", state="settled")
        res = _as(_admin(engine)).get("/api/admin/billing/owed-refunds")
        assert res.status_code == 200
        (entry,) = res.json()
        assert (entry["subscription_id"], entry["state"], entry["owed_cents"]) == (
            "sub_9", "owed", 200)
        assert entry["reason"] == "charge_disputed"

    def test_a_pending_refund_stuck_past_its_lease_is_listed(self, engine, clock):
        """F-C: nothing may sit unseen."""
        from src.billing.withdrawal import LEASE_SECONDS
        self._owe(engine, sub="sub_stuck", state="pending",
                  created_at=clock.now - LEASE_SECONDS - 1)
        self._owe(engine, sub="sub_fresh", state="pending", created_at=clock.now)
        res = _as(_admin(engine)).get("/api/admin/billing/owed-refunds")
        assert [e["subscription_id"] for e in res.json()] == ["sub_stuck"]

    def test_settling_while_the_account_exists_keeps_a_tombstone(self, engine, clock):
        """F-B: the account can still withdraw or be deleted; the tombstone is
        what keeps either from refunding the subscription again."""
        uid = _seed(engine, customer="cus_9", sub_id="sub_9", contract="sub_9")
        self._owe(engine)
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_9/settle")
        assert res.status_code == 200
        entry = _ledger(engine, "sub_9")
        assert (entry.state, entry.owed, entry.settled_at) == ("settled", 200, clock.now)
        assert _open(uid) is False

    def test_settling_after_the_account_is_gone_deletes_the_record(self, engine):
        """F-B: no account is left for a tombstone to protect, and the privacy
        policy promises the record goes once settled."""
        self._owe(engine)  # cus_9 has no account
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_9/settle")
        assert res.status_code == 200
        assert _ledger(engine, "sub_9") is None

    def test_settling_is_refused_while_a_request_holds_the_refund(self, engine, clock):
        self._owe(engine, state="pending", lease_until=clock.now + 60)
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_9/settle")
        assert res.status_code == 409
        assert _ledger(engine, "sub_9").state == "pending"

    @pytest.mark.parametrize("state", ["done", "settled"])
    def test_a_finished_refund_cannot_be_settled(self, engine, state):
        self._owe(engine, state=state)
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_9/settle")
        assert res.status_code == 404

    def test_not_for_users(self, engine):
        uid = _seed(engine)
        assert _as(uid).get("/api/admin/billing/owed-refunds").status_code == 403


# ── Review round 3: the reviewer's reproducers ───────────────────────────────

class TestSettledIsFinal:
    """F-B: once the owner settled it by hand, nothing refunds it again."""

    def _owed_then_settled(self, engine, gw):
        uid = _seed(engine)
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").json()["owed_cents"] > 0
        assert _ledger(engine).state == "owed"
        admin = _admin(engine)
        assert _as(admin).post(
            "/api/admin/billing/owed-refunds/sub_1/settle").status_code == 200
        gw.refund_mode = "ok"
        return uid

    def test_a_withdrawal_after_settling_refunds_nothing(self, engine):
        gw = StripeLikeGateway(refund_mode="permanent")
        uid = self._owed_then_settled(engine, gw)
        assert _open(uid) is False
        _as(uid).post("/api/billing/withdraw")
        assert gw.total_refunded == 0
        assert _ledger(engine).state == "settled"

    def test_a_deletion_after_settling_refunds_nothing(self, engine):
        gw = StripeLikeGateway(refund_mode="permanent")
        uid = self._owed_then_settled(engine, gw)
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert gw.total_refunded == 0
        # Finished refunds go with the account.
        assert _ledger(engine) is None


class TestOwedIsFinalAgainstTheRealGateway:
    """F-A: the real StripeGateway, not the fake — a retry of an owed row
    must not turn it into 'done' and erase what is owed."""

    def _gateway(self, monkeypatch, **kw):
        from tests.test_billing_stripe_gateway import _RefundStripe
        from src.billing.stripe_gateway import StripeGateway

        fake = _RefundStripe(subscription={"id": "sub_1", "status": "canceled",
                                           "ended_at": CANCEL_AT}, **kw)
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        return StripeGateway(), fake

    def _invoice(self, **kw):
        base = {"id": "in_1", "currency": "eur",
                "lines": {"object": "list", "data": [
                    {"period": {"start": PERIOD_START, "end": PERIOD_END}}]}}
        base.update(kw)
        return base

    def _settle(self, gw, uid, now):
        from src.billing import withdrawal
        return withdrawal.settle(gw, user_info_id=uid, customer_id="cus_1",
                                 subscription_id="sub_1", contract_start=START,
                                 requested_at=now, now=now)

    def test_a_balance_paid_owed_refund_survives_a_retry(self, engine, monkeypatch, clock):
        uid = _seed(engine)
        gw, fake = self._gateway(monkeypatch,
                                 invoices=[self._invoice(amount_paid=0, total=PAID)],
                                 invoice_payments=[])
        first = self._settle(gw, uid, clock.now)
        assert first.owed_cents == EXPECTED
        calls = len(fake.log)

        second = self._settle(gw, uid, clock.now + 1)

        assert second.owed_cents == EXPECTED
        assert _ledger(engine).state == "owed"
        assert len(fake.log) == calls  # Stripe not asked again

    def test_a_partly_credited_owed_refund_survives_a_retry(self, engine, monkeypatch, clock):
        """The invoice was already credited 300 of 400: 100 goes back, the
        rest of the 266 owed is recorded — and stays recorded."""
        uid = _seed(engine)
        gw, fake = self._gateway(
            monkeypatch,
            invoices=[self._invoice(amount_paid=PAID, total=PAID, payment_intent="pi_1")],
            credit_notes=[{"id": "cn_balance", "amount": 300, "status": "issued",
                           "metadata": {}}])
        first = self._settle(gw, uid, clock.now)
        assert (first.amount_cents, first.owed_cents) == (100, 166)

        second = self._settle(gw, uid, clock.now + 1)

        assert (second.amount_cents, second.owed_cents) == (100, 166)
        assert _ledger(engine).state == "owed"
        assert len(fake.created) == 1


class TestEarlierContractsPendingRefund:
    """F-C: a pending refund of an earlier contract is not stranded when a
    new contract starts."""

    def _strand(self, engine, clock):
        uid = _seed(engine)
        gw = StripeLikeGateway(refund_mode="transient", subscriptions=("sub_1", "sub_2"))
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502
        assert _ledger(engine).state == "pending"
        gw.refund_mode = "ok"
        with Session(engine) as sess:
            row = sess.exec(select(Subscription).where(
                Subscription.user_info_id == uid)).first()
            row.provider_subscription_id = "sub_2"
            row.contract_subscription_id = "sub_2"
            row.contract_started_at = START + 12 * DAY
            row.status = "active"
            sess.add(row)
            sess.commit()
        clock.now = START + 30 * DAY  # sub_2's window closed too
        return uid, gw

    def test_a_withdrawal_finishes_it(self, engine, clock):
        uid, gw = self._strand(engine, clock)
        res = _as(uid).post("/api/billing/withdraw")
        assert res.status_code == 200, res.text
        assert res.json()["refunded_cents"] == EXPECTED
        assert _ledger(engine).state == "done"
        assert not [c for c in gw.calls("cancel") if c[1] == "sub_2"]

    def test_a_deletion_finishes_it(self, engine, clock):
        uid, gw = self._strand(engine, clock)
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert gw.total_refunded == EXPECTED
        assert _ledger(engine) is None  # done, gone with the account

    def test_a_deletion_that_cannot_finish_it_is_refused(self, engine, clock):
        uid, gw = self._strand(engine, clock)
        gw.refund_mode = "transient"
        res = _as(uid).delete("/api/auth/me")
        assert res.status_code == 502
        assert _account_exists(engine, uid)


# ── Review round 4 ───────────────────────────────────────────────────────────

def _stuck_row(engine, clock, **extra):
    """A pending refund whose credit note was issued (to_refund frozen) and
    whose answer was lost; its lease ran out long ago."""
    from src.billing.withdrawal import LEASE_SECONDS
    fields = dict(subscription_id="sub_1", customer_id="cus_1", state="pending",
                  amount=EXPECTED, to_refund=EXPECTED, owed=0, currency="eur",
                  invoice_id="in_1", contract_started_at=START,
                  requested_at=clock.now - 2 * LEASE_SECONDS,
                  created_at=clock.now - 2 * LEASE_SECONDS)
    fields.update(extra)
    with Session(engine) as sess:
        sess.add(SubscriptionRefund(**fields))
        sess.commit()


class TestStuckPendingResolvedFromStripe:
    """Item 2: before the owner refunds a stuck refund by hand, Stripe is
    asked whether its credit note was made after all."""

    def test_the_list_resolves_a_credit_note_made_after_all(self, engine, clock):
        _seed(engine)
        _stuck_row(engine, clock)
        gw = StripeLikeGateway()
        gw.notes[KEY] = EXPECTED  # created; the answer was lost
        set_gateway(gw)

        res = _as(_admin(engine)).get("/api/admin/billing/owed-refunds")

        assert res.json() == []
        entry = _ledger(engine)
        assert (entry.state, entry.refunded, entry.credit_note_id) == (
            "done", EXPECTED, "cn_1")

    def test_settling_one_refunded_after_all_is_refused(self, engine, clock):
        """The double refund the review found: the owner would have paid by
        hand what Stripe had already refunded."""
        _seed(engine)
        _stuck_row(engine, clock)
        gw = StripeLikeGateway()
        gw.notes[KEY] = EXPECTED
        set_gateway(gw)

        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_1/settle")

        assert res.status_code == 409
        assert "refunded after all" in res.json()["detail"]
        assert _ledger(engine).state == "done"

    def test_one_that_cannot_be_checked_is_not_settled(self, engine, clock):
        _seed(engine)
        _stuck_row(engine, clock)
        gw = StripeLikeGateway()

        def down(*args, **kwargs):
            raise GatewayError("Stripe down")
        gw.refund_plan = down
        set_gateway(gw)

        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_1/settle")

        assert res.status_code == 409
        assert "could not be checked" in res.json()["detail"]
        assert _ledger(engine).state == "pending"
        listed = _as(_admin(engine)).get("/api/admin/billing/owed-refunds").json()
        assert "not checked at Stripe" in listed[0]["reason"]

    def test_one_with_no_credit_note_is_settled(self, engine, clock):
        _seed(engine)
        _stuck_row(engine, clock)
        set_gateway(StripeLikeGateway())  # no note under the key
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_1/settle")
        assert res.status_code == 200
        assert _ledger(engine).state == "settled"


class TestSettleIsFenced:
    """Item 5: whatever touches the row between settle's read and its write
    wins; the settle answers 409 and changes nothing. One race per clause of
    the fence."""

    RACES = {
        # Another request finished the refund meanwhile.
        "finished": dict(state="done", refunded=EXPECTED, owed=0),
        # A holder claimed it and died: its lease has run out, its token stays.
        "crashed_holder": dict(claim_token="someone-else", lease_until=1.0),
        # A request holds it right now.
        "live_claim": dict(claim_token="someone-else", lease_until=9e12),
    }

    @pytest.mark.parametrize("race", sorted(RACES))
    def test_a_change_in_between_stops_the_settle(self, engine, clock, monkeypatch,
                                                  race):
        import api.admin as admin_mod

        _seed(engine)
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(subscription_id="sub_1", customer_id="cus_1",
                                        state="owed", amount=EXPECTED, owed=EXPECTED))
            sess.commit()
        original = admin_mod.owner_account

        def change_first(sess, customer_id):
            with Session(engine) as other:
                row = other.get(SubscriptionRefund, "sub_1")
                for name, value in self.RACES[race].items():
                    setattr(row, name, value)
                other.add(row)
                other.commit()
            return original(sess, customer_id)
        monkeypatch.setattr(admin_mod, "owner_account", change_first)

        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_1/settle")

        assert res.status_code == 409
        assert _ledger(engine).state != "settled"


class TestSettleIsFencedOnTheVersion:
    """R5-5 (guard): claimed and released in between, the row reads as it
    did — same state, token "" before and after (ABA). Only the version,
    bumped by every write, shows that something happened."""

    def test_a_claim_and_release_in_between_stops_the_settle(
        self, engine, clock, monkeypatch
    ):
        import api.admin as admin_mod
        from src.billing.withdrawal import _claim, _release

        uid = _seed(engine)
        _stuck_row(engine, clock)
        set_gateway(StripeLikeGateway())  # no credit note: it stays pending
        original = admin_mod.owner_account

        def claim_and_release(sess, customer_id):
            _row_, token = _claim(uid, "cus_1", "sub_1", START, clock.now)
            _release("sub_1", uid, token)
            return original(sess, customer_id)
        monkeypatch.setattr(admin_mod, "owner_account", claim_and_release)

        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_1/settle")

        assert res.status_code == 409
        entry = _ledger(engine)
        assert (entry.state, entry.claim_token) == ("pending", "")


class TestNoRowForADeletedAccount:
    """Item 6: a withdrawal that lost a race with the deletion leaves no
    refund row behind for an account that no longer exists."""

    def test_a_claim_for_a_deleted_account_is_refused(self, engine):
        from src.billing.withdrawal import AccountGone, settle

        uid = _seed(engine)
        with Session(engine) as sess:
            sess.delete(sess.get(UserInfo, uid))
            sess.commit()
        gw = StripeLikeGateway()
        gw.ended["sub_1"] = CANCEL_AT
        with pytest.raises(AccountGone):
            settle(gw, user_info_id=uid, customer_id="cus_1", subscription_id="sub_1",
                   contract_start=START, requested_at=CANCEL_AT)
        assert _ledger(engine) is None
        assert gw.total_refunded == 0


class TestLeaseTimedAtTheClaim:
    """Item 9: the lease runs from the claim, not from the request's start."""

    def test_the_lease_starts_when_the_row_is_claimed(self, engine, clock):
        from src.billing.withdrawal import LEASE_SECONDS, _claim

        uid = _seed(engine)
        request_started = clock.now
        clock.now += 120  # the cancel and whatever came first took a while
        row, _token = _claim(uid, "cus_1", "sub_1", START, request_started)
        assert row.lease_until == clock.now + LEASE_SECONDS
        assert row.requested_at == request_started


class TestRefundFailingAfterItWasMade:
    """Item 4: Stripe reports a refund failing after the credit note made it
    (``refund.failed``); the money did not go back, so it is owed."""

    def _post(self, event):
        from api.router import app as the_app

        class Gateway(StripeLikeGateway):
            def parse_webhook(self, payload, signature):
                return event
        set_gateway(Gateway())
        return TestClient(the_app).post("/api/billing/webhook", content=b"{}",
                                        headers={"stripe-signature": "x"})

    def _event(self, etype="refund.failed", refund_id="re_1", status="failed"):
        return {"id": "evt_r", "type": etype, "created": 1, "data": {"object": {
            "id": refund_id, "object": "refund", "amount": EXPECTED,
            "status": status, "failure_reason": "expired_or_canceled_card"}}}

    def _done(self, engine):
        uid = _seed(engine)
        set_gateway(StripeLikeGateway())
        assert _as(uid).post("/api/billing/withdraw").status_code == 200
        entry = _ledger(engine)
        assert (entry.state, entry.refund_id) == ("done", "re_1")
        return uid

    @pytest.mark.parametrize("etype", ["refund.failed", "refund.updated",
                                       "charge.refund.updated"])
    def test_it_moves_the_refund_to_owed(self, engine, caplog, etype):
        self._done(engine)
        with caplog.at_level(logging.ERROR):
            res = self._post(self._event(etype))
        assert res.json() == {"received": True, "applied": True}
        entry = _ledger(engine)
        assert (entry.state, entry.refunded, entry.owed) == ("owed", 0, EXPECTED)
        assert "expired_or_canceled_card" in entry.reason
        assert any("OWED REFUND" in r.getMessage() for r in caplog.records)
        listed = _as(_admin(engine)).get("/api/admin/billing/owed-refunds").json()
        assert [e["subscription_id"] for e in listed] == ["sub_1"]

    def test_a_redelivery_changes_nothing(self, engine):
        self._done(engine)
        self._post(self._event())
        res = self._post(self._event())
        assert res.json()["applied"] is False
        assert _ledger(engine).owed == EXPECTED

    def test_a_refund_that_did_not_fail_changes_nothing(self, engine):
        self._done(engine)
        assert self._post(self._event("refund.updated", status="succeeded")).json()[
            "applied"] is False
        assert _ledger(engine).state == "done"

    def test_someone_elses_refund_changes_nothing(self, engine, caplog):
        """A refund that is not ours still logs at ERROR (R6-1 keeps that)."""
        self._done(engine)
        with caplog.at_level(logging.INFO):
            res = self._post(self._event(refund_id="re_other"))
        assert res.json()["applied"] is False
        assert _ledger(engine).state == "done"
        assert any(r.levelno == logging.ERROR and "re_other" in r.getMessage()
                   and "matches no refund of ours" in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.parametrize("etype", ["refund.updated", "charge.refund.updated",
                                       "refund.failed"])
    def test_a_repeat_event_for_a_recorded_failure_is_not_an_error(
        self, engine, caplog, etype
    ):
        """R6-1: Stripe reports one failure in several events. After the first
        records it, the others are "already recorded" at INFO — never the
        no-match ERROR that tells the owner to refund it by hand again — and
        the owed amount does not move."""
        self._done(engine)
        assert self._post(self._event("refund.failed")).json()["applied"] is True
        owed = _ledger(engine).owed

        caplog.clear()
        with caplog.at_level(logging.INFO):
            res = self._post(self._event(etype))

        assert res.status_code == 200
        assert res.json()["applied"] is False
        entry = _ledger(engine)
        assert (entry.state, entry.owed, entry.failed_refund_id) == (
            "owed", owed, "re_1")
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(r.levelno == logging.INFO and "already recorded" in r.getMessage()
                   and "re_1" in r.getMessage() for r in caplog.records)


class TestDeletionRefusedLoudly:
    """Item 3: a deletion refused because of the refund is logged at ERROR."""

    def test_logged_at_error(self, engine, caplog):
        uid = _seed(engine)
        set_gateway(StripeLikeGateway(refund_mode="transient"))
        with caplog.at_level(logging.ERROR):
            assert _as(uid).delete("/api/auth/me").status_code == 502
        assert any("refused" in r.getMessage() and r.levelno == logging.ERROR
                   for r in caplog.records)

    def test_a_customer_deleted_at_stripe_is_owed_not_blocking(self, engine):
        """Nothing can be refunded through a deleted customer: the refund is
        owed, with the reason, and the deletion goes ahead."""
        from src.billing.gateway import CustomerGone

        uid = _seed(engine)
        gw = StripeLikeGateway()

        def gone(*args, **kwargs):
            raise CustomerGone("No such customer: 'cus_1'")
        gw.issue_refund = gone
        set_gateway(gw)

        assert _as(uid).delete("/api/auth/me").status_code == 200
        entry = _ledger(engine)
        assert entry.state == "owed" and "customer" in entry.reason

    def test_a_customer_gone_before_the_amount_is_known_is_still_owed(self, engine):
        """Refused before anything was frozen: owed 0 must not read as done."""
        from src.billing.gateway import CustomerGone

        uid = _seed(engine)
        gw = StripeLikeGateway()

        def gone(*args, **kwargs):
            raise CustomerGone("No such customer: 'cus_1'")
        gw.refund_basis = gone
        set_gateway(gw)

        assert _as(uid).delete("/api/auth/me").status_code == 200
        entry = _ledger(engine)
        assert entry.state == "owed"
        assert "amount unknown" in entry.reason


# ── Review round 5 ───────────────────────────────────────────────────────────

def _webhook(event):
    class Gateway(StripeLikeGateway):
        def parse_webhook(self, payload, signature):
            return event
    set_gateway(Gateway())
    return TestClient(app).post("/api/billing/webhook", content=b"{}",
                                headers={"stripe-signature": "x"})


def _refund_event(refund_id="re_1", amount=100, status="failed"):
    return {"id": "evt_f", "type": "refund.failed", "created": 1, "data": {"object": {
        "id": refund_id, "object": "refund", "amount": amount, "status": status,
        "currency": "eur", "charge": "ch_1", "payment_intent": "pi_1",
        "failure_reason": "expired_or_canceled_card"}}}


class TestReviewRound5Probes:
    """The reviewer's probes, ported as they were written."""

    def test_p1_failed_cancel_in_time_retried_after_deadline_is_still_withdrawal(
        self, engine, clock
    ):
        """Terms: 'it counts if you make it within the 14 days, even if your
        subscription is only cancelled ... after them'."""
        clock.now = LAST_MOMENT - 60
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True)
        set_gateway(gw)
        first = _as(uid).post("/api/billing/withdraw")
        assert first.status_code == 502
        clock.now = CLOSES + 120
        gw.fail_cancel = False
        gw.cancel_at = CLOSES + 120
        second = _as(uid).post("/api/billing/withdraw")
        assert second.status_code == 200, second.text
        assert gw.total_refunded > 0 or second.json()["owed_cents"] > 0

    def test_p1b_cancel_landed_but_answer_lost_then_deadline_passes(self, engine, clock):
        clock.now = LAST_MOMENT - 30
        uid = _seed(engine)
        gw = StripeLikeGateway(cancel_at=LAST_MOMENT - 29)
        real = gw.cancel_subscription

        def lost(subscription_id, customer_id=""):
            real(subscription_id, customer_id)
            raise GatewayError("read timed out")
        gw.cancel_subscription = lost
        set_gateway(gw)
        first = _as(uid).post("/api/billing/withdraw")
        assert first.status_code == 502
        assert gw.ended["sub_1"]  # it IS cancelled at Stripe
        gw.cancel_subscription = real
        clock.now = CLOSES + 60
        second = _as(uid).post("/api/billing/withdraw")
        assert second.status_code == 200, second.text

    def test_p2_failure_after_settled_does_not_reowe_what_was_settled(
        self, engine, clock
    ):
        _seed(engine)
        # 100 went to the card (re_1), 200 owed from the balance share.
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(
                subscription_id="sub_1", customer_id="cus_1", state="owed",
                amount=300, to_refund=100, refunded=100, owed=200, currency="eur",
                reason="paid partly from the customer's balance", invoice_id="in_1",
                credit_note_id="cn_1", refund_id="re_1", created_at=1.0))
            sess.commit()
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_1/settle")
        assert res.status_code == 200
        assert _ledger(engine).state == "settled"
        # Later, the card part of the refund fails.
        assert _webhook(_refund_event(amount=100)).json()["applied"] is True
        row = _ledger(engine)
        assert row.state == "owed"
        # Only the failed 100 is owed now: the 200 was settled by hand already.
        assert row.owed == 100, (row.owed, row.reason)
        assert "200 cents were settled by hand" in row.reason
        assert row.refunded == 0

    def test_p3_failure_after_account_deletion_is_not_silent(self, engine, clock,
                                                              caplog):
        uid = _seed(engine)
        set_gateway(StripeLikeGateway())
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert _ledger(engine) is None  # the done row went with the account
        with caplog.at_level(logging.WARNING):
            res = _webhook(_refund_event(amount=EXPECTED))
        assert res.status_code == 200
        assert res.json()["applied"] is False
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("re_1" in m and "ch_1" in m and "pi_1" in m and str(EXPECTED) in m
                   and "evt_f" in m for m in errors), errors


class TestRefundFailureEvents:
    """R5-2, R5-7: every failed refund is accounted for."""

    def test_a_canceled_refund_is_owed_like_a_failed_one(self, engine):
        uid = _seed(engine)
        set_gateway(StripeLikeGateway())
        assert _as(uid).post("/api/billing/withdraw").status_code == 200

        res = _webhook(_refund_event(amount=EXPECTED, status="canceled"))

        assert res.json()["applied"] is True
        entry = _ledger(engine)
        assert (entry.state, entry.owed, entry.refunded) == ("owed", EXPECTED, 0)


class TestAdminListRound5:
    def test_a_pending_row_with_no_amount_says_so(self, engine, clock):
        """R5-9a: never computed — no owed figure, and a pointer to Stripe."""
        from src.billing.withdrawal import LEASE_SECONDS

        _seed(engine)
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(
                subscription_id="sub_1", customer_id="cus_1", state="pending",
                invoice_id="in_1", requested_at=START + DAY,
                created_at=clock.now - 2 * LEASE_SECONDS))
            sess.commit()
        set_gateway(StripeLikeGateway())

        (entry,) = _as(_admin(engine)).get("/api/admin/billing/owed-refunds").json()

        assert entry["owed_cents"] is None
        assert "amount unknown" in entry["reason"]
        assert "check Stripe" in entry["reason"]


class TestDeletionRecordsTheWithdrawalFirst:
    def test_a_deletion_that_cannot_record_it_cancels_nothing(self, engine, caplog):
        """R5-1 for deletion: the withdrawal is recorded before anything is
        cancelled; if Stripe cannot be read for it, nothing is cancelled and
        nothing deleted (ERROR logged), and the retry goes through."""
        uid = _seed(engine)
        gw = StripeLikeGateway()
        real = gw.refund_basis

        def down(*args, **kwargs):
            raise GatewayError("503 from Stripe")
        gw.refund_basis = down
        set_gateway(gw)

        with caplog.at_level(logging.ERROR):
            res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 502
        assert not gw.calls("cancel_all") and not gw.calls("cancel")
        assert _account_exists(engine, uid)
        assert any(r.levelno == logging.ERROR and "could not be recorded" in
                   r.getMessage() for r in caplog.records)

        gw.refund_basis = real
        assert _as(uid).delete("/api/auth/me").status_code == 200
        assert gw.total_refunded == EXPECTED

    def test_the_deletion_records_before_it_cancels(self, engine):
        uid = _seed(engine)
        gw = StripeLikeGateway()
        seen = []
        gw.on_cancel = lambda: seen.append(_ledger(engine))
        set_gateway(gw)

        assert _as(uid).delete("/api/auth/me").status_code == 200

        assert seen[0] is not None
        assert (seen[0].state, seen[0].invoice_id) == ("pending", "in_1")


class TestEarlierContractsCancelNeverLanded:
    def test_its_subscription_is_cancelled_again_before_the_refund(self, engine, clock):
        """R5-1: an earlier contract's pending withdrawal whose cancel never
        landed is cancelled again when the next withdrawal finishes it."""
        uid = _seed(engine)
        gw = StripeLikeGateway(fail_cancel=True, subscriptions=("sub_1", "sub_2"))
        set_gateway(gw)
        assert _as(uid).post("/api/billing/withdraw").status_code == 502
        with Session(engine) as sess:
            row = sess.exec(select(Subscription).where(
                Subscription.user_info_id == uid)).first()
            row.provider_subscription_id = "sub_2"
            row.contract_subscription_id = "sub_2"
            row.contract_started_at = START + 12 * DAY
            sess.add(row)
            sess.commit()
        clock.now = START + 30 * DAY  # sub_2's window closed too
        gw.fail_cancel = False
        gw.cancel_at = clock.now

        res = _as(uid).post("/api/billing/withdraw")

        assert res.status_code == 200, res.text
        assert ("cancel", "sub_1") in gw.calls("cancel")
        assert gw.total_refunded == _unused_at(clock.now)
        assert _ledger(engine).state == "done"


class TestEveryWriteBumpsTheVersion:
    def test_settling_bumps_it(self, engine):
        """R5-5: the settle fence relies on every write bumping the version,
        the admin's own settle included."""
        _seed(engine)
        with Session(engine) as sess:
            sess.add(SubscriptionRefund(subscription_id="sub_1", customer_id="cus_1",
                                        state="owed", amount=EXPECTED, owed=EXPECTED,
                                        version=3))
            sess.commit()
        res = _as(_admin(engine)).post("/api/admin/billing/owed-refunds/sub_1/settle")
        assert res.status_code == 200
        entry = _ledger(engine)
        assert (entry.state, entry.version) == ("settled", 4)

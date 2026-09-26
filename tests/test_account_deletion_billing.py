"""Deleting an account cancels its paid plan first (issue #429).

Before this, both deletion routes removed the local ``Subscription`` row and
left the subscription running at Stripe — charging someone who no longer had an
account to cancel it from.

Drives the real ``api.router.app`` so the ``AccountDeletionRefused`` handler is
the one production uses. The payment provider is a fake gateway; each test that
must *not* reach it installs one anyway and asserts it stayed untouched.
"""
from __future__ import annotations

import threading

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

    def __init__(self, engine, *, fail=False, event=None, cancelled=("sub_live",)):
        self.engine = engine
        self.fail = fail
        self.event = event or {}
        self.cancelled = list(cancelled)
        self.customer_calls: list[str] = []
        self.subscription_calls: list[str] = []
        self.checkout_calls: list[dict] = []
        self.expired: list[str] = []
        self.rows_at_cancel: list[bool] = []

    def _record(self):
        # The cancel must come before anything is deleted — look from a
        # separate session, as another request would.
        with Session(self.engine) as sess:
            self.rows_at_cancel.append(
                sess.exec(select(Subscription)).first() is not None
                and sess.exec(select(UserInfo)).first() is not None
            )
        if self.fail:
            raise GatewayError("provider down")

    def cancel_all_for_customer(self, customer_id):
        self.customer_calls.append(customer_id)
        self._record()
        return list(self.cancelled)

    def cancel_subscription(self, subscription_id, customer_id=""):
        self.subscription_calls.append(subscription_id)
        self._record()

    def create_checkout_session(self, **kwargs):
        self.checkout_calls.append(kwargs)
        return {"url": "https://pay.test/session", "customer_id": "cus_new",
                "session_id": "cs_new"}

    def expire_checkout_session(self, session_id):
        self.expired.append(session_id)
        if self.fail:
            raise GatewayError("provider down")

    def parse_webhook(self, payload, signature):
        return self.event

    @property
    def touched(self) -> bool:
        return bool(self.customer_calls or self.subscription_calls)


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


_seq = iter(range(1, 10_000))


def _seed(engine, *, status="active", customer="cus_1", sub_id="sub_live",
          admin=False, with_subscription=True) -> int:
    """A user with a trip, a memory, usage and (optionally) a subscription."""
    with Session(engine) as sess:
        local = LocalUser(username=f"u{next(_seq)}@x.io")
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
                provider_customer_id=customer, provider_subscription_id=sub_id,
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
    @pytest.mark.parametrize("status", ["active", "none", "canceled", "past_due"])
    def test_a_customer_is_settled_at_the_provider_whatever_the_cache_says(
        self, engine, status,
    ):
        """The cached status can lag: "none" is what a checkout opened before
        the deletion and paid afterwards looks like, and a customer can hold
        subscriptions the row never recorded. So the provider decides."""
        uid = _seed(engine, status=status)
        gw = FakeGateway(engine)
        set_gateway(gw)

        res = _delete_me(uid)

        assert res.status_code == 200, res.text
        assert gw.customer_calls == ["cus_1"]
        assert gw.subscription_calls == []
        assert gw.rows_at_cancel == [True], "rows were deleted before the cancel"
        assert _everything_gone(engine, uid)

    @pytest.mark.parametrize(
        "status", ["active", "trialing", "past_due", "unpaid", "incomplete", "paused"]
    )
    def test_without_a_customer_the_stored_subscription_is_cancelled(self, engine, status):
        uid = _seed(engine, status=status, customer="")
        gw = FakeGateway(engine)
        set_gateway(gw)

        res = _delete_me(uid)

        assert res.status_code == 200, res.text
        assert gw.subscription_calls == ["sub_live"]
        assert gw.rows_at_cancel == [True]
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
    @pytest.mark.parametrize("row", [
        None,                                        # never opened the plan page
        {"status": "none", "sub_id": ""},            # admin comp
        {"status": "canceled", "sub_id": "sub_old"},  # ended, no customer kept
        {"status": "incomplete_expired", "sub_id": "sub_old"},
    ])
    def test_nothing_at_the_provider_never_touches_the_gateway(
        self, engine, row, gateway_installed,
    ):
        """A free user, a comp, or an ended subscription with no customer on
        record delete fine with or without a gateway."""
        uid = _seed(engine, with_subscription=row is not None, customer="",
                    **(row or {}))
        gw = FakeGateway(engine)
        set_gateway(gw if gateway_installed else None)

        res = _delete_me(uid)

        assert res.status_code == 200, res.text
        assert not gw.touched
        with Session(engine) as sess:
            assert sess.get(UserInfo, uid) is None
            assert sess.exec(select(Subscription)).first() is None

    def test_billing_switched_off_later_does_not_trap_a_finished_customer(self, engine):
        """No gateway can be asked, and the last word is that nothing runs:
        refusing would make the account undeletable for good."""
        uid = _seed(engine, status="canceled")
        set_gateway(None)

        assert _delete_me(uid).status_code == 200
        assert _everything_gone(engine, uid)

    def test_a_retry_after_a_refusal_succeeds(self, engine):
        uid = _seed(engine)
        gw = FakeGateway(engine, fail=True)
        set_gateway(gw)
        assert _delete_me(uid).status_code == 502

        gw.fail = False
        assert _delete_me(uid).status_code == 200
        assert gw.customer_calls == ["cus_1", "cus_1"]
        assert _everything_gone(engine, uid)


# ── Admin DELETE /api/admin/users/{id} ───────────────────────────────────────

class TestAdminDelete:
    def test_a_live_subscription_is_cancelled_first(self, engine):
        uid = _seed(engine)
        gw = FakeGateway(engine)
        set_gateway(gw)

        res = _admin_delete(engine, uid)

        assert res.status_code == 200, res.text
        assert gw.customer_calls == ["cus_1"]
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

def _subscription_event(etype, uid, *, customer="cus_1", sub_id="sub_live",
                        status="canceled", event_id="evt_late") -> dict:
    return {
        "id": event_id, "type": etype, "created": 2000,
        "data": {"object": {
            "id": sub_id, "customer": customer, "status": status,
            "metadata": {"user_info_id": str(uid), "plan": "tier_2"},
            "items": {"data": [{"price": {"id": "price_t2"}}]},
        }},
    }


def _checkout_completed(uid, *, customer="cus_new", sub_id="sub_new") -> dict:
    return {
        "id": "evt_co", "type": "checkout.session.completed", "created": 2000,
        "data": {"object": {
            "id": "cs_1", "mode": "subscription", "customer": customer,
            "subscription": sub_id, "client_reference_id": str(uid),
            "metadata": {"user_info_id": str(uid), "plan": "tier_2"},
        }},
    }


def _post_webhook(gw):
    set_gateway(gw)
    app.dependency_overrides.clear()
    return TestClient(app).post("/api/billing/webhook", content=b"{}",
                                headers={"stripe-signature": "good"})


def _no_subscription_rows(engine) -> bool:
    with Session(engine) as sess:
        return sess.exec(select(Subscription)).first() is None


class TestLateWebhook:
    def test_a_late_cancellation_event_recreates_nothing(self, engine):
        uid = _seed(engine)
        gw = FakeGateway(engine)
        set_gateway(gw)
        assert _delete_me(uid).status_code == 200

        late = FakeGateway(engine, event=_subscription_event(
            "customer.subscription.deleted", uid))
        res = _post_webhook(late)

        assert res.status_code == 200
        assert res.json() == {"received": True, "applied": False}
        assert _no_subscription_rows(engine)
        assert not late.touched  # an ended subscription needs no cancelling
        with Session(engine) as sess:
            assert sess.exec(select(UserInfo)).first() is None

    def test_a_deleted_accounts_id_is_never_given_to_the_next_account(self, engine):
        """The id lives on in Stripe metadata. Were it handed out again, the
        late event above would name a stranger instead of nobody."""
        uid = _seed(engine)
        set_gateway(FakeGateway(engine))
        assert _delete_me(uid).status_code == 200

        with Session(engine) as sess:
            newcomer = UserInfo(email="new@x.io")
            sess.add(newcomer)
            sess.commit()
            sess.refresh(newcomer)
            assert newcomer.id != uid


class TestCheckoutPaidAfterDeletion:
    """The in-flight checkout: subscribe, delete, then pay the open page.

    Deletion expires the customer's open checkouts when it knows the customer,
    but a first purchase has none until it is paid. The completion events then
    name an account that no longer exists and a customer no account has — a
    subscription nobody can ever cancel from the app, so it is cancelled on
    arrival.
    """

    @pytest.mark.parametrize("known_customer", [True, False])
    def test_the_completion_events_cancel_it_and_create_nothing(
        self, engine, known_customer,
    ):
        uid = _seed(engine, with_subscription=False)
        gw = FakeGateway(engine, cancelled=())
        set_gateway(gw)
        if known_customer:
            # Clicking Subscribe records the customer the provider returned.
            res = _as(uid).post("/api/billing/checkout", json={"plan": "tier_2"})
            assert res.status_code == 200, res.text
        else:
            # A first purchase by email: no customer until it is paid.
            with Session(engine) as sess:
                sess.add(Subscription(user_info_id=uid))
                sess.commit()

        assert _delete_me(uid).status_code == 200
        assert gw.customer_calls == (["cus_new"] if known_customer else [])

        for event in (
            _checkout_completed(uid),
            _subscription_event("customer.subscription.created", uid,
                                customer="cus_new", sub_id="sub_new",
                                status="active", event_id="evt_created"),
        ):
            late = FakeGateway(engine, event=event)
            res = _post_webhook(late)
            assert res.status_code == 200, res.text
            assert res.json() == {"received": True, "applied": False}
            assert late.customer_calls == ["cus_new"]

        assert _no_subscription_rows(engine)

    def test_a_failed_cancel_asks_stripe_to_deliver_again(self, engine):
        uid = _seed(engine, with_subscription=False)
        set_gateway(FakeGateway(engine))
        assert _delete_me(uid).status_code == 200

        res = _post_webhook(FakeGateway(engine, fail=True,
                                        event=_checkout_completed(uid)))

        assert res.status_code == 502
        assert _no_subscription_rows(engine)

    def test_a_purchase_by_a_live_account_is_applied_not_cancelled(self, engine):
        uid = _seed(engine, with_subscription=False)
        gw = FakeGateway(engine, event=_checkout_completed(uid))

        res = _post_webhook(gw)

        assert res.json() == {"received": True, "applied": True}
        assert not gw.touched
        with Session(engine) as sess:
            row = sess.exec(select(Subscription)).one()
            assert row.user_info_id == uid
            assert row.provider_customer_id == "cus_new"

    def test_a_customer_another_account_still_holds_is_not_cancelled(self, engine):
        """Only a subscription that belongs to nobody is an orphan."""
        keeper = _seed(engine, customer="cus_new", sub_id="sub_keep")
        gone = _seed(engine, with_subscription=False)
        set_gateway(FakeGateway(engine))
        assert _delete_me(gone).status_code == 200

        gw = FakeGateway(engine, event=_checkout_completed(gone))
        res = _post_webhook(gw)

        assert res.status_code == 200
        assert not gw.touched
        with Session(engine) as sess:
            assert sess.exec(select(Subscription)).one().user_info_id == keeper


# ── A start event racing the deletion ────────────────────────────────────────

@pytest.fixture
def file_engine(monkeypatch, tmp_path):
    """A real file database, one connection per session, WAL and busy_timeout
    as in production — the in-memory StaticPool shares one connection between
    sessions and cannot show two writers contending."""
    test_engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    db_module._configure_sqlite(test_engine)
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(test_engine)
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield test_engine
    app.dependency_overrides.clear()
    set_gateway(None)
    test_engine.dispose()


class TestStartEventRacingTheDeletion:
    """A first purchase has no customer on the row, so deletion settles
    billing without asking Stripe. Its completion event can land while the
    deletion is running (issue #429, review round 2)."""

    def test_landing_before_the_deletes_refuses_and_the_retry_cancels(
        self, file_engine, monkeypatch,
    ):
        """The event lands after deletion read the row and before it deleted
        anything: it records the new customer on the row. Deleting on would
        drop the only record of a subscription nobody cancelled."""
        import src.auth.account_deletion as deletion

        uid = _seed(file_engine, status="none", customer="", sub_id="")
        gw = FakeGateway(file_engine, cancelled=("sub_new",))
        set_gateway(gw)
        webhook = {}
        real = deletion.cancel_live_subscription

        def settle_then_race(sess, user_info_id):
            cancelled = real(sess, user_info_id)
            if not webhook:
                webhook["res"] = _post_webhook(
                    FakeGateway(file_engine, event=_checkout_completed(uid)))
                set_gateway(gw)
            return cancelled

        monkeypatch.setattr(deletion, "cancel_live_subscription", settle_then_race)

        res = _delete_me(uid)

        assert webhook["res"].json() == {"received": True, "applied": True}
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "billing_changed"
        assert _everything_present(file_engine, uid)
        assert gw.customer_calls == []

        # The retry sees the customer and cancels what it started.
        res = _delete_me(uid)
        assert res.status_code == 200, res.text
        assert gw.customer_calls == ["cus_new"]
        assert _everything_gone(file_engine, uid)

    def test_landing_during_the_deletes_waits_and_cancels(self, file_engine):
        """The event lands while the deletion holds the write lock. It must
        wait for the deletion to commit, then see the account gone and
        cancel — not read the account as alive and write to its rows."""
        from sqlalchemy import event as sa_event

        from src.auth.account_deletion import delete_user_and_data

        uid = _seed(file_engine, status="none", customer="", sub_id="")
        late = FakeGateway(file_engine, event=_checkout_completed(uid))
        set_gateway(late)
        webhook = {}

        def post():
            webhook["res"] = TestClient(app).post(
                "/api/billing/webhook", content=b"{}",
                headers={"stripe-signature": "good"})

        racer = threading.Thread(target=post)

        with Session(file_engine) as sess:
            def race(_session):
                if not racer.is_alive() and "started" not in webhook:
                    webhook["started"] = True
                    racer.start()
                    # Long enough for the event to read everything it would
                    # read, were it not waiting for the lock.
                    racer.join(timeout=1.5)

            sa_event.listen(sess, "before_commit", race)
            delete_user_and_data(sess, uid)
        racer.join(timeout=60)

        assert webhook["res"].status_code == 200, webhook["res"].text
        assert webhook["res"].json() == {"received": True, "applied": False}
        assert late.customer_calls == ["cus_new"]
        assert _everything_gone(file_engine, uid)

    def test_landing_after_the_recheck_waits_for_the_deletion(
        self, file_engine, monkeypatch,
    ):
        """The re-check only helps if nothing can write between it and the
        deletes: the deletion holds the lock from before the re-check to its
        commit, so an event arriving just after the re-check waits too."""
        import src.auth.account_deletion as deletion

        uid = _seed(file_engine, status="none", customer="", sub_id="")
        late = FakeGateway(file_engine, event=_checkout_completed(uid))
        set_gateway(late)
        webhook = {}
        calls = []
        real = deletion._billing_state

        def post():
            webhook["res"] = TestClient(app).post(
                "/api/billing/webhook", content=b"{}",
                headers={"stripe-signature": "good"})

        def recheck_then_race(sess, user_info_id):
            state = real(sess, user_info_id)
            calls.append(state)
            if len(calls) == 2:  # the re-check, under the lock
                racer = threading.Thread(target=post)
                webhook["racer"] = racer
                racer.start()
                racer.join(timeout=1.5)
            return state

        monkeypatch.setattr(deletion, "_billing_state", recheck_then_race)

        with Session(file_engine) as sess:
            deletion.delete_user_and_data(sess, uid)
        webhook["racer"].join(timeout=60)

        assert webhook["res"].json() == {"received": True, "applied": False}
        assert late.customer_calls == ["cus_new"]
        assert _everything_gone(file_engine, uid)


# ── Billing switched off while a live status is cached ───────────────────────

def _documented_update() -> str:
    """The UPDATE docs/BILLING.md tells an operator to run."""
    import re
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "BILLING.md").read_text(
        encoding="utf-8")
    found = re.findall(r"^\s*(UPDATE subscription SET status = 'canceled'[^;]*;)",
                       doc, re.MULTILINE)
    assert len(found) == 1, "docs/BILLING.md must document exactly one such UPDATE"
    return found[0]


class TestNoGatewayEscape:
    """With billing switched off, a cached live status refuses deletion for
    good — deliberately, with no force flag. The documented way out is for
    the operator: the user is told to ask them, the operator is pointed at it,
    and that way out works."""

    def test_the_user_is_told_to_ask_the_administrator(self, engine, caplog):
        uid = _seed(engine)
        set_gateway(None)

        with caplog.at_level("WARNING"):
            detail = _delete_me(uid).json()["detail"]

        # A file path in the repository means nothing to someone deleting
        # their own account.
        assert "contact the administrator" in detail
        assert "docs/" not in detail
        # The operator finds the way out in the server log.
        assert any("docs/BILLING.md" in r.getMessage() for r in caplog.records)

    def test_the_admin_is_pointed_at_the_procedure(self, engine):
        uid = _seed(engine)
        set_gateway(None)

        res = _admin_delete(engine, uid)

        assert res.status_code == 409
        assert res.json()["code"] == "billing_unavailable"
        assert "docs/BILLING.md" in res.json()["detail"]
        assert "Deleting an account" in res.json()["detail"]
        # Older clients read `detail` with a regex that stops at the first
        # quote; keep the message readable for them too.
        assert '"' not in res.json()["detail"]
        assert _everything_present(engine, uid)

    def test_the_documented_update_lets_the_deletion_through(self, engine):
        from sqlalchemy import text

        uid = _seed(engine)
        set_gateway(None)
        assert _delete_me(uid).status_code == 409

        # What the operator runs once Stripe shows nothing billing.
        with engine.begin() as conn:
            conn.execute(text(_documented_update().replace("<id>", str(uid))))

        assert _delete_me(uid).status_code == 200
        assert _everything_gone(engine, uid)


# ── The deletion's own cancellation, and a second deletion ──────────────────

def _post_raw_webhook(event) -> "object":
    """Deliver one event, keeping the deletion's gateway installed."""
    from src.billing import gateway as gateway_mod

    deleting = gateway_mod._override
    set_gateway(FakeGateway(None, event=event))
    try:
        return TestClient(app).post("/api/billing/webhook", content=b"{}",
                                    headers={"stripe-signature": "good"})
    finally:
        set_gateway(deleting)


class _CancelThenNotify(FakeGateway):
    """Stripe tells us about the cancellation we just asked for — and the event
    can arrive before the deletion takes the lock."""

    def __init__(self, engine, uid, events):
        super().__init__(engine, cancelled=("sub_live",))
        self.uid = uid
        self.events = events
        self.webhook_results = []

    def cancel_all_for_customer(self, customer_id):
        cancelled = super().cancel_all_for_customer(customer_id)
        for event in self.events:
            self.webhook_results.append(_post_raw_webhook(event))
        return cancelled


class TestOwnCancellationDuringDeletion:
    """Review round 3: the deletion's own cancel produced
    ``customer.subscription.deleted``, the webhook recorded "canceled", and the
    re-check then refused a paying user with 409 "billing changed"."""

    @pytest.mark.parametrize("sub_id, recorded", [
        ("sub_live", True),     # the tracked one: recorded as canceled
        ("sub_second", False),  # another one ending never overwrites the row
    ])
    def test_the_cancellation_event_does_not_stop_the_deletion(
        self, file_engine, sub_id, recorded,
    ):
        uid = _seed(file_engine, status="active")
        gw = _CancelThenNotify(file_engine, uid, [_subscription_event(
            "customer.subscription.deleted", uid, sub_id=sub_id,
            status="canceled")])
        set_gateway(gw)

        res = _delete_me(uid)

        assert [r.json()["applied"] for r in gw.webhook_results] == [recorded]
        assert res.status_code == 200, res.text
        assert _everything_gone(file_engine, uid)

    def test_a_new_subscription_is_not_hidden_by_the_old_ones_cancellation(
        self, file_engine,
    ):
        """Review round 4: sub_new starts, then the tracked sub_live's own
        cancellation arrives. Recording that on the row made it read "the
        tracked subscription ended" and the deletion went on while sub_new
        kept billing."""
        uid = _seed(file_engine, status="active")
        gw = _CancelThenNotify(file_engine, uid, [
            _subscription_event("customer.subscription.created", uid,
                                sub_id="sub_new", status="active",
                                event_id="evt_new"),
            _subscription_event("customer.subscription.deleted", uid,
                                sub_id="sub_live", status="canceled"),
        ])
        set_gateway(gw)

        res = _delete_me(uid)

        assert res.status_code == 409, res.text
        assert res.json()["code"] == "billing_changed"
        assert _everything_present(file_engine, uid)

    def test_a_subscription_that_can_still_bill_still_stops_it(self, file_engine):
        """A new subscription started in the same window must not be deleted
        past: that is what the re-check exists for."""
        uid = _seed(file_engine, status="active")
        gw = _CancelThenNotify(file_engine, uid, [_subscription_event(
            "customer.subscription.created", uid, sub_id="sub_new",
            status="active", event_id="evt_new")])
        set_gateway(gw)

        res = _delete_me(uid)

        assert res.status_code == 409, res.text
        assert res.json()["code"] == "billing_changed"
        assert _everything_present(file_engine, uid)

    def test_a_new_customer_still_stops_it(self, file_engine, monkeypatch):
        """Even an ended subscription: a customer the deletion never settled
        may hold others."""
        import src.auth.account_deletion as deletion

        uid = _seed(file_engine, status="none", customer="", sub_id="")
        gw = _CancelThenNotify(file_engine, uid, [_subscription_event(
            "customer.subscription.deleted", uid, customer="cus_other",
            sub_id="sub_x", status="canceled")])
        set_gateway(gw)
        # No customer on the row, so deletion never calls the gateway: post the
        # event from the pre-lock window directly instead.
        real = deletion.cancel_live_subscription

        def settle_then_notify(sess, user_info_id):
            out = real(sess, user_info_id)
            gw.webhook_results.append(_post_raw_webhook(gw.events[0]))
            return out

        monkeypatch.setattr(deletion, "cancel_live_subscription", settle_then_notify)

        res = _delete_me(uid)

        assert [r.json()["applied"] for r in gw.webhook_results] == [True]
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "billing_changed"


class TestConcurrentDoubleDeletion:
    """Two deletions of the same account at once — a double tap, or the user
    and an admin. The one that loses the lock finds the account gone."""

    def test_the_second_deletion_succeeds_rather_than_reporting_a_change(
        self, file_engine, monkeypatch,
    ):
        import src.auth.account_deletion as deletion

        uid = _seed(file_engine, status="active")
        set_gateway(FakeGateway(file_engine))
        both_settled = threading.Barrier(2, timeout=30)
        real = deletion.cancel_live_subscription

        def settle_together(sess, user_info_id):
            out = real(sess, user_info_id)
            both_settled.wait()  # neither has taken the lock yet
            return out

        monkeypatch.setattr(deletion, "cancel_live_subscription", settle_together)
        errors = []

        def delete():
            try:
                with Session(file_engine) as sess:
                    deletion.delete_user_and_data(sess, uid)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=delete) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert errors == []
        assert _everything_gone(file_engine, uid)


class TestWebhookDoesNotBlockTheEventLoop:
    """The webhook can wait up to busy_timeout for an account's write lock.
    Doing that on the event loop froze every async endpoint meanwhile."""

    def test_other_requests_are_served_while_it_waits(self, file_engine):
        import time

        from fastapi import FastAPI

        from api.billing import router as billing_router
        from src.billing.subscriptions import lock_account

        mini = FastAPI()
        mini.include_router(billing_router)

        @mini.get("/ping")
        async def ping():
            return {"ok": True}

        uid = _seed(file_engine, with_subscription=False)
        set_gateway(FakeGateway(file_engine, event=_checkout_completed(uid)))
        webhook = {}

        with TestClient(mini) as client:  # one event loop for every request
            holder = Session(file_engine)
            lock_account(holder, uid)  # someone else holds the account lock

            def post():
                webhook["res"] = client.post(
                    "/api/billing/webhook", content=b"{}",
                    headers={"stripe-signature": "good"})

            racer = threading.Thread(target=post)
            racer.start()
            time.sleep(0.5)  # let the webhook reach the lock and wait on it
            release = threading.Timer(3.0, holder.rollback)
            release.start()

            started = time.monotonic()
            assert client.get("/ping").json() == {"ok": True}
            waited = time.monotonic() - started

            release.join()
            racer.join(timeout=60)
            holder.close()

        assert waited < 1.5, f"/ping waited {waited:.1f}s behind the webhook"
        # And the webhook itself still verifies and applies once the lock frees.
        assert webhook["res"].json() == {"received": True, "applied": True}


# ── A checkout racing the deletion ───────────────────────────────────────────

class _DeletedDuringCheckout(FakeGateway):
    """The account is deleted while Stripe creates its checkout session —
    fast for a first purchase, which has no customer to settle."""

    def __init__(self, engine, uid, *, fail_expiry=False):
        super().__init__(engine, cancelled=())
        self.uid = uid
        self.fail_expiry = fail_expiry
        self.deletion = None

    def create_checkout_session(self, **kwargs):
        result = super().create_checkout_session(**kwargs)
        self.deletion = _delete_me(self.uid)
        _as(self.uid)  # the checkout request carries on as that user
        return result

    def expire_checkout_session(self, session_id):
        self.expired.append(session_id)
        if self.fail_expiry:
            raise GatewayError("provider down")


class TestCheckoutRacingTheDeletion:
    """Review round 4: create_checkout checked the account, called Stripe, and
    then recorded the new customer on a re-created row for the account the
    deletion had just removed. The payment's webhook then found that row by
    customer and applied the purchase instead of cancelling it."""

    @pytest.mark.parametrize("fail_expiry", [False, True])
    def test_no_billing_state_is_recreated_and_the_page_is_expired(
        self, file_engine, fail_expiry,
    ):
        uid = _seed(file_engine, status="none", customer="", sub_id="")
        gw = _DeletedDuringCheckout(file_engine, uid, fail_expiry=fail_expiry)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/checkout", json={"plan": "tier_2"})

        assert gw.deletion.status_code == 200, gw.deletion.text
        assert res.status_code == 404, res.text
        assert gw.expired == ["cs_new"]
        assert _no_subscription_rows(file_engine)

    def test_paying_that_page_anyway_is_still_cancelled(self, file_engine):
        """If the page could not be expired, the safety net must still see the
        purchase for what it is."""
        uid = _seed(file_engine, status="none", customer="", sub_id="")
        set_gateway(_DeletedDuringCheckout(file_engine, uid, fail_expiry=True))
        assert _as(uid).post("/api/billing/checkout",
                             json={"plan": "tier_2"}).status_code == 404

        late = FakeGateway(file_engine, event=_checkout_completed(uid))
        res = _post_webhook(late)

        assert res.json() == {"received": True, "applied": False}
        assert late.customer_calls == ["cus_new"]
        assert _no_subscription_rows(file_engine)

    def test_a_deletion_still_committing_is_waited_for(self, file_engine):
        """The deletion holds the account lock, not yet committed, while the
        checkout records its customer. Reading the account without the lock,
        the checkout would see it still there and write to rows being
        deleted; with the lock it waits, then sees it gone."""
        from sqlalchemy import event as sa_event

        from src.auth.account_deletion import delete_user_and_data

        uid = _seed(file_engine, status="none", customer="", sub_id="")
        locked, go = threading.Event(), threading.Event()
        deleting = {}

        def delete():
            with Session(file_engine) as sess:
                def hold(_session):
                    locked.set()
                    go.wait(timeout=30)

                sa_event.listen(sess, "before_commit", hold)
                delete_user_and_data(sess, uid)
            deleting["done"] = True

        class _DeletingNow(FakeGateway):
            def create_checkout_session(self, **kwargs):
                result = super().create_checkout_session(**kwargs)
                deleting["thread"] = threading.Thread(target=delete)
                deleting["thread"].start()
                assert locked.wait(timeout=30)  # deletion holds the lock now
                threading.Timer(1.5, go.set).start()
                return result

        gw = _DeletingNow(file_engine)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/checkout", json={"plan": "tier_2"})
        deleting["thread"].join(timeout=60)

        assert deleting.get("done") is True
        assert res.status_code == 404, res.text
        assert gw.expired == ["cs_new"]
        assert _no_subscription_rows(file_engine)

    def test_a_live_account_still_gets_its_customer_recorded(self, file_engine):
        uid = _seed(file_engine, status="none", customer="", sub_id="")
        gw = FakeGateway(file_engine)
        set_gateway(gw)

        res = _as(uid).post("/api/billing/checkout", json={"plan": "tier_2"})

        assert res.status_code == 200, res.text
        assert gw.expired == []
        with Session(file_engine) as sess:
            row = sess.exec(select(Subscription)).one()
            assert (row.user_info_id, row.provider_customer_id) == (uid, "cus_new")

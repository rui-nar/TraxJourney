"""Provider webhook events → subscription state (issue #121).

Two layers, both without a network:

  * :func:`subscription_update_from_event` is pure — fed the payload shapes
    Stripe actually sends, including the 2025 change that moved
    ``current_period_end`` onto subscription items.
  * :func:`apply_update` is fed those results against a real (in-memory) DB, to
    pin down the at-least-once, out-of-order delivery guarantees.
"""
from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from models.billing import Subscription
from models.user import UserInfo
from src.billing.plans import FREE, TIER_1, TIER_2, TIER_3, price_lookup_key
from src.billing.subscriptions import (
    apply_schedule,
    apply_update,
    get_subscription,
    is_orphaned,
    set_admin_override,
)
from src.billing.webhook_events import (
    ScheduleUpdate,
    SubscriptionUpdate,
    plan_for_price,
    plan_for_price_object,
    schedule_update_from_event,
    subscription_update_from_event,
)


@pytest.fixture(autouse=True)
def _configured_prices(monkeypatch):
    """One Stripe price per paid tier, as a configured deployment has."""
    monkeypatch.setenv("STRIPE_PRICE_TIER_1", "price_t1")
    monkeypatch.setenv("STRIPE_PRICE_TIER_2", "price_t2")
    monkeypatch.setenv("STRIPE_PRICE_TIER_3", "price_t3")
    yield


# ── Payload builders (shaped like real Stripe events) ─────────────────────────

def _subscription_event(etype: str, *, event_id="evt_1", created=1000,
                        status="active", period_end=5000, cancel=False,
                        customer="cus_1", sub_id="sub_1", price="price_t2",
                        user_info_id=None, items_period=False) -> dict:
    item: dict = {"price": {"id": price}}
    if items_period:
        # Stripe 2025 API versions carry the period on the item, not the sub.
        item["current_period_end"] = period_end
    obj = {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel,
        "items": {"data": [item]},
    }
    if not items_period:
        obj["current_period_end"] = period_end
    if user_info_id is not None:
        obj["metadata"] = {"user_info_id": str(user_info_id)}
    return {"id": event_id, "type": etype, "created": created,
            "data": {"object": obj}}


def _checkout_event(*, event_id="evt_co", created=900, customer="cus_1",
                    sub_id="sub_1", user_info_id=1, mode="subscription",
                    plan=TIER_2) -> dict:
    metadata = {"user_info_id": str(user_info_id)}
    if plan is not None:
        metadata["plan"] = plan
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "created": created,
        "data": {"object": {
            "id": "cs_1",
            "mode": mode,
            "customer": customer,
            "subscription": sub_id,
            "client_reference_id": str(user_info_id),
            "metadata": metadata,
        }},
    }


# ── Pure mapping ──────────────────────────────────────────────────────────────

class TestEventMapping:
    def test_checkout_completed_grants_the_tier_that_was_bought(self):
        update = subscription_update_from_event(_checkout_event(user_info_id=42))
        assert update.plan == TIER_2
        assert update.status == "active"
        assert update.user_info_id == 42
        assert update.customer_id == "cus_1"
        assert update.subscription_id == "sub_1"

    def test_checkout_without_a_recorded_tier_falls_back_to_the_entry_one(self):
        """A session created before tiers existed, or by an older client: the
        subscription event that follows carries the real price and corrects it."""
        update = subscription_update_from_event(_checkout_event(plan=None))
        assert update.plan == TIER_1

    def test_checkout_with_an_unknown_tier_falls_back_to_the_entry_one(self):
        update = subscription_update_from_event(_checkout_event(plan="tier_9"))
        assert update.plan == TIER_1

    def test_one_off_payment_is_not_a_subscription(self):
        """A ``payment`` mode session would grant a plan that never renews."""
        assert subscription_update_from_event(_checkout_event(mode="payment")) is None

    def test_subscription_updated_carries_status_and_period(self):
        update = subscription_update_from_event(
            _subscription_event("customer.subscription.updated",
                                status="active", period_end=7777)
        )
        assert update.plan == TIER_2
        assert update.status == "active"
        assert update.current_period_end == 7777

    def test_period_end_read_from_items_when_absent_on_the_subscription(self):
        """Stripe moved this field onto items; both shapes must work."""
        update = subscription_update_from_event(
            _subscription_event("customer.subscription.updated",
                                period_end=8888, items_period=True)
        )
        assert update.current_period_end == 8888

    def test_deleted_drops_to_free(self):
        update = subscription_update_from_event(
            _subscription_event("customer.subscription.deleted", status="canceled")
        )
        assert update.plan == FREE
        assert update.status == "canceled"

    def test_cancel_at_period_end_is_carried_through(self):
        update = subscription_update_from_event(
            _subscription_event("customer.subscription.updated", cancel=True)
        )
        assert update.cancel_at_period_end is True

    def test_payment_failed_changes_nothing(self):
        """The subscription event that follows carries the real state; acting on
        the invoice alone would cut access off mid-retry-window."""
        event = {"id": "evt_f", "type": "invoice.payment_failed", "created": 1,
                 "data": {"object": {"customer": "cus_1", "subscription": "sub_1"}}}
        assert subscription_update_from_event(event) is None

    def test_unhandled_event_type_is_ignored(self):
        event = {"id": "evt_x", "type": "customer.created", "created": 1,
                 "data": {"object": {"id": "cus_1"}}}
        assert subscription_update_from_event(event) is None

    def test_expanded_customer_object_is_accepted(self):
        event = _subscription_event("customer.subscription.updated")
        event["data"]["object"]["customer"] = {"id": "cus_expanded"}
        assert subscription_update_from_event(event).customer_id == "cus_expanded"

    def test_empty_payload_does_not_raise(self):
        assert subscription_update_from_event({}) is None

    def test_subscription_without_price_is_free(self):
        event = _subscription_event("customer.subscription.updated")
        event["data"]["object"]["items"] = {"data": []}
        assert subscription_update_from_event(event).plan == FREE

    def test_legacy_plan_id_counts_as_a_price(self):
        """Old payloads carry the price under "plan"; it is still a paid line
        item, so it must not read as free."""
        event = _subscription_event("customer.subscription.updated")
        event["data"]["object"]["items"] = {"data": [{"plan": {"id": "price_t2"}}]}
        assert subscription_update_from_event(event).plan == TIER_2


class TestPlanForPrice:
    def test_each_configured_price_maps_to_its_tier(self):
        assert plan_for_price("price_t1") == TIER_1
        assert plan_for_price("price_t2") == TIER_2
        assert plan_for_price("price_t3") == TIER_3

    def test_no_price_is_free(self):
        assert plan_for_price("") == FREE

    def test_an_unrecognised_price_grants_the_entry_tier(self):
        """They are paying for something, so free is wrong; the top tier would
        turn a config typo into an invisible revenue leak."""
        assert plan_for_price("price_rotated_last_week") == TIER_1

    def test_an_unconfigured_deployment_grants_the_entry_tier(self, monkeypatch):
        for tier in ("TIER_1", "TIER_2", "TIER_3"):
            monkeypatch.delenv(f"STRIPE_PRICE_{tier}", raising=False)
        assert plan_for_price("price_t2") == TIER_1


class TestPlanForPriceObject:
    """Resolution prefers the handles we control over the account-scoped id.

    Order: lookup_key → metadata.plan → configured id → loud TIER_1 (issue #154).
    """

    def test_the_lookup_key_decides(self):
        price = {"id": "price_from_some_other_account",
                 "lookup_key": price_lookup_key(TIER_3)}
        assert plan_for_price_object(price) == TIER_3

    def test_a_price_id_from_another_account_still_resolves(self, monkeypatch):
        """The failure this change exists to remove: a sandbox key paired with
        another account's configured ids. Unwritable before, because the id was
        the only handle."""
        for tier in ("TIER_1", "TIER_2", "TIER_3"):
            monkeypatch.delenv(f"STRIPE_PRICE_{tier}", raising=False)
        price = {"id": "price_unknown_here", "lookup_key": price_lookup_key(TIER_2)}
        assert plan_for_price_object(price) == TIER_2

    def test_metadata_covers_a_renamed_lookup_key(self):
        price = {"id": "price_x", "lookup_key": "renamed_by_hand",
                 "metadata": {"plan": TIER_3}}
        assert plan_for_price_object(price) == TIER_3

    def test_falls_back_to_the_configured_id(self):
        """Prices that predate scripts/stripe_catalog.py carry neither handle."""
        assert plan_for_price_object({"id": "price_t2"}) == TIER_2

    def test_unknown_everything_grants_the_entry_tier(self):
        assert plan_for_price_object({"id": "price_mystery"}) == TIER_1

    def test_no_price_object_is_free(self):
        assert plan_for_price_object({}) == FREE

    def test_a_foreign_lookup_key_does_not_win(self):
        """Someone else's key must not silently map to one of our tiers."""
        price = {"id": "price_t3", "lookup_key": "acme_gold_monthly_usd"}
        assert plan_for_price_object(price) == TIER_3  # falls through to the id

    def test_metadata_naming_a_plan_we_do_not_sell_is_ignored(self):
        price = {"id": "price_t1", "metadata": {"plan": "tier_99"}}
        assert plan_for_price_object(price) == TIER_1


class TestEventUsesTheLookupKey:
    def test_a_subscription_event_resolves_by_lookup_key(self, monkeypatch):
        for tier in ("TIER_1", "TIER_2", "TIER_3"):
            monkeypatch.delenv(f"STRIPE_PRICE_{tier}", raising=False)
        event = _subscription_event("customer.subscription.updated")
        event["data"]["object"]["items"] = {"data": [{
            "price": {"id": "price_from_elsewhere",
                      "lookup_key": price_lookup_key(TIER_3)},
        }]}
        assert subscription_update_from_event(event).plan == TIER_3

    def test_the_legacy_plan_shape_still_works(self):
        """Pre-2018 payloads put the same fields under `plan`."""
        event = _subscription_event("customer.subscription.updated")
        event["data"]["object"]["items"] = {"data": [{
            "plan": {"id": "price_x", "lookup_key": price_lookup_key(TIER_2)},
        }]}
        assert subscription_update_from_event(event).plan == TIER_2


# ── Applying updates to stored state ──────────────────────────────────────────

@pytest.fixture
def sess():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserInfo(id=1, email="a@example.com", display_name="A"))
        session.commit()
        yield session


def _update(**kw) -> SubscriptionUpdate:
    defaults = dict(
        event_id="evt_1", event_at=1000.0, customer_id="cus_1",
        subscription_id="sub_1", plan=TIER_2, status="active",
        current_period_end=5000.0, cancel_at_period_end=False, user_info_id=1,
    )
    defaults.update(kw)
    return SubscriptionUpdate(**defaults)


class TestApplyUpdate:
    def test_creates_the_row_on_first_event(self, sess):
        assert apply_update(sess, _update()) is True
        row = get_subscription(sess, 1)
        assert row.plan == TIER_2
        assert row.status == "active"
        assert row.provider_customer_id == "cus_1"

    def test_replaying_the_same_event_is_a_no_op(self, sess):
        apply_update(sess, _update())
        assert apply_update(sess, _update()) is False

    def test_an_older_event_cannot_undo_a_newer_one(self, sess):
        """Stripe redelivers out of order; a stale 'canceled' must not land on
        top of an already-applied renewal."""
        apply_update(sess, _update(event_id="evt_new", event_at=2000.0))
        applied = apply_update(sess, _update(
            event_id="evt_old", event_at=1000.0, plan=FREE, status="canceled",
        ))
        assert applied is False
        assert get_subscription(sess, 1).plan == TIER_2

    def test_a_newer_event_updates_the_row(self, sess):
        apply_update(sess, _update(event_id="evt_1", event_at=1000.0))
        apply_update(sess, _update(
            event_id="evt_2", event_at=2000.0, plan=FREE, status="canceled",
        ))
        assert get_subscription(sess, 1).plan == FREE

    def test_events_are_matched_by_customer_id_when_our_id_is_absent(self, sess):
        apply_update(sess, _update())  # checkout event carries user_info_id
        applied = apply_update(sess, _update(
            event_id="evt_2", event_at=2000.0, user_info_id=None,
            status="past_due",
        ))
        assert applied is True
        assert get_subscription(sess, 1).status == "past_due"

    def test_an_unattributable_event_is_ignored_not_an_error(self, sess):
        """A 2xx is still owed, or the provider retries forever."""
        assert apply_update(sess, _update(
            user_info_id=None, customer_id="cus_unknown"
        )) is False

    def test_checkout_without_a_period_keeps_the_known_period(self, sess):
        apply_update(sess, _update(event_id="evt_sub", event_at=1000.0,
                                   current_period_end=9999.0))
        apply_update(sess, _update(event_id="evt_co", event_at=1500.0,
                                   current_period_end=0.0))
        assert get_subscription(sess, 1).current_period_end == 9999.0

    def test_a_webhook_does_not_clear_an_admin_comp(self, sess):
        set_admin_override(sess, 1, TIER_2)
        apply_update(sess, _update(plan=FREE, status="canceled"))
        row = get_subscription(sess, 1)
        assert row.admin_override_plan == TIER_2
        assert row.plan == FREE  # provider state is still recorded faithfully


class TestDeletedAccounts:
    """The metadata id can outlive the account it names (issue #429).

    Deleting an account cancels its subscription, so Stripe's
    ``customer.subscription.deleted`` arrives after the account is gone. Ids
    are never reused, so "the account exists" is the whole test.
    """

    def test_an_event_for_a_deleted_account_creates_no_row(self, sess):
        assert apply_update(sess, _update(
            user_info_id=99, plan=FREE, status="canceled",
        )) is False
        assert sess.exec(select(Subscription)).all() == []

    def test_the_buyer_is_matched_whatever_the_clocks_say(self, sess):
        """No comparison with the account's creation time any more: a server
        clock running ahead of Stripe's must not disown a real first purchase."""
        user = sess.get(UserInfo, 1)
        event = _subscription_event("customer.subscription.created",
                                    created=int(user.created_at), user_info_id=1)
        # Stripe's clock an hour behind ours: the subscription looks older
        # than the account that bought it.
        event["data"]["object"]["created"] = int(user.created_at) - 3600
        assert apply_update(sess, subscription_update_from_event(event)) is True
        assert get_subscription(sess, 1).provider_customer_id == "cus_1"


class TestIsOrphaned:
    """A subscription that belongs to no account at all (issue #429)."""

    def test_a_deleted_account_whose_customer_no_one_holds(self, sess):
        assert is_orphaned(sess, _update(user_info_id=99, customer_id="cus_x"))

    def test_a_live_account_is_not(self, sess):
        assert not is_orphaned(sess, _update(user_info_id=1))

    def test_a_customer_another_account_holds_is_not(self, sess):
        apply_update(sess, _update(customer_id="cus_held"))
        assert not is_orphaned(sess, _update(user_info_id=99, customer_id="cus_held"))

    def test_an_event_naming_no_account_is_not(self, sess):
        """Without metadata there is nothing to say whose it was."""
        assert not is_orphaned(sess, _update(user_info_id=None, customer_id="cus_x"))


# ── Scheduled changes (issue #153) ────────────────────────────────────────────

def _schedule_event(etype="subscription_schedule.updated", *, event_id="evt_s",
                    created=1000, customer="cus_1", sub_id="sub_1",
                    phases=None) -> dict:
    """A schedule payload shaped like the one the portal's downgrade creates."""
    if phases is None:
        phases = [
            # Running now on the old price…
            {"start_date": 1000, "end_date": 5000,
             "items": [{"price": "price_t3"}]},
            # …switching to the new one when the paid period ends.
            {"start_date": 5000, "items": [{"price": "price_t1"}]},
        ]
    return {
        "id": event_id, "type": etype, "created": created,
        "data": {"object": {
            "id": "sub_sched_1", "customer": customer,
            "subscription": sub_id, "phases": phases,
        }},
    }


class TestScheduleMapping:
    def test_reads_the_tier_the_account_is_moving_to(self):
        update = schedule_update_from_event(_schedule_event(), now=2000)
        assert update.plan == TIER_1
        assert update.effective_at == 5000
        assert update.customer_id == "cus_1"
        assert update.subscription_id == "sub_1"

    def test_ignores_the_phase_already_running(self):
        """Reading the first phase would announce the tier they already have as
        the one that is coming."""
        update = schedule_update_from_event(_schedule_event(), now=2000)
        assert update.plan != TIER_3

    def test_a_schedule_whose_phases_have_all_started_is_not_pending(self):
        update = schedule_update_from_event(_schedule_event(), now=9000)
        assert update.plan == ""
        assert update.effective_at == 0.0

    def test_the_nearest_future_phase_wins(self):
        update = schedule_update_from_event(_schedule_event(phases=[
            {"start_date": 8000, "items": [{"price": "price_t3"}]},
            {"start_date": 5000, "items": [{"price": "price_t1"}]},
        ]), now=2000)
        assert update.plan == TIER_1

    def test_an_expanded_price_object_resolves_by_lookup_key(self):
        """Same rule as the subscription events: our handles beat account-scoped
        ids, so a payload from an unconfigured account still maps (#154)."""
        update = schedule_update_from_event(_schedule_event(phases=[
            {"start_date": 5000, "items": [
                {"price": {"id": "price_unknown",
                           "lookup_key": price_lookup_key(TIER_2)}}]},
        ]), now=2000)
        assert update.plan == TIER_2

    @pytest.mark.parametrize("etype", [
        "subscription_schedule.released",
        "subscription_schedule.canceled",
        "subscription_schedule.completed",
    ])
    def test_a_schedule_that_will_not_run_clears_the_pending_change(self, etype):
        update = schedule_update_from_event(_schedule_event(etype), now=2000)
        assert update.plan == ""

    def test_other_events_are_not_schedules(self):
        assert schedule_update_from_event(
            _subscription_event("customer.subscription.updated")) is None
        assert schedule_update_from_event(_checkout_event()) is None


def _schedule(**kw) -> ScheduleUpdate:
    defaults = dict(
        event_id="evt_s", event_at=1000.0, customer_id="cus_1",
        subscription_id="sub_1", plan=TIER_1, effective_at=5000.0,
    )
    defaults.update(kw)
    return ScheduleUpdate(**defaults)


class TestApplySchedule:
    def test_records_the_pending_change_without_touching_the_plan(self, sess):
        apply_update(sess, _update(plan=TIER_3))
        assert apply_schedule(sess, _schedule()) is True
        row = get_subscription(sess, 1)
        assert row.pending_plan == TIER_1
        assert row.pending_plan_at == 5000.0
        # The point of "at the end of the period": they keep what they paid for.
        assert row.plan == TIER_3

    def test_an_unattributable_schedule_is_ignored_not_an_error(self, sess):
        assert apply_schedule(sess, _schedule(customer_id="cus_unknown")) is False

    def test_a_redelivery_changes_nothing(self, sess):
        apply_update(sess, _update(plan=TIER_3))
        apply_schedule(sess, _schedule())
        assert apply_schedule(sess, _schedule(event_id="evt_s2")) is False

    def test_a_schedule_for_another_subscription_is_ignored(self, sess):
        apply_update(sess, _update(plan=TIER_3, subscription_id="sub_1"))
        assert apply_schedule(sess, _schedule(subscription_id="sub_other")) is False
        assert get_subscription(sess, 1).pending_plan == ""

    def test_cancelling_the_schedule_clears_what_was_pending(self, sess):
        apply_update(sess, _update(plan=TIER_3))
        apply_schedule(sess, _schedule())
        assert apply_schedule(sess, _schedule(plan="", effective_at=0.0)) is True
        row = get_subscription(sess, 1)
        assert row.pending_plan == ""
        assert row.pending_plan_at == 0.0

    def test_the_change_landing_clears_the_promise(self, sess):
        """The scheduled phase runs and Stripe sends a subscription.updated with
        the new price — nothing is pending any more."""
        apply_update(sess, _update(plan=TIER_3))
        apply_schedule(sess, _schedule())
        apply_update(sess, _update(event_id="evt_2", event_at=6000.0, plan=TIER_1))
        row = get_subscription(sess, 1)
        assert row.plan == TIER_1
        assert row.pending_plan == ""

    def test_buying_a_different_tier_supersedes_the_promise(self, sess):
        """A queued downgrade the account can no longer keep is worse than none."""
        apply_update(sess, _update(plan=TIER_3))
        apply_schedule(sess, _schedule())
        apply_update(sess, _update(event_id="evt_2", event_at=6000.0, plan=TIER_2))
        assert get_subscription(sess, 1).pending_plan == ""

    def test_an_unrelated_update_leaves_the_promise_alone(self, sess):
        """A card retry does not cancel a scheduled downgrade."""
        apply_update(sess, _update(plan=TIER_3))
        apply_schedule(sess, _schedule())
        apply_update(sess, _update(event_id="evt_2", event_at=6000.0,
                                   plan=TIER_3, status="past_due"))
        assert get_subscription(sess, 1).pending_plan == TIER_1


class TestAdminOverride:
    def test_set_and_clear(self, sess):
        set_admin_override(sess, 1, TIER_2)
        assert get_subscription(sess, 1).admin_override_plan == TIER_2
        set_admin_override(sess, 1, "")
        assert get_subscription(sess, 1).admin_override_plan == ""

    def test_creates_a_row_for_a_user_who_never_paid(self, sess):
        assert get_subscription(sess, 1) is None
        set_admin_override(sess, 1, TIER_2)
        assert isinstance(get_subscription(sess, 1), Subscription)

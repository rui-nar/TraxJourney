"""Applying provider events to stored subscription state (issue #121).

Webhooks are at-least-once and unordered: Stripe retries a delivery it did not
get a 2xx for, and a ``checkout.session.completed`` can land after the
``customer.subscription.updated`` it logically precedes. Two guards make that
harmless — the same event id is never applied twice, and an event older than
the one already applied is dropped rather than overwriting newer state.
"""
from __future__ import annotations

import time

from sqlmodel import select

from models.billing import Subscription
from models.user import UserInfo
from src.billing.webhook_events import ScheduleUpdate, SubscriptionUpdate
from src.utils.logging import get_logger

_log = get_logger(__name__)


def get_subscription(sess, user_info_id: int) -> Subscription | None:
    return sess.exec(
        select(Subscription).where(Subscription.user_info_id == user_info_id)
    ).first()


def get_or_create(sess, user_info_id: int) -> Subscription:
    row = get_subscription(sess, user_info_id)
    if row is None:
        row = Subscription(user_info_id=user_info_id)
        sess.add(row)
        sess.commit()
        sess.refresh(row)
    return row


def _by_customer(sess, customer_id: str) -> Subscription | None:
    if not customer_id:
        return None
    return sess.exec(
        select(Subscription).where(Subscription.provider_customer_id == customer_id)
    ).first()


#: Slack between this server's clock and the provider's when comparing an
#: account's creation time with a purchase's (see :func:`_bought_by`). A real
#: buyer's account always predates their checkout session by far more than
#: this; an account that reused a deleted one's id always postdates the old
#: purchase by at least the deletion in between.
_CLOCK_SKEW_SECONDS = 60.0


def _bought_by(sess, update: SubscriptionUpdate) -> bool:
    """True when ``update.user_info_id`` still names the account that bought it.

    The id in the metadata was stamped at checkout, and can outlive the account
    (issue #429): deleting an account cancels its subscription, and the
    provider's ``customer.subscription.deleted`` arrives afterwards. SQLite may
    also hand the deleted account's id to the next one registered, so the id
    existing is not enough — the account must also predate the purchase, or a
    late event would attach the old customer and subscription to a stranger.
    """
    user = sess.get(UserInfo, update.user_info_id)
    if user is None:
        return False
    if (
        update.object_created
        and user.created_at > update.object_created + _CLOCK_SKEW_SECONDS
    ):
        return False
    return True


def resolve_row(sess, update: SubscriptionUpdate) -> Subscription | None:
    """Find the subscription row an event belongs to.

    Prefers our own user id (carried in checkout metadata) and falls back to the
    provider customer id, which is all most subscription events contain. An id
    that no longer names the buyer — the account was deleted, or its id reused —
    is not trusted, and only the customer id is left to go on.
    """
    if update.user_info_id:
        if _bought_by(sess, update):
            row = get_subscription(sess, update.user_info_id)
            if row is None:
                row = Subscription(user_info_id=update.user_info_id)
            return row
        _log.info(
            "Webhook %s: user %s is not the account that bought it — "
            "matching by customer only", update.event_id, update.user_info_id,
        )
    return _by_customer(sess, update.customer_id)


def apply_update(sess, update: SubscriptionUpdate) -> bool:
    """Apply one event's state. Returns True when the row changed.

    Returns False — without an error — when the event is a duplicate, is older
    than what we already applied, or cannot be attributed to any account. All
    three are normal and must still be acknowledged with a 2xx, or Stripe will
    retry them forever.
    """
    row = resolve_row(sess, update)
    if row is None:
        _log.info(
            "Webhook %s: no account for customer %s — ignoring",
            update.event_id, update.customer_id,
        )
        return False

    if update.event_id and row.last_event_id == update.event_id:
        return False  # already applied (Stripe redelivery)
    if update.event_at and row.last_event_at and update.event_at < row.last_event_at:
        return False  # out-of-order redelivery of an older event

    # A pending change that has now happened is no longer pending. Clearing it
    # on *any* plan move, not just the one that was scheduled, is deliberate:
    # buying a different tier outright supersedes whatever was queued, and a
    # promise the account can no longer keep is worse than none.
    if update.plan != row.plan:
        row.pending_plan = ""
        row.pending_plan_at = 0.0

    row.plan = update.plan
    row.status = update.status
    row.provider = "stripe"
    if update.customer_id:
        row.provider_customer_id = update.customer_id
    if update.subscription_id:
        row.provider_subscription_id = update.subscription_id
    # A checkout-completed event carries no period; keep whatever the
    # subscription event gave us rather than zeroing the paid-until date.
    if update.current_period_end:
        row.current_period_end = update.current_period_end
    row.cancel_at_period_end = update.cancel_at_period_end
    row.last_event_id = update.event_id
    row.last_event_at = update.event_at
    row.updated_at = time.time()
    sess.add(row)
    sess.commit()
    return True


def apply_schedule(sess, update: ScheduleUpdate) -> bool:
    """Record a tier change agreed now and applied later. True when it changed.

    Deliberately *not* folded into :func:`apply_update`. Schedule events and
    subscription events are two streams about the same account, arriving
    independently: sharing ``last_event_id`` / ``last_event_at`` would make each
    look like an out-of-order redelivery of the other and drop it. This one has
    no ordering guard of its own — a schedule event always carries the schedule's
    whole current shape, so the latest one to arrive is the right answer and a
    redelivery is a no-op by construction.
    """
    row = _by_customer(sess, update.customer_id)
    if row is None:
        _log.info(
            "Schedule webhook %s: no account for customer %s — ignoring",
            update.event_id, update.customer_id,
        )
        return False

    # A schedule for some *other* subscription of the same customer says nothing
    # about the one we track.
    if (
        update.subscription_id
        and row.provider_subscription_id
        and update.subscription_id != row.provider_subscription_id
    ):
        return False

    if row.pending_plan == update.plan and row.pending_plan_at == update.effective_at:
        return False

    row.pending_plan = update.plan
    row.pending_plan_at = update.effective_at if update.plan else 0.0
    row.updated_at = time.time()
    sess.add(row)
    sess.commit()
    return True


def clear_pending_plan(sess, row: Subscription) -> None:
    """Forget a scheduled change. Called once the plan itself has moved."""
    if not row.pending_plan and not row.pending_plan_at:
        return
    row.pending_plan = ""
    row.pending_plan_at = 0.0
    row.updated_at = time.time()
    sess.add(row)
    sess.commit()


def set_admin_override(sess, user_info_id: int, plan: str) -> Subscription:
    """Grant or clear an operator-granted plan (``""`` clears it)."""
    row = get_or_create(sess, user_info_id)
    row.admin_override_plan = plan
    row.updated_at = time.time()
    sess.add(row)
    sess.commit()
    sess.refresh(row)
    return row

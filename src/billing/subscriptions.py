"""Applying provider events to stored subscription state (issue #121).

Webhooks are at-least-once and unordered: Stripe retries a delivery it did not
get a 2xx for, and a ``checkout.session.completed`` can land after the
``customer.subscription.updated`` it logically precedes. Two guards make that
harmless — the same event id is never applied twice, and an event older than
the one already applied is dropped rather than overwriting newer state.
"""
from __future__ import annotations

import time

from sqlalchemy import update
from sqlmodel import select

from models.billing import Subscription
from models.user import UserInfo
from src.billing.entitlements import subscription_is_live
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


def lock_account(sess, user_info_id: int) -> None:
    """Take the write lock that serialises an account's billing and deletion.

    Account deletion and a webhook naming that account each call this before
    reading what they act on (issue #429). Without it, a start event landing
    between the deletion's read of the subscription row and its deletes saw
    the account still there, recorded the new customer on the row — and the
    deletion then removed the row without cancelling anything.

    A no-op UPDATE of the account row, the same idiom as
    ``repo_core.bump_lock_version``: a write is what takes the lock.
    - SQLite: pysqlite issues ``BEGIN`` only before the first write, so this
      must be the first write *and* come before the reads it protects. It
      takes the database write lock, waiting out ``busy_timeout`` while
      another writer holds it, and every read after it sees the latest
      committed state.
    - Postgres: it takes a row lock that conflicts with the deletion's
      ``DELETE`` of the same row (equivalent to ``SELECT … FOR UPDATE``), and
      READ COMMITTED reads after it see what the other side committed.

    A deleted account matches no row; the statement still waits for the
    deletion that removed it, which is the point.
    """
    sess.execute(
        update(UserInfo)
        .where(UserInfo.id == user_info_id)
        .values(created_at=UserInfo.created_at)
    )


def _account_exists(sess, user_info_id: int) -> bool:
    """True when ``user_info_id`` still names an account.

    The id in the metadata was stamped at checkout and outlives the account
    (issue #429): deleting it cancels the subscription, and the provider's
    ``customer.subscription.deleted`` arrives afterwards. Account ids are never
    reused (``userinfo`` is AUTOINCREMENT), so existing is enough to trust it.
    """
    return sess.get(UserInfo, user_info_id) is not None


def resolve_row(sess, update: SubscriptionUpdate) -> Subscription | None:
    """Find the subscription row an event belongs to.

    Prefers our own user id (carried in checkout metadata) and falls back to the
    provider customer id, which is all most subscription events contain. An id
    whose account has been deleted is not trusted — creating a row for it would
    leave an orphan — and only the customer id is left to go on.
    """
    if update.user_info_id:
        if _account_exists(sess, update.user_info_id):
            row = get_subscription(sess, update.user_info_id)
            if row is None:
                row = Subscription(user_info_id=update.user_info_id)
            return row
        _log.info(
            "Webhook %s: account %s no longer exists — matching by customer only",
            update.event_id, update.user_info_id,
        )
    return _by_customer(sess, update.customer_id)


def is_orphaned(sess, update: SubscriptionUpdate) -> bool:
    """True when the event's subscription belongs to no account at all.

    That is: it names an account (metadata) that has been deleted, and its
    customer matches no remaining one. The case this exists for is a checkout
    page opened before the account was deleted and paid afterwards — nothing
    would ever cancel what it started (issue #429).
    """
    if not update.user_info_id or _account_exists(sess, update.user_info_id):
        return False
    return _by_customer(sess, update.customer_id) is None


def _about_another_subscription(row: Subscription, update: SubscriptionUpdate) -> bool:
    """True when both name a subscription and they are not the same one."""
    return bool(
        update.subscription_id
        and row.provider_subscription_id
        and update.subscription_id != row.provider_subscription_id
    )


def apply_update(sess, update: SubscriptionUpdate) -> bool:
    """Apply one event's state. Returns True when the row changed.

    Returns False — without an error — when the event is a duplicate, is older
    than what we already applied, cannot be attributed to any account, or is
    about another subscription than the one tracked and would not replace it.
    All are normal and must still be acknowledged with a 2xx, or Stripe will
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
    if _about_another_subscription(row, update) and not subscription_is_live(update.status):
        # The row tracks one subscription. Another one ending must not
        # overwrite it: a paying user would lose their plan, and account
        # deletion would read "canceled" while the tracked one still bills
        # (issue #429). Only a live subscription takes over the row.
        _log.info(
            "Webhook %s: %s is %s but %s is the one tracked — ignoring",
            update.event_id, update.subscription_id, update.status,
            row.provider_subscription_id,
        )
        return False

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

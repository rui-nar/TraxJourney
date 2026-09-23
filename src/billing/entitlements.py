"""What a given user is allowed to do (issue #121).

The rule that shapes this module: **billing is off unless it is switched on**.
TraxJourney ships as a public MIT Docker image, and a self-hoster must never meet a
paywall — so with no provider configured, ``billing_enabled()`` is False,
everyone is on the strongest plan, and no limit is ever checked.

Layering:
  * :func:`plan_from_subscription` and :func:`plans.over_quota` are pure — the
    interesting rules are testable without a database.
  * :func:`plan_for` and the ``ensure_*`` helpers do the DB reads and raise
    :class:`~src.exceptions.errors.QuotaExceeded`, which the app maps to 402.
"""
from __future__ import annotations

import os
import time

from sqlmodel import func, select

from models.billing import Subscription, UserUsage
from models.project_db import DBProject
from src.billing.plans import (
    FREE,
    TOP_PLAN,
    Limits,
    UNLIMITED,
    known_plan,
    limits_for,
    over_quota,
    plan_name,
)
from src.billing.trip_days import bounds, project_day_bounds, span_days
from src.exceptions.errors import QuotaExceeded

#: Provider statuses that still grant the paid plan. ``past_due`` is included on
#: purpose: a failed renewal starts a retry window at the provider, and locking
#: the account out on day one of that window loses customers who simply need to
#: update a card. ``canceled`` keeps access until ``current_period_end`` instead.
_LIVE_STATUSES = frozenset({"active", "trialing", "past_due"})


def billing_enabled() -> bool:
    """True only when this deployment sells plans.

    Read from the environment on every call rather than cached at import: tests
    flip it per case, and it costs nothing.
    """
    flag = os.environ.get("BILLING_ENABLED", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # Unset → infer from configuration: a deployment with Stripe keys is selling.
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


def quotas_enforced() -> bool:
    """True when exceeding a limit blocks the request rather than just counting.

    Separate from :func:`billing_enabled` so the hosted deployment can turn
    billing on, watch real usage for a while, and only then start refusing —
    without a release in between, and without locking out accounts that grew
    past the limits before the limits existed.
    """
    if not billing_enabled():
        return False
    flag = os.environ.get("BILLING_ENFORCE_QUOTAS", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def plan_from_subscription(
    plan: str,
    status: str,
    current_period_end: float,
    now: float | None = None,
) -> str:
    """Pure mapping from stored provider state to the plan in force right now.

    A live status grants the plan. A dead one (``canceled``, ``incomplete``,
    ``unpaid``) still grants it until the paid period runs out — the user paid
    for that time. Anything else is ``free``.
    """
    ts = time.time() if now is None else now
    if not plan or plan == FREE:
        return FREE
    if status in _LIVE_STATUSES:
        return plan
    if current_period_end and ts < current_period_end:
        return plan
    return FREE


def subscription_is_live(status: str) -> bool:
    """True while the provider still considers the subscription running.

    Distinct from "the plan is in force": a ``canceled`` subscription inside its
    paid period still grants the plan (see :func:`plan_from_subscription`) but is
    *not* live — nothing will renew it. Callers that need to know "would buying
    again create a second subscription" want this one.
    """
    return status in _LIVE_STATUSES


def plan_for(sess, user_info_id: int, now: float | None = None) -> str:
    """The plan in force for a user: override > subscription > free.

    Returns the top plan for everyone when billing is disabled — a self-hosted
    instance has no tiers, so nothing downstream needs a special case.
    """
    if not billing_enabled():
        return TOP_PLAN
    row = _subscription(sess, user_info_id)
    if row is None:
        return FREE
    if row.admin_override_plan:
        return row.admin_override_plan
    return plan_from_subscription(row.plan, row.status, row.current_period_end, now)


def limits_for_user(sess, user_info_id: int, now: float | None = None) -> Limits:
    """Effective limits for a user — unlimited when billing is disabled."""
    if not billing_enabled():
        return UNLIMITED
    return limits_for(plan_for(sess, user_info_id, now))


def _subscription(sess, user_info_id: int) -> Subscription | None:
    return sess.exec(
        select(Subscription).where(Subscription.user_info_id == user_info_id)
    ).first()


def project_count(sess, user_info_id: int) -> int:
    """Projects the user *owns*. Projects shared with them are the owner's."""
    return int(
        sess.exec(
            select(func.count(DBProject.id)).where(
                DBProject.user_info_id == user_info_id
            )
        ).one()
    )


def storage_used(sess, user_info_id: int) -> int:
    """Bytes currently counted against the user (0 when never accounted)."""
    row = sess.exec(
        select(UserUsage).where(UserUsage.user_info_id == user_info_id)
    ).first()
    return int(row.storage_bytes) if row else 0


def ensure_project_quota(sess, user_info_id: int, now: float | None = None) -> None:
    """Raise :class:`QuotaExceeded` if creating one more project is not allowed."""
    if not quotas_enforced():
        return
    plan = plan_for(sess, user_info_id, now)
    limit = limits_for(plan).max_projects
    used = project_count(sess, user_info_id)
    if over_quota(used, 1, limit):
        raise QuotaExceeded(
            f"Your plan includes {limit} trip{'s' if limit != 1 else ''}. "
            "Upgrade to create more.",
            plan=plan, limit=limit, used=used, needed=used + 1,
            resource="projects",
        )


def ensure_storage_quota(
    sess, user_info_id: int, incoming_bytes: int, now: float | None = None
) -> None:
    """Raise :class:`QuotaExceeded` if storing ``incoming_bytes`` would overflow."""
    if not quotas_enforced():
        return
    plan = plan_for(sess, user_info_id, now)
    limit = limits_for(plan).max_storage_bytes
    used = storage_used(sess, user_info_id)
    if over_quota(used, incoming_bytes, limit):
        raise QuotaExceeded(
            "This upload would exceed your plan's storage. "
            "Upgrade for more space, or delete some photos.",
            plan=plan, limit=limit, used=used, needed=used + incoming_bytes,
            resource="storage",
        )


def trip_days_used(sess, project_id: int, *extra_dates) -> int:
    """How many days the trip would span once ``extra_dates`` are part of it."""
    first, last = project_day_bounds(sess, project_id)
    return span_days(*bounds([first, last, *extra_dates]))


def ensure_trip_days_quota(
    sess,
    project_id: int,
    owner_id: int,
    *extra_dates,
    now: float | None = None,
) -> None:
    """Raise :class:`QuotaExceeded` if dating something would stretch the trip
    past the plan's limit.

    ``extra_dates`` are the dates about to join the trip — a memory's date, an
    imported activity's, a new ``trip_start``. A date that already falls inside
    the trip's span changes nothing and is always allowed; only the ones that
    push the first or last day outwards can fail.

    The *owner's* plan applies, like storage: a companion editing a shared trip
    spends the owner's allowance, not their own.
    """
    if not quotas_enforced():
        return
    plan = plan_for(sess, owner_id, now)
    limit = limits_for(plan).max_trip_days
    if limit is None:
        return
    used = trip_days_used(sess, project_id)
    prospective = trip_days_used(sess, project_id, *extra_dates)
    # Only a change that makes the trip *longer* can fail. An action that leaves
    # the span alone — or shortens it — is always allowed, so a trip that was
    # already too long when the limits arrived stays fully editable instead of
    # freezing solid, and clearing a date is never refused.
    if prospective > limit and prospective > used:
        raise QuotaExceeded(
            f"That would make this trip {prospective} days long, and your plan "
            f"covers {limit}. Upgrade for longer trips.",
            plan=plan, limit=limit, used=used, needed=prospective,
            resource="trip_days",
        )


def plan_display_name(plan: str) -> str:
    """Name to show for a plan id — falls back sanely for an unknown one."""
    return plan_name(plan if known_plan(plan) else FREE)

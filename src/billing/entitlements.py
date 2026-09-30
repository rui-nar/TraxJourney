"""What a given user is allowed to do (issue #121).

The rule that shapes this module: **billing is off unless it is switched on**.
TraxJourney ships as a public AGPL-3.0 Docker image, and a self-hoster must never meet a
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
from datetime import datetime, timezone

from sqlmodel import and_, func, or_, select

from models.billing import Subscription, UserUsage
from models.project_db import DBProject, DBVideoJob
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


def _utc_month_bounds(now: float) -> tuple[float, float]:
    """Epoch seconds of the first instant of ``now``'s UTC month and the next."""
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.timestamp(), end.timestamp()


def videos_this_month(sess, user_info_id: int, now: float | None = None) -> int:
    """Video jobs the user started in the current UTC calendar month (D3).

    Every job counts — pending, running, done, expired — except a ``failed``
    one: the user did not get a video, so it must not cost them one. The month
    is UTC rather than the user's local one so nobody can buy an extra render
    by moving their clock across a timezone. Previews (``kind='preview'``) are
    free and never count (docs/VIDEO_PREVIEW_PLAN.md D3).
    """
    start, end = _utc_month_bounds(time.time() if now is None else now)
    return int(
        sess.exec(
            select(func.count(DBVideoJob.id)).where(
                DBVideoJob.user_info_id == user_info_id,
                DBVideoJob.kind == "video",
                DBVideoJob.status != "failed",
                DBVideoJob.created_at >= start,
                DBVideoJob.created_at < end,
            )
        ).one()
    )


#: The rolling window of the preview rate limit (docs/VIDEO_PREVIEW_PLAN.md D6).
PREVIEW_WINDOW_S = 3600.0
#: How long a pending or running preview keeps counting. Past this it is taken
#: as lost (its worker died, its enqueue vanished) and stops costing the user.
PREVIEW_IN_FLIGHT_S = 900.0


def _counting_preview(user_info_id: int, now: float):
    """The WHERE clause of a preview that counts toward the hourly limit (D6).

    The one definition shared by :func:`previews_in_last_hour` and
    :func:`preview_slot_frees_at`, so the count and the Retry-After can never
    disagree about which rows cost the user a slot.
    """
    return and_(
        DBVideoJob.user_info_id == user_info_id,
        DBVideoJob.kind == "preview",
        DBVideoJob.created_at > now - PREVIEW_WINDOW_S,
        or_(
            DBVideoJob.status == "done",
            and_(DBVideoJob.status == "failed",
                 DBVideoJob.started_at.is_not(None),
                 DBVideoJob.completed_at.is_not(None),
                 DBVideoJob.completed_at
                 <= DBVideoJob.created_at + PREVIEW_IN_FLIGHT_S),
            and_(DBVideoJob.status.in_(("pending", "running")),
                 DBVideoJob.created_at > now - PREVIEW_IN_FLIGHT_S),
        ),
    )


def previews_in_last_hour(sess, user_info_id: int, now: float | None = None) -> int:
    """Preview jobs that count toward the user's hourly rate limit (D6).

    Counted: previews created in the last hour that are ``done``, ``failed``
    after starting and within :data:`PREVIEW_IN_FLIGHT_S` of creation (a
    timed-out or OOM-killed attempt still used a worker), or
    ``pending``/``running`` and younger than :data:`PREVIEW_IN_FLIGHT_S`. A
    preview that failed before it started, or has ``expired``, does not count.
    The window is open at its old end: a row exactly one hour old has left it.

    The in-flight bound on ``failed`` is what keeps a row from coming back: a
    preview orphaned in ``running`` stops counting at 15 minutes, and when the
    hourly sweep later fails it (``started_at`` kept, ``completed_at`` well
    past 15 minutes) it must not start counting again.
    """
    now = time.time() if now is None else now
    return int(
        sess.exec(
            select(func.count(DBVideoJob.id)).where(
                _counting_preview(user_info_id, now))
        ).one()
    )


def preview_slot_frees_at(sess, user_info_id: int, now: float | None = None,
                          limit: int = 10) -> float | None:
    """The earliest instant the preview count drops below ``limit``, or None.

    None when the user is already under ``limit``. Otherwise each counting row
    leaves the count at its own instant — ``created_at +``
    :data:`PREVIEW_IN_FLIGHT_S` while ``pending``/``running``, ``created_at +``
    :data:`PREVIEW_WINDOW_S` once ``done`` or failed — and the count first goes
    below ``limit`` when ``count - limit + 1`` of them have left. The answer is
    a forecast from the rows as they are now: an in-flight preview that
    finishes meanwhile counts for the full hour and pushes the instant later.
    """
    now = time.time() if now is None else now
    rows = sess.exec(
        select(DBVideoJob.status, DBVideoJob.created_at).where(
            _counting_preview(user_info_id, now))
    ).all()
    excess = len(rows) - limit
    if excess < 0:
        return None
    leaves = sorted(
        created + (PREVIEW_IN_FLIGHT_S if status in ("pending", "running")
                   else PREVIEW_WINDOW_S)
        for status, created in rows
    )
    return leaves[excess]


def ensure_video_quota(sess, user_info_id: int, now: float | None = None) -> None:
    """Raise :class:`QuotaExceeded` if starting one more video is not allowed.

    ``user_info_id`` is the *requester*: on a shared trip the companion who asks
    for the render spends their own allowance, not the owner's (D12).
    """
    if not quotas_enforced():
        return
    plan = plan_for(sess, user_info_id, now)
    limit = limits_for(plan).max_videos_per_month
    used = videos_this_month(sess, user_info_id, now)
    if over_quota(used, 1, limit):
        raise QuotaExceeded(
            f"Your plan includes {limit} video{'s' if limit != 1 else ''} per "
            "month. Upgrade to make more.",
            plan=plan, limit=limit, used=used, needed=used + 1,
            resource="videos",
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

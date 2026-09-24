"""Pure translation of provider webhook events into subscription state (#121).

No SDK, no network, no database — a dict in, a :class:`SubscriptionUpdate` (or
``None``) out. That is what makes the interesting parts of billing testable:
every rule about which event grants what, and every defensive read of a payload
shape that Stripe has changed over the years, is exercised from a recorded JSON
fixture in the test suite.

Ordering and replay are the caller's job (see :mod:`api.billing`): Stripe
retries deliveries and does not guarantee order, so each update carries the
event id and timestamp needed to reject a duplicate or an out-of-date event.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from src.billing.plans import FREE, PAID_PLANS, TIER_1, plan_for_lookup_key
from src.utils.logging import get_logger

_log = get_logger(__name__)

#: Events we act on. Anything else is acknowledged and ignored — Stripe sends a
#: lot of them, and a 2xx with no side effect is the correct response.
HANDLED_TYPES = frozenset({
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
})

#: Events describing a change agreed now and applied later — a downgrade waiting
#: for the paid period to end (issue #153). A separate family because they say
#: something different: not "this is the plan", but "this will be the plan".
SCHEDULE_TYPES = frozenset({
    "subscription_schedule.created",
    "subscription_schedule.updated",
    "subscription_schedule.released",
    "subscription_schedule.canceled",
    "subscription_schedule.completed",
})


@dataclass(frozen=True)
class SubscriptionUpdate:
    """The state one event implies. ``user_info_id`` is None when the event
    identifies the account only by provider customer id (most subscription
    events) — the caller then resolves it from the stored customer id."""

    event_id: str
    event_at: float
    customer_id: str
    subscription_id: str
    plan: str
    status: str
    current_period_end: float
    cancel_at_period_end: bool
    user_info_id: int | None = None


@dataclass(frozen=True)
class ScheduleUpdate:
    """A tier change agreed now and applied later.

    ``plan`` is empty when the schedule no longer implies one — it was released,
    cancelled, or has finished running — which clears whatever was pending.
    """

    event_id: str
    event_at: float
    customer_id: str
    subscription_id: str
    plan: str
    effective_at: float


def price_id_for_plan(plan: str) -> str:
    """Configured provider price id for a paid plan ("" when unconfigured)."""
    if plan not in PAID_PLANS:
        return ""
    return os.environ.get(f"STRIPE_PRICE_{plan.upper()}", "").strip()


def plan_for_price(price_id: str) -> str:
    """Map a provider price id to the plan it grants.

    A subscription with no priced line item at all is ``free``. A price we do
    not recognise grants the entry paid tier and logs loudly: the customer is
    paying for *something*, so they must not land on free, but silently handing
    out the top tier would turn a config typo into an invisible revenue leak.
    An operator can always lift them with the admin plan override.
    """
    if not price_id:
        return FREE
    for plan in PAID_PLANS:
        if price_id and price_id == price_id_for_plan(plan):
            return plan
    _log.warning(
        "Unrecognised price id %s — granting %s. Check STRIPE_PRICE_TIER_*.",
        price_id, TIER_1,
    )
    return TIER_1


def plan_for_price_object(price: dict) -> str:
    """Map a price *object* from a webhook payload to the plan it grants.

    Prefers the handles we control over the account-scoped id (issue #154):

    1. ``lookup_key`` — ours, and identical in every Stripe account, so this
       resolves correctly even against a payload from an account whose price
       ids were never configured here.
    2. ``metadata.plan`` — also stamped by scripts/stripe_catalog.py; survives a
       lookup-key rename.
    3. the configured ``STRIPE_PRICE_*`` ids, for prices that predate the
       provisioning script.

    Falling through all three lands on :func:`plan_for_price`'s loud ``TIER_1``
    default, which is deliberately unchanged.

    Reading these off the payload rather than the environment is also what makes
    this module's "no network, no config" claim true of the reverse mapping.
    """
    if not price:
        return FREE
    plan = plan_for_lookup_key(str(price.get("lookup_key") or ""))
    if plan:
        return plan
    meta_plan = str(((price.get("metadata") or {}).get("plan") or "")).strip()
    if meta_plan in PAID_PLANS:
        return meta_plan
    return plan_for_price(str(price.get("id") or ""))


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _period_end(sub: dict) -> float:
    """Read the period end, tolerating both Stripe payload generations.

    ``current_period_end`` sat on the subscription for years and moved onto the
    subscription *items* in the 2025 API versions. Read whichever is present so
    the integration survives an API-version bump.
    """
    top = _as_float(sub.get("current_period_end"))
    if top:
        return top
    items = (sub.get("items") or {}).get("data") or []
    return max((_as_float(i.get("current_period_end")) for i in items), default=0.0)


def _price_id(sub: dict) -> str:
    items = (sub.get("items") or {}).get("data") or []
    for item in items:
        price = item.get("price") or {}
        if price.get("id"):
            return str(price["id"])
        legacy_plan = item.get("plan") or {}
        if legacy_plan.get("id"):
            return str(legacy_plan["id"])
    return ""


def _price_object(sub: dict) -> dict:
    """The first priced line item's price object, ``{}`` when there is none.

    Returns the whole object rather than just its id so the caller can read the
    lookup key and metadata — the handles that are stable across accounts. Falls
    back to the pre-2018 ``plan`` shape, which carried the same fields.
    """
    items = (sub.get("items") or {}).get("data") or []
    for item in items:
        price = item.get("price") or {}
        if price.get("id"):
            return price
        legacy_plan = item.get("plan") or {}
        if legacy_plan.get("id"):
            return legacy_plan
    return {}


def _customer_id(obj: dict) -> str:
    """Stripe sends ``customer`` as an id, or as an expanded object."""
    cust = obj.get("customer")
    if isinstance(cust, dict):
        return str(cust.get("id") or "")
    return str(cust or "")


def _subscription_id(obj: dict) -> str:
    sub = obj.get("subscription")
    if isinstance(sub, dict):
        return str(sub.get("id") or "")
    return str(sub or "")


def _plan_from_metadata(obj: dict) -> str:
    """Tier recorded on the checkout session, defaulting to the entry tier."""
    plan = str(((obj.get("metadata") or {}).get("plan") or "")).strip()
    return plan if plan in PAID_PLANS else TIER_1


def _user_info_id(obj: dict) -> int | None:
    """Our own user id, as attached when the checkout session was created."""
    meta = obj.get("metadata") or {}
    return _as_int(meta.get("user_info_id")) or _as_int(obj.get("client_reference_id"))


def _future_phase(schedule: dict, now: float) -> dict:
    """The phase that has not started yet, ``{}`` when every phase has.

    A schedule Stripe builds for a plan change has two phases: the one running
    now on the old price, and the one that starts when the paid period ends on
    the new one. The *second* is the pending change; reading the first would
    report the tier they already have as "coming soon".
    """
    phases = schedule.get("phases") or []
    upcoming = [p for p in phases if _as_float(p.get("start_date")) > now]
    if not upcoming:
        return {}
    return min(upcoming, key=lambda p: _as_float(p.get("start_date")))


def _phase_price(phase: dict) -> dict:
    """The price object of a phase's first item, ``{}`` when it has none."""
    for item in (phase.get("items") or []):
        price = item.get("price")
        # A phase item's price is an id unless the payload expanded it.
        if isinstance(price, dict) and price.get("id"):
            return price
        if price:
            return {"id": str(price)}
    return {}


def schedule_update_from_event(
    event: dict, now: float | None = None
) -> ScheduleUpdate | None:
    """Translate a subscription-schedule event. ``None`` for events we ignore.

    Pure, like :func:`subscription_update_from_event`: ``now`` is a parameter so
    "which phase is still in the future" is testable against a fixed payload.
    """
    etype = event.get("type") or ""
    if etype not in SCHEDULE_TYPES:
        return None

    obj = ((event.get("data") or {}).get("object")) or {}
    ts = time.time() if now is None else now

    # A schedule that is no longer going to run implies nothing pending. Say so
    # explicitly rather than leaving a stale promise on the account.
    ended = etype in (
        "subscription_schedule.released",
        "subscription_schedule.canceled",
        "subscription_schedule.completed",
    )
    phase = {} if ended else _future_phase(obj, ts)
    plan = plan_for_price_object(_phase_price(phase)) if phase else ""
    # plan_for_price_object maps "no price" to free; an empty phase means "no
    # pending change at all", which is a different statement.
    if phase and not _phase_price(phase):
        plan = ""

    return ScheduleUpdate(
        event_id=str(event.get("id") or ""),
        event_at=_as_float(event.get("created")),
        customer_id=_customer_id(obj),
        subscription_id=_subscription_id(obj),
        plan=plan,
        effective_at=_as_float(phase.get("start_date")) if phase else 0.0,
    )


def subscription_update_from_event(event: dict) -> SubscriptionUpdate | None:
    """Translate one provider event. Returns ``None`` for events we ignore."""
    etype = event.get("type") or ""
    if etype not in HANDLED_TYPES:
        return None

    obj = ((event.get("data") or {}).get("object")) or {}
    event_id = str(event.get("id") or "")
    event_at = _as_float(event.get("created"))

    if etype == "checkout.session.completed":
        # A completed one-off payment is not a subscription — ignore it rather
        # than granting a plan that will never renew or cancel.
        if (obj.get("mode") or "subscription") != "subscription":
            return None
        return SubscriptionUpdate(
            event_id=event_id,
            event_at=event_at,
            customer_id=_customer_id(obj),
            subscription_id=_subscription_id(obj),
            # Which tier was bought is in the metadata we attached when the
            # session was created; the subscription event that follows carries
            # the authoritative price and corrects this if it ever disagrees.
            plan=_plan_from_metadata(obj),
            # The subscription object itself arrives in its own event moments
            # later with the authoritative status and period; "active" here just
            # unlocks the account immediately rather than making the user wait.
            status="active",
            current_period_end=0.0,
            cancel_at_period_end=False,
            user_info_id=_user_info_id(obj),
        )

    if etype == "invoice.payment_failed":
        # Do not downgrade here: Stripe moves the subscription to past_due /
        # unpaid and sends a subscription.updated event with the real state.
        # Acting on the invoice alone would cut access off mid-retry-window.
        return None

    if etype == "customer.subscription.deleted":
        return SubscriptionUpdate(
            event_id=event_id,
            event_at=event_at,
            customer_id=_customer_id(obj),
            subscription_id=str(obj.get("id") or ""),
            plan=FREE,
            status="canceled",
            current_period_end=_period_end(obj),
            cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
            user_info_id=_user_info_id(obj),
        )

    # customer.subscription.created / updated
    return SubscriptionUpdate(
        event_id=event_id,
        event_at=event_at,
        customer_id=_customer_id(obj),
        subscription_id=str(obj.get("id") or ""),
        plan=plan_for_price_object(_price_object(obj)),
        status=str(obj.get("status") or ""),
        current_period_end=_period_end(obj),
        cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
        user_info_id=_user_info_id(obj),
    )

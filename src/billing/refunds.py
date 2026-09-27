"""Withdrawal window and pro-rata refund arithmetic (issue #441). Pure.

The policy the owner set on 2026-09-23 and refined on 2026-09-26 (not legal
advice; see docs/BILLING.md):

* Checkout collects the buyer's express consent to start the service at once.
* Each **contract** — a subscription started while no other subscription of the
  account was running — can be withdrawn from until the end of the 14th day
  after the day it started (UTC), and the unused part of the current period is
  refunded pro rata. Renewals and plan changes do not start a new contract.
* After that, cancelling only stops the renewal. No refund.
* Deleting the account cancels at once; inside the window it also refunds.

Every instant here is a unix timestamp in seconds (float), the same unit the
``Subscription`` model and Stripe's payloads use. The only calendar arithmetic
is the deadline's, and it is done in UTC.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone

#: Whole calendar days after the day the contract started, in UTC.
WITHDRAWAL_WINDOW_DAYS = 14

#: Which wording of the withdrawal terms a buyer consented to at checkout.
#: Stamped into the checkout session's metadata and stored with the consent, so
#: the proof says what was shown. Bump it whenever the checkout text changes.
WITHDRAWAL_TERMS_VERSION = "2026-09-26.2"


def withdrawal_window_closes_at(contract_start: float) -> float:
    """The first instant the window is closed, or 0 when no start is on record.

    The window runs to the end of the 14th day after the day the contract
    started, in UTC — 23:59:59.999… on (start date + 14 days) — so this is
    midnight UTC at the start of the day after that.
    """
    if not contract_start or contract_start <= 0:
        return 0.0
    started = datetime.fromtimestamp(contract_start, tz=timezone.utc).date()
    first_closed_day = started + timedelta(days=WITHDRAWAL_WINDOW_DAYS + 1)
    return datetime.combine(first_closed_day, time.min, tzinfo=timezone.utc).timestamp()


def withdrawal_window_open(contract_start: float, at: float) -> bool:
    """True while a withdrawal made at ``at`` is still refunded.

    No start on record (0) means no window. So does 1.0, which the migrations
    write for subscriptions that were already running when this shipped.
    """
    closes_at = withdrawal_window_closes_at(contract_start)
    return bool(closes_at) and at < closes_at


def prorated_refund_amount(
    period_start: float, period_end: float, amount_paid_cents: int, now: float
) -> int:
    """The unused part of ``amount_paid_cents`` at ``now``, in whole cents.

    Rounded down, so the refund never exceeds the unused share by a fraction of
    a cent, and clamped to ``[0, amount_paid_cents]``: ``now`` before the period
    refunds everything, after it nothing. A free period (a trial, a coupon, a
    100%-off promotion code) refunds nothing.
    """
    amount = int(amount_paid_cents or 0)
    if amount <= 0 or period_end <= period_start:
        return 0
    unused = amount * (period_end - now) / (period_end - period_start)
    return max(0, min(amount, math.floor(unused)))

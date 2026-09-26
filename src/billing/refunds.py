"""Withdrawal window and pro-rata refund arithmetic (issue #441). Pure.

The policy the owner set on 2026-09-23 (not legal advice; see docs/BILLING.md):

* Checkout collects the buyer's express consent to start the service at once.
* Withdrawing within 14 days of the **initial** purchase — the start of the
  account's first paid subscription, not a renewal or a plan change — refunds
  the unused part of the current period, pro rata.
* After that, cancelling only stops the renewal. No refund.
* Deleting the account cancels at once; inside the window it also refunds.

Every instant here is a unix timestamp in seconds (float), the same unit the
``Subscription`` model and Stripe's payloads use, so nothing is ever converted
between timezones.
"""
from __future__ import annotations

import math

#: How long after the initial purchase a withdrawal is refunded.
WITHDRAWAL_WINDOW_SECONDS = 14 * 24 * 60 * 60

#: Which wording of the withdrawal terms a buyer consented to at checkout.
#: Stamped into the checkout session's metadata and stored with the consent, so
#: the proof says what was shown. Bump it whenever the checkout text changes.
WITHDRAWAL_TERMS_VERSION = "2026-09-26"


def withdrawal_window_closes_at(initial_start: float) -> float:
    """When the window closes, or 0 when there is no initial purchase on record."""
    if not initial_start or initial_start <= 0:
        return 0.0
    return initial_start + WITHDRAWAL_WINDOW_SECONDS


def withdrawal_window_open(initial_start: float, now: float) -> bool:
    """True while a withdrawal is still refunded.

    Open while ``initial_start + 14 days > now``: at exactly 14 days it is
    closed. No initial purchase on record (0) means no window — that is how an
    account that subscribed before the purchase date was tracked is treated.
    """
    closes_at = withdrawal_window_closes_at(initial_start)
    return bool(closes_at) and closes_at > now


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

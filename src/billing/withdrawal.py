"""Refunding the unused part of a cancelled subscription (issue #441).

Shared by ``POST /api/billing/withdraw`` and account deletion, so the two can
never refund the same subscription twice: both derive the same idempotency key
from the subscription id, and the amount from the same facts.

**Cancel first, then refund.** A refund is only ever made for a subscription
that has already ended at the provider:

* there is never a refund without a cancellation — the failure the other order
  leaves behind (refunded, still billing) is the one that costs money;
* the amount is measured at the subscription's ``ended_at``, an instant the
  provider has recorded, not at "now". A retry therefore computes the same
  amount as the first attempt, which is what lets it reuse the idempotency key
  (the provider rejects a key reused with different parameters);
* a failure between the two leaves a cancelled subscription and no refund.
  Cancelling again is a no-op and the refund is simply attempted again.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.billing.gateway import BillingGateway, GatewayError
from src.billing.refunds import prorated_refund_amount


@dataclass(frozen=True)
class Refund:
    amount_cents: int
    currency: str


def refund_idempotency_key(subscription_id: str) -> str:
    """The one key a subscription's unused-period refund is ever made under.

    Derived from the subscription and the purpose alone — not from which route
    asked — because a subscription has one unused period to refund, whether
    the user withdrew, deleted the account, or did both.
    """
    return f"traxjourney-unused-period-refund-{subscription_id}"


def refund_unused_period(gateway: BillingGateway, subscription_id: str) -> Refund:
    """Refund what is left of an *ended* subscription's paid period.

    Raises :class:`GatewayError` if the subscription is still running (the
    caller cancels first), or if the provider fails. Refunds nothing, without
    calling the provider, when nothing is left: a free period, or one used up.
    """
    basis = gateway.refund_basis(subscription_id)
    if not basis.ended_at:
        raise GatewayError(
            f"Subscription {subscription_id} is still running — cancel it first"
        )
    amount = prorated_refund_amount(
        basis.period_start, basis.period_end, basis.amount_paid, basis.ended_at
    )
    if amount <= 0:
        return Refund(0, basis.currency)
    refunded = gateway.refund_unused(
        subscription_id, amount, refund_idempotency_key(subscription_id)
    )
    return Refund(refunded, basis.currency)


def refund_quote(gateway: BillingGateway, subscription_id: str, now: float) -> Refund:
    """What withdrawing at ``now`` would refund. Nothing is changed.

    An estimate for the confirmation dialog: the refund itself is measured at
    the moment the cancellation lands, a few seconds later.
    """
    basis = gateway.refund_basis(subscription_id)
    at = basis.ended_at or now
    return Refund(
        prorated_refund_amount(
            basis.period_start, basis.period_end, basis.amount_paid, at
        ),
        basis.currency,
    )

"""Refunding the unused part of a cancelled subscription (issue #441).

Shared by ``POST /api/billing/withdraw`` and account deletion, and driven by a
ledger — one ``SubscriptionRefund`` row per subscription — so the two can never
refund the same subscription twice:

1. **Claim** the row under the account's lock (``lock_account``). A row another
   request is working on (a live lease) refuses with :class:`RefundInProgress`;
   a ``done`` one answers at once, without Stripe.
2. **Freeze** the amount and the invoice at the first computation, from Stripe:
   the unused fraction of the contract's paid invoice, measured at the
   subscription's ``ended_at``. A retry reuses both, so it can reuse the
   provider idempotency key and can never reach a later invoice.
3. **Refund** through the gateway, outside the lock, under the key
   ``refund_key:attempt``. ``attempt`` goes up only after a definite refusal.
4. **Record** the result under the lock: ``done``; ``failed_permanent`` when
   the refund is owed but Stripe refused for good, or there is nothing to
   refund it against; or back to ``pending`` (lease released) after a transient
   failure, for the caller to retry.

The row is created only once the subscription's cancellation has landed. That
is what "asked in time" means: a refund that fails after a successful
cancellation can be completed after the deadline, and nothing else can.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlmodel import Session

from models.billing import SubscriptionRefund
from models.db import get_session
from src.billing.gateway import BillingGateway, GatewayError, PermanentGatewayError
from src.billing.refunds import prorated_refund_amount, withdrawal_window_open
from src.billing.subscriptions import lock_account
from src.utils.logging import get_logger

_log = get_logger(__name__)

PENDING = "pending"
DONE = "done"
FAILED_PERMANENT = "failed_permanent"

#: How long a claim keeps others off a row. Longer than any Stripe round trip;
#: a request that died holding it only delays the retry by this much.
LEASE_SECONDS = 300.0


class RefundInProgress(Exception):
    """Another request is refunding this subscription right now."""


class NotEligible(Exception):
    """The subscription has not ended, or not inside its withdrawal window."""


@dataclass(frozen=True)
class Refund:
    amount_cents: int
    currency: str
    #: Owed but not refunded automatically: recorded for the owner to settle.
    owed_cents: int = 0

    @property
    def settled(self) -> bool:
        return self.owed_cents == 0


def refund_key(subscription_id: str) -> str:
    """The key a subscription's unused-period refund is recorded under.

    Derived from the subscription and the purpose alone, not from which route
    asked: a subscription has one unused period to refund, whether the user
    withdrew, deleted the account, or did both.
    """
    return f"traxjourney-unused-period-refund-{subscription_id}"


def ledger_state(sess: Session, subscription_id: str) -> str:
    """``pending`` / ``done`` / ``failed_permanent``, or "" with no row."""
    if not subscription_id:
        return ""
    row = sess.get(SubscriptionRefund, subscription_id)
    return row.state if row is not None else ""


def _outcome(row: SubscriptionRefund) -> Refund:
    owed = max(0, row.amount - row.refunded) if row.state == FAILED_PERMANENT else 0
    return Refund(row.refunded, row.currency, owed)


def _claim(user_info_id: int, customer_id: str, subscription_id: str,
           requested_at: float, now: float) -> SubscriptionRefund:
    """Take the row (creating it) for this request, under the account's lock."""
    with get_session() as sess:
        lock_account(sess, user_info_id)
        row = sess.get(SubscriptionRefund, subscription_id)
        if row is None:
            row = SubscriptionRefund(subscription_id=subscription_id,
                                     customer_id=customer_id,
                                     requested_at=requested_at, created_at=now)
        elif row.state == DONE:
            sess.expunge(row)  # before the rollback expires it
            sess.rollback()
            return row
        elif row.lease_until > now:
            sess.rollback()
            raise RefundInProgress(subscription_id)
        row.lease_until = now + LEASE_SECONDS
        row.updated_at = now
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        sess.expunge(row)
        return row


def _save(subscription_id: str, user_info_id: int, **fields) -> SubscriptionRefund:
    with get_session() as sess:
        lock_account(sess, user_info_id)
        row = sess.get(SubscriptionRefund, subscription_id)
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_at = time.time()
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        sess.expunge(row)
        return row


def _forget(subscription_id: str, user_info_id: int) -> None:
    """Drop a row claimed for nothing: nothing was frozen, nothing is owed."""
    with get_session() as sess:
        lock_account(sess, user_info_id)
        row = sess.get(SubscriptionRefund, subscription_id)
        if row is not None and row.amount < 0 and row.state == PENDING:
            sess.delete(row)
            sess.commit()
        else:
            sess.rollback()


def settle(gateway: BillingGateway, *, user_info_id: int, customer_id: str,
           subscription_id: str, contract_start: float, requested_at: float,
           now: float) -> Refund:
    """Refund what is left of the *cancelled* contract subscription, once.

    Call only after the cancellation succeeded. Raises
    :class:`RefundInProgress`, :class:`NotEligible`, or :class:`GatewayError`
    for a transient failure (the row is left ``pending`` to retry). A
    permanent failure does not raise: the refund is recorded as owed, logged
    at ERROR, and returned with ``owed_cents``.
    """
    row = _claim(user_info_id, customer_id, subscription_id, requested_at, now)
    if row.state == DONE:
        return _outcome(row)
    try:
        if row.amount < 0:
            basis = gateway.refund_basis(subscription_id)
            # Late completion needs the subscription ended at Stripe, inside
            # the window or after the request was made.
            if not basis.ended_at or not (
                withdrawal_window_open(contract_start, basis.ended_at)
                or basis.ended_at >= row.requested_at
            ):
                raise NotEligible(subscription_id)
            owed = prorated_refund_amount(
                basis.period_start, basis.period_end, basis.amount_paid, basis.ended_at)
            unpayable = 0
            if basis.amount_paid <= 0 and basis.total > 0:
                # Paid from the customer's balance: nothing on a card to refund.
                unpayable = prorated_refund_amount(
                    basis.period_start, basis.period_end, basis.total, basis.ended_at)
            row = _save(subscription_id, user_info_id, amount=owed + unpayable,
                        invoice_id=basis.invoice_id, currency=basis.currency)
            if unpayable:
                return _owed(row, user_info_id, 0, "paid from the customer's balance")
        if row.amount == 0:
            _save(subscription_id, user_info_id, state=DONE, lease_until=0.0)
            return Refund(0, row.currency)
        gateway.discard_pending_items(customer_id, subscription_id)
        result = gateway.refund_unused(
            subscription_id, row.amount, refund_key(subscription_id),
            invoice_id=row.invoice_id, attempt=row.attempt)
    except PermanentGatewayError as exc:
        return _owed(row, user_info_id, row.refunded, str(exc)[:500], bump=True)
    except NotEligible:
        _forget(subscription_id, user_info_id)
        raise
    except GatewayError:
        # Transient: release the claim, keep the row and its attempt, so a
        # retry reuses the same provider key.
        _save(subscription_id, user_info_id, lease_until=0.0)
        raise
    if result.unrefunded_cents:
        row = _save(subscription_id, user_info_id, refunded=result.refunded_cents,
                    credit_note_id=result.credit_note_id)
        return _owed(row, user_info_id, result.refunded_cents, result.reason)
    row = _save(subscription_id, user_info_id, state=DONE, lease_until=0.0,
                refunded=result.refunded_cents, credit_note_id=result.credit_note_id,
                reason="")
    return _outcome(row)


def _owed(row: SubscriptionRefund, user_info_id: int, refunded: int, reason: str,
          *, bump: bool = False) -> Refund:
    """Record the refund as owed — to be settled by hand — and say so loudly."""
    row = _save(row.subscription_id, user_info_id, state=FAILED_PERMANENT,
                lease_until=0.0, refunded=refunded, reason=reason,
                attempt=row.attempt + (1 if bump else 0))
    _log.error(
        "OWED REFUND: %s cents (%s) of subscription %s, customer %s, could not "
        "be refunded automatically: %s. Refund it in the Stripe dashboard, then "
        "mark it settled (docs/BILLING.md, \"Owed refunds\").",
        row.amount - row.refunded, row.currency, row.subscription_id,
        row.customer_id, reason,
    )
    return _outcome(row)


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

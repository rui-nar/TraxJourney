"""Refunding the unused part of a cancelled subscription (issue #441).

Shared by ``POST /api/billing/withdraw`` and account deletion, and driven by a
ledger — one ``SubscriptionRefund`` row per subscription — so that nothing is
ever refunded twice:

    pending ──> done
       └────> owed ──(owner)──> settled

1. **Claim** the row under the account's lock (``lock_account``), with a lease
   and a fencing token. A live lease refuses with :class:`RefundInProgress`. A
   ``done``, ``owed`` or ``settled`` row is final: it is answered from the
   ledger, and Stripe is not asked again — an ``owed`` refund is the owner's to
   settle, never the app's to retry.
2. **Freeze**, at the first computation: the amount (the unused fraction of
   the contract's invoice *total*, at the subscription's ``ended_at``), the
   invoice, and how it splits — what goes back to the card now, and what is
   owed by hand. A retry sends exactly the frozen amount, so a replay under the
   same provider key carries the same parameters.
3. **Refund** outside the lock: the subscription's pending invoice items are
   removed, then a credit note is issued under ``refund_key:attempt``.
   ``attempt`` goes up only when Stripe saw the key with other parameters.
4. **Record** ``done``, or ``owed`` (logged at ERROR), under the lock — only
   while this request still holds the claim.

The row is created by :func:`request_withdrawal` *before* the subscription is
cancelled — the request is the withdrawal — with ``requested_at`` and the
contract's invoice frozen. A cancellation or refund that then fails can be
completed later, even after the deadline: the retry cancels again and refunds
the frozen invoice. A subscription that renewed meanwhile is never refunded
automatically (see :func:`settle`); a request made outside the window leaves
no row.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlmodel import Session, select

from models.billing import Subscription, SubscriptionRefund
from models.db import get_session
from models.user import UserInfo
from src.billing.gateway import (
    BillingGateway,
    GatewayError,
    IdempotencyConflict,
    IssuedRefund,
    PermanentGatewayError,
    RefundPlan,
)
from src.billing.refunds import prorated_refund_amount, withdrawal_window_open
from src.billing.subscriptions import lock_account
from src.utils.logging import get_logger

_log = get_logger(__name__)

PENDING = "pending"
DONE = "done"
OWED = "owed"
SETTLED = "settled"
#: States the app never sends to Stripe again.
FINAL = frozenset({DONE, OWED, SETTLED})

#: How long a claim keeps others off a row. It must outlast the slowest run of
#: :func:`settle`: about 14 SDK calls (basis 2, pending items up to 4, plan 4,
#: issue 2, plus 2 more on an idempotency retry), each at most
#: (MAX_NETWORK_RETRIES + 1) x HTTP_TIMEOUT_SECONDS = 3 x 15 = 45 s in
#: ``stripe_gateway`` — 630 s. Fifteen minutes leaves margin; a request that
#: died holding a claim delays the retry by at most this.
LEASE_SECONDS = 15 * 60.0

#: How long after the latest cancel attempt (``cancel_attempted_at``) Stripe
#: may record the end and the refund still be made automatically. The cancel
#: is one SDK call — at most 45 s — so this leaves ample slack. An end
#: recorded later than this is recorded as owed, for the owner to check.
#: Measured from the latest attempt, not the request, because a retry days
#: after a failed cancel (R5-1) is still the same, in-time withdrawal.
CANCEL_BOUND_SECONDS = 10 * 60.0


class RefundInProgress(Exception):
    """Another request is refunding this subscription right now."""


class LostClaim(RefundInProgress):
    """This request's claim expired and another took it: its result is not
    written. The holder of the current claim finishes, and finds any credit
    note this one made by its refund key."""


class AccountGone(Exception):
    """The account was deleted meanwhile: no refund row is created for it."""


class NotEligible(Exception):
    """The withdrawal was asked for outside its window: nothing to refund."""


@dataclass(frozen=True)
class Refund:
    amount_cents: int
    currency: str
    #: Owed but not refunded automatically: recorded for the owner to settle.
    owed_cents: int = 0


def refund_key(subscription_id: str) -> str:
    """The key a subscription's unused-period refund is recorded under.

    Derived from the subscription and the purpose alone, not from which route
    asked: a subscription has one unused period to refund, whether the user
    withdrew, deleted the account, or did both.
    """
    return f"traxjourney-unused-period-refund-{subscription_id}"


def ledger_state(sess: Session, subscription_id: str) -> str:
    """``pending`` / ``done`` / ``owed`` / ``settled``, or "" with no row."""
    if not subscription_id:
        return ""
    row = sess.get(SubscriptionRefund, subscription_id)
    return row.state if row is not None else ""


def pending_refunds(sess: Session, customer_id: str) -> list[SubscriptionRefund]:
    """The customer's refunds whose cancellation landed but which are not
    finished — of any contract, not just the current one."""
    if not customer_id:
        return []
    return list(sess.exec(
        select(SubscriptionRefund).where(
            SubscriptionRefund.customer_id == customer_id,
            SubscriptionRefund.state == PENDING,
        )
    ).all())


def _outcome(row: SubscriptionRefund) -> Refund:
    return Refund(row.refunded, row.currency, row.owed if row.state == OWED else 0)


def _detached(sess: Session, row: SubscriptionRefund) -> SubscriptionRefund:
    sess.refresh(row)
    sess.expunge(row)
    return row


def _claim(user_info_id: int, customer_id: str, subscription_id: str,
           contract_start: float, requested_at: float, *,
           create: bool = True) -> tuple[SubscriptionRefund | None, str]:
    """Take the row for this request: ``(row, token)``, token "" when final.

    The lease is timed from the claim itself, not from when the request
    began. A row is created only for an account that still exists — checked
    under its lock — so a withdrawal that lost a race with the account's
    deletion leaves nothing behind (:class:`AccountGone`). With ``create``
    off, a missing row is ``(None, "")``.
    """
    with get_session() as sess:
        lock_account(sess, user_info_id)
        now = time.time()
        row = sess.get(SubscriptionRefund, subscription_id)
        if row is None:
            if not create:
                sess.rollback()
                return None, ""
            if sess.get(UserInfo, user_info_id) is None:
                sess.rollback()
                raise AccountGone(subscription_id)
            row = SubscriptionRefund(subscription_id=subscription_id,
                                     customer_id=customer_id,
                                     contract_started_at=contract_start,
                                     requested_at=requested_at,
                                     cancel_attempted_at=requested_at, created_at=now)
        elif row.state in FINAL:
            sess.expunge(row)  # before the rollback expires it
            sess.rollback()
            return row, ""
        elif row.lease_until > now:
            sess.rollback()
            raise RefundInProgress(subscription_id)
        token = uuid.uuid4().hex
        row.claim_token = token
        row.lease_until = now + LEASE_SECONDS
        row.updated_at = now
        row.version = (row.version or 0) + 1
        sess.add(row)
        sess.commit()
        return _detached(sess, row), token


def _save(subscription_id: str, user_info_id: int, token: str,
          **fields) -> SubscriptionRefund:
    """Write ``fields`` — only while ``token`` still holds the claim."""
    with get_session() as sess:
        lock_account(sess, user_info_id)
        row = sess.get(SubscriptionRefund, subscription_id)
        if row is None or row.claim_token != token:
            sess.rollback()
            raise LostClaim(subscription_id)
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_at = time.time()
        row.version = (row.version or 0) + 1
        sess.add(row)
        sess.commit()
        return _detached(sess, row)


def _release(subscription_id: str, user_info_id: int, token: str) -> None:
    """Give the claim back after a transient failure, keeping what is frozen."""
    try:
        _save(subscription_id, user_info_id, token, lease_until=0.0, claim_token="")
    except LostClaim:
        pass


def _forget(subscription_id: str, user_info_id: int, token: str) -> None:
    """Drop a row claimed for nothing: asked outside the window, nothing
    frozen, nothing owed."""
    with get_session() as sess:
        lock_account(sess, user_info_id)
        row = sess.get(SubscriptionRefund, subscription_id)
        if row is not None and row.claim_token == token and row.amount < 0:
            sess.delete(row)
            sess.commit()
        else:
            sess.rollback()


def _log_owed(row: SubscriptionRefund) -> None:
    _log.error(
        "OWED REFUND: %s cents (%s) of subscription %s, customer %s, invoice %s "
        "could not be refunded automatically: %s. Refund it in the Stripe "
        "dashboard, then mark it settled (docs/BILLING.md, \"Owed refunds\").",
        row.owed, row.currency, row.subscription_id, row.customer_id,
        row.invoice_id, row.reason,
    )


def _finish(row: SubscriptionRefund, user_info_id: int, token: str, *,
            refunded: int, note_id: str, owed: int, reason: str,
            refund_id: str = "", owed_anyway: bool = False) -> Refund:
    row = _save(row.subscription_id, user_info_id, token,
                state=OWED if owed > 0 or owed_anyway else DONE, refunded=refunded,
                credit_note_id=note_id, refund_id=refund_id, owed=owed, reason=reason,
                lease_until=0.0, claim_token="")
    if row.state == OWED:
        _log_owed(row)
    return _outcome(row)


def _issue(gateway: BillingGateway, row: SubscriptionRefund, user_info_id: int,
           token: str) -> IssuedRefund:
    """Issue the frozen refund; on a key conflict, once more under a new key."""
    key = refund_key(row.subscription_id)
    try:
        return gateway.issue_refund(row.subscription_id, row.to_refund, key,
                                    invoice_id=row.invoice_id, attempt=row.attempt)
    except IdempotencyConflict:
        row = _save(row.subscription_id, user_info_id, token, attempt=row.attempt + 1)
        return gateway.issue_refund(row.subscription_id, row.to_refund, key,
                                    invoice_id=row.invoice_id, attempt=row.attempt)


def request_withdrawal(gateway: BillingGateway, *, user_info_id: int,
                       customer_id: str, subscription_id: str,
                       contract_start: float, requested_at: float) -> str:
    """Record the withdrawal *before* anything is cancelled. Returns the state.

    The request is the withdrawal (the terms say so), so it is written down
    first: the row, tied to the contract's subscription, with ``requested_at``
    frozen, and the contract's current invoice frozen with it — read from
    Stripe now, before a renewal could make another invoice the latest. A
    cancellation that then fails, or whose answer is lost, leaves this row
    ``pending``, and a retry — even after the deadline — completes the same
    withdrawal: it cancels again (idempotent) and refunds the frozen invoice.

    Each call also stamps ``cancel_attempted_at`` just before the caller
    cancels: the late-landing bound runs from the latest attempt.

    A final row (``done``/``owed``/``settled``) is left alone. Raises
    :class:`RefundInProgress`, :class:`AccountGone` and, before anything
    is cancelled, :class:`GatewayError`.
    """
    row, token = _claim(user_info_id, customer_id, subscription_id,
                        contract_start, requested_at)
    if not token:
        return row.state
    try:
        if not row.invoice_id and row.amount < 0:
            basis = gateway.refund_basis(subscription_id)
            if basis.invoice_id:
                row = _save(subscription_id, user_info_id, token,
                            invoice_id=basis.invoice_id, currency=basis.currency)
        _save(subscription_id, user_info_id, token,
              cancel_attempted_at=time.time(), lease_until=0.0, claim_token="")
    except PermanentGatewayError:
        # Nothing to read about the invoice (a deleted customer, say): the
        # request still stands; settle records what is owed.
        _save(subscription_id, user_info_id, token,
              cancel_attempted_at=time.time(), lease_until=0.0, claim_token="")
    except GatewayError:
        _release(subscription_id, user_info_id, token)
        raise
    return PENDING


def settle(gateway: BillingGateway, *, user_info_id: int, customer_id: str,
           subscription_id: str, contract_start: float, requested_at: float,
           now: float = 0.0) -> Refund:
    """Refund what is left of the *cancelled* subscription, once.

    Call after its cancellation, with the row recorded by
    :func:`request_withdrawal` (a row missing here is created, as for an
    earlier contract's refund). The withdrawal is refunded when it was asked
    for inside the window, on the invoice frozen with the request:

    * Stripe's ``ended_at`` must be no later than the latest cancel attempt +
      :data:`CANCEL_BOUND_SECONDS`. A cancellation recorded later than that is
      not refunded automatically — something went wrong — but recorded as
      owed for the owner;
    * if the subscription ended after the frozen invoice's period — it
      **renewed** while the cancellation kept failing — nothing is refunded
      automatically either: the renewal was charged after the withdrawal was
      asked for, and the owner refunds it and the unused part (measured at the
      request) by hand. The same when no invoice could be frozen and the
      latest one began after the request. A request never opens a window on a
      renewal.

    Neither is a refusal after cancelling. Raises :class:`RefundInProgress`,
    :class:`AccountGone`, :class:`NotEligible` (asked outside the window), or
    :class:`GatewayError` for a transient failure (the row stays ``pending``).
    A definite refusal does not raise: the refund is recorded as owed.
    """
    row, token = _claim(user_info_id, customer_id, subscription_id,
                        contract_start, requested_at)
    if not token:
        return _outcome(row)
    key = refund_key(subscription_id)
    try:
        gateway.discard_pending_items(customer_id, subscription_id)
        if row.amount < 0:
            start = row.contract_started_at or contract_start
            if not withdrawal_window_open(start, row.requested_at):
                raise NotEligible(subscription_id)
            basis = gateway.refund_basis(subscription_id, invoice_id=row.invoice_id)
            if not basis.ended_at:
                # Stripe does not show it ended (yet): the caller cancels
                # again on its retry. Transient, never a refusal.
                raise GatewayError(f"{subscription_id} has not ended at Stripe yet")
            total = basis.total or basis.amount_paid
            attempted = row.cancel_attempted_at or row.requested_at
            problem, at = "", basis.ended_at
            if basis.ended_at > attempted + CANCEL_BOUND_SECONDS:
                problem = (f"the cancellation landed {int(basis.ended_at - attempted)} s "
                           f"after it was attempted; check it before refunding")
            elif basis.period_start > row.requested_at:
                # No invoice was frozen (Stripe could not be read at the
                # request) and the latest one began after it: a renewal.
                problem = (f"the subscription renewed ({basis.invoice_id}) after the "
                           "withdrawal was asked for: refund the renewal, and the "
                           "unused part of the invoice before it (measured at the "
                           "request, not computed here), by hand")
                at = row.requested_at
            elif row.invoice_id and basis.period_end and basis.ended_at > basis.period_end:
                problem = ("the subscription renewed after the withdrawal was asked "
                           "for: refund the renewal and the unused part (measured at "
                           "the request) by hand")
                at = row.requested_at
            row = _save(subscription_id, user_info_id, token,
                        amount=prorated_refund_amount(basis.period_start,
                                                      basis.period_end, total, at),
                        invoice_id=basis.invoice_id, currency=basis.currency)
            if problem:
                return _finish(row, user_info_id, token, refunded=0, note_id="",
                               owed=row.amount, reason=problem, owed_anyway=True)
        if row.to_refund < 0:
            plan = (RefundPlan(0) if row.amount == 0 else gateway.refund_plan(
                subscription_id, row.amount, key, invoice_id=row.invoice_id))
            if plan.existing_note_id:
                return _finish(row, user_info_id, token, refunded=plan.existing_cents,
                               note_id=plan.existing_note_id, owed=plan.owed_cents,
                               reason=plan.reason, refund_id=plan.existing_refund_id)
            row = _save(subscription_id, user_info_id, token, to_refund=plan.send_cents,
                        owed=plan.owed_cents, reason=plan.reason)
        issued = (_issue(gateway, row, user_info_id, token) if row.to_refund > 0
                  else IssuedRefund(""))
    except PermanentGatewayError as exc:
        owed = max(0, row.to_refund) + row.owed if row.to_refund >= 0 else max(0, row.amount)
        reason = str(exc)[:500]
        if row.amount < 0:
            # Refused before the amount could be read (a deleted customer,
            # say): owed all the same, for the owner to work out from Stripe.
            reason += " (amount unknown: see the subscription's last invoice)"
        return _finish(row, user_info_id, token, refunded=0, note_id="", owed=owed,
                       reason=reason, owed_anyway=True)
    except NotEligible:
        _forget(subscription_id, user_info_id, token)
        raise
    except LostClaim:
        raise
    except GatewayError:
        _release(subscription_id, user_info_id, token)
        raise
    return _finish(row, user_info_id, token, refunded=max(0, row.to_refund),
                   note_id=issued.credit_note_id, refund_id=issued.refund_id,
                   owed=row.owed, reason=row.reason)


def finish_pending(gateway: BillingGateway, *, user_info_id: int, customer_id: str,
                   now: float, skip: str = "") -> list[Refund]:
    """Complete every pending refund of the customer (but ``skip``).

    A pending row means its withdrawal was asked for in time: it is owed a
    refund whatever contract has started since. Its subscription is cancelled
    again first — idempotent — in case the cancellation never landed. Raises
    like :func:`settle`, except that a row no longer eligible is left to the
    owner's list.
    """
    with get_session() as sess:
        rows = [(r.subscription_id, r.contract_started_at, r.requested_at)
                for r in pending_refunds(sess, customer_id) if r.subscription_id != skip]
    done = []
    for subscription_id, start, requested_at in rows:
        try:
            request_withdrawal(gateway, user_info_id=user_info_id,
                               customer_id=customer_id, subscription_id=subscription_id,
                               contract_start=start, requested_at=requested_at)
            gateway.cancel_subscription(subscription_id, customer_id)
            done.append(settle(gateway, user_info_id=user_info_id,
                               customer_id=customer_id, subscription_id=subscription_id,
                               contract_start=start, requested_at=requested_at))
        except NotEligible:
            continue
    return done


def owner_account(sess: Session, customer_id: str) -> int:
    """The account holding a Stripe customer, or 0 when it is gone."""
    if not customer_id:
        return 0
    found = sess.exec(
        select(Subscription.user_info_id).where(
            Subscription.provider_customer_id == customer_id)
    ).first()
    return int(found or 0)


def resolve_pending(gateway: BillingGateway, subscription_id: str) -> str:
    """Settle a stuck pending row's fate from what Stripe shows (admin).

    A pending row whose lease ran out may hide a credit note that was made
    but whose answer was lost. Looked up by refund key on the frozen invoice:
    found, the row becomes ``done`` (or ``owed`` for any shortfall); not found
    — or nothing was frozen, so nothing can have been issued — it stays
    ``pending``, and it is certain that nothing was refunded under it.
    Returns the state. Raises :class:`RefundInProgress` if a request holds it
    and :class:`GatewayError` if Stripe cannot be asked.
    """
    with get_session() as sess:
        row = sess.get(SubscriptionRefund, subscription_id)
        if row is None:
            return ""
        user_info_id = owner_account(sess, row.customer_id)
    row, token = _claim(user_info_id, row.customer_id, subscription_id,
                        row.contract_started_at, row.requested_at, create=False)
    if row is None:
        return ""
    if not token:
        return row.state
    try:
        if row.to_refund > 0:
            plan = gateway.refund_plan(subscription_id, row.amount,
                                       refund_key(subscription_id),
                                       invoice_id=row.invoice_id)
            if plan.existing_note_id:
                _finish(row, user_info_id, token, refunded=plan.existing_cents,
                        note_id=plan.existing_note_id, owed=plan.owed_cents,
                        reason=plan.reason, refund_id=plan.existing_refund_id)
                return _state(subscription_id)
    except GatewayError:
        _release(subscription_id, user_info_id, token)
        raise
    _release(subscription_id, user_info_id, token)
    return PENDING


def _state(subscription_id: str) -> str:
    with get_session() as sess:
        return ledger_state(sess, subscription_id)


def record_refund_failure(refund_id: str, amount: int, reason: str) -> bool:
    """A refund of ours failed (or was canceled) after it was created.

    The money did not go back. Matched by the refund id stored when the
    credit note was made:
    * a ``done`` or ``owed`` row: owed grows by the failed amount;
    * a ``settled`` row: the owner settled the rest by hand, so only the
      failed amount is owed now (the settled one is kept in the reason);
    and the id is cleared, so a redelivered event changes nothing.

    Returns whether a row changed; with none, the caller logs the failure.
    """
    if not refund_id:
        return False
    with get_session() as sess:
        row = sess.exec(select(SubscriptionRefund).where(
            SubscriptionRefund.refund_id == refund_id)).first()
        if row is None:
            sess.rollback()
            return False
        user_info_id = owner_account(sess, row.customer_id)
    with get_session() as sess:
        lock_account(sess, user_info_id)
        row = sess.exec(select(SubscriptionRefund).where(
            SubscriptionRefund.refund_id == refund_id)).first()
        if row is None:
            sess.rollback()
            return False
        failed = min(max(0, amount or row.refunded), row.refunded)
        why = f"the refund failed after it was made: {reason or 'no reason given'}"
        if row.state == SETTLED:
            why += f" (after {row.owed} cents were settled by hand)"
            row.owed = failed
        else:
            row.owed += failed
        row.refunded -= failed
        row.state = OWED
        row.refund_id = ""
        row.reason = why
        row.updated_at = time.time()
        row.version = (row.version or 0) + 1
        sess.add(row)
        sess.commit()
        row = _detached(sess, row)
    _log_owed(row)
    return True


@dataclass(frozen=True)
class Quote:
    """What withdrawing now would refund: to the card, and owed by hand."""

    to_card_cents: int
    owed_cents: int
    currency: str


def refund_quote(gateway: BillingGateway, subscription_id: str, now: float, *,
                 invoice_id: str = "", requested_at: float = 0.0) -> Quote:
    """What withdrawing at ``now`` would refund. Nothing changes.

    Computed exactly as the refund is: the unused part of the invoice *total*,
    split by the same plan into what goes back to the card and what would be
    owed. An estimate: the refund itself is measured when the cancellation
    lands, a few seconds later.

    For a withdrawal already asked for (a pending row), pass its frozen
    ``invoice_id`` and ``requested_at``: if the subscription renewed since,
    :func:`settle` will record the refund as owed, measured at the request,
    and the quote says so rather than promise a refund to the card.
    """
    basis = gateway.refund_basis(subscription_id, invoice_id=invoice_id)
    at = basis.ended_at or now
    if requested_at and (basis.period_start > requested_at or (
            invoice_id and basis.period_end and at > basis.period_end)):
        return Quote(0, prorated_refund_amount(
            basis.period_start, basis.period_end, basis.total or basis.amount_paid,
            requested_at), basis.currency)
    amount = prorated_refund_amount(basis.period_start, basis.period_end,
                                    basis.total or basis.amount_paid, at)
    if amount <= 0 or not basis.invoice_id:
        return Quote(0, 0, basis.currency)
    plan = gateway.refund_plan(subscription_id, amount, refund_key(subscription_id),
                               invoice_id=basis.invoice_id)
    if plan.existing_note_id:
        return Quote(plan.existing_cents, plan.owed_cents, basis.currency)
    return Quote(plan.send_cents, plan.owed_cents, basis.currency)

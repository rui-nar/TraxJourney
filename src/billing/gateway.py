"""Payment-gateway seam (issue #121).

Everything above this line is provider-agnostic; everything below it is Stripe.
The seam exists for two concrete reasons, not for speculative flexibility:

  * the API layer must be testable without network access or an SDK, and
  * ``import stripe`` must not happen at all on a self-hosted instance that
    never configured billing.

:func:`get_gateway` returns ``None`` when billing is off, which is the signal
the API uses to 404 the payment routes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class GatewayError(Exception):
    """A call to the payment provider failed. Retrying may succeed."""


class PermanentGatewayError(GatewayError):
    """The provider refused definitely — a refusal code that says the same
    request will be refused again (a disputed charge, one already refunded…).
    A refund refused like this is owed, and settled by hand (#441)."""


class IdempotencyConflict(GatewayError):
    """The provider saw this idempotency key with different parameters. The
    caller retries under a new key; never a reason to give up on a refund."""


@dataclass(frozen=True)
class RefundPlan:
    """How a refund of ``amount_cents`` would go, read from the provider (#441).

    Read-only. The caller freezes it and sends exactly ``send_cents`` with
    :meth:`BillingGateway.issue_refund`, so a replay under the same key
    carries the same parameters.
    """

    #: To refund to the card now, through a credit note.
    send_cents: int
    #: Owed but not refundable automatically — the share paid from the
    #: customer's balance, what the invoice can no longer be credited, or all
    #: of it when there is no payment — and why. The owner settles it.
    owed_cents: int = 0
    reason: str = ""
    #: A credit note already carrying the refund key (a lost response): its
    #: amount was refunded, and ``owed_cents`` is what is still missing.
    existing_note_id: str = ""
    existing_cents: int = 0


@dataclass(frozen=True)
class RefundBasis:
    """What a pro-rata refund of one subscription is computed from (#441).

    Read from the provider, never from our cached row: the amount actually
    paid (after coupons, including tax) and the period it paid for are on the
    invoice, and when the subscription ended is on the subscription.
    """

    subscription_id: str
    #: When the subscription ended (unix seconds), 0 while it still runs.
    ended_at: float
    #: The latest paid invoice's id; "" when nothing was ever paid.
    invoice_id: str
    #: What that invoice took, in the smallest currency unit. 0 for a trial, a
    #: coupon or a 100%-off promotion code.
    amount_paid: int
    currency: str
    #: The service period that invoice paid for, unix seconds.
    period_start: float
    period_end: float
    #: The invoice's total. Above ``amount_paid`` when part was paid from the
    #: customer's credit balance, which a refund to the card cannot return.
    total: int = 0


class BillingGateway(Protocol):
    """The provider operations the app needs."""

    def create_checkout_session(
        self, *, user_info_id: int, plan: str, email: str, customer_id: str,
        success_url: str, cancel_url: str, terms_url: str = "",
    ) -> dict:
        """Start a subscription purchase.

        The page must collect the buyer's express consent to start at once and
        their acknowledgement of the withdrawal terms (#441), linking
        ``terms_url`` when given. Returns ``{"url", "customer_id",
        "session_id"}``; ``session_id`` is what :meth:`expire_checkout_session`
        takes.
        """

    def expire_checkout_session(self, session_id: str) -> None:
        """Make a checkout page unpayable. A session already closed is fine.

        Used when the account it was opened for is deleted before the page is
        even handed out (issue #429). Raises :class:`GatewayError` when it may
        still be open.
        """

    def create_portal_session(self, *, customer_id: str, return_url: str) -> dict:
        """Open the provider's billing portal. Returns ``{"url"}``."""

    def create_plan_change_session(
        self, *, customer_id: str, subscription_id: str, plan: str,
        return_url: str,
    ) -> dict:
        """Move an existing subscription to another plan. Returns ``{"url"}``.

        ``plan`` of ``"free"`` means cancel: there is nothing to move to, and the
        subscription ending *is* the downgrade.
        """

    def parse_webhook(self, payload: bytes, signature: str) -> dict:
        """Verify the signature and return the event dict. Raises on mismatch."""

    def cancel_subscription(self, subscription_id: str, customer_id: str = "") -> None:
        """End a subscription *now*, not at the end of the paid period.

        Used when the account is being deleted (issue #429): there will be no
        account left from which to cancel it, so nothing may renew. A
        subscription that has already ended, or that the provider does not
        know, counts as success — a retry after a partial failure must not be
        refused. When ``customer_id`` is given, an unknown subscription is only
        success if the provider knows the customer: both missing means the
        configured key belongs to another account. Raises
        :class:`GatewayError` on anything else.
        """

    def cancel_all_for_customer(self, customer_id: str) -> list[str]:
        """Stop everything that could still bill ``customer_id``, now.

        Expires the customer's open checkout sessions first — so a payment
        page left open cannot start a subscription afterwards — then cancels
        every subscription that has not ended. Decides from the provider, not
        from our cached state, which can lag or track only one subscription.
        Returns the ids of the subscriptions it cancelled. The customer itself
        is kept: its invoices are the accounting record. Raises
        :class:`GatewayError` when anything could not be stopped, or when the
        provider does not know the customer at all.
        """

    def latest_subscription(self, customer_id: str) -> tuple[str, float] | None:
        """``(id, start)`` of the customer's most recently started paid-for
        subscription, or None (#441).

        Only for when our row has no contract start on record — its webhook has
        not arrived yet — and account deletion must still decide the window.
        """

    def discard_pending_items(self, customer_id: str, subscription_id: str) -> int:
        """Delete the customer's not-yet-invoiced items from ``subscription_id``.

        An upgrade's prorated difference waits as a pending invoice item for
        the next renewal. After a withdrawal or a deletion there is none, and
        the item must not be billed on some later invoice (#441). Returns how
        many were deleted. Raises :class:`GatewayError` on failure.
        """

    def refund_basis(self, subscription_id: str) -> RefundBasis:
        """The provider's facts a pro-rata refund is computed from (#441)."""

    def subscriptions_in_force(self, customer_id: str) -> list[str]:
        """Ids of the customer's subscriptions still in force at the provider:
        active, trialing, past due, unpaid or paused (#441)."""

    def refund_plan(
        self, subscription_id: str, amount_cents: int, refund_key: str, *,
        invoice_id: str,
    ) -> RefundPlan:
        """How ``amount_cents`` — the whole unused period — would be refunded.

        Every refund already made on the invoice's payment (by hand, say)
        counts towards it; the card can get back at most what it paid; the
        invoice can be credited at most what is left of it. What cannot go
        back to the card is reported as owed, never dropped. Changes nothing.
        """

    def issue_refund(
        self, subscription_id: str, send_cents: int, refund_key: str, *,
        invoice_id: str, attempt: int = 0,
    ) -> str:
        """Refund ``send_cents`` of ``invoice_id`` through a credit note (#441).

        A credit note already carrying ``refund_key`` is returned instead of
        making another. The provider idempotency key is ``refund_key:attempt``.
        Returns the credit note id. Raises :class:`PermanentGatewayError` on a
        definite refusal, :class:`IdempotencyConflict` when the key was used
        with other parameters, and :class:`GatewayError` on anything that may
        pass.
        """


_override: BillingGateway | None = None


def set_gateway(gateway: BillingGateway | None) -> None:
    """Install a gateway (tests inject a fake). ``None`` restores the default."""
    global _override
    _override = gateway


def get_gateway() -> BillingGateway | None:
    """The active gateway, or ``None`` when this deployment does not sell plans."""
    if _override is not None:
        return _override

    from src.billing.entitlements import billing_enabled

    if not billing_enabled():
        return None

    from src.billing.stripe_gateway import StripeGateway

    return StripeGateway()

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

from typing import Protocol


class GatewayError(Exception):
    """A call to the payment provider failed."""


class BillingGateway(Protocol):
    """The provider operations the app needs."""

    def create_checkout_session(
        self, *, user_info_id: int, plan: str, email: str, customer_id: str,
        success_url: str, cancel_url: str,
    ) -> dict:
        """Start a subscription purchase.

        Returns ``{"url", "customer_id", "session_id"}``; ``session_id`` is
        what :meth:`expire_checkout_session` takes.
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

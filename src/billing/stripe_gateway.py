"""Stripe implementation of :class:`~src.billing.gateway.BillingGateway` (#121).

The only module in the codebase that imports the Stripe SDK, and it does so
lazily so a self-hosted instance never touches it.

Kept deliberately thin: it creates sessions and verifies signatures, and does
not interpret anything. Interpretation lives in
:mod:`src.billing.webhook_events`, where it can be tested against recorded
payloads without a network or an API key.
"""
from __future__ import annotations

import json
import os
import time

from src.billing.gateway import GatewayError
from src.billing.plans import FREE, PAID_PLANS, price_lookup_key
from src.billing.webhook_events import price_id_for_plan
from src.utils.logging import get_logger

_log = get_logger(__name__)


def _stripe():
    """Import and configure the SDK on first use."""
    import stripe  # imported lazily — see module docstring

    secret = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        raise GatewayError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = secret
    return stripe


def _field(obj, name: str):
    """Read a field off a Stripe SDK object.

    ``StripeObject`` does not subclass ``dict``: its ``.get`` is the API's GET
    helper, so ``session.get("url")`` raises ``AttributeError`` rather than
    returning the URL. Subscripting is the supported access, and a missing key
    raises, hence the membership check.
    """
    return obj[name] if name in obj else None


#: How long a resolved price id is trusted. Prices are immutable, but a reprice
#: moves the lookup key onto a *new* price and archives the old one
#: (scripts/stripe_catalog.py), so caching forever would keep selling something
#: archived until the next deploy. Negative results expire sooner, so fixing a
#: misconfiguration does not need a restart.
_CACHE_TTL_SECONDS = 600.0
_MISS_TTL_SECONDS = 60.0

#: lookup key → (price id or "", expires_at)
_price_cache: dict[str, tuple[str, float]] = {}


def reset_price_cache() -> None:
    """Drop the resolved-price cache. For tests and after a catalogue change."""
    _price_cache.clear()


def resolve_price_id(plan: str) -> str:
    """Price id for a paid plan, resolved by lookup key (issue #154).

    ``STRIPE_PRICE_TIER_N`` still wins when set: an explicit pin, and the escape
    hatch if resolution ever misbehaves. Otherwise the id is looked up by the
    key :func:`~src.billing.plans.price_lookup_key` derives, which is identical
    in every Stripe account — so one configuration is correct in a sandbox, in
    test mode and in live, and pairing a key with another account's price ids
    stops being expressible.

    Resolution is lazy on purpose. A self-hosted instance with billing disabled
    must never call Stripe, so this cannot move to import or startup.
    """
    if plan not in PAID_PLANS:
        return ""
    pinned = price_id_for_plan(plan)
    if pinned:
        return pinned

    key = price_lookup_key(plan)
    cached = _price_cache.get(key)
    if cached is not None and time.time() < cached[1]:
        if not cached[0]:
            raise GatewayError(f"No Stripe price with lookup key {key}")
        return cached[0]

    stripe = _stripe()
    try:
        found = stripe.Price.list(lookup_keys=[key], limit=1)["data"] or []
    except Exception as exc:
        # Do not cache a transport failure as "missing" — that would turn a
        # blip into a minute of refused checkouts.
        _log.warning("Stripe price lookup failed for %s: %s", key, exc)
        raise GatewayError(str(exc)) from exc

    if not found:
        _price_cache[key] = ("", time.time() + _MISS_TTL_SECONDS)
        _log.warning(
            "No Stripe price with lookup key %s — run scripts/stripe_catalog.py "
            "--apply against this account.", key,
        )
        raise GatewayError(f"No Stripe price with lookup key {key}")

    price_id = str(found[0]["id"])
    _price_cache[key] = (price_id, time.time() + _CACHE_TTL_SECONDS)
    return price_id


def cloud_price_id(plan: str) -> str:
    """Price id for a paid plan. See :func:`resolve_price_id`."""
    return resolve_price_id(plan)


def webhook_secret() -> str:
    """Signing secret for webhook verification, from the environment."""
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


class StripeGateway:
    """Checkout, Customer Portal and webhook verification via Stripe."""

    def create_checkout_session(
        self, *, user_info_id: int, plan: str, email: str, customer_id: str,
        success_url: str, cancel_url: str,
    ) -> dict:
        stripe = _stripe()
        # Raises GatewayError when the catalogue has no price for this plan;
        # the empty string is only possible for a plan that is not sold.
        price = cloud_price_id(plan)
        if not price:
            raise GatewayError(f"'{plan}' is not a plan that can be bought")
        metadata = {"user_info_id": str(user_info_id), "plan": plan}
        params: dict = {
            "mode": "subscription",
            "line_items": [{"price": price, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            # Both, on purpose: metadata rides along to the subscription object,
            # client_reference_id shows up in the dashboard for support.
            "client_reference_id": str(user_info_id),
            "metadata": metadata,
            "subscription_data": {"metadata": metadata},
            "allow_promotion_codes": True,
        }
        # Reuse the customer across purchases so one person is one customer in
        # Stripe (and their portal shows their whole history).
        if customer_id:
            params["customer"] = customer_id
        elif email:
            params["customer_email"] = email
        try:
            session = stripe.checkout.Session.create(**params)
        except Exception as exc:  # SDK raises a family of StripeError subclasses
            _log.warning("Stripe checkout session failed: %s", exc)
            raise GatewayError(str(exc)) from exc
        return {
            "url": _field(session, "url") or "",
            "customer_id": str(_field(session, "customer") or customer_id or ""),
        }

    def create_plan_change_session(
        self, *, customer_id: str, subscription_id: str, plan: str,
        return_url: str,
    ) -> dict:
        """Portal session opened straight on the confirm-change screen (#153).

        Checkout cannot do this: in subscription mode it always *creates* a
        subscription, which is why buying a second tier is refused (issue #163).
        A ``subscription_update_confirm`` flow moves the existing one instead,
        and Stripe owns everything that makes tier changes hard — the prorated
        amount, tax, 3DS re-authentication on an upgrade, and the receipt.

        Moving to ``free`` is a cancellation, not a price change: there is no
        price to move to, so it opens the cancel flow instead. Both honour the
        deployment's portal configuration, which
        ``scripts/stripe_catalog.py`` provisions — including the rule that a
        downgrade takes effect at the end of the paid period.
        """
        stripe = _stripe()
        if not customer_id:
            raise GatewayError("No billing account for this user")
        if not subscription_id:
            raise GatewayError("No subscription to change")

        after = {"type": "redirect", "redirect": {"return_url": return_url}}
        if plan == FREE:
            flow_data: dict = {
                "type": "subscription_cancel",
                "subscription_cancel": {"subscription": subscription_id},
                "after_completion": after,
            }
        else:
            price = cloud_price_id(plan)
            if not price:
                raise GatewayError(f"'{plan}' is not a plan that can be bought")
            flow_data = {
                "type": "subscription_update_confirm",
                "subscription_update_confirm": {
                    "subscription": subscription_id,
                    # The flow updates a subscription *item*, so the item id has
                    # to be read off the subscription first — the subscription id
                    # alone is not enough.
                    "items": [
                        {
                            "id": self._first_item_id(stripe, subscription_id),
                            "price": price,
                            "quantity": 1,
                        }
                    ],
                },
                "after_completion": after,
            }

        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id, return_url=return_url, flow_data=flow_data
            )
        except Exception as exc:
            _log.warning("Stripe plan-change session failed: %s", exc)
            raise GatewayError(str(exc)) from exc
        return {"url": _field(session, "url") or ""}

    @staticmethod
    def _first_item_id(stripe, subscription_id: str) -> str:
        """Id of the subscription's single priced item.

        Our subscriptions carry exactly one item — one tier, quantity one — and
        the update flow refuses subscriptions with several anyway, so the first
        is the right one.
        """
        try:
            sub = stripe.Subscription.retrieve(subscription_id)
        except Exception as exc:
            _log.warning("Stripe subscription retrieve failed: %s", exc)
            raise GatewayError(str(exc)) from exc
        items = (_field(sub, "items") or {})
        data = (_field(items, "data") or []) if items else []
        for item in data:
            item_id = _field(item, "id")
            if item_id:
                return str(item_id)
        raise GatewayError(f"Subscription {subscription_id} has no items")

    def create_portal_session(self, *, customer_id: str, return_url: str) -> dict:
        stripe = _stripe()
        if not customer_id:
            raise GatewayError("No billing account for this user")
        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id, return_url=return_url
            )
        except Exception as exc:
            _log.warning("Stripe portal session failed: %s", exc)
            raise GatewayError(str(exc)) from exc
        return {"url": _field(session, "url") or ""}

    def parse_webhook(self, payload: bytes, signature: str) -> dict:
        stripe = _stripe()
        secret = webhook_secret()
        if not secret:
            raise GatewayError("STRIPE_WEBHOOK_SECRET is not configured")
        try:
            stripe.Webhook.construct_event(payload, signature, secret)
        except Exception as exc:
            # Covers both a bad signature and a malformed body. Either way the
            # request did not come from Stripe as far as we can tell.
            raise GatewayError(f"Invalid webhook signature: {exc}") from exc
        # Signature verified above; take the event from the raw bytes rather than
        # the SDK object. ``StripeObject`` is not a dict — ``dict(event)`` raises
        # — and its recursive conversion is private, while webhook_events wants
        # plain nested dicts it can ``.get()`` through.
        return json.loads(payload)

    def cancel_subscription(self, subscription_id: str) -> None:
        """Cancel immediately — the account it belongs to is being deleted (#429).

        Not ``cancel_at_period_end``: once the account is gone there is nothing
        left to use the remaining period, nor anyone to stop a renewal.

        A refusal is only a failure if the subscription is still running. Stripe
        refuses to cancel one that no longer exists or has already ended, and a
        retry after a partial failure (cancelled here, then the account deletion
        failed) lands exactly there — so such a refusal is checked against the
        subscription's real state rather than parsed from its message.
        """
        stripe = _stripe()
        if not subscription_id:
            raise GatewayError("No subscription to cancel")
        try:
            stripe.Subscription.cancel(subscription_id)
            return
        except Exception as exc:
            if _is_missing(exc):
                _log.info("Stripe subscription %s does not exist — nothing to "
                          "cancel", subscription_id)
                return
            cancel_error = exc

        try:
            sub = stripe.Subscription.retrieve(subscription_id)
        except Exception as exc:
            if _is_missing(exc):
                return
            _log.warning("Stripe subscription cancel failed for %s: %s",
                         subscription_id, cancel_error)
            raise GatewayError(str(cancel_error)) from cancel_error
        status = str(_field(sub, "status") or "")
        if status in _ENDED_STATUSES:
            _log.info("Stripe subscription %s already %s", subscription_id, status)
            return
        _log.warning("Stripe subscription cancel failed for %s (status %s): %s",
                     subscription_id, status, cancel_error)
        raise GatewayError(str(cancel_error)) from cancel_error


#: Stripe subscription statuses that will never bill again.
_ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})


def _is_missing(exc: Exception) -> bool:
    """True for Stripe's "No such subscription" (``resource_missing``)."""
    return getattr(exc, "code", None) == "resource_missing"

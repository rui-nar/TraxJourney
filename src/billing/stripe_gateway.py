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

from src.billing.gateway import (
    GatewayError,
    PermanentGatewayError,
    RefundBasis,
    RefundResult,
)
from src.billing.plans import FREE, PAID_PLANS, price_lookup_key
from src.billing.refunds import WITHDRAWAL_TERMS_VERSION
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


def automatic_tax_enabled() -> bool:
    """True when Stripe Tax computes VAT at checkout (``STRIPE_AUTOMATIC_TAX``).

    Off by default (issue #441): Stripe Tax needs the account registered for
    VAT (the EU OSS scheme) and switched on in the dashboard first, and a
    checkout asking for it before then fails. A runtime variable, read per
    call, so the owner flips it without a release.
    """
    flag = os.environ.get("STRIPE_AUTOMATIC_TAX", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _consent_text(terms_url: str) -> dict:
    """What checkout shows beside the required terms box, and by the button.

    The box is the buyer's express consent to start the service at once, and
    their acknowledgement that withdrawing then refunds only the unused part
    (#441). The wording is versioned by ``WITHDRAWAL_TERMS_VERSION``: change
    one, bump the other.
    """
    terms = f"[Terms of Service]({terms_url})" if terms_url else "Terms of Service"
    return {
        "terms_of_service_acceptance": {
            "message": (
                f"I agree to the {terms}. I ask for my subscription to start "
                "immediately. If I withdraw by the end of the 14th day after "
                "the day it starts (UTC), I will be refunded only the unused "
                "part of the current period. After that, cancelling stops "
                "renewal and nothing is refunded."
            ),
        },
        "submit": {
            "message": (
                "Your subscription starts as soon as you pay. You can withdraw "
                "within 14 days of that for a pro-rata refund of the unused "
                "period."
            ),
        },
    }


class StripeGateway:
    """Checkout, Customer Portal and webhook verification via Stripe."""

    def create_checkout_session(
        self, *, user_info_id: int, plan: str, email: str, customer_id: str,
        success_url: str, cancel_url: str, terms_url: str = "",
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
            # The session's own metadata also names the wording of the
            # withdrawal terms the consent box stood for; the webhook stores
            # it with the consent, as proof (#441).
            "metadata": {**metadata, "terms_version": WITHDRAWAL_TERMS_VERSION},
            "subscription_data": {"metadata": metadata},
            "allow_promotion_codes": True,
            # Express consent to start at once, given on the page that
            # concludes the contract (#441). Stripe refuses the session unless
            # the account has a terms-of-service URL in its public details:
            # see "Refunds and withdrawal" in docs/BILLING.md.
            "consent_collection": {"terms_of_service": "required"},
            "custom_text": _consent_text(terms_url),
        }
        # Reuse the customer across purchases so one person is one customer in
        # Stripe (and their portal shows their whole history).
        if customer_id:
            params["customer"] = customer_id
        elif email:
            params["customer_email"] = email
        if automatic_tax_enabled():
            # VAT depends on where the buyer lives, so the address is required;
            # a returning customer's is saved on them, for renewals to use.
            params["automatic_tax"] = {"enabled": True}
            params["billing_address_collection"] = "required"
            if customer_id:
                params["customer_update"] = {"address": "auto", "name": "auto"}
        try:
            session = stripe.checkout.Session.create(**params)
        except Exception as exc:  # SDK raises a family of StripeError subclasses
            if _is_terms_url_missing(exc):
                # Not a blip: every checkout fails until the owner fixes it.
                _log.error(
                    "Stripe refused checkout because the account has no terms "
                    "of service URL, which consent_collection needs. Set it in "
                    "the Stripe Dashboard: Settings > Public details > Terms "
                    "of service (https://<host>/terms). Until then NO checkout "
                    "can succeed. Stripe said: %s", exc,
                )
            else:
                _log.warning("Stripe checkout session failed: %s", exc)
            raise GatewayError(str(exc)) from exc
        return {
            "url": _field(session, "url") or "",
            "customer_id": str(_field(session, "customer") or customer_id or ""),
            "session_id": str(_field(session, "id") or ""),
        }

    def expire_checkout_session(self, session_id: str) -> None:
        """Expire one checkout session; see :meth:`_expire_session`."""
        stripe = _stripe()
        if not session_id:
            raise GatewayError("No checkout session to expire")
        self._expire_session(stripe, session_id)

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

    def cancel_subscription(self, subscription_id: str, customer_id: str = "") -> None:
        """Cancel immediately — the account it belongs to is being deleted (#429).

        Not ``cancel_at_period_end``: once the account is gone there is nothing
        left to use the remaining period, nor anyone to stop a renewal.

        A refusal is only a failure if the subscription is still running. Stripe
        refuses to cancel one that no longer exists or has already ended, and a
        retry after a partial failure (cancelled here, then the account deletion
        failed) lands exactly there — so such a refusal is checked against the
        subscription's real state rather than parsed from its message.

        "No such subscription" is also what a key for the *wrong* Stripe account
        says (a sandbox key against live ids). So when the customer is known it
        is looked up too, and success needs the provider to know it.
        """
        stripe = _stripe()
        if not subscription_id:
            raise GatewayError("No subscription to cancel")
        try:
            stripe.Subscription.cancel(subscription_id)
            return
        except Exception as exc:
            if _is_missing(exc):
                self._require_customer(stripe, customer_id)
                _log.info("Stripe subscription %s does not exist — nothing to "
                          "cancel", subscription_id)
                return
            cancel_error = exc

        try:
            sub = stripe.Subscription.retrieve(subscription_id)
        except Exception as exc:
            if _is_missing(exc):
                self._require_customer(stripe, customer_id)
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

    def cancel_all_for_customer(self, customer_id: str) -> list[str]:
        """Expire open checkouts, then cancel every running subscription (#429).

        Asks Stripe rather than trusting our cached row, which can lag behind a
        payment made on a checkout page opened before the deletion, and which
        tracks only one subscription per account. Sessions go first: one
        completed in between shows up in the subscription list that follows.

        The customer is kept, not deleted — its invoices are the accounting
        record, and a refund needs them.
        """
        stripe = _stripe()
        if not customer_id:
            raise GatewayError("No billing account to cancel")
        if not self._require_customer(stripe, customer_id):
            return []  # a deleted customer has nothing left that can bill

        try:
            sessions = stripe.checkout.Session.list(
                customer=customer_id, status="open", limit=100
            )
            for session in sessions.auto_paging_iter():
                self._expire_session(stripe, str(_field(session, "id") or ""))
            subscriptions = stripe.Subscription.list(
                customer=customer_id, status="all", limit=100
            )
            running = [
                str(_field(sub, "id") or "")
                for sub in subscriptions.auto_paging_iter()
                if str(_field(sub, "status") or "") not in _ENDED_STATUSES
            ]
        except GatewayError:
            raise
        except Exception as exc:
            _log.warning("Stripe listing failed for customer %s: %s", customer_id, exc)
            raise GatewayError(str(exc)) from exc

        for subscription_id in running:
            self.cancel_subscription(subscription_id, customer_id)
        if running:
            _log.info("Stripe customer %s: cancelled %s", customer_id, running)
        return running

    def refund_basis(self, subscription_id: str) -> RefundBasis:
        """The latest paid invoice of ``subscription_id``, and when it ended."""
        stripe = _stripe()
        if not subscription_id:
            raise GatewayError("No subscription to refund")
        try:
            sub = stripe.Subscription.retrieve(subscription_id)
            invoice = _latest_paid_invoice(stripe, subscription_id)
        except Exception as exc:
            _log.warning("Stripe refund lookup failed for %s: %s", subscription_id, exc)
            raise GatewayError(str(exc)) from exc
        if invoice is None:
            return RefundBasis(subscription_id, _ended_at(sub), "", 0, "", 0.0, 0.0)
        start, end = _invoice_period(invoice)
        return RefundBasis(
            subscription_id=subscription_id,
            ended_at=_ended_at(sub),
            invoice_id=str(_field(invoice, "id") or ""),
            amount_paid=int(_field(invoice, "amount_paid") or 0),
            currency=str(_field(invoice, "currency") or ""),
            period_start=start,
            period_end=end,
            total=int(_field(invoice, "total") or 0),
        )

    def refund_unused(
        self, subscription_id: str, amount_cents: int, refund_key: str, *,
        invoice_id: str, attempt: int = 0,
    ) -> RefundResult:
        """Refund the unused period of ``invoice_id`` through a credit note (#441).

        A credit note rather than a bare refund, whether or not Stripe Tax is
        on: it is what reverses the VAT in Stripe Tax's reports, and it gives
        the customer a document. ``refund_amount`` makes Stripe refund the
        invoice's payment for it.

        Before creating anything:
        * a credit note of the invoice (not void), or a refund of its payment,
          stamped with ``refund_key`` means the work is done — whatever
          provider key it was made under;
        * every other refund of the payment — one made by hand in the
          dashboard, say — counts towards ``amount_cents``, and so does
          everything the invoice has already been credited (credit notes to the
          balance or out of band too): only the rest can go out. What is owed
          but cannot go out is reported, never silently dropped.

        The provider idempotency key is ``refund_key:attempt``. The caller's
        ledger bumps ``attempt`` only after a definite refusal, so a timeout's
        retry reuses the key and Stripe deduplicates it.
        """
        if amount_cents <= 0:
            return RefundResult(0)
        if not refund_key or not invoice_id:
            raise GatewayError("A refund needs a refund key and an invoice")
        stripe = _stripe()
        try:
            invoice = stripe.Invoice.retrieve(invoice_id)
            credited = 0
            for note in stripe.CreditNote.list(invoice=invoice_id, limit=100).auto_paging_iter():
                if str(_field(note, "status") or "") == "void":
                    continue
                if _field(_field(note, "metadata") or {}, "refund_key") == refund_key:
                    return RefundResult(int(_field(note, "amount") or 0),
                                        str(_field(note, "id") or ""))
                credited += int(_field(note, "amount") or 0)
            paid = int(_field(invoice, "amount_paid") or 0)
            creditable = max(0, int(_field(invoice, "total") or paid) - credited)
            target = _invoice_payment(stripe, invoice)
            already = 0
            if target is not None:
                for refund in stripe.Refund.list(limit=100, **target).auto_paging_iter():
                    if str(_field(refund, "status") or "") in ("failed", "canceled"):
                        continue
                    if _field(_field(refund, "metadata") or {}, "refund_key") == refund_key:
                        return RefundResult(int(_field(refund, "amount") or 0))
                    already += int(_field(refund, "amount") or 0)
            owed = max(0, min(amount_cents, paid) - already)
            if target is None:
                # Paid without a payment to refund: from the customer's credit
                # balance, or marked paid by hand. A retry cannot change that.
                return RefundResult(0, unrefunded_cents=owed,
                                    reason="paid without a refundable payment")
            amount = min(owed, creditable)
            short = owed - amount
            if amount <= 0:
                if already:
                    _log.info("Refund of %s: %s cents owed, %s already refunded "
                              "on its payment (by hand, or earlier)",
                              subscription_id, amount_cents, already)
                return RefundResult(0, unrefunded_cents=short,
                                    reason="the invoice was already credited" if short else "")
            note = stripe.CreditNote.create(
                invoice=invoice_id,
                amount=amount,
                refund_amount=amount,
                memo="Withdrawal: the unused part of the period is refunded.",
                metadata={"refund_key": refund_key, "subscription": subscription_id},
                idempotency_key=f"{refund_key}:{attempt}",
            )
        except GatewayError:
            raise
        except Exception as exc:
            _log.warning("Stripe refund failed for %s: %s", subscription_id, exc)
            if _is_definite_refusal(exc):
                raise PermanentGatewayError(str(exc)) from exc
            raise GatewayError(str(exc)) from exc
        _log.info("Stripe credit note %s: refunded %s cents of %s (%s already "
                  "refunded; key %s:%s)", _field(note, "id"), amount,
                  subscription_id, already, refund_key, attempt)
        return RefundResult(amount, str(_field(note, "id") or ""),
                            unrefunded_cents=short,
                            reason="the invoice was already credited" if short else "")

    def subscriptions_in_force(self, customer_id: str) -> list[str]:
        """The customer's subscriptions still in force at Stripe (#441)."""
        stripe = _stripe()
        if not customer_id:
            return []
        try:
            subscriptions = stripe.Subscription.list(
                customer=customer_id, status="all", limit=100
            )
            return [
                str(_field(sub, "id") or "")
                for sub in subscriptions.auto_paging_iter()
                if str(_field(sub, "status") or "") in IN_FORCE_STATUSES
            ]
        except Exception as exc:
            _log.warning("Stripe listing failed for customer %s: %s", customer_id, exc)
            raise GatewayError(str(exc)) from exc

    def discard_pending_items(self, customer_id: str, subscription_id: str) -> int:
        """Delete ``subscription_id``'s pending invoice items (#441)."""
        stripe = _stripe()
        if not customer_id or not subscription_id:
            return 0
        deleted = 0
        try:
            items = stripe.InvoiceItem.list(customer=customer_id, pending=True, limit=100)
            for item in items.auto_paging_iter():
                if _item_subscription(item) != subscription_id:
                    continue
                try:
                    stripe.InvoiceItem.delete(str(_field(item, "id") or ""))
                    deleted += 1
                except Exception as exc:
                    if not _is_missing(exc):  # gone meanwhile is fine
                        raise
        except Exception as exc:
            _log.warning("Stripe pending items of %s could not be removed: %s",
                         subscription_id, exc)
            raise GatewayError(str(exc)) from exc
        if deleted:
            _log.info("Stripe: removed %s pending item(s) of %s", deleted, subscription_id)
        return deleted

    def latest_subscription(self, customer_id: str) -> tuple[str, float] | None:
        """The customer's most recently started subscription that was paid for."""
        stripe = _stripe()
        if not customer_id:
            return None
        try:
            subscriptions = stripe.Subscription.list(
                customer=customer_id, status="all", limit=100
            )
            started = [
                (str(_field(sub, "id") or ""), float(_field(sub, "start_date") or 0))
                for sub in subscriptions.auto_paging_iter()
                if str(_field(sub, "status") or "") not in _NEVER_PAID
            ]
        except Exception as exc:
            _log.warning("Stripe listing failed for customer %s: %s", customer_id, exc)
            raise GatewayError(str(exc)) from exc
        return max(started, key=lambda s: s[1]) if started else None

    @staticmethod
    def _require_customer(stripe, customer_id: str) -> bool:
        """Check the provider knows ``customer_id``. False if it was deleted.

        No-op (True) without a customer id. A missing customer means this key
        belongs to another Stripe account than the one that created it, and
        every "not found" after that proves nothing — so it is an error, never
        a success.
        """
        if not customer_id:
            return True
        try:
            customer = stripe.Customer.retrieve(customer_id)
        except Exception as exc:
            if _is_missing(exc):
                _log.warning(
                    "Stripe does not know customer %s — STRIPE_SECRET_KEY looks "
                    "like it belongs to another account", customer_id,
                )
                raise GatewayError(f"No such customer {customer_id}") from exc
            _log.warning("Stripe customer lookup failed for %s: %s", customer_id, exc)
            raise GatewayError(str(exc)) from exc
        return not _field(customer, "deleted")

    @staticmethod
    def _expire_session(stripe, session_id: str) -> None:
        """Expire an open checkout session, so it can no longer be paid.

        A session completed or expired since it was listed refuses; that is
        fine as long as it is no longer open.
        """
        try:
            stripe.checkout.Session.expire(session_id)
            return
        except Exception as exc:
            expire_error = exc
        try:
            session = stripe.checkout.Session.retrieve(session_id)
        except Exception as exc:
            raise GatewayError(str(expire_error)) from expire_error
        if str(_field(session, "status") or "") == "open":
            _log.warning("Stripe session %s could not be expired: %s",
                         session_id, expire_error)
            raise GatewayError(str(expire_error)) from expire_error


#: Stripe subscription statuses that will never bill again.
_ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})


def _is_terms_url_missing(exc: Exception) -> bool:
    """True for Stripe refusing ``consent_collection`` for want of a ToS URL.

    Recognised by the parameter Stripe blames, and failing that by its
    message: Stripe does not document a dedicated error code for it. The
    account API does not expose the setting either, so this cannot be checked
    ahead of time (docs/BILLING.md, Stripe setup step 5).
    """
    param = str(getattr(exc, "param", "") or "")
    if param.startswith("consent_collection"):
        return True
    return "terms of service" in str(exc).lower()


#: Statuses of a subscription still in force: it may renew or bill. Matches
#: migration 3828d92db32c and ``subscriptions.IN_FORCE_STATUSES``.
IN_FORCE_STATUSES = frozenset({"active", "trialing", "past_due", "unpaid", "paused"})


def _is_definite_refusal(exc: Exception) -> bool:
    """True when Stripe answered, and refused for good: a 4xx other than a
    conflict (409, e.g. the idempotency key in use by a concurrent request)
    or a rate limit (429). Timeouts, connection errors and 5xx are not: the
    request may have succeeded, and must be retried under the same key."""
    status = getattr(exc, "http_status", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (409, 429)


#: Statuses of a subscription whose first payment never went through.
_NEVER_PAID = frozenset({"incomplete", "incomplete_expired"})


def _utc_hour() -> str:
    """The current UTC hour, as the suffix of a provider idempotency key."""
    return time.strftime("%Y%m%d%H", time.gmtime())


def _item_subscription(item) -> str:
    """The subscription an invoice item came from, in either API shape."""
    found = _object_id(_field(item, "subscription"))
    if found:
        return found
    details = _field(_field(item, "parent") or {}, "subscription_details") or {}
    return _object_id(_field(details, "subscription"))


def _ended_at(sub) -> float:
    """When a subscription ended, unix seconds; 0 while it can still run.

    ``ended_at``, not ``canceled_at``: a cancellation scheduled for the end of
    the period sets ``canceled_at`` when it is asked for, but the service runs
    on until ``ended_at``, and the unused part is measured from there.
    """
    if str(_field(sub, "status") or "") not in _ENDED_STATUSES:
        return 0.0
    return float(_field(sub, "ended_at") or _field(sub, "canceled_at") or 0)


def _latest_paid_invoice(stripe, subscription_id: str):
    """The subscription's most recent paid invoice, or None (newest first)."""
    found = stripe.Invoice.list(
        subscription=subscription_id, status="paid", limit=1
    )["data"] or []
    return found[0] if found else None


def _invoice_period(invoice) -> tuple[float, float]:
    """The service period an invoice paid for.

    Read off its line items: the invoice's own ``period_start``/``period_end``
    describe the *previous* period, for usage billing, and on a first invoice
    are both its creation time. They are only the fallback.
    """
    lines = _field(_field(invoice, "lines") or {}, "data") or []
    periods = [_field(line, "period") for line in lines]
    starts = [float(_field(p, "start") or 0) for p in periods if p]
    ends = [float(_field(p, "end") or 0) for p in periods if p]
    if starts and ends:
        return min(starts), max(ends)
    return (float(_field(invoice, "period_start") or 0),
            float(_field(invoice, "period_end") or 0))


def _object_id(value) -> str:
    """An id, whether the payload carries it bare or as an expanded object."""
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return str(_field(value, "id") or "")


def _invoice_payment(stripe, invoice) -> dict | None:
    """``{"payment_intent": id}`` or ``{"charge": id}`` that paid ``invoice``.

    API versions before 2025 carried them on the invoice; later ones list the
    payments separately (``InvoicePayment``). None when it was paid without
    one.
    """
    for field in ("payment_intent", "charge"):
        found = _object_id(_field(invoice, field))
        if found:
            return {field: found}
    payments = stripe.InvoicePayment.list(
        invoice=str(_field(invoice, "id") or ""), status="paid", limit=10
    )
    for invoice_payment in payments.auto_paging_iter():
        payment = _field(invoice_payment, "payment") or {}
        for field in ("payment_intent", "charge"):
            found = _object_id(_field(payment, field))
            if found:
                return {field: found}
    return None


def _is_missing(exc: Exception) -> bool:
    """True for Stripe's "No such …" (``resource_missing``)."""
    return getattr(exc, "code", None) == "resource_missing"

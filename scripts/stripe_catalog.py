#!/usr/bin/env python
"""Provision the Stripe product/price catalogue for the paid tiers (issue #121).

The dashboard can do this in a few clicks, which is exactly the problem: those
clicks are unrecorded, unrepeatable, and have to be performed twice — once in
test mode and once in live. The three `price_...` ids they produce are what
`STRIPE_PRICE_TIER_1..3` point at, and a wrong one silently grants Tier 1
(see docs/BILLING.md). So the catalogue is declared here and applied by running
this, against either mode, as many times as you like.

**Names and descriptions are not written here.** They come from
`src.billing.plans`, the same module the server enforces and the pricing UI
renders, so a Stripe product can never advertise limits the server does not
grant. Only the amounts live in this file — Stripe is the one place that has to
know what to charge.

It also provisions the **customer portal configuration**, because
`POST /api/billing/change-plan` (issue #153) is only as correct as that is: the
in-app tier switch opens a portal flow, and the flow refuses a price the
configuration does not list. Declaring it here is what makes "a downgrade takes
effect at the end of the paid period" a fact in the repository rather than a
setting somebody remembers toggling.

Idempotency has three handles:

* **Products** use a caller-supplied id (`traxjourney_tier_1`), so re-running
  updates the existing product rather than creating a second one.
* **Prices** use a `lookup_key`. A price's amount, currency and interval are
  **immutable** in Stripe — repricing is not an edit. So when an amount changes
  this creates a *new* price, moves the lookup key onto it
  (`transfer_lookup_key`), and archives the old one. Subscriptions already on
  the old price keep paying the old amount; Stripe does not re-price anyone
  behind your back, and neither does this script. Prices are tax-inclusive
  (``TAX_BEHAVIOR``); a price with no tax behaviour yet is given it in place,
  the one change Stripe allows on an existing price.
* **The portal configuration** carries `metadata.managed_by`, since Stripe
  assigns its id — the run finds its own configuration by that mark instead of
  creating another one each time.

Dry run by default — it prints what it would do and touches nothing. Pass
``--apply`` to write, and additionally ``--live`` if the key is a live one:
building a live catalogue from guessed numbers leaves archived prices you can
never delete.

    export STRIPE_SECRET_KEY=sk_test_...
    python scripts/stripe_catalog.py                # show the plan
    python scripts/stripe_catalog.py --apply        # create/update
    python scripts/stripe_catalog.py --apply --live # against sk_live_...

On success it prints the `STRIPE_PRICE_TIER_*` block to copy into the
environment.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Run from anywhere: the catalogue is derived from src.billing.plans.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.billing import plans  # noqa: E402

#: Single currency for now. The lookup key encodes it so a second currency can
#: be added later without colliding with these. Owned by plans.py, because the
#: server now resolves prices by that key at runtime (issue #154) — the two must
#: agree on the format or nothing resolves.
CURRENCY = plans.LOOKUP_CURRENCY
INTERVAL = "month"

#: What to charge, in the smallest currency unit (99 = €0.99). The only numbers
#: in the catalogue that plans.py does not already own.
AMOUNTS_CENTS = {
    plans.TIER_1: 99,
    plans.TIER_2: 399,
    plans.TIER_3: 999,
}

#: The amounts above include VAT (issue #441): EU consumer prices must be shown
#: tax-inclusive, so with Stripe Tax on (``STRIPE_AUTOMATIC_TAX``) the VAT is
#: carved out of €3.99 rather than added to it.
TAX_BEHAVIOR = "inclusive"


def product_id(plan: str) -> str:
    return f"traxjourney_{plan}"


def lookup_key(plan: str) -> str:
    """Delegates to plans.py — one definition, shared with the server."""
    return plans.price_lookup_key(plan)


def env_var(plan: str) -> str:
    """The environment variable the server reads this price id from."""
    return f"STRIPE_PRICE_{plan.upper()}"


def description_for(plan: str) -> str:
    """Product blurb, generated from the plan limits so it cannot drift."""
    return " · ".join(plans.features_for(plan))


def _stripe(secret: str):
    import stripe

    stripe.api_key = secret
    return stripe


def _sync_product(stripe, plan: str, *, apply: bool) -> list[str]:
    """Create or update the product for ``plan``. Returns lines to report."""
    pid, name, blurb = product_id(plan), plans.plan_name(plan), description_for(plan)
    try:
        existing = stripe.Product.retrieve(pid)
    except stripe.error.InvalidRequestError:
        existing = None

    if existing is None:
        if apply:
            stripe.Product.create(
                id=pid, name=name, description=blurb,
                metadata={"plan": plan, "managed_by": "scripts/stripe_catalog.py"},
            )
        return [f"  product {pid}: CREATE  {name!r}"]

    # Index rather than .get(): Stripe's objects expose resource methods as
    # attributes, so `.get` is the API's GET helper, not the dict accessor.
    if existing["name"] == name and existing["description"] == blurb:
        return [f"  product {pid}: ok"]
    if apply:
        stripe.Product.modify(pid, name=name, description=blurb)
    return [f"  product {pid}: UPDATE name/description"]


def _sync_price(stripe, plan: str, *, apply: bool) -> tuple[str, list[str]]:
    """Ensure a price with the right amount exists. Returns (price_id, report)."""
    key, amount = lookup_key(plan), AMOUNTS_CENTS[plan]
    found = stripe.Price.list(lookup_keys=[key], limit=1)["data"] or []
    current = found[0] if found else None

    if current is not None:
        # A one-off price carrying this lookup key has recurring=None; treat it
        # as a mismatch rather than crashing, so the replace path fixes it.
        recurring = current["recurring"]
        matches = (
            current["unit_amount"] == amount
            and current["currency"] == CURRENCY
            and recurring is not None
            and recurring["interval"] == INTERVAL
        )
        # A price made before tax behaviour was declared has none on record.
        tax = str(_field(current, "tax_behavior") or "unspecified")
        if matches and tax == TAX_BEHAVIOR:
            return current["id"], [f"  price   {key}: ok  ({amount / 100:.2f} {CURRENCY.upper()})"]
        if matches and tax == "unspecified":
            # The one field Stripe lets you set on an existing price, once.
            # Setting it in place rather than replacing the price is what
            # reaches existing subscribers: a replacement would leave them on
            # the old price, which Stripe Tax would then tax by the account's
            # default — possibly adding VAT on top of what they were charged.
            if apply:
                stripe.Price.modify(current["id"], tax_behavior=TAX_BEHAVIOR)
            return current["id"], [
                f"  price   {key}: SET tax_behavior {tax} → {TAX_BEHAVIOR} "
                f"on {current['id']} (in place; subscribers keep it)"
            ]
        # Immutable otherwise — reprice by replacing, and carry the lookup key
        # across so nothing else has to be repointed. Also the only way off an
        # "exclusive" price: once declared, tax behaviour cannot change.
        was = current["unit_amount"]
        change = f"{was / 100:.2f} → {amount / 100:.2f} {CURRENCY.upper()}"
        if tax != TAX_BEHAVIOR:
            change += f", tax_behavior {tax} → {TAX_BEHAVIOR}"
        if not apply:
            return "(new)", [
                f"  price   {key}: REPLACE {change} (archives {current['id']})"
            ]
        new = stripe.Price.create(
            product=product_id(plan), unit_amount=amount, currency=CURRENCY,
            recurring={"interval": INTERVAL}, lookup_key=key,
            transfer_lookup_key=True, nickname=f"{plans.plan_name(plan)} monthly ({CURRENCY.upper()})",
            metadata={"plan": plan}, tax_behavior=TAX_BEHAVIOR,
        )
        stripe.Price.modify(current["id"], active=False)
        stripe.Product.modify(product_id(plan), default_price=new["id"])
        return new["id"], [
            f"  price   {key}: REPLACED {change}; archived {current['id']}"
        ]

    if not apply:
        return "(new)", [
            f"  price   {key}: CREATE  {amount / 100:.2f} {CURRENCY.upper()}/{INTERVAL} "
            f"(tax {TAX_BEHAVIOR})"
        ]
    new = stripe.Price.create(
        product=product_id(plan), unit_amount=amount, currency=CURRENCY,
        recurring={"interval": INTERVAL}, lookup_key=key,
        nickname=f"{plans.plan_name(plan)} monthly ({CURRENCY.upper()})",
        metadata={"plan": plan}, tax_behavior=TAX_BEHAVIOR,
    )
    stripe.Product.modify(product_id(plan), default_price=new["id"])
    return new["id"], [f"  price   {key}: CREATED {new['id']}"]


#: Marks the portal configuration this script owns, so re-running updates it
#: rather than stacking up a new one per run. Same idea as the caller-supplied
#: product ids: an idempotency handle we choose, not one Stripe assigns.
_MANAGED_BY = "scripts/stripe_catalog.py"


def _field(obj, name: str):
    """Read a field off a Stripe SDK object, or a plain dict.

    ``StripeObject`` does **not** subclass ``dict``: ``.get`` is the API's GET
    helper, so ``config.get("managed_by")`` raises ``AttributeError`` instead of
    returning the value — and nested reads return ``StripeObject`` too, so it is
    the *second* access that usually bites. Subscripting is the supported form,
    and a missing key raises, hence the membership check.

    The same helper, for the same reason, as ``src.billing.stripe_gateway._field``
    — duplicated rather than imported so this script keeps running with nothing
    but ``src.billing.plans`` on the path.
    """
    if obj is None:
        return None
    return obj[name] if name in obj else None


def _managed_by(config) -> str:
    """``metadata.managed_by`` of a portal configuration, "" when absent."""
    return str(_field(_field(config, "metadata"), "managed_by") or "")


def _portal_features(price_ids: dict[str, str]) -> dict:
    """Customer-portal behaviour, declared here rather than clicked in a dashboard.

    ``POST /api/billing/change-plan`` opens a ``subscription_update_confirm``
    flow, and that flow only works if the portal configuration permits price
    updates on these products — so the endpoint is only as correct as this is.

    The two rules worth reading:

    * ``schedule_at_period_end`` on ``decreasing_item_amount`` — a **downgrade
      takes effect at the end of the paid period**, not immediately. It matches
      what ``entitlements.plan_from_subscription`` already does for a
      cancellation: they paid for that time, so they keep it. Upgrades stay
      immediate, prorated, and are what ``create_prorations`` bills for.
    * ``subscription_cancel.mode = at_period_end`` — same rule, same reason.
    """
    return {
        "subscription_update": {
            "enabled": True,
            "default_allowed_updates": ["price", "promotion_code"],
            "products": [
                {"product": product_id(plan), "prices": [price_ids[plan]]}
                for plan in plans.PAID_PLANS
            ],
            "proration_behavior": "create_prorations",
            "schedule_at_period_end": {
                "conditions": [{"type": "decreasing_item_amount"}],
            },
        },
        "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
        "payment_method_update": {"enabled": True},
        "invoice_history": {"enabled": True},
        "customer_update": {
            "enabled": True,
            "allowed_updates": ["email", "address", "tax_id"],
        },
    }


def _sync_portal_configuration(
    stripe, price_ids: dict[str, str], *, apply: bool
) -> list[str]:
    """Create or update the customer-portal configuration. Returns report lines.

    Must run *after* the prices, since it names them.
    """
    if any(not pid or pid == "(new)" for pid in price_ids.values()):
        return ["  portal  : SKIP (prices not created yet — re-run with --apply)"]

    features = _portal_features(price_ids)
    existing = None
    for config in stripe.billing_portal.Configuration.list(limit=100)["data"]:
        if _managed_by(config) == _MANAGED_BY:
            existing = config
            break

    if existing is None:
        if apply:
            created = stripe.billing_portal.Configuration.create(
                features=features,
                business_profile={"headline": "TraxJourney"},
                metadata={"managed_by": _MANAGED_BY},
            )
            return [f"  portal  : CREATED {created['id']}"]
        return ["  portal  : CREATE  price updates + cancel, both at period end"]

    if apply:
        stripe.billing_portal.Configuration.modify(existing["id"], features=features)
    # No diffing: the feature block is nested deeply enough that comparing it
    # would be more code than the update it saves, and a modify with identical
    # values is a no-op at Stripe.
    return [f"  portal  : UPDATE  {existing['id']}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="perform the writes (default: dry run)")
    parser.add_argument("--live", action="store_true",
                        help="permit a live-mode key; required with sk_live_")
    args = parser.parse_args(argv)

    secret = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        print("STRIPE_SECRET_KEY is not set", file=sys.stderr)
        return 2

    is_live = secret.startswith("sk_live_")
    if is_live and not args.live:
        print("refusing: STRIPE_SECRET_KEY is a live key; pass --live to confirm",
              file=sys.stderr)
        return 2

    stripe = _stripe(secret)
    mode = "LIVE" if is_live else "test"
    print(f"Stripe catalogue — {mode} mode — {'applying' if args.apply else 'dry run'}\n")

    price_ids: dict[str, str] = {}
    for plan in plans.PAID_PLANS:
        print(f"{plan} ({plans.plan_name(plan)})")
        for line in _sync_product(stripe, plan, apply=args.apply):
            print(line)
        pid, report = _sync_price(stripe, plan, apply=args.apply)
        for line in report:
            print(line)
        price_ids[plan] = pid
        print()

    print("customer portal")
    for line in _sync_portal_configuration(stripe, price_ids, apply=args.apply):
        print(line)
    print()

    print("Environment:")
    for plan in plans.PAID_PLANS:
        print(f"{env_var(plan)}={price_ids[plan]}")
    if not args.apply:
        print("\n(dry run — nothing was written; re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

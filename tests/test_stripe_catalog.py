"""The provisioner's customer-portal configuration (issue #153).

``POST /api/billing/change-plan`` opens a ``subscription_update_confirm`` portal
flow, and that flow is refused unless the account's portal configuration lists
the price being switched to. So this configuration is not cosmetic — it is the
other half of the endpoint, and the half that used to be a sequence of dashboard
clicks nobody could review.

The two rules asserted hardest are the ones a reader of `docs/BILLING.md` is
entitled to rely on: a downgrade lands at the end of the paid period, and so
does a cancellation.

**The configurations here are real ``StripeObject`` instances, not dicts.** The
first version of this file used dicts and passed while the script crashed
against Stripe on the first run: ``StripeObject`` does not subclass ``dict``, so
``config["metadata"].get("managed_by")`` reaches the SDK's GET helper and raises
``AttributeError``. That is the same trap ``test_billing_stripe_gateway.py``
exists to catch, and a dict fake walks straight past it.
"""
from __future__ import annotations

import types

import pytest
from stripe import StripeObject

from scripts import stripe_catalog as cat
from src.billing import plans


def _obj(**fields) -> StripeObject:
    """A real StripeObject — the type the SDK actually returns."""
    return StripeObject.construct_from(dict(fields), "sk_test_x")


@pytest.fixture
def price_ids() -> dict[str, str]:
    return {plan: f"price_{plan}" for plan in plans.PAID_PLANS}


class TestPortalFeatures:
    def test_every_paid_tier_is_switchable(self, price_ids):
        """A tier missing here is a tier the in-app switch cannot reach."""
        products = cat._portal_features(price_ids)["subscription_update"]["products"]
        assert [p["product"] for p in products] == [
            cat.product_id(plan) for plan in plans.PAID_PLANS
        ]
        assert [p["prices"] for p in products] == [
            [price_ids[plan]] for plan in plans.PAID_PLANS
        ]

    def test_price_updates_are_allowed(self, price_ids):
        update = cat._portal_features(price_ids)["subscription_update"]
        assert update["enabled"] is True
        assert "price" in update["default_allowed_updates"]

    def test_a_downgrade_is_scheduled_for_the_end_of_the_paid_period(self, price_ids):
        """The documented promise: they paid for that time, so they keep it —
        the same rule entitlements.plan_from_subscription applies to a
        cancellation."""
        update = cat._portal_features(price_ids)["subscription_update"]
        conditions = update["schedule_at_period_end"]["conditions"]
        assert {"type": "decreasing_item_amount"} in conditions

    def test_an_upgrade_is_immediate_and_prorated(self, price_ids):
        """Only a *decreasing* amount waits; anything else applies at once, and
        create_prorations is what charges the difference."""
        update = cat._portal_features(price_ids)["subscription_update"]
        assert update["proration_behavior"] == "create_prorations"
        types_ = {c["type"] for c in update["schedule_at_period_end"]["conditions"]}
        assert "shortening_interval" not in types_

    def test_cancelling_also_waits_for_the_period_to_end(self, price_ids):
        cancel = cat._portal_features(price_ids)["subscription_cancel"]
        assert cancel == {"enabled": True, "mode": "at_period_end"}

    def test_invoices_are_reachable(self, price_ids):
        """"Manage billing" is where a customer goes for their receipts."""
        assert cat._portal_features(price_ids)["invoice_history"]["enabled"] is True


class _FakeConfiguration:
    """Records writes; answers `list` from whatever it was seeded with.

    Everything it hands back is a ``StripeObject``, because that is what the SDK
    hands back — see the module docstring.
    """

    def __init__(self, existing: list[StripeObject] | None = None):
        self.existing = existing or []
        self.created: list[dict] = []
        self.modified: list[tuple[str, dict]] = []

    def list(self, *, limit):
        return _obj(data=list(self.existing))

    def create(self, **params):
        self.created.append(params)
        return _obj(id="bpc_new")

    def modify(self, config_id, **params):
        self.modified.append((config_id, params))
        return _obj(id=config_id)


def _stripe(configuration: _FakeConfiguration):
    return types.SimpleNamespace(
        billing_portal=types.SimpleNamespace(Configuration=configuration)
    )


class TestSyncPortalConfiguration:
    def test_a_dry_run_writes_nothing(self, price_ids):
        config = _FakeConfiguration()
        report = cat._sync_portal_configuration(
            _stripe(config), price_ids, apply=False)
        assert config.created == [] and config.modified == []
        assert any("CREATE" in line for line in report)

    def test_creates_one_when_the_account_has_none_of_ours(self, price_ids):
        config = _FakeConfiguration()
        cat._sync_portal_configuration(_stripe(config), price_ids, apply=True)
        assert len(config.created) == 1
        assert config.created[0]["metadata"]["managed_by"] == cat._MANAGED_BY

    def test_reuses_its_own_configuration_on_a_second_run(self, price_ids):
        """Stripe assigns the id, so without the marker every run would leave
        another configuration behind."""
        config = _FakeConfiguration([
            _obj(id="bpc_mine",
                 metadata=_obj(managed_by=cat._MANAGED_BY)),
        ])
        cat._sync_portal_configuration(_stripe(config), price_ids, apply=True)
        assert config.created == []
        assert config.modified[0][0] == "bpc_mine"

    def test_leaves_a_hand_made_configuration_alone(self, price_ids):
        """Someone else's portal configuration is not ours to rewrite."""
        config = _FakeConfiguration([_obj(id="bpc_theirs", metadata=_obj())])
        cat._sync_portal_configuration(_stripe(config), price_ids, apply=True)
        assert config.modified == []
        assert len(config.created) == 1

    def test_tolerates_a_configuration_with_no_metadata(self, price_ids):
        config = _FakeConfiguration([_obj(id="bpc_old", metadata=None)])
        cat._sync_portal_configuration(_stripe(config), price_ids, apply=True)
        assert len(config.created) == 1

    def test_tolerates_a_configuration_with_no_metadata_field_at_all(
        self, price_ids
    ):
        """Reading a key a StripeObject does not have raises; ``_field`` is what
        keeps that from taking the whole run down."""
        config = _FakeConfiguration([_obj(id="bpc_bare")])
        cat._sync_portal_configuration(_stripe(config), price_ids, apply=True)
        assert len(config.created) == 1


class TestFieldAccess:
    """The trap that broke the first live run of this script."""

    def test_stripe_objects_have_no_dict_get(self):
        with pytest.raises(AttributeError):
            _obj(managed_by="x").get("managed_by")

    def test_nested_reads_return_stripe_objects_not_dicts(self):
        """Why the bug survived review: the *outer* read looks fine."""
        config = _obj(metadata=_obj(managed_by="x"))
        assert isinstance(config["metadata"], StripeObject)
        with pytest.raises(AttributeError):
            config["metadata"].get("managed_by")

    def test_managed_by_reads_through_both_levels(self):
        config = _obj(metadata=_obj(managed_by=cat._MANAGED_BY))
        assert cat._managed_by(config) == cat._MANAGED_BY

    @pytest.mark.parametrize("config", [
        _obj(id="x"),                    # no metadata key at all
        _obj(id="x", metadata=None),     # metadata explicitly null
        _obj(id="x", metadata=_obj()),   # metadata present but empty
    ])
    def test_managed_by_is_empty_rather_than_raising(self, config):
        assert cat._managed_by(config) == ""

    def test_plain_dicts_still_work(self):
        """A dry run and the tests both hand it ordinary dicts."""
        assert cat._managed_by({"metadata": {"managed_by": "z"}}) == "z"

    def test_skips_when_the_prices_do_not_exist_yet(self):
        """A dry run reports "(new)" for prices it did not create; naming those
        in a portal configuration would fail."""
        config = _FakeConfiguration()
        report = cat._sync_portal_configuration(
            _stripe(config), {plan: "(new)" for plan in plans.PAID_PLANS},
            apply=True)
        assert config.created == [] and config.modified == []
        assert any("SKIP" in line for line in report)


# ── Tax-inclusive prices (issue #441) ────────────────────────────────────────

class _FakePrices:
    """``stripe.Price`` / ``stripe.Product`` for one plan's price, as StripeObjects."""

    def __init__(self, current: StripeObject | None):
        self.current = current
        self.created: list[dict] = []
        self.modified: list[tuple[str, dict]] = []
        self.product_modified: list[tuple[str, dict]] = []
        fake = self

        class Price:
            @staticmethod
            def list(**params):
                return _obj(data=[fake.current] if fake.current else [])

            @staticmethod
            def create(**params):
                fake.created.append(params)
                return _obj(id="price_new", **{k: v for k, v in params.items()
                                               if k in ("unit_amount", "currency")})

            @staticmethod
            def modify(price_id, **params):
                fake.modified.append((price_id, params))
                return _obj(id=price_id)

        class Product:
            @staticmethod
            def modify(product_id, **params):
                fake.product_modified.append((product_id, params))

        self.Price = Price
        self.Product = Product


def _price(*, amount=None, tax_behavior="unspecified", plan=plans.TIER_2,
           omit_tax=False) -> StripeObject:
    fields = dict(
        id="price_old", unit_amount=cat.AMOUNTS_CENTS[plan] if amount is None else amount,
        currency=cat.CURRENCY, recurring=_obj(interval=cat.INTERVAL),
    )
    if not omit_tax:
        fields["tax_behavior"] = tax_behavior
    return _obj(**fields)


class TestTaxInclusivePrices:
    PLAN = plans.TIER_2

    def test_prices_are_declared_tax_inclusive(self):
        assert cat.TAX_BEHAVIOR == "inclusive"

    def test_a_new_price_is_created_tax_inclusive(self):
        fake = _FakePrices(None)
        cat._sync_price(fake, self.PLAN, apply=True)
        assert fake.created[0]["tax_behavior"] == "inclusive"

    def test_the_dry_run_says_so_for_a_new_price(self):
        fake = _FakePrices(None)
        _, report = cat._sync_price(fake, self.PLAN, apply=False)
        assert fake.created == []
        assert "tax inclusive" in report[0]

    def test_an_inclusive_price_with_the_right_amount_is_left_alone(self):
        fake = _FakePrices(_price(tax_behavior="inclusive"))
        pid, report = cat._sync_price(fake, self.PLAN, apply=True)
        assert pid == "price_old"
        assert fake.created == [] and fake.modified == []
        assert ": ok" in report[0]

    @pytest.mark.parametrize("omit", [False, True])
    def test_an_untagged_price_is_tagged_in_place(self, omit):
        """The one change Stripe allows on a price. In place, so existing
        subscribers — who stay on this price — become inclusive too."""
        fake = _FakePrices(_price(tax_behavior="unspecified", omit_tax=omit))
        pid, report = cat._sync_price(fake, self.PLAN, apply=True)
        assert pid == "price_old"
        assert fake.modified == [("price_old", {"tax_behavior": "inclusive"})]
        assert fake.created == []
        assert "SET tax_behavior unspecified → inclusive" in report[0]

    def test_the_dry_run_shows_the_in_place_change_and_writes_nothing(self):
        fake = _FakePrices(_price(tax_behavior="unspecified"))
        _, report = cat._sync_price(fake, self.PLAN, apply=False)
        assert fake.modified == [] and fake.created == []
        assert "SET tax_behavior unspecified → inclusive" in report[0]
        assert "price_old" in report[0]

    def test_an_exclusive_price_is_replaced_through_the_reprice_path(self):
        """Once declared, tax behaviour cannot change: a new price takes the
        lookup key, and the old one is archived."""
        fake = _FakePrices(_price(tax_behavior="exclusive"))
        pid, report = cat._sync_price(fake, self.PLAN, apply=True)
        assert pid == "price_new"
        (created,) = fake.created
        assert created["tax_behavior"] == "inclusive"
        assert created["transfer_lookup_key"] is True
        assert created["lookup_key"] == cat.lookup_key(self.PLAN)
        assert ("price_old", {"active": False}) in fake.modified
        assert fake.product_modified == [
            (cat.product_id(self.PLAN), {"default_price": "price_new"})]
        assert "tax_behavior exclusive → inclusive" in report[0]

    def test_the_dry_run_shows_the_replacement(self):
        fake = _FakePrices(_price(tax_behavior="exclusive"))
        pid, report = cat._sync_price(fake, self.PLAN, apply=False)
        assert pid == "(new)"
        assert fake.created == [] and fake.modified == []
        assert report[0].startswith(f"  price   {cat.lookup_key(self.PLAN)}: REPLACE")
        assert "tax_behavior exclusive → inclusive" in report[0]
        assert "archives price_old" in report[0]

    def test_a_reprice_creates_the_new_price_tax_inclusive(self):
        fake = _FakePrices(_price(amount=1, tax_behavior="unspecified"))
        cat._sync_price(fake, self.PLAN, apply=True)
        (created,) = fake.created
        assert created["tax_behavior"] == "inclusive"
        assert created["unit_amount"] == cat.AMOUNTS_CENTS[self.PLAN]
        assert ("price_old", {"active": False}) in fake.modified

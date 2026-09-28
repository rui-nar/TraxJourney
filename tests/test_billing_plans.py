"""Plan catalogue, limits and the pure entitlement rules (issue #121).

No database and no provider here — these are the rules that decide what a user
is allowed to do, exercised directly.
"""
from __future__ import annotations

import pytest

from src.billing import plans
from src.billing.entitlements import (
    billing_enabled,
    plan_from_subscription,
    quotas_enforced,
    subscription_is_live,
)
from src.billing.plans import (
    FREE,
    PLAN_ORDER,
    TIER_1,
    TIER_2,
    TIER_3,
    TOP_PLAN,
    cheapest_plan_with,
    features_for,
    is_at_least,
    limits_for,
    over_quota,
    plan_name,
    price_label,
)

_MB = 1024 * 1024
_GB = 1024 * _MB


@pytest.fixture(autouse=True)
def _clean_billing_env(monkeypatch):
    """Start every test from an unconfigured deployment."""
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    for prefix in ("FREE", "TIER_1", "TIER_2", "TIER_3"):
        for suffix in ("MAX_PROJECTS", "MAX_STORAGE_MB", "MAX_TRIP_DAYS",
                       "NAME", "PRICE_LABEL"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    yield


# ── Limits ────────────────────────────────────────────────────────────────────

class TestLimits:
    def test_free_defaults(self):
        limits = limits_for(FREE)
        assert limits.max_projects == 1
        assert limits.max_storage_bytes == 500 * _MB
        assert limits.max_trip_days == 10

    def test_tier_1_defaults(self):
        limits = limits_for(TIER_1)
        assert limits.max_projects == 2
        assert limits.max_storage_bytes == 5 * _GB
        assert limits.max_trip_days == 100

    def test_tier_2_defaults(self):
        limits = limits_for(TIER_2)
        assert limits.max_projects == 10
        assert limits.max_storage_bytes == 20 * _GB
        assert limits.max_trip_days == 365

    def test_tier_3_defaults(self):
        limits = limits_for(TIER_3)
        assert limits.max_projects is None       # unlimited trips
        assert limits.max_storage_bytes == 50 * _GB
        assert limits.max_trip_days is None      # trips of any length

    def test_every_limit_grows_with_the_tier(self):
        """A higher tier must never be worse at anything — otherwise the
        upgrade path is a downgrade for someone."""
        def rank(value):  # None = unlimited = the largest
            return float("inf") if value is None else value

        for weaker, stronger in zip(PLAN_ORDER, PLAN_ORDER[1:]):
            a, b = limits_for(weaker), limits_for(stronger)
            assert rank(b.max_projects) >= rank(a.max_projects)
            assert rank(b.max_storage_bytes) >= rank(a.max_storage_bytes)
            assert rank(b.max_trip_days) >= rank(a.max_trip_days)

    def test_unknown_plan_falls_back_to_free(self):
        """Fail safe: an unrecognised plan must not hand out paid limits."""
        assert limits_for("enterprise") == limits_for(FREE)

    def test_env_overrides_per_tier(self, monkeypatch):
        monkeypatch.setenv("TIER_1_MAX_PROJECTS", "3")
        monkeypatch.setenv("TIER_1_MAX_TRIP_DAYS", "120")
        monkeypatch.setenv("TIER_1_MAX_STORAGE_MB", "8192")
        limits = limits_for(TIER_1)
        assert (limits.max_projects, limits.max_trip_days) == (3, 120)
        assert limits.max_storage_bytes == 8 * _GB

    def test_env_can_make_a_limit_unlimited(self, monkeypatch):
        monkeypatch.setenv("FREE_MAX_TRIP_DAYS", "unlimited")
        assert limits_for(FREE).max_trip_days is None

    def test_garbage_env_falls_back_to_the_default(self, monkeypatch):
        """A typo in an env var must not silently remove a limit."""
        monkeypatch.setenv("FREE_MAX_TRIP_DAYS", "ten")
        assert limits_for(FREE).max_trip_days == 10

    def test_env_read_per_call_not_at_import(self, monkeypatch):
        assert limits_for(FREE).max_projects == 1
        monkeypatch.setenv("FREE_MAX_PROJECTS", "7")
        assert limits_for(FREE).max_projects == 7

    def test_as_dict_carries_all_three_limits(self):
        keys = set(limits_for(TIER_2).as_dict())
        assert keys == {"max_projects", "max_storage_bytes", "max_trip_days"}


class TestOverQuota:
    def test_under_the_limit(self):
        assert over_quota(used=10, incoming=5, limit=100) is False

    def test_exactly_on_the_limit_is_allowed(self):
        assert over_quota(used=95, incoming=5, limit=100) is False

    def test_one_past_the_limit(self):
        assert over_quota(used=95, incoming=6, limit=100) is True

    def test_none_limit_is_never_exceeded(self):
        assert over_quota(used=10**12, incoming=10**12, limit=None) is False

    def test_already_over_the_limit_blocks_further_writes(self):
        assert over_quota(used=200, incoming=1, limit=100) is True


class TestPlanOrder:
    def test_stronger_satisfies_weaker(self):
        assert is_at_least(TIER_2, TIER_1) is True

    def test_weaker_does_not_satisfy_stronger(self):
        assert is_at_least(TIER_1, TIER_3) is False

    def test_unknown_plan_satisfies_nothing(self):
        assert is_at_least("enterprise", FREE) is False

    def test_top_plan_is_the_strongest(self):
        assert TOP_PLAN == PLAN_ORDER[-1] == TIER_3


class TestCheapestPlanWith:
    def test_free_covers_a_small_need(self):
        assert cheapest_plan_with(projects=1, trip_days=10) == FREE

    def test_a_long_trip_needs_tier_1(self):
        assert cheapest_plan_with(trip_days=30) == TIER_1

    def test_a_very_long_trip_needs_tier_2(self):
        assert cheapest_plan_with(trip_days=200) == TIER_2

    def test_an_open_ended_trip_needs_tier_3(self):
        assert cheapest_plan_with(trip_days=500) == TIER_3

    def test_the_strictest_need_wins(self):
        """Short trip, huge photo library → storage decides the tier."""
        assert cheapest_plan_with(trip_days=5, storage_bytes=30 * _GB) == TIER_3

    def test_nothing_covers_it(self):
        assert cheapest_plan_with(storage_bytes=500 * _GB) is None

    def test_no_constraints_is_the_free_plan(self):
        assert cheapest_plan_with() == FREE


# ── Naming and pricing ────────────────────────────────────────────────────────

class TestNamesAndPrices:
    def test_default_names(self):
        assert plan_name(FREE) == "Free"
        assert plan_name(TIER_2) == "Tier 2"

    def test_name_is_overridable(self, monkeypatch):
        monkeypatch.setenv("TIER_2_NAME", "Voyager")
        assert plan_name(TIER_2) == "Voyager"

    def test_unknown_plan_reads_as_free(self):
        assert plan_name("enterprise") == "Free"

    def test_price_labels_are_overridable(self, monkeypatch):
        monkeypatch.setenv("TIER_3_PRICE_LABEL", "$12 / month")
        assert price_label(TIER_3) == "$12 / month"

    def test_default_labels_match_what_stripe_charges(self):
        """The label is cosmetic, so nothing else notices when it drifts from
        the amount — except a customer reading €1 and being billed €0.99.
        scripts/stripe_catalog.py holds the amounts; tie the two together."""
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parent.parent / "scripts" / "stripe_catalog.py"
        spec = importlib.util.spec_from_file_location("stripe_catalog", script)
        catalog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(catalog)

        for plan, cents in catalog.AMOUNTS_CENTS.items():
            assert price_label(plan) == f"€{cents / 100:.2f} / month", plan


    def test_paid_prices_rise_along_the_plan_order(self):
        """The landing page quotes the *first* purchasable plan as the cheapest
        ("from €0.99 / month", #432), trusting that plan order is price order.
        Pin that here, so a reprice that breaks it fails instead of quietly
        advertising a starting price that is not the lowest."""
        import re

        amounts = []
        for plan in plans.PAID_PLANS:
            match = re.search(r"(\d+(?:\.\d+)?)", price_label(plan))
            assert match, (plan, price_label(plan))
            amounts.append(float(match.group(1)))
        assert all(a < b for a, b in zip(amounts, amounts[1:])), amounts


class TestFeatures:
    def test_bullets_are_generated_from_the_limits(self, monkeypatch):
        """The bullets and the limits drifted apart when both were hand-written;
        they are one source now, so changing a limit changes the pricing page."""
        monkeypatch.setenv("FREE_MAX_TRIP_DAYS", "3")
        monkeypatch.setenv("FREE_MAX_PROJECTS", "4")
        bullets = features_for(FREE)
        assert "4 trips" in bullets
        assert "Up to 3 days per trip" in bullets

    def test_singular_trip(self):
        assert "1 trip" in features_for(FREE)

    def test_unlimited_reads_as_unlimited(self):
        bullets = features_for(TIER_3)
        assert "Unlimited trips" in bullets
        assert "Trips of any length" in bullets

    def test_storage_is_human_readable(self):
        assert "500 MB of photos" in features_for(FREE)
        assert "5 GB of photos" in features_for(TIER_1)


# ── Provider state → plan ─────────────────────────────────────────────────────

class TestPlanFromSubscription:
    def test_active_grants_the_plan(self):
        assert plan_from_subscription(TIER_2, "active", 0, now=1000) == TIER_2

    def test_trialing_grants_the_plan(self):
        assert plan_from_subscription(TIER_1, "trialing", 0, now=1000) == TIER_1

    def test_past_due_still_grants_during_the_retry_window(self):
        """A failed renewal starts a provider retry window; locking the account
        out on day one loses customers who just need to update a card."""
        assert plan_from_subscription(TIER_3, "past_due", 0, now=1000) == TIER_3

    def test_cancelled_keeps_access_until_the_period_ends(self):
        assert plan_from_subscription(TIER_2, "canceled", 2000, now=1000) == TIER_2

    def test_cancelled_drops_to_free_after_the_period_ends(self):
        assert plan_from_subscription(TIER_2, "canceled", 2000, now=3000) == FREE

    def test_incomplete_checkout_grants_nothing(self):
        assert plan_from_subscription(TIER_1, "incomplete", 0, now=1000) == FREE

    def test_no_plan_is_free(self):
        assert plan_from_subscription("", "active", 0, now=1000) == FREE


class TestPriceLookupKeys:
    """Account-independent handles for provider prices (issue #154)."""

    def test_round_trips_for_every_paid_plan(self):
        for plan in plans.PAID_PLANS:
            assert plans.plan_for_lookup_key(plans.price_lookup_key(plan)) == plan

    def test_keys_are_distinct(self):
        keys = {plans.price_lookup_key(p) for p in plans.PAID_PLANS}
        assert len(keys) == len(plans.PAID_PLANS)

    def test_the_key_carries_the_currency(self):
        """So a second currency can be added without colliding with these."""
        assert plans.price_lookup_key(TIER_2).endswith(f"_{plans.LOOKUP_CURRENCY}")

    def test_a_foreign_key_is_not_ours(self):
        assert plans.plan_for_lookup_key("someone_elses_key") is None
        assert plans.plan_for_lookup_key("") is None

    def test_the_provisioner_and_the_server_agree(self):
        """scripts/stripe_catalog.py stamps the key the server later resolves by.
        Two definitions would mean prices nothing can find."""
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parent.parent / "scripts" / "stripe_catalog.py"
        spec = importlib.util.spec_from_file_location("stripe_catalog", script)
        catalog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(catalog)

        for plan in plans.PAID_PLANS:
            assert catalog.lookup_key(plan) == plans.price_lookup_key(plan)


class TestSubscriptionIsLive:
    """"Live" is not the same question as "which plan is in force" (issue #163).

    Callers asking "would buying again create a second subscription" need this
    one; callers asking "what may this user do today" need plan_from_subscription.
    """

    @pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
    def test_live_statuses(self, status):
        assert subscription_is_live(status) is True

    def test_cancelled_is_not_live_even_while_it_still_grants_the_plan(self):
        """The distinction the whole helper exists for: nothing will renew it,
        so buying again is a new purchase rather than a duplicate."""
        assert subscription_is_live("canceled") is False
        assert plan_from_subscription(TIER_2, "canceled", 2000, now=1000) == TIER_2

    @pytest.mark.parametrize("status", ["incomplete", "unpaid", "", "nonsense"])
    def test_everything_else_is_not_live(self, status):
        assert subscription_is_live(status) is False


# ── Deployment switches ───────────────────────────────────────────────────────

class TestBillingEnabled:
    def test_off_by_default(self):
        """The self-hosting promise: no configuration, no billing."""
        assert billing_enabled() is False

    def test_inferred_from_stripe_keys(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
        assert billing_enabled() is True

    def test_explicit_flag_wins_over_inference(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
        monkeypatch.setenv("BILLING_ENABLED", "false")
        assert billing_enabled() is False

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("BILLING_ENABLED", "1")
        assert billing_enabled() is True


class TestQuotasEnforced:
    def test_off_when_billing_is_off(self, monkeypatch):
        monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
        assert quotas_enforced() is False

    def test_off_by_default_even_with_billing_on(self, monkeypatch):
        """Turning billing on measures usage; it does not start refusing writes
        for accounts that grew before the limits existed."""
        monkeypatch.setenv("BILLING_ENABLED", "1")
        assert quotas_enforced() is False

    def test_on_when_both_flags_are_set(self, monkeypatch):
        monkeypatch.setenv("BILLING_ENABLED", "1")
        monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "on")
        assert quotas_enforced() is True


class TestCatalogue:
    def test_lists_every_plan_weakest_first(self):
        assert [e["id"] for e in plans.catalogue()] == list(PLAN_ORDER)

    def test_entries_carry_limits_and_bullets(self, monkeypatch):
        monkeypatch.setenv("FREE_MAX_PROJECTS", "2")
        free = plans.catalogue()[0]
        assert free["limits"]["max_projects"] == 2
        assert free["features"][0] == "2 trips"

    def test_price_label_is_configurable(self, monkeypatch):
        monkeypatch.setenv("TIER_1_PRICE_LABEL", "$2 / month")
        tier_1 = [e for e in plans.catalogue() if e["id"] == TIER_1][0]
        assert tier_1["price_label"] == "$2 / month"

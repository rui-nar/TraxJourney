"""The Stripe gateway's own translation layer (issue #121).

These exist because the rest of the billing tests hand plain dicts straight to
:func:`subscription_update_from_event`, which skips
:mod:`src.billing.stripe_gateway` entirely. That gap hid a bug that broke every
payment route in production: ``StripeObject`` does **not** subclass ``dict``, so
``session.get("url")`` resolves to the SDK's GET helper and raises
``AttributeError``, and ``dict(event)`` raises ``KeyError: 0``.

So every case here drives the gateway with genuine ``StripeObject`` instances
rather than dicts. A dict would pass while the real SDK type fails, which is
exactly how the bug survived.
"""
from __future__ import annotations

import json
import time
import types

import pytest
from stripe import StripeObject

import src.billing.stripe_gateway as gw
from src.billing.gateway import GatewayError
from src.billing.plans import FREE, TIER_1, TIER_2, TIER_3, price_lookup_key
from src.billing.stripe_gateway import StripeGateway, _field


def _obj(**fields) -> StripeObject:
    """A real StripeObject — the type the SDK actually returns."""
    return StripeObject.construct_from(dict(fields), "sk_test_x")


@pytest.fixture(autouse=True)
def _price_env(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_TIER_1", "price_t1")
    monkeypatch.setenv("STRIPE_PRICE_TIER_2", "price_t2")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")


class TestFieldAccess:
    def test_stripe_objects_have_no_dict_get(self):
        """The trap itself: .get is the API helper, not the dict accessor."""
        with pytest.raises(AttributeError):
            _obj(url="https://x").get("url")

    def test_field_reads_present_and_missing(self):
        o = _obj(url="https://x", customer=None)
        assert _field(o, "url") == "https://x"
        assert _field(o, "customer") is None
        assert _field(o, "never_set") is None


class TestCheckoutSession:
    def _gateway(self, monkeypatch, session):
        created = {}

        class _Session:
            @staticmethod
            def create(**params):
                created.update(params)
                return session

        fake = types.SimpleNamespace(checkout=types.SimpleNamespace(Session=_Session))
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        return StripeGateway(), created

    def test_returns_url_and_customer_from_a_stripe_object(self, monkeypatch):
        session = _obj(url="https://checkout.stripe.com/c/pay/cs_test_1",
                       customer="cus_new")
        gateway, _ = self._gateway(monkeypatch, session)
        out = gateway.create_checkout_session(
            user_info_id=6, plan="tier_2", email="a@b.c", customer_id="",
            success_url="https://app/ok", cancel_url="https://app/no",
        )
        assert out["url"] == "https://checkout.stripe.com/c/pay/cs_test_1"
        assert out["customer_id"] == "cus_new"

    def test_keeps_the_existing_customer_when_the_session_omits_one(self, monkeypatch):
        gateway, params = self._gateway(monkeypatch, _obj(url="https://u", customer=None))
        out = gateway.create_checkout_session(
            user_info_id=6, plan="tier_2", email="a@b.c", customer_id="cus_old",
            success_url="https://app/ok", cancel_url="https://app/no",
        )
        assert out["customer_id"] == "cus_old"
        # An existing customer is reused rather than a second one created.
        assert params["customer"] == "cus_old"
        assert "customer_email" not in params

    def test_unconfigured_price_is_refused(self, monkeypatch):
        monkeypatch.delenv("STRIPE_PRICE_TIER_2", raising=False)
        gateway, _ = self._gateway(monkeypatch, _obj(url="https://u"))
        with pytest.raises(GatewayError):
            gateway.create_checkout_session(
                user_info_id=6, plan="tier_2", email="a@b.c", customer_id="",
                success_url="https://app/ok", cancel_url="https://app/no",
            )


class TestPlanChangeSession:
    """Switching tier through the provider's hosted flow (issue #153).

    Driven with real ``StripeObject`` instances for the same reason as the rest
    of this file: the subscription item id has to be dug out of a nested SDK
    object, which is exactly where the ``.get`` trap bites.
    """

    def _gateway(self, monkeypatch, *, items=("si_1",), retrieve_fails=False):
        created: dict = {}

        class _Session:
            @staticmethod
            def create(**params):
                created.update(params)
                return _obj(url="https://billing.stripe.com/p/session/flow_1")

        class _Subscription:
            @staticmethod
            def retrieve(sub_id):
                if retrieve_fails:
                    raise RuntimeError("no such subscription")
                return _obj(id=sub_id,
                            items=_obj(data=[_obj(id=i) for i in items]))

        fake = types.SimpleNamespace(
            billing_portal=types.SimpleNamespace(Session=_Session),
            Subscription=_Subscription,
        )
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        return StripeGateway(), created

    def test_opens_the_confirm_change_flow_for_the_target_price(self, monkeypatch):
        gateway, params = self._gateway(monkeypatch)
        out = gateway.create_plan_change_session(
            customer_id="cus_1", subscription_id="sub_1", plan=TIER_2,
            return_url="https://app/settings/plan",
        )
        assert out["url"] == "https://billing.stripe.com/p/session/flow_1"
        assert params["customer"] == "cus_1"
        flow = params["flow_data"]
        assert flow["type"] == "subscription_update_confirm"
        confirm = flow["subscription_update_confirm"]
        assert confirm["subscription"] == "sub_1"
        # The flow updates a subscription *item*: the id has to come off the
        # subscription, not be invented from the subscription id.
        assert confirm["items"] == [
            {"id": "si_1", "price": "price_t2", "quantity": 1}
        ]

    def test_comes_back_to_the_app_afterwards(self, monkeypatch):
        gateway, params = self._gateway(monkeypatch)
        gateway.create_plan_change_session(
            customer_id="cus_1", subscription_id="sub_1", plan=TIER_2,
            return_url="https://app/settings/plan",
        )
        after = params["flow_data"]["after_completion"]
        assert after == {"type": "redirect",
                         "redirect": {"return_url": "https://app/settings/plan"}}

    def test_moving_to_free_cancels_instead(self, monkeypatch):
        """There is no free price to move to — ending the subscription *is* the
        downgrade, so asking Stripe to switch price would be meaningless."""
        gateway, params = self._gateway(monkeypatch)
        gateway.create_plan_change_session(
            customer_id="cus_1", subscription_id="sub_1", plan=FREE,
            return_url="https://app/settings/plan",
        )
        flow = params["flow_data"]
        assert flow["type"] == "subscription_cancel"
        assert flow["subscription_cancel"] == {"subscription": "sub_1"}

    def test_the_first_priced_item_is_used(self, monkeypatch):
        gateway, params = self._gateway(monkeypatch, items=("si_first", "si_second"))
        gateway.create_plan_change_session(
            customer_id="cus_1", subscription_id="sub_1", plan=TIER_2,
            return_url="https://app/x",
        )
        items = params["flow_data"]["subscription_update_confirm"]["items"]
        assert items[0]["id"] == "si_first"

    def test_a_subscription_without_items_raises(self, monkeypatch):
        gateway, _ = self._gateway(monkeypatch, items=())
        with pytest.raises(GatewayError, match="no items"):
            gateway.create_plan_change_session(
                customer_id="cus_1", subscription_id="sub_1", plan=TIER_2,
                return_url="https://app/x",
            )

    def test_a_failed_retrieve_is_a_gateway_error(self, monkeypatch):
        gateway, _ = self._gateway(monkeypatch, retrieve_fails=True)
        with pytest.raises(GatewayError):
            gateway.create_plan_change_session(
                customer_id="cus_1", subscription_id="sub_1", plan=TIER_2,
                return_url="https://app/x",
            )

    def test_an_unconfigured_price_is_refused(self, monkeypatch):
        monkeypatch.delenv("STRIPE_PRICE_TIER_2", raising=False)
        gw.reset_price_cache()
        gateway, _ = self._gateway(monkeypatch)
        with pytest.raises(GatewayError):
            gateway.create_plan_change_session(
                customer_id="cus_1", subscription_id="sub_1", plan=TIER_2,
                return_url="https://app/x",
            )

    @pytest.mark.parametrize("customer,subscription", [("", "sub_1"), ("cus_1", "")])
    def test_missing_provider_ids_raise(self, monkeypatch, customer, subscription):
        gateway, _ = self._gateway(monkeypatch)
        with pytest.raises(GatewayError):
            gateway.create_plan_change_session(
                customer_id=customer, subscription_id=subscription, plan=TIER_2,
                return_url="https://app/x",
            )


class TestParseWebhook:
    def _gateway(self, monkeypatch, *, verified=True):
        def construct_event(payload, signature, secret):
            if not verified:
                raise ValueError("bad signature")
            return StripeObject.construct_from(json.loads(payload), "sk_test_x")

        fake = types.SimpleNamespace(
            Webhook=types.SimpleNamespace(construct_event=construct_event))
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        return StripeGateway()

    def test_returns_plain_nested_dicts(self, monkeypatch):
        """webhook_events walks the result with .get(), so every level must be a
        real dict — not the StripeObject the SDK hands back."""
        gateway = self._gateway(monkeypatch)
        payload = json.dumps({
            "id": "evt_1", "type": "customer.subscription.updated",
            "created": 1700000000,
            "data": {"object": {"id": "sub_1", "customer": "cus_1",
                                "status": "active",
                                "metadata": {"plan": "tier_2", "user_info_id": "6"}}},
        }).encode()

        event = gateway.parse_webhook(payload, "sig")

        assert type(event) is dict
        assert type(event["data"]) is dict
        assert type(event["data"]["object"]) is dict
        assert type(event["data"]["object"]["metadata"]) is dict
        # The access pattern webhook_events actually uses.
        assert event.get("type") == "customer.subscription.updated"
        obj = (event.get("data") or {}).get("object") or {}
        assert (obj.get("metadata") or {}).get("plan") == "tier_2"

    def test_bad_signature_raises(self, monkeypatch):
        gateway = self._gateway(monkeypatch, verified=False)
        with pytest.raises(GatewayError):
            gateway.parse_webhook(b"{}", "nope")

    def test_missing_secret_raises(self, monkeypatch):
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        gateway = self._gateway(monkeypatch)
        with pytest.raises(GatewayError):
            gateway.parse_webhook(b"{}", "sig")


class TestResolvePriceId:
    """Prices resolve by lookup key, not by an account-scoped id (issue #154)."""

    def _stripe(self, monkeypatch, *, prices=None, fail=False):
        """Fake SDK recording how many lookups were actually performed."""
        calls: list[list[str]] = []

        class _Price:
            @staticmethod
            def list(*, lookup_keys, limit):
                calls.append(list(lookup_keys))
                if fail:
                    raise RuntimeError("stripe unreachable")
                data = [_obj(id=prices[k]) for k in lookup_keys if k in (prices or {})]
                return _obj(data=data)

        monkeypatch.setattr("src.billing.stripe_gateway._stripe",
                            lambda: types.SimpleNamespace(Price=_Price))
        return calls

    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch):
        for plan in ("TIER_1", "TIER_2", "TIER_3"):
            monkeypatch.delenv(f"STRIPE_PRICE_{plan}", raising=False)
        gw.reset_price_cache()
        yield
        gw.reset_price_cache()

    def test_a_pinned_env_var_wins_without_calling_stripe(self, monkeypatch):
        """The escape hatch, and what keeps the change non-breaking on rollout."""
        monkeypatch.setenv("STRIPE_PRICE_TIER_2", "price_pinned")
        calls = self._stripe(monkeypatch, prices={})
        assert gw.resolve_price_id(TIER_2) == "price_pinned"
        assert calls == []

    def test_resolves_by_lookup_key_when_unpinned(self, monkeypatch):
        key = price_lookup_key(TIER_2)
        calls = self._stripe(monkeypatch, prices={key: "price_resolved"})
        assert gw.resolve_price_id(TIER_2) == "price_resolved"
        assert calls == [[key]]

    def test_the_second_call_is_cached(self, monkeypatch):
        key = price_lookup_key(TIER_1)
        calls = self._stripe(monkeypatch, prices={key: "price_x"})
        assert gw.resolve_price_id(TIER_1) == "price_x"
        assert gw.resolve_price_id(TIER_1) == "price_x"
        assert len(calls) == 1

    def test_the_cache_expires_so_a_reprice_is_picked_up(self, monkeypatch):
        """A reprice transfers the lookup key onto a new price and archives the
        old one, so caching forever would keep selling something archived."""
        key = price_lookup_key(TIER_1)
        prices = {key: "price_old"}
        calls = self._stripe(monkeypatch, prices=prices)
        assert gw.resolve_price_id(TIER_1) == "price_old"

        prices[key] = "price_new"
        # Capture the real clock first: gw.time is the stdlib module, so the
        # replacement would otherwise call itself.
        later = time.time() + gw._CACHE_TTL_SECONDS + 1
        monkeypatch.setattr(gw.time, "time", lambda: later)
        assert gw.resolve_price_id(TIER_1) == "price_new"
        assert len(calls) == 2

    def test_a_missing_price_raises_and_is_cached_briefly(self, monkeypatch):
        calls = self._stripe(monkeypatch, prices={})
        with pytest.raises(GatewayError, match="lookup key"):
            gw.resolve_price_id(TIER_3)
        with pytest.raises(GatewayError):
            gw.resolve_price_id(TIER_3)
        assert len(calls) == 1, "a miss should be cached, not re-queried every time"

    def test_a_transport_failure_is_not_cached_as_missing(self, monkeypatch):
        """Otherwise a blip becomes a minute of refused checkouts."""
        calls = self._stripe(monkeypatch, fail=True)
        for _ in range(2):
            with pytest.raises(GatewayError):
                gw.resolve_price_id(TIER_2)
        assert len(calls) == 2

    def test_an_unsellable_plan_resolves_to_nothing(self, monkeypatch):
        calls = self._stripe(monkeypatch, prices={})
        assert gw.resolve_price_id(FREE) == ""
        assert calls == []


class TestCancelSubscription:
    """Immediate cancellation when the account is deleted (issue #429).

    The refusals are real ``stripe.InvalidRequestError`` instances, and the
    retrieved subscription a real ``StripeObject`` — the ``code`` attribute and
    the ``.get`` trap are exactly what a dict or a bare Exception would hide.
    """

    def _gateway(self, monkeypatch, *, cancel_error=None, status="canceled",
                 retrieve_error=None):
        calls: dict = {"cancel": [], "retrieve": []}

        class _Subscription:
            @staticmethod
            def cancel(sub_id, **params):
                calls["cancel"].append((sub_id, params))
                if cancel_error is not None:
                    raise cancel_error
                return _obj(id=sub_id, status="canceled")

            @staticmethod
            def retrieve(sub_id):
                calls["retrieve"].append(sub_id)
                if retrieve_error is not None:
                    raise retrieve_error
                return _obj(id=sub_id, status=status)

        fake = types.SimpleNamespace(Subscription=_Subscription)
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        return StripeGateway(), calls

    @staticmethod
    def _missing():
        import stripe
        return stripe.InvalidRequestError(
            "No such subscription: 'sub_1'", "id", code="resource_missing",
            http_status=404)

    @staticmethod
    def _refused():
        import stripe
        return stripe.InvalidRequestError(
            "A canceled subscription can only update its cancellation_details "
            "and metadata.", None, http_status=400)

    def test_cancels_immediately_by_id(self, monkeypatch):
        gateway, calls = self._gateway(monkeypatch)
        gateway.cancel_subscription("sub_1")
        # Subscription.cancel ends it now; there is no at-period-end flag on it.
        assert calls["cancel"] == [("sub_1", {})]

    def test_a_subscription_stripe_does_not_know_is_success(self, monkeypatch):
        gateway, calls = self._gateway(monkeypatch, cancel_error=self._missing())
        gateway.cancel_subscription("sub_1")  # must not raise
        assert calls["retrieve"] == []

    @pytest.mark.parametrize("status", ["canceled", "incomplete_expired"])
    def test_an_already_ended_subscription_is_success(self, monkeypatch, status):
        """The retry after a partial failure: cancelled at Stripe last time,
        then the account deletion itself failed."""
        gateway, calls = self._gateway(monkeypatch, cancel_error=self._refused(),
                                       status=status)
        gateway.cancel_subscription("sub_1")  # must not raise
        assert calls["retrieve"] == ["sub_1"]

    def test_vanishing_between_cancel_and_retrieve_is_success(self, monkeypatch):
        gateway, _ = self._gateway(monkeypatch, cancel_error=self._refused(),
                                   retrieve_error=self._missing())
        gateway.cancel_subscription("sub_1")  # must not raise

    def test_a_refusal_while_still_running_is_a_gateway_error(self, monkeypatch):
        gateway, _ = self._gateway(monkeypatch, cancel_error=self._refused(),
                                   status="active")
        with pytest.raises(GatewayError):
            gateway.cancel_subscription("sub_1")

    def test_a_network_failure_is_a_gateway_error(self, monkeypatch):
        import stripe
        down = stripe.APIConnectionError("connection reset")
        gateway, _ = self._gateway(monkeypatch, cancel_error=down,
                                   retrieve_error=down)
        with pytest.raises(GatewayError):
            gateway.cancel_subscription("sub_1")

    def test_no_subscription_id_is_a_gateway_error(self, monkeypatch):
        gateway, calls = self._gateway(monkeypatch)
        with pytest.raises(GatewayError):
            gateway.cancel_subscription("")
        assert calls["cancel"] == []


def _missing(what="subscription"):
    import stripe
    return stripe.InvalidRequestError(
        f"No such {what}: 'x_1'", "id", code="resource_missing", http_status=404)


def _page(*items):
    """A real ListObject, so ``auto_paging_iter`` is the SDK's own."""
    from stripe import ListObject
    return ListObject.construct_from(
        {"object": "list", "data": [dict(i) for i in items], "has_more": False,
         "url": "/v1/x"}, "sk_test_x")


class _FakeStripe:
    """Just enough of the SDK for the account-deletion calls (issue #429).

    Records every call in order, so tests can check what happened first.
    """

    def __init__(self, *, customer=None, sessions=(), subscriptions=(),
                 customer_error=None, list_error=None, expire_error=None,
                 session_status_after_expire_error="complete",
                 subscription_cancel_error=None, subscription_retrieve_error=None):
        self.log: list[tuple] = []
        fake = self
        self.customer = customer if customer is not None else {"id": "cus_1"}

        class Customer:
            @staticmethod
            def retrieve(cid):
                fake.log.append(("customer.retrieve", cid))
                if customer_error is not None:
                    raise customer_error
                return _obj(**fake.customer)

            @staticmethod
            def delete(cid):  # must never be called
                fake.log.append(("customer.delete", cid))

        class Session:
            @staticmethod
            def list(**params):
                fake.log.append(("session.list", params))
                if list_error is not None:
                    raise list_error
                return _page(*sessions)

            @staticmethod
            def expire(sid):
                fake.log.append(("session.expire", sid))
                if expire_error is not None:
                    raise expire_error
                return _obj(id=sid, status="expired")

            @staticmethod
            def retrieve(sid):
                fake.log.append(("session.retrieve", sid))
                return _obj(id=sid, status=session_status_after_expire_error)

        class Subscription:
            @staticmethod
            def list(**params):
                fake.log.append(("subscription.list", params))
                return _page(*subscriptions)

            @staticmethod
            def cancel(sid, **params):
                fake.log.append(("subscription.cancel", sid))
                if subscription_cancel_error is not None:
                    raise subscription_cancel_error
                return _obj(id=sid, status="canceled")

            @staticmethod
            def retrieve(sid):
                fake.log.append(("subscription.retrieve", sid))
                if subscription_retrieve_error is not None:
                    raise subscription_retrieve_error
                return _obj(id=sid, status="active")

        self.Customer = Customer
        self.Subscription = Subscription
        self.checkout = types.SimpleNamespace(Session=Session)

    def calls(self, name):
        return [c[1] for c in self.log if c[0] == name]


def _install(monkeypatch, fake):
    monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
    return StripeGateway()


class TestCancelAllForCustomer:
    """Deleting an account asks Stripe what could still bill (issue #429).

    Our cached row can say "none" while a checkout page opened before the
    deletion is still payable, and it tracks one subscription where Stripe may
    hold several, so the customer's own state is what gets stopped.
    """

    def test_cancels_every_subscription_that_has_not_ended(self, monkeypatch):
        fake = _FakeStripe(subscriptions=[
            {"id": "sub_a", "status": "active"},
            {"id": "sub_b", "status": "past_due"},
            {"id": "sub_c", "status": "canceled"},
            {"id": "sub_d", "status": "incomplete_expired"},
            {"id": "sub_e", "status": "unpaid"},
        ])
        gateway = _install(monkeypatch, fake)

        cancelled = gateway.cancel_all_for_customer("cus_1")

        assert cancelled == ["sub_a", "sub_b", "sub_e"]
        assert fake.calls("subscription.cancel") == ["sub_a", "sub_b", "sub_e"]
        # Every status is listed, not just the live ones Stripe lists by default.
        assert fake.calls("subscription.list") == [
            {"customer": "cus_1", "status": "all", "limit": 100}]

    def test_expires_open_checkouts_before_listing_subscriptions(self, monkeypatch):
        """A checkout opened before the deletion must not be payable after it,
        and one paid in between must show up in the list that follows."""
        fake = _FakeStripe(sessions=[{"id": "cs_1"}, {"id": "cs_2"}])
        gateway = _install(monkeypatch, fake)

        gateway.cancel_all_for_customer("cus_1")

        assert fake.calls("session.list") == [
            {"customer": "cus_1", "status": "open", "limit": 100}]
        assert fake.calls("session.expire") == ["cs_1", "cs_2"]
        names = [c[0] for c in fake.log]
        assert names.index("session.expire") < names.index("subscription.list")

    def test_the_customer_is_kept(self, monkeypatch):
        """Its invoices are the accounting record, and a refund needs them."""
        fake = _FakeStripe(subscriptions=[{"id": "sub_a", "status": "active"}])
        _install(monkeypatch, fake).cancel_all_for_customer("cus_1")
        assert fake.calls("customer.delete") == []

    def test_a_customer_stripe_does_not_know_means_the_wrong_account(self, monkeypatch):
        """A sandbox key against live ids says "no such customer" too. That
        proves nothing was cancelled, so it must not pass for success."""
        fake = _FakeStripe(customer_error=_missing("customer"))
        gateway = _install(monkeypatch, fake)
        with pytest.raises(GatewayError):
            gateway.cancel_all_for_customer("cus_1")
        assert fake.calls("subscription.list") == []

    def test_a_deleted_customer_has_nothing_left_to_cancel(self, monkeypatch):
        fake = _FakeStripe(customer={"id": "cus_1", "deleted": True})
        assert _install(monkeypatch, fake).cancel_all_for_customer("cus_1") == []
        assert fake.calls("subscription.list") == []

    def test_a_session_completed_meanwhile_is_fine(self, monkeypatch):
        import stripe
        fake = _FakeStripe(
            sessions=[{"id": "cs_1"}],
            expire_error=stripe.InvalidRequestError("not open", None, http_status=400),
            session_status_after_expire_error="complete",
        )
        _install(monkeypatch, fake).cancel_all_for_customer("cus_1")  # no raise
        assert fake.calls("session.retrieve") == ["cs_1"]

    def test_a_session_still_open_after_a_failed_expire_is_an_error(self, monkeypatch):
        import stripe
        fake = _FakeStripe(
            sessions=[{"id": "cs_1"}],
            expire_error=stripe.APIConnectionError("reset"),
            session_status_after_expire_error="open",
        )
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).cancel_all_for_customer("cus_1")

    def test_a_listing_failure_is_an_error(self, monkeypatch):
        import stripe
        fake = _FakeStripe(list_error=stripe.APIConnectionError("reset"))
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).cancel_all_for_customer("cus_1")

    def test_a_failed_cancel_is_an_error(self, monkeypatch):
        import stripe
        fake = _FakeStripe(
            subscriptions=[{"id": "sub_a", "status": "active"}],
            subscription_cancel_error=stripe.APIConnectionError("reset"),
            subscription_retrieve_error=stripe.APIConnectionError("reset"),
        )
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).cancel_all_for_customer("cus_1")

    def test_no_customer_id_is_an_error(self, monkeypatch):
        fake = _FakeStripe()
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).cancel_all_for_customer("")


class TestMissingSubscriptionWithAKnownCustomer:
    """A missing subscription is only success if Stripe knows the customer."""

    def test_the_customer_exists_so_missing_means_gone(self, monkeypatch):
        fake = _FakeStripe(subscription_cancel_error=_missing())
        _install(monkeypatch, fake).cancel_subscription("sub_1", "cus_1")
        assert fake.calls("customer.retrieve") == ["cus_1"]

    def test_the_customer_is_missing_too_so_the_key_is_wrong(self, monkeypatch):
        fake = _FakeStripe(subscription_cancel_error=_missing(),
                           customer_error=_missing("customer"))
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).cancel_subscription("sub_1", "cus_1")


class TestExpireCheckoutSession:
    """Expiring the page opened for an account deleted meanwhile (#429)."""

    def test_the_session_id_is_returned_from_checkout(self, monkeypatch):
        class _Session:
            @staticmethod
            def create(**params):
                return _obj(id="cs_1", url="https://checkout/cs_1", customer=None)

        fake = types.SimpleNamespace(checkout=types.SimpleNamespace(Session=_Session))
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        out = StripeGateway().create_checkout_session(
            user_info_id=6, plan="tier_2", email="a@b.c", customer_id="",
            success_url="https://app/ok", cancel_url="https://app/no",
        )
        assert out["session_id"] == "cs_1"

    def test_expires_it(self, monkeypatch):
        fake = _FakeStripe()
        _install(monkeypatch, fake).expire_checkout_session("cs_1")
        assert fake.calls("session.expire") == ["cs_1"]

    def test_one_completed_meanwhile_is_fine(self, monkeypatch):
        import stripe
        fake = _FakeStripe(
            expire_error=stripe.InvalidRequestError("not open", None, http_status=400),
            session_status_after_expire_error="complete")
        _install(monkeypatch, fake).expire_checkout_session("cs_1")  # no raise

    def test_one_still_open_is_an_error(self, monkeypatch):
        import stripe
        fake = _FakeStripe(expire_error=stripe.APIConnectionError("reset"),
                           session_status_after_expire_error="open")
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).expire_checkout_session("cs_1")


# ── Refunds (issue #441) ─────────────────────────────────────────────────────

_START, _END = 1_780_000_000, 1_780_000_000 + 30 * 86400
KEY = "traxjourney-unused-period-refund-sub_1"


def _invoice(*, amount_paid=399, lines=((_START, _END),), **extra) -> dict:
    return {
        "id": "in_1", "amount_paid": amount_paid, "currency": "eur",
        "lines": {"object": "list",
                  "data": [{"period": {"start": s, "end": e}} for s, e in lines]},
        **extra,
    }


class _RefundStripe:
    """The SDK surface the refund path uses, returning real StripeObjects.

    An invoice without ``payment_intent``/``charge`` is the current API shape,
    where payments are listed separately; ``invoice_payments`` answers that.
    """

    def __init__(self, *, subscription=None, invoices=(), invoice_payments=(),
                 refunds=(), credit_notes=(), subscriptions=(), items=(),
                 create_error=None, delete_error=None):
        fake = self
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.log: list[tuple] = []
        self.refunds = list(refunds)
        self.credit_notes = list(credit_notes)
        sub = subscription or {"id": "sub_1", "status": "canceled",
                               "ended_at": _START + 10 * 86400}

        class Subscription:
            @staticmethod
            def retrieve(sid):
                fake.log.append(("subscription.retrieve", sid))
                return _obj(**sub)

            @staticmethod
            def list(**params):
                fake.log.append(("subscription.list", params))
                return _page(*subscriptions)

        class Invoice:
            @staticmethod
            def list(**params):
                fake.log.append(("invoice.list", params))
                return _page(*invoices)

        class InvoicePayment:
            @staticmethod
            def list(**params):
                fake.log.append(("invoice_payment.list", params))
                return _page(*invoice_payments)

        class Refund:
            @staticmethod
            def list(**params):
                fake.log.append(("refund.list", params))
                return _page(*fake.refunds)

            @staticmethod
            def create(**params):  # must not be used: refunds go via credit notes
                fake.log.append(("refund.create", params))
                raise AssertionError("Refund.create called")

        class CreditNote:
            @staticmethod
            def list(**params):
                fake.log.append(("credit_note.list", params))
                return _page(*fake.credit_notes)

            @staticmethod
            def create(**params):
                fake.log.append(("credit_note.create", params))
                if create_error is not None:
                    raise create_error
                fake.created.append(params)
                return _obj(id="cn_new", amount=params["amount"], status="issued")

        class InvoiceItem:
            @staticmethod
            def list(**params):
                fake.log.append(("invoice_item.list", params))
                return _page(*items)

            @staticmethod
            def delete(item_id):
                fake.log.append(("invoice_item.delete", item_id))
                if delete_error is not None:
                    raise delete_error
                fake.deleted.append(item_id)

        self.Subscription = Subscription
        self.Invoice = Invoice
        self.InvoicePayment = InvoicePayment
        self.Refund = Refund
        self.CreditNote = CreditNote
        self.InvoiceItem = InvoiceItem


class TestRefundBasis:
    def test_reads_the_latest_paid_invoice_and_when_it_ended(self, monkeypatch):
        fake = _RefundStripe(invoices=[_invoice()])
        basis = _install(monkeypatch, fake).refund_basis("sub_1")
        assert basis.invoice_id == "in_1"
        assert basis.amount_paid == 399 and basis.currency == "eur"
        assert (basis.period_start, basis.period_end) == (_START, _END)
        assert basis.ended_at == _START + 10 * 86400
        assert ("invoice.list", {"subscription": "sub_1", "status": "paid",
                                 "limit": 1}) in fake.log

    def test_a_running_subscription_has_not_ended(self, monkeypatch):
        fake = _RefundStripe(
            subscription={"id": "sub_1", "status": "active", "canceled_at": 5,
                          "ended_at": None},
            invoices=[_invoice()])
        assert _install(monkeypatch, fake).refund_basis("sub_1").ended_at == 0

    def test_ended_is_ended_at_not_when_the_cancellation_was_asked(self, monkeypatch):
        """Cancelled at period end: asked on day 3, ran until the end."""
        fake = _RefundStripe(
            subscription={"id": "sub_1", "status": "canceled",
                          "canceled_at": _START + 3 * 86400, "ended_at": _END},
            invoices=[_invoice()])
        assert _install(monkeypatch, fake).refund_basis("sub_1").ended_at == _END

    def test_the_period_spans_every_line(self, monkeypatch):
        fake = _RefundStripe(invoices=[_invoice(
            lines=((_START + 5, _END), (_START, _END - 5)))])
        basis = _install(monkeypatch, fake).refund_basis("sub_1")
        assert (basis.period_start, basis.period_end) == (_START, _END)

    def test_nothing_ever_paid(self, monkeypatch):
        fake = _RefundStripe(invoices=[])
        basis = _install(monkeypatch, fake).refund_basis("sub_1")
        assert basis.amount_paid == 0 and basis.invoice_id == ""

    def test_a_provider_failure_is_a_gateway_error(self, monkeypatch):
        import stripe
        fake = _RefundStripe()

        def boom(**params):
            raise stripe.APIConnectionError("reset")
        fake.Invoice.list = staticmethod(boom)
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).refund_basis("sub_1")


_PAID_BY_PI = [{"id": "inpay_1", "status": "paid",
                "payment": {"type": "payment_intent", "payment_intent": "pi_1"}}]


class TestRefundUnused:
    """Refunds go through a credit note (finding 5), and everything already
    refunded on the payment counts towards what is owed (finding 2)."""

    def test_refunds_through_a_credit_note_on_the_latest_invoice(self, monkeypatch):
        fake = _RefundStripe(invoices=[_invoice()], invoice_payments=_PAID_BY_PI)
        out = _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY)
        assert out == 266
        (params,) = fake.created
        assert params["invoice"] == "in_1"
        assert params["amount"] == 266
        assert params["refund_amount"] == 266
        assert params["metadata"] == {"refund_key": KEY, "subscription": "sub_1"}
        assert ("refund.create",) not in [c[:1] for c in fake.log]

    def test_the_same_path_with_stripe_tax_on(self, monkeypatch):
        """A credit note is what reverses the VAT in Stripe Tax's reports."""
        monkeypatch.setenv("STRIPE_AUTOMATIC_TAX", "1")
        fake = _RefundStripe(invoices=[_invoice()], invoice_payments=_PAID_BY_PI)
        _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY)
        assert fake.created[0]["refund_amount"] == 266

    def test_the_provider_key_is_the_refund_key_plus_the_utc_hour(self, monkeypatch):
        """Finding 11: an error Stripe cached under a key stops blocking the
        retry at the next hour; a double tap in the same hour shares the key."""
        monkeypatch.setattr(gw, "_utc_hour", lambda: "2026092614")
        fake = _RefundStripe(invoices=[_invoice()], invoice_payments=_PAID_BY_PI)
        _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY)
        assert fake.created[0]["idempotency_key"] == f"{KEY}:2026092614"

    def test_a_new_hour_gives_a_new_provider_key(self, monkeypatch):
        hours = iter(["2026092614", "2026092615"])
        monkeypatch.setattr(gw, "_utc_hour", lambda: next(hours))
        import stripe
        fake = _RefundStripe(invoices=[_invoice()], invoice_payments=_PAID_BY_PI,
                             create_error=stripe.InvalidRequestError("x", "amount"))
        gateway = _install(monkeypatch, fake)
        for _ in range(2):
            with pytest.raises(GatewayError):
                gateway.refund_unused("sub_1", 266, KEY)
        keys = [c[1]["idempotency_key"] for c in fake.log if c[0] == "credit_note.create"]
        assert keys == [f"{KEY}:2026092614", f"{KEY}:2026092615"]

    def test_a_credit_note_already_carrying_the_key_is_not_made_again(self, monkeypatch):
        """Whatever hour its provider key had: the metadata sees every attempt."""
        fake = _RefundStripe(
            invoices=[_invoice()], invoice_payments=_PAID_BY_PI,
            credit_notes=[{"id": "cn_old", "amount": 266, "status": "issued",
                           "metadata": {"refund_key": KEY}}])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY) == 266
        assert fake.created == []

    def test_a_void_credit_note_does_not_count_as_done(self, monkeypatch):
        fake = _RefundStripe(
            invoices=[_invoice()], invoice_payments=_PAID_BY_PI,
            credit_notes=[{"id": "cn_void", "amount": 266, "status": "void",
                           "metadata": {"refund_key": KEY}}])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY) == 266
        assert len(fake.created) == 1

    def test_a_refund_carrying_the_key_is_not_made_again(self, monkeypatch):
        fake = _RefundStripe(
            invoices=[_invoice(payment_intent="pi_1")],
            refunds=[{"id": "re_old", "amount": 266, "status": "succeeded",
                      "metadata": {"refund_key": KEY}}])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY) == 266
        assert fake.created == []

    def test_a_manual_refund_of_it_all_leaves_nothing_to_refund(self, monkeypatch):
        """Finding 2: the owner refunded the €2.66 by hand; the retry must not
        send another €1.33 (399 - 266)."""
        fake = _RefundStripe(
            invoices=[_invoice(amount_paid=399, payment_intent="pi_1")],
            refunds=[{"id": "re_hand", "amount": 266, "status": "succeeded",
                      "metadata": {}}])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY) == 0
        assert fake.created == []

    def test_a_partial_manual_refund_is_topped_up_to_what_is_owed(self, monkeypatch):
        fake = _RefundStripe(
            invoices=[_invoice(amount_paid=399, payment_intent="pi_1")],
            refunds=[{"id": "re_hand", "amount": 100, "status": "succeeded",
                      "metadata": {}}])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY) == 166
        assert fake.created[0]["amount"] == 166

    def test_never_more_than_was_paid(self, monkeypatch):
        fake = _RefundStripe(invoices=[_invoice(amount_paid=200, payment_intent="pi_1")])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY) == 200

    def test_a_failed_earlier_refund_does_not_count(self, monkeypatch):
        fake = _RefundStripe(
            invoices=[_invoice(amount_paid=399, payment_intent="pi_1")],
            refunds=[{"id": "re_f", "amount": 266, "status": "failed",
                      "metadata": {"refund_key": KEY}}])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY) == 266
        assert len(fake.created) == 1

    def test_reads_the_older_invoice_shape_too(self, monkeypatch):
        fake = _RefundStripe(invoices=[_invoice(charge="ch_1")])
        _install(monkeypatch, fake).refund_unused("sub_1", 100, KEY)
        assert ("refund.list", {"limit": 100, "charge": "ch_1"}) in fake.log
        assert len(fake.created) == 1

    def test_zero_does_not_call_stripe(self, monkeypatch):
        fake = _RefundStripe(invoices=[_invoice(amount_paid=0)])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 0, KEY) == 0
        assert fake.log == []

    def test_paid_without_a_payment_refunds_nothing_rather_than_trap(self, monkeypatch):
        fake = _RefundStripe(invoices=[_invoice()], invoice_payments=[])
        assert _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY) == 0
        assert fake.created == []

    def test_a_refusal_is_a_gateway_error(self, monkeypatch):
        import stripe
        fake = _RefundStripe(
            invoices=[_invoice(payment_intent="pi_1")],
            create_error=stripe.InvalidRequestError("nope", "amount"))
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY)

    def test_no_paid_invoice_is_a_gateway_error(self, monkeypatch):
        fake = _RefundStripe(invoices=[])
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).refund_unused("sub_1", 266, KEY)

    def test_a_key_is_required(self, monkeypatch):
        fake = _RefundStripe(invoices=[_invoice(payment_intent="pi_1")])
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).refund_unused("sub_1", 266, "")


class TestDiscardPendingItems:
    """Finding 6: an upgrade's prorated difference waits as a pending item."""

    def test_deletes_the_subscriptions_pending_items_only(self, monkeypatch):
        fake = _RefundStripe(items=[
            # current API shape
            {"id": "ii_1", "parent": {"type": "subscription_details",
                                      "subscription_details": {"subscription": "sub_1"}}},
            # older shape
            {"id": "ii_2", "subscription": "sub_1"},
            {"id": "ii_other", "subscription": "sub_9"},
            {"id": "ii_manual"},
        ])
        out = _install(monkeypatch, fake).discard_pending_items("cus_1", "sub_1")
        assert out == 2
        assert fake.deleted == ["ii_1", "ii_2"]
        assert fake.log[0] == ("invoice_item.list", {
            "customer": "cus_1", "pending": True, "limit": 100})

    def test_one_deleted_meanwhile_is_fine(self, monkeypatch):
        fake = _RefundStripe(items=[{"id": "ii_1", "subscription": "sub_1"}],
                             delete_error=_missing("invoiceitem"))
        assert _install(monkeypatch, fake).discard_pending_items("cus_1", "sub_1") == 0

    def test_a_failure_is_a_gateway_error(self, monkeypatch):
        import stripe
        fake = _RefundStripe(items=[{"id": "ii_1", "subscription": "sub_1"}],
                             delete_error=stripe.APIConnectionError("reset"))
        with pytest.raises(GatewayError):
            _install(monkeypatch, fake).discard_pending_items("cus_1", "sub_1")


class TestLatestSubscription:
    def test_the_most_recently_started_one_that_was_paid_for(self, monkeypatch):
        fake = _RefundStripe(subscriptions=[
            {"id": "sub_old", "status": "canceled", "start_date": 100},
            {"id": "sub_new", "status": "active", "start_date": 500},
            {"id": "sub_failed", "status": "incomplete_expired", "start_date": 900},
        ])
        out = _install(monkeypatch, fake).latest_subscription("cus_1")
        assert out == ("sub_new", 500.0)
        assert fake.log[0] == ("subscription.list", {
            "customer": "cus_1", "status": "all", "limit": 100})

    def test_none_when_there_is_none(self, monkeypatch):
        assert _install(monkeypatch, _RefundStripe()).latest_subscription("cus_1") is None


# ── Checkout consent and VAT (issue #441) ────────────────────────────────────

class TestCheckoutConsentAndTax:
    def _params(self, monkeypatch, *, customer_id=""):
        created: dict = {}

        class _Session:
            @staticmethod
            def create(**params):
                created.update(params)
                return _obj(id="cs_1", url="https://u", customer="cus_1")

        fake = types.SimpleNamespace(checkout=types.SimpleNamespace(Session=_Session))
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        StripeGateway().create_checkout_session(
            user_info_id=6, plan="tier_2", email="a@b.c", customer_id=customer_id,
            success_url="https://app/ok", cancel_url="https://app/no",
            terms_url="https://app/terms",
        )
        return created

    def test_consent_to_start_now_is_required(self, monkeypatch):
        params = self._params(monkeypatch)
        assert params["consent_collection"] == {"terms_of_service": "required"}
        box = params["custom_text"]["terms_of_service_acceptance"]["message"]
        assert "[Terms of Service](https://app/terms)" in box
        assert "start immediately" in box
        assert "14th day" in box and "unused part" in box
        assert "14 days" in params["custom_text"]["submit"]["message"]
        for text in params["custom_text"].values():
            assert len(text["message"]) <= 1200  # Stripe's limit

    def test_the_session_names_the_terms_version(self, monkeypatch):
        from src.billing.refunds import WITHDRAWAL_TERMS_VERSION
        params = self._params(monkeypatch)
        assert params["metadata"]["terms_version"] == WITHDRAWAL_TERMS_VERSION
        assert params["metadata"]["user_info_id"] == "6"
        # Only the session carries it; the subscription's metadata is unchanged.
        assert "terms_version" not in params["subscription_data"]["metadata"]

    @pytest.mark.parametrize("flag", ["", "0", "false", "off"])
    def test_tax_is_off_by_default(self, monkeypatch, flag):
        monkeypatch.setenv("STRIPE_AUTOMATIC_TAX", flag)
        params = self._params(monkeypatch, customer_id="cus_1")
        for key in ("automatic_tax", "billing_address_collection", "customer_update"):
            assert key not in params

    def test_tax_is_off_when_unset(self, monkeypatch):
        monkeypatch.delenv("STRIPE_AUTOMATIC_TAX", raising=False)
        assert "automatic_tax" not in self._params(monkeypatch, customer_id="cus_1")

    @pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
    def test_tax_on_collects_the_address(self, monkeypatch, flag):
        monkeypatch.setenv("STRIPE_AUTOMATIC_TAX", flag)
        params = self._params(monkeypatch, customer_id="cus_1")
        assert params["automatic_tax"] == {"enabled": True}
        assert params["billing_address_collection"] == "required"
        assert params["customer_update"] == {"address": "auto", "name": "auto"}

    def test_a_missing_terms_url_is_logged_as_an_error_naming_the_fix(
        self, monkeypatch, caplog
    ):
        """Finding 7: deployed before the dashboard setting, every checkout
        fails; the log must say which setting."""
        import logging
        import stripe

        class _Session:
            @staticmethod
            def create(**params):
                raise stripe.InvalidRequestError(
                    "You must provide a terms of service URL in your public "
                    "business details to collect consent.",
                    "consent_collection[terms_of_service]")

        fake = types.SimpleNamespace(checkout=types.SimpleNamespace(Session=_Session))
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        with caplog.at_level(logging.WARNING, logger="src.billing.stripe_gateway"):
            with pytest.raises(GatewayError):
                StripeGateway().create_checkout_session(
                    user_info_id=6, plan="tier_2", email="a@b.c", customer_id="",
                    success_url="https://app/ok", cancel_url="https://app/no")
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "Public details" in errors[0].getMessage()
        assert "Terms" in errors[0].getMessage()

    @pytest.mark.parametrize("exc_args", [
        ("Your card was declined.", None),
        ("No such price: 'price_x'", "line_items[0][price]"),
    ])
    def test_other_failures_are_not_blamed_on_the_terms(self, monkeypatch, caplog, exc_args):
        import logging
        import stripe

        class _Session:
            @staticmethod
            def create(**params):
                raise stripe.InvalidRequestError(*exc_args)

        fake = types.SimpleNamespace(checkout=types.SimpleNamespace(Session=_Session))
        monkeypatch.setattr("src.billing.stripe_gateway._stripe", lambda: fake)
        with caplog.at_level(logging.WARNING, logger="src.billing.stripe_gateway"):
            with pytest.raises(GatewayError):
                StripeGateway().create_checkout_session(
                    user_info_id=6, plan="tier_2", email="a@b.c", customer_id="",
                    success_url="https://app/ok", cancel_url="https://app/no")
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]

    def test_the_consent_text_states_the_calendar_deadline(self, monkeypatch):
        box = self._params(monkeypatch)["custom_text"][
            "terms_of_service_acceptance"]["message"]
        assert "end of the 14th day after the day it starts (UTC)" in box

    def test_tax_on_for_a_new_customer_sends_no_customer_update(self, monkeypatch):
        """Stripe refuses customer_update without a customer."""
        monkeypatch.setenv("STRIPE_AUTOMATIC_TAX", "1")
        params = self._params(monkeypatch, customer_id="")
        assert params["automatic_tax"] == {"enabled": True}
        assert "customer_update" not in params
        assert params["customer_email"] == "a@b.c"

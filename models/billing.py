"""Billing models — subscription state and per-user storage usage (issue #121).

Two tables, deliberately separate:

* ``Subscription`` mirrors what the payment provider says about a user. It is a
  *cache* of provider state, rewritten by webhooks; the provider remains the
  source of truth. ``admin_override_plan`` sits alongside it so a comped or
  support-granted plan survives untouched when a webhook rewrites the rest.
* ``UserUsage`` is a running counter of bytes on disk, incremented/decremented
  on upload and delete. Quota checks read this instead of walking the
  filesystem — ``src.admin.storage.dir_size`` takes seconds on a photo-heavy
  account, which is fine for a dashboard and far too slow for an upload path.
  A nightly job reconciles the counter against the real tree.
"""
from __future__ import annotations

import time

import sqlmodel


class Subscription(sqlmodel.SQLModel, table=True):
    """Per-user subscription state, as last reported by the payment provider."""

    id: int | None = sqlmodel.Field(default=None, primary_key=True)
    user_info_id: int = sqlmodel.Field(
        foreign_key="userinfo.id", unique=True, index=True
    )
    # Plan this subscription grants while ``status`` is live: a plans.py id.
    plan: str = sqlmodel.Field(default="free")
    # Provider status verbatim ("active", "trialing", "past_due", "canceled", …);
    # "none" when the user has never checked out.
    status: str = sqlmodel.Field(default="none")
    provider: str = sqlmodel.Field(default="stripe")
    # Provider identifiers. The customer id is indexed because webhooks arrive
    # keyed by customer, not by our user id.
    provider_customer_id: str = sqlmodel.Field(default="", index=True)
    provider_subscription_id: str = sqlmodel.Field(default="")
    # End of the paid period, unix seconds. Access is granted up to this instant
    # even after a cancellation, which is what "cancel at period end" means.
    current_period_end: float = sqlmodel.Field(default=0.0)
    cancel_at_period_end: bool = sqlmodel.Field(default=False)
    # A tier change the provider has agreed to but not applied yet — a downgrade
    # waits for the paid period to end (issue #153). Stored separately from
    # ``plan`` because the plan in force is still the old one: overwriting it
    # would downgrade the account the moment they asked, which is the opposite
    # of what "at the end of the period" means. "" = nothing pending.
    pending_plan: str = sqlmodel.Field(default="")
    # When the pending change takes effect, unix seconds.
    pending_plan_at: float = sqlmodel.Field(default=0.0)
    # Operator-granted plan, independent of any payment ("" = no override).
    admin_override_plan: str = sqlmodel.Field(default="")
    # Last provider event applied, for idempotent webhook replay.
    last_event_id: str = sqlmodel.Field(default="")
    # Provider timestamp of the last applied event, so a redelivered *older*
    # event cannot move state backwards (webhooks arrive out of order).
    last_event_at: float = sqlmodel.Field(default=0.0)
    # The current contract (issue #441): the subscription that started while
    # no other subscription of the account was running, and when it started
    # being paid for, unix seconds. The withdrawal window runs from its day.
    # Renewals and plan changes keep the same subscription, so they never move
    # it; a new subscription after the previous one ended starts a new one.
    # 0 = none on record, which means no window. Subscriptions already running
    # when this shipped were given 1.0 by the migration (window closed).
    contract_started_at: float = sqlmodel.Field(default=0.0)
    contract_subscription_id: str = sqlmodel.Field(default="")
    # Proof of the express consent collected at checkout (issue #441): when
    # the buyer ticked the terms box, and which wording of the withdrawal
    # terms (refunds.WITHDRAWAL_TERMS_VERSION) that box stood for. The latest
    # purchase's; Stripe keeps every session's own record too.
    terms_accepted_at: float = sqlmodel.Field(default=0.0)
    terms_version: str = sqlmodel.Field(default="")
    # When the user last withdrew through POST /api/billing/withdraw (#441),
    # and from which subscription: the withdrawal is done for that one only.
    withdrawn_at: float = sqlmodel.Field(default=0.0)
    withdrawn_subscription_id: str = sqlmodel.Field(default="")
    updated_at: float = sqlmodel.Field(default_factory=time.time)


class SubscriptionRefund(sqlmodel.SQLModel, table=True):
    """The refund of one subscription's unused period (issue #441).

    One row per subscription, claimed under the account's lock before Stripe is
    asked, so a withdrawal and a deletion — or two taps — cannot both refund:

    * ``pending`` — being refunded, or a transient failure left it to retry.
      ``lease_until`` in the future means a request is working on it now.
    * ``done`` — refunded (``refunded`` may be 0: nothing was left).
    * ``failed_permanent`` — Stripe refused definitely, or there was no payment
      to refund. The refund is **owed** and the owner settles it in Stripe.

    Deliberately not keyed to ``userinfo``: an owed refund must survive the
    account's deletion. It holds only the Stripe identifiers, amounts and a
    reason, and is deleted once settled (docs/BILLING.md, "Owed refunds").
    """

    __tablename__ = "subscription_refund"

    subscription_id: str = sqlmodel.Field(primary_key=True)
    customer_id: str = sqlmodel.Field(default="", index=True)
    state: str = sqlmodel.Field(default="pending")
    # Bumped after a definite refusal only, so the next try has a new
    # provider idempotency key; never after a timeout, whose request may yet
    # have succeeded under the old one.
    attempt: int = sqlmodel.Field(default=0)
    # What the unused period is owed, frozen at the first computation; -1 =
    # not computed yet. And what was actually refunded.
    amount: int = sqlmodel.Field(default=-1)
    refunded: int = sqlmodel.Field(default=0)
    currency: str = sqlmodel.Field(default="")
    # The invoice refunded — the contract's, frozen with the amount, so a
    # retry can never refund a later invoice.
    invoice_id: str = sqlmodel.Field(default="")
    credit_note_id: str = sqlmodel.Field(default="")
    reason: str = sqlmodel.Field(default="")
    # When the withdrawal or deletion that cancelled it was asked for.
    requested_at: float = sqlmodel.Field(default=0.0)
    lease_until: float = sqlmodel.Field(default=0.0)
    created_at: float = sqlmodel.Field(default_factory=time.time)
    updated_at: float = sqlmodel.Field(default_factory=time.time)


class UserUsage(sqlmodel.SQLModel, table=True):
    """Running total of bytes stored under ``data/users/{id}/`` for one user."""

    id: int | None = sqlmodel.Field(default=None, primary_key=True)
    user_info_id: int = sqlmodel.Field(
        foreign_key="userinfo.id", unique=True, index=True
    )
    storage_bytes: int = sqlmodel.Field(default=0)
    # When the counter was last reconciled against the real filesystem tree.
    reconciled_at: float = sqlmodel.Field(default=0.0)
    updated_at: float = sqlmodel.Field(default_factory=time.time)

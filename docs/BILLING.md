# Billing and tier plans

Issue #121. Four plans — **Free** plus three paid tiers — with payments through
Stripe.

**Self-hosting is not affected.** With no payment provider configured there is
no billing: the payment endpoints return 404, no paywall exists anywhere in the
client, and every account has unlimited trips and storage. The landing page's
"Self-hosted · MIT · Unlimited projects, users" is enforced by
`billing_enabled()` returning False, not by good intentions.

## What the plans limit

| | Free | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|---|
| Trips | 1 | 2 | 10 | unlimited |
| Photo storage | 500 MB | 5 GB | 20 GB | 50 GB |
| Days per trip | 10 | 100 | 365 | unlimited |

Every number is an environment variable (`FREE_MAX_TRIP_DAYS`,
`TIER_2_MAX_STORAGE_MB`, …), read per request — retuning them is a container
restart, not a release. So are the plan names and price labels, so the tiers can
be renamed or repriced without one either.

The pricing bullets shown in the app are **generated from these limits**, not
written alongside them: when both were maintained by hand they drifted apart
within a day.

Nothing else is gated. Strava/Polarsteps sync, posters, sharing, companions and
encounters are on every plan.

### Days per trip

A trip's length is its **calendar span — first day to last, inclusive, counting
the empty days in between**. A three-week ride with a rest week in the middle is
21 days, not 14: those days are still days of the trip, and the app shows them
as such.

The span is derived, not stored (`src/billing/trip_days.py`): it is the range
covered by the declared `trip_start`/`trip_end` *and* the dates of every
activity, memory, journal entry and transport segment in the trip. Anything that
would push the first or last day outwards is checked — creating or re-dating a
memory or journal entry, importing activities, adding a segment, declaring trip
dates.

Two rules keep this from being obnoxious:

* Dating something **inside** the existing span is always allowed, however full
  the plan is.
* A change is only ever refused if it makes the trip **longer**. A trip that was
  already over the limit when enforcement was switched on stays fully editable —
  it just cannot grow — and clearing a date is never refused.

### Whose allowance

Storage and trip length are attributed to the **trip owner**: a companion
uploading photos to a shared trip, or dating a memory outside its span, spends
the owner's allowance, because it is the owner's trip and the owner's directory
the files land in.

## Turning it on

Two switches, deliberately separate:

```
BILLING_ENABLED=1          # sell plans; measure usage; show the plan UI
BILLING_ENFORCE_QUOTAS=1   # start refusing writes past the limit (402)
```

Enable the first one alone at first. Usage counters fill in, `/api/billing/me`
reports real numbers, and nobody is blocked. Flip the second only once you have
looked at what existing accounts actually use — otherwise everyone who grew
past 500 MB before the limits existed is locked out the moment you deploy.

## Stripe setup

1. **Products and prices** — run the provisioner against the account:

   ```bash
   export STRIPE_SECRET_KEY=sk_test_...
   python scripts/stripe_catalog.py            # show what it would do
   python scripts/stripe_catalog.py --apply    # create or update
   ```

   It is idempotent (re-running an unchanged catalogue reports `ok` and touches
   nothing) and refuses an `sk_live_` key unless you also pass `--live`. Product
   names and descriptions come from `src/billing/plans.py`, so a Stripe product
   cannot advertise limits the server will not grant.

   **You do not need to copy any `price_...` id anywhere.** Each price is
   stamped with a lookup key (`traxjourney_tier_2_monthly_eur`) and the server
   resolves by that at runtime, caching for ten minutes. Lookup keys are ours and
   identical in every account, so one configuration is correct in a sandbox, in
   test mode and in live — while a price id belongs to exactly one account.
   Setting `STRIPE_PRICE_TIER_N` still works and wins, as a pin or an escape
   hatch.

   Note that **sandboxes are separate accounts**, not the same thing as test
   mode: products, prices, webhook endpoints and portal configuration all have to
   be provisioned in each one.

   A price the server cannot map to a tier grants **Tier 1** and logs a warning.
   Free would be wrong — the customer is paying for something — and granting the
   top tier would turn a config typo into an invisible revenue leak. Re-run the
   provisioner, or lift the account with the admin plan override.
2. **Webhook endpoint** — point it at `https://<host>/api/billing/webhook` and
   subscribe to:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
   - `subscription_schedule.created`
   - `subscription_schedule.updated`
   - `subscription_schedule.released`
   - `subscription_schedule.canceled`
   - `subscription_schedule.completed`

   The `subscription_schedule.*` family is what a downgrade looks like before it
   happens (see [Changing tier](#changing-tier)). Without them the app cannot
   tell anyone that the change they just asked for is coming.

   Copy the signing secret (`whsec_...`) into `STRIPE_WEBHOOK_SECRET`.
3. **Customer portal** — provisioned by the same script as the catalogue, in the
   same run. It is not optional and not a dashboard task: in-app tier switching
   opens a portal flow, and Stripe refuses the flow unless the configuration
   lists the price being switched to. `scripts/stripe_catalog.py --apply`
   creates or updates a configuration marked
   `metadata.managed_by=scripts/stripe_catalog.py`; a configuration it did not
   create is left alone.
4. **Secret key** → `STRIPE_SECRET_KEY`.

All four are **runtime** environment variables. Never pass them to
`docker build` — the published image is public.

### Local testing

```bash
export BILLING_ENABLED=1 BILLING_ENFORCE_QUOTAS=1
export STRIPE_SECRET_KEY=sk_test_...
python scripts/stripe_catalog.py --apply     # once per account; no ids to copy
stripe listen --forward-to localhost:8000/api/billing/webhook
# copy the whsec_... it prints into STRIPE_WEBHOOK_SECRET, then restart the server
```

Check out with card `4242 4242 4242 4242`, then confirm `GET /api/billing/me`
reports the tier you bought.

## Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/billing/plans` | none | Plan catalogue for the pricing UI |
| `GET /api/billing/me` | user | Plan, limits, usage |
| `POST /api/billing/checkout` | user | → Stripe Checkout URL; body carries the `plan` to buy |
| `POST /api/billing/change-plan` | user | → Stripe URL that *moves* a live subscription to another `plan` |
| `POST /api/billing/portal` | user | → Stripe Customer Portal URL |
| `POST /api/billing/webhook` | signature | Provider callbacks |
| `PUT /api/admin/users/{id}/plan` | admin | Comp an account, or clear a comp |

A refused action returns **402** with the numbers the client needs:

```json
{"detail": "…", "code": "quota_exceeded", "resource": "trip_days",
 "plan": "free", "limit": 10, "used": 10, "needed": 30}
```

`resource` is `projects`, `storage` or `trip_days`. `needed` is what the refused
action would have required — the client uses it to recommend the *cheapest* tier
that would have allowed it, rather than always pushing the most expensive.

## How it holds together

- `src/billing/plans.py` — the catalogue, the limits, `cheapest_plan_with`, and
  the price lookup keys both the provisioner and the server derive from. Pure.
- `src/billing/trip_days.py` — trip length. The arithmetic is pure; one function
  reads the database and does nothing else.
- `src/billing/entitlements.py` — which plan is in force, and the quota checks.
  `plan_from_subscription` is pure; the `ensure_*` helpers raise `QuotaExceeded`,
  which `api/router.py` maps to 402.
- `src/billing/webhook_events.py` — provider event → state. Pure, so every rule
  is tested against recorded payloads with no network. Which tier a subscription
  grants is read off the payload's price (`lookup_key`, then `metadata.plan`)
  rather than compared against configured ids, so it resolves correctly even for
  an account whose price ids were never configured here.
- `src/billing/subscriptions.py` — applies those updates idempotently. Stripe
  delivers at least once and out of order; a repeated event id is dropped, and
  an event older than the last applied one cannot move state backwards.
  `apply_schedule` is deliberately separate from `apply_update`: schedule events
  and subscription events are two streams about one account, arriving
  independently, and sharing the ordering guards would make each look like a
  stale redelivery of the other and drop it.
- `src/billing/stripe_gateway.py` — the only module that imports the Stripe SDK,
  lazily, so a self-hosted instance never loads it. `resolve_price_id` turns a
  plan into a price id by lookup key, cached for ten minutes — long enough to
  keep it off the hot path, short enough that a reprice (which moves the lookup
  key to a new price) is picked up without a deploy. Resolution is lazy for the
  same reason the import is: billing disabled must mean no Stripe call at all.
- `src/billing/usage.py` — the storage counter. Every photo write adds its bytes
  and every delete subtracts them, because walking the filesystem (what the
  admin dashboard does) is far too slow to put on an upload path. A nightly job
  (03:30 UTC) re-walks each user's tree and corrects any drift.

### Changing tier

Issue #153. **Checkout cannot change a plan** — in subscription mode it always
*creates* a subscription, so sending a subscriber there bills them twice
(issue #163). `/api/billing/checkout` refuses a live subscriber for that reason;
`/api/billing/change-plan` is where they go instead.

It returns a Customer Portal session opened straight on the confirm-change
screen (`flow_data.type=subscription_update_confirm`). The change is Stripe's to
carry out, not ours: the prorated amount, tax, 3DS re-authentication on an
upgrade and the receipt all already work there, and reimplementing them is how
you end up charging the wrong number. Nothing is written when the session is
created — the result arrives as a `customer.subscription.updated` webhook like
every other state change.

| Direction | When it applies | What the customer pays |
|---|---|---|
| Up a tier | immediately | the difference, prorated, on the spot |
| Down a tier | at the end of the paid period | nothing now; the lower price from then on |
| To **Free** | at the end of the paid period | nothing; this is a cancellation |

"Down" waiting for the period to end is the same rule as a cancellation: they
paid for that time, so they keep it. It is expressed as
`schedule_at_period_end` on `decreasing_item_amount` in the portal
configuration, in `scripts/stripe_catalog.py` — in code, so it can be reviewed,
rather than in a dashboard toggle nobody can diff.

**A scheduled change is not the plan in force.** Stripe records it as a
subscription *schedule*, and the subscription keeps reporting the old tier until
the phase runs. `Subscription.pending_plan` / `pending_plan_at` mirror that,
stored beside `plan` rather than in it — writing it into `plan` would downgrade
the account the moment they asked, which is the opposite of what they were
promised. `/api/billing/me` reports both, and the app says "Switching to Tier 1
on 3 September" instead of looking untouched.

The promise is cleared as soon as the plan actually moves, including when a
different tier is bought outright: a queued change the account can no longer
keep is worse than none.

### Grace and cancellation

`active`, `trialing` and `past_due` all keep the paid plan — a failed renewal
opens a retry window at Stripe, and cutting access off on day one of it loses
customers who only need to update a card. A cancelled subscription keeps the
plan until `current_period_end`: they paid for that time.

An admin comp (`admin_override_plan`) is stored *beside* provider state, not on
top of it, so a webhook can never silently wipe it. It accepts any plan id, so
it doubles as the fix for a mis-mapped price.

### Deleting an account

Both deletion routes (`DELETE /api/auth/me` and the admin's
`DELETE /api/admin/users/{id}`) stop billing at Stripe **before** removing any
row (issue #429, `cancel_live_subscription` in `src/auth/account_deletion.py`):

- when the account has a Stripe customer, Stripe is asked rather than our
  cached row: the customer's open checkout sessions are expired, then every
  subscription that has not ended is cancelled **immediately**, not at period
  end. The customer itself is kept — its invoices are the accounting record;
- a row with no customer falls back to its stored subscription id;
- if that fails the deletion is refused with **502**, and with **409** when a
  subscription may still bill but this deployment has no gateway configured.
  Nothing is deleted either way, and retrying is safe.
- Stripe is called without holding any database lock. The deletion then takes
  the account's write lock and re-reads the billing row; if a webhook changed
  it in between (a first purchase completing), it rolls back and answers
  **409** `billing_changed`, and the retry cancels what that purchase started.
  Webhooks naming an account take the same lock before reading anything, so a
  start event arriving mid-deletion waits and then sees the account gone.

A first purchase has no customer until it is paid, so a checkout page opened
before the deletion cannot be found then. If it is paid afterwards,
`checkout.session.completed` / `customer.subscription.created` arrive naming a
deleted account and a customer no account holds; the webhook cancels that
subscription on arrival and logs a warning.

Account ids are never reused (`userinfo` is `AUTOINCREMENT`, migration
`6abe17b5d61f`), so the `user_info_id` in Stripe metadata names its buyer or
nobody — with one exception. The migration can only start the sequence above
the *current* highest id: an account deleted before the migration with an id
above that is not remembered anywhere, and its id is handed out once more to
the next account registered. Late events for such an account, if any
subscription of it was left running, would name that newcomer.

#### When deletion is refused with 409 "billing is not configured here"

The account's cached subscription status says it may still bill, but this
deployment has no Stripe keys, so it cannot ask Stripe or cancel anything.
This happens when billing was switched off after the account subscribed.
There is deliberately no force-delete: the one safe way out is to confirm at
Stripe that nothing bills any more, then record that here.

1. Find the account and its billing row in the deployment's database (the
   file `DATABASE_URL` points at, `traxjourney.db` by default):

   ```sql
   SELECT id, email FROM userinfo WHERE email = 'user@example.com';
   SELECT provider_customer_id, provider_subscription_id, status
     FROM subscription WHERE user_info_id = <id>;
   ```

2. In the Stripe dashboard of the account this deployment used, open the
   customer `provider_customer_id` (or search for `provider_subscription_id`)
   and make sure **every** subscription is canceled. Cancel any that is not,
   and expire any open checkout session.

3. Only then, record it (back up the database first):

   ```sql
   UPDATE subscription SET status = 'canceled' WHERE user_info_id = <id>;
   ```

4. Delete the account again. With a customer on record and a status that
   has ended, deletion no longer needs the gateway.

## Where the plan UI lives

`/settings` shows a summary card — plan, anything worth flagging, and how full
the account is — and opens **`/settings/plan`**, which is the whole picture:
usage against the limits, every tier, change or cancel, and a link into the
provider's portal for invoices and the card.

`/settings/plan` is a real route rather than a pushed page because the payment
provider redirects the *browser* back to it by URL, with
`?checkout=success|cancelled`. Two consequences that have already caused bugs:

- The plan is granted by a webhook that can land after the redirect, so a single
  read on arrival shows the old plan and reads as "the payment did nothing".
  Both the page and the Settings card re-read a few times before believing it
  (issue #192).
- That redirect is a *fresh* navigation with nothing on the router stack, so an
  unconditional `context.pop()` on the back arrow is a silent no-op. Both
  screens fall back to `context.go('/')`.

## Not in scope yet

- Apple / Google in-app purchase (the App Store requires IAP for digital goods
  sold inside the iOS app, so mobile currently has no purchase flow). Checkout
  and plan changes open the provider in an external browser on every platform;
  the plan page makes them more prominent, which is worth remembering when the
  iOS build is submitted.
- Feature gates beyond trip count, storage and trip length.
- Dunning and receipt emails (Stripe sends its own for now).
- VAT: we are the merchant of record. Stripe Tax can be switched on when the
  thresholds start to matter.

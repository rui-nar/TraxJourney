"""Plan catalogue and per-plan limits (issue #121).

Pure data + pure functions: no DB, no network, no provider SDK. Everything a
caller needs to answer "what is this plan allowed to do" lives here, which keeps
the numbers testable and lets the client render the pricing table from the same
source the server enforces.

Four plans: a free tier and three paid ones. Each limit is read from the
environment *per call* (same pattern as the rest of the billing config) so the
hosted deployment can retune the numbers with a restart rather than a release.
``None`` means unlimited.

The pricing bullets are generated from the limits rather than written out
alongside them: maintained by hand the two drifted apart immediately, and a plan
page promising "2 trips" while the server allows ten is worse than no plan page.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

FREE = "free"
TIER_1 = "tier_1"
TIER_2 = "tier_2"
TIER_3 = "tier_3"

#: Ordered weakest → strongest. Used to compare two plans without a lookup table.
PLAN_ORDER = (FREE, TIER_1, TIER_2, TIER_3)

#: Everything that has to be paid for, cheapest first.
PAID_PLANS = (TIER_1, TIER_2, TIER_3)

#: What a deployment without billing behaves as — the strongest plan.
TOP_PLAN = PLAN_ORDER[-1]

#: Currency the catalogue is priced in. Baked into the lookup key so a second
#: currency can be added later without colliding with these.
LOOKUP_CURRENCY = "eur"

_LOOKUP_PREFIX = "traxjourney"


def price_lookup_key(plan: str) -> str:
    """Stable, account-independent handle for a paid plan's provider price.

    Provider price ids are scoped to one account, so the same id is meaningless
    in a sandbox and in live. Lookup keys are ours: identical everywhere, which
    is what lets one configuration be correct in every account (issue #154).

    Owned here rather than in scripts/stripe_catalog.py so the provisioner and
    the server cannot disagree about the format — the same single-source rule
    that keeps :func:`features_for` and the limits from drifting.
    """
    return f"{_LOOKUP_PREFIX}_{plan}_monthly_{LOOKUP_CURRENCY}"


def plan_for_lookup_key(key: str) -> str | None:
    """Inverse of :func:`price_lookup_key`; None when the key is not one of ours."""
    for plan in PAID_PLANS:
        if key == price_lookup_key(plan):
            return plan
    return None

_MB = 1024 * 1024

#: Environment-variable prefix per plan id.
_ENV_PREFIX = {
    FREE: "FREE",
    TIER_1: "TIER_1",
    TIER_2: "TIER_2",
    TIER_3: "TIER_3",
}

#: Default names, overridable per plan (``TIER_1_NAME``) — the tiers are still
#: being named, and renaming one should not need a release.
_DEFAULT_NAMES = {
    FREE: "Free",
    TIER_1: "Tier 1",
    TIER_2: "Tier 2",
    TIER_3: "Tier 3",
}

#: Must match what the Stripe prices actually charge — the label is cosmetic, so
#: nothing catches a drift here except a customer reading one number and being
#: billed another. scripts/stripe_catalog.py owns the amounts these mirror.
_DEFAULT_PRICE_LABELS = {
    FREE: "Free",
    TIER_1: "€0.99 / month",
    TIER_2: "€3.99 / month",
    TIER_3: "€9.99 / month",
}

#: Tallest video the renderer produces (1920x1080). What a deployment without
#: billing renders at, and the paid tiers' default.
FULL_HD_HEIGHT = 1080

#: (max_projects, max_storage_mb, max_trip_days, max_videos_per_month,
#: max_video_height). None = unlimited.
_DEFAULT_LIMITS: dict[
    str, tuple[int | None, int | None, int | None, int | None, int | None]
] = {
    FREE:   (1, 500, 10, 1, 720),
    TIER_1: (2, 5 * 1024, 100, 10, FULL_HD_HEIGHT),
    TIER_2: (10, 20 * 1024, 365, 30, FULL_HD_HEIGHT),
    TIER_3: (None, 50 * 1024, None, None, FULL_HD_HEIGHT),
}

#: What each plan offers beyond the limits, which are rendered separately.
#:
#: Every line here is a promise to someone paying for it, so each one must name
#: something the code actually provides. tests/test_plan_features_backed.py
#: fails on any string that is neither generated from the limits, mapped there to
#: the routes that implement it, nor listed in :data:`MARKETING_ONLY_FEATURES`.
#:
#: The paid tiers used to list "Weekly backups", "Daily backups" and "Priority
#: support" (issue #432). None of it existed: every plan, Free included, shares
#: one server-wide database copy, and there is no support tier. They can come
#: back once per-user backups, restore and a support channel are built.
_EXTRA_FEATURES = {
    FREE: [
        "Strava & Polarsteps import",
        "Memories, encounters, journal",
        "Share links",
    ],
}

#: Advertised lines with no code behind them, which the owner has approved as
#: marketing copy. Only the owner adds entries here — not a contributor, and not
#: a coding agent making a test pass. Anything on this list must still be true.
MARKETING_ONLY_FEATURES: frozenset[str] = frozenset()


def _env_int(name: str, default: int | None) -> int | None:
    """Read an int from the environment; empty / "unlimited" / "-1" → ``None``."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("unlimited", "none", "-1"):
        return None
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    """Per-plan ceilings. ``None`` = unlimited."""

    max_projects: int | None
    max_storage_bytes: int | None
    #: Calendar length of one trip, first day to last inclusive — empty days in
    #: the middle count, because they are still days of the trip.
    max_trip_days: int | None
    #: Video renders started in one UTC calendar month; failed ones don't count.
    max_videos_per_month: int | None
    #: Tallest video resolution the plan may render, in pixels (720 = 1280x720).
    #: ``None`` means no plan cap — the renderer's own maximum applies.
    max_video_height: int | None

    # The video limits are left out on purpose: this is the shape
    # /api/billing/me and /api/billing/plans have always served, and the video
    # routes report the requester's quota and resolutions themselves.
    def as_dict(self) -> dict:
        return {
            "max_projects": self.max_projects,
            "max_storage_bytes": self.max_storage_bytes,
            "max_trip_days": self.max_trip_days,
        }


#: Unlimited everything — what a self-hosted deployment (billing disabled) gets.
#: Videos stay capped at full HD: that is the renderer's maximum, not a tier.
UNLIMITED = Limits(max_projects=None, max_storage_bytes=None, max_trip_days=None,
                   max_videos_per_month=None, max_video_height=FULL_HD_HEIGHT)


def _mb_to_bytes(mb: int | None) -> int | None:
    return None if mb is None else mb * _MB


def known_plan(plan: str) -> bool:
    return plan in _ENV_PREFIX


def limits_for(plan: str) -> Limits:
    """Limits for a plan id. An unknown plan falls back to ``free`` (fail safe)."""
    if not known_plan(plan):
        plan = FREE
    prefix = _ENV_PREFIX[plan]
    projects, storage_mb, trip_days, videos, video_height = _DEFAULT_LIMITS[plan]
    return Limits(
        max_projects=_env_int(f"{prefix}_MAX_PROJECTS", projects),
        max_storage_bytes=_mb_to_bytes(
            _env_int(f"{prefix}_MAX_STORAGE_MB", storage_mb)),
        max_trip_days=_env_int(f"{prefix}_MAX_TRIP_DAYS", trip_days),
        max_videos_per_month=_env_int(f"{prefix}_MAX_VIDEOS_PER_MONTH", videos),
        max_video_height=_env_int(f"{prefix}_MAX_VIDEO_HEIGHT", video_height),
    )


def plan_name(plan: str) -> str:
    """Display name — what the client shows as "your plan"."""
    if not known_plan(plan):
        plan = FREE
    return (os.environ.get(f"{_ENV_PREFIX[plan]}_NAME", "").strip()
            or _DEFAULT_NAMES[plan])


def price_label(plan: str) -> str:
    """Human-readable price. Cosmetic: the provider charges what it is told to."""
    if not known_plan(plan):
        plan = FREE
    return (os.environ.get(f"{_ENV_PREFIX[plan]}_PRICE_LABEL", "").strip()
            or _DEFAULT_PRICE_LABELS[plan])


def is_at_least(plan: str, required: str) -> bool:
    """True when ``plan`` is ``required`` or stronger."""
    try:
        return PLAN_ORDER.index(plan) >= PLAN_ORDER.index(required)
    except ValueError:
        return False


def over_quota(used: int, incoming: int, limit: int | None) -> bool:
    """Would ``used + incoming`` exceed ``limit``? ``None`` limit is never over.

    The comparison is strict-greater so a value landing exactly on the limit is
    allowed: a 500 MB plan accepts the upload that brings you to exactly 500 MB.
    """
    if limit is None:
        return False
    return used + incoming > limit


def cheapest_plan_with(
    *,
    projects: int | None = None,
    storage_bytes: int | None = None,
    trip_days: int | None = None,
) -> str | None:
    """Weakest plan whose limits cover the given need, or None if none does.

    Lets the paywall offer the tier that actually solves the problem in front of
    the user, instead of always pushing the most expensive one.
    """
    for plan in PLAN_ORDER:
        limits = limits_for(plan)
        if projects is not None and over_quota(projects, 0, limits.max_projects):
            continue
        if storage_bytes is not None and over_quota(
                storage_bytes, 0, limits.max_storage_bytes):
            continue
        if trip_days is not None and over_quota(trip_days, 0, limits.max_trip_days):
            continue
        return plan
    return None


def _format_bytes(value: int) -> str:
    gb = value / (1024 * _MB)
    if gb >= 1:
        return f"{gb:.0f} GB" if gb == int(gb) else f"{gb:.1f} GB"
    return f"{value // _MB} MB"


def features_for(plan: str) -> list[str]:
    """Pricing bullets, generated from the limits so the two cannot disagree."""
    limits = limits_for(plan)

    if limits.max_projects is None:
        trips = "Unlimited trips"
    else:
        trips = f"{limits.max_projects} trip" + ("" if limits.max_projects == 1 else "s")

    if limits.max_storage_bytes is None:
        photos = "Unlimited photo storage"
    else:
        photos = f"{_format_bytes(limits.max_storage_bytes)} of photos"

    if limits.max_trip_days is None:
        days = "Trips of any length"
    else:
        days = f"Up to {limits.max_trip_days} days per trip"

    if limits.max_videos_per_month is None:
        videos = "Unlimited videos"
    else:
        n = limits.max_videos_per_month
        videos = f"{n} video{'' if n == 1 else 's'} per month"
    if limits.max_video_height is not None:
        videos += f" · {limits.max_video_height}p"

    return [trips, photos, days, videos] + _EXTRA_FEATURES.get(plan, [])


def catalogue() -> list[dict]:
    """Public plan catalogue — what the pricing UI renders."""
    return [
        {
            "id": plan,
            "name": plan_name(plan),
            "price_label": price_label(plan),
            "limits": limits_for(plan).as_dict(),
            "features": features_for(plan),
        }
        for plan in PLAN_ORDER
    ]

"""Every advertised plan feature is something the code provides (issue #432).

The paid plans once listed "Weekly backups", "Daily backups" and "Priority
support". None of it existed, and people were being asked to pay for it. This
guard makes the next unbacked line fail CI instead of shipping.

A string that ``features_for()`` / ``catalogue()`` returns must be one of:

- a limit line, rendered here *independently* from ``limits_for()`` — so a
  hand-written "10 trips" that the limits do not back is still caught;
- a key of ``_CODE_BACKED`` below;
- on ``plans.MARKETING_ONLY_FEATURES``, which only the owner edits.

What this proves is narrow. The gate is ``_CODE_BACKED`` itself: a new line
fails until someone adds it there, in a diff a reviewer reads. The routes each
entry lists are a review aid saying where the feature lives. The test checks
that those routes still exist, so removing a feature's endpoint surfaces the
stale claim, but a route existing does not prove it delivers what the line
says. Judging that is the reviewer's job. Nothing is gated by plan today, so the
test does not check which plan may use a route; add that once some feature is.

To advertise something new, build it first, then add it to ``_CODE_BACKED``
with the routes that implement it.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from api.router import app
from src.billing import plans
from src.billing.plans import PLAN_ORDER, features_for, limits_for

_MB = 1024 * 1024
_GB = 1024 * _MB

#: Advertised line -> the (method, path) routes that deliver it.
_CODE_BACKED: dict[str, tuple[tuple[str, str], ...]] = {
    # api/strava.py (sync activities into a trip) and api/polarsteps.py (read a
    # trip's steps, which the client imports as memories via api/memories.py).
    "Strava & Polarsteps import": (
        ("POST", "/api/projects/{name}/strava/sync"),
        ("GET", "/api/polarsteps/trips/{trip_id}/steps"),
    ),
    # api/memories.py, api/encounters.py, api/journal.py.
    "Memories, encounters, journal": (
        ("POST", "/api/memories/"),
        ("POST", "/api/encounters/"),
        ("POST", "/api/journal/"),
    ),
    # api/project_shares.py creates the link, api/share.py serves it.
    "Share links": (
        ("POST", "/api/projects/{name}/share"),
        ("GET", "/api/share/{token}"),
    ),
}


@pytest.fixture(autouse=True)
def _default_limits(monkeypatch):
    """Judge the shipped defaults, not whatever the host environment sets."""
    for prefix in ("FREE", "TIER_1", "TIER_2", "TIER_3"):
        for suffix in ("MAX_PROJECTS", "MAX_STORAGE_MB", "MAX_TRIP_DAYS"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)


def _limit_lines(plan: str) -> set[str]:
    """What the limits alone justify saying — written out here, not borrowed
    from plans.py, so a bullet that merely *looks* like a limit still fails."""
    limits = limits_for(plan)
    lines = set()

    n = limits.max_projects
    lines.add("Unlimited trips" if n is None else f"{n} trip{'' if n == 1 else 's'}")

    b = limits.max_storage_bytes
    if b is None:
        lines.add("Unlimited photo storage")
    elif b >= _GB:
        gb = b / _GB
        lines.add(f"{gb:.0f} GB of photos" if gb == int(gb) else f"{gb:.1f} GB of photos")
    else:
        lines.add(f"{b // _MB} MB of photos")

    d = limits.max_trip_days
    lines.add("Trips of any length" if d is None else f"Up to {d} days per trip")
    return lines


def _routes() -> set[tuple[str, str]]:
    """Every (method, path) the app serves, including nested routers."""
    found: set[tuple[str, str]] = set()

    def walk(routes):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes)
            elif isinstance(route, APIRoute):
                found.update((m, route.path) for m in route.methods)

    walk(app.routes)
    return found


def _unbacked(plan: str, features: list[str]) -> list[str]:
    allowed = _limit_lines(plan) | set(_CODE_BACKED) | plans.MARKETING_ONLY_FEATURES
    return [f for f in features if f not in allowed]


@pytest.mark.parametrize("plan", PLAN_ORDER)
def test_every_bullet_is_backed(plan):
    unbacked = _unbacked(plan, features_for(plan))
    assert not unbacked, (
        f"{plan} advertises {unbacked}, which nothing in the code provides. "
        "Build it and add it to _CODE_BACKED with its routes, or have the owner "
        "approve it in plans.MARKETING_ONLY_FEATURES."
    )


def test_the_catalogue_the_app_renders_is_backed():
    """``catalogue()`` is what /api/billing/plans serves and the plan picker
    shows; guard it directly in case it ever stops going through features_for."""
    for entry in plans.catalogue():
        unbacked = _unbacked(entry["id"], entry["features"])
        assert not unbacked, f"{entry['id']} advertises {unbacked}"


def test_every_plan_states_all_three_limits():
    """The limit oracle above and the real wording agree, and every plan shows
    its trips, photo and trip-length limits — a pricing card that silently lost
    one of them would pass the other tests."""
    for plan in PLAN_ORDER:
        assert _limit_lines(plan) <= set(features_for(plan)), plan


@pytest.mark.parametrize("feature", sorted(_CODE_BACKED))
def test_code_backed_features_have_their_routes(feature):
    missing = [r for r in _CODE_BACKED[feature] if r not in _routes()]
    assert not missing, f"{feature!r} is advertised but {missing} no longer exist"


def test_the_route_scan_finds_routes():
    """Guard the guard: an empty scan would make every route look missing, and a
    broken flatten would hide the nested routers entirely."""
    assert len(_routes()) > 50


def test_no_plan_promises_backups_or_support():
    """The specific claims issue #432 removed. Every plan shares one server-wide
    database copy and there is no support tier, so none of these may return
    until per-user backups, restore and a support channel exist."""
    for plan in PLAN_ORDER:
        for line in features_for(plan):
            lowered = line.lower()
            assert "backup" not in lowered and "support" not in lowered, (plan, line)


def test_marketing_only_list_is_owner_approved():
    """Adding to MARKETING_ONLY_FEATURES is the owner's call. Pinning it empty
    means an entry cannot slip in to turn the guard above green: whoever adds
    one has to edit this test too, in plain view of review."""
    assert plans.MARKETING_ONLY_FEATURES == frozenset()

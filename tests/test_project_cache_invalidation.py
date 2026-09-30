"""Every mutating project route must invalidate the project payload cache.

``GET /{name}/meta`` is served from a cache (issue #178), so a route that changes
what it returns without busting leaves the activity panel showing pre-edit
content until the TTL expires — a staleness bug strictly worse than the slow
load the cache exists to fix.

This walks the real router table rather than trusting a checklist: a route added
next year lands here automatically. The check is a source scan for one of the
bust helpers, which is coarse by design — it cannot prove the bust runs on every
branch, only that the route knows it has to. Routes that legitimately don't
touch project payloads are listed in ``_EXEMPT`` with the reason.
"""
from __future__ import annotations

import inspect

from fastapi.routing import APIRoute

from api.router import app

# Routers whose payloads are part of GET /{name}/meta.
_PROJECT_PREFIXES = (
    "/api/projects",
    "/api/journal",
    "/api/memories",
    "/api/people",
    "/api/groups",
    "/api/encounters",
)

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Handler name → why it needs no bust. Anything not listed must bust.
_EXEMPT = {
    # Creates an empty project; nothing can be cached for it yet.
    "create_project": "new project — no cached payload exists",
    # A POST only because it takes an upload: it reads the file and writes
    # nothing at all. Busting a cache here would be the giveaway that the dry
    # run is not dry (issue #260, unit 4).
    "inspect_gpx_file": "dry run — reads an upload, writes nothing",
    # Sync settings live in /sync-meta, not in the /meta payload.
    "update_sync_meta": "sync config is not part of the /meta payload",
    # Share tokens are read from /share-info, not from /meta.
    "create_share_link": "share tokens are not part of the /meta payload",
    "revoke_share_link": "share tokens are not part of the /meta payload",
    "create_share_link_no_memories": "share tokens are not part of the /meta payload",
    "revoke_share_link_no_memories": "share tokens are not part of the /meta payload",
    "upload_share_memory_content": "writes share ciphertext rows, not project content",
    "create_invite": "an unaccepted invite grants nobody a caller_role yet",
    "revoke_invite": "an unaccepted invite grants nobody a caller_role yet",
    "revoke_pending_invite": "an unaccepted invite grants nobody a caller_role yet",
    # Renders from the project; never writes to it.
    "create_poster_job": "poster rendering does not mutate the project",
    "preview_poster": "poster rendering does not mutate the project",
    "create_video_job": "video rendering does not mutate the project",
    "plan_video": "video rendering does not mutate the project",
    "create_video_preview_job": "video rendering does not mutate the project",
    # Delegates its write (and the bust) to a helper in the same module.
    "upload_photo": "busts via _append_photo/_append_photo_to_memory",
    "queue_photo_from_url": "background task busts via _append_photo",
}

_BUST_CALLS = ("bust_geo_cache", "bust_project_cache", "bust_project_payloads")


def _all_routes(routes):
    """Flatten the app's route table.

    ``include_router`` keeps each router nested behind an ``_IncludedRouter``
    entry (it holds the original APIRouter plus the prefix it was mounted at)
    rather than copying its routes onto the app, so a flat scan of
    ``app.routes`` sees only the four routes declared on the app itself.
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _all_routes(inner.routes)
        else:
            yield route


def _project_routes():
    for route in _all_routes(app.routes):
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith(_PROJECT_PREFIXES):
            continue
        if not (route.methods & _MUTATING):
            continue
        yield route


def test_the_scan_actually_finds_routes():
    """Guard the guard: a bad prefix list would make this suite vacuous."""
    assert len(list(_project_routes())) > 25


def test_every_mutating_project_route_busts_the_payload_cache():
    missing = []
    for route in sorted(_project_routes(), key=lambda r: r.path):
        name = route.endpoint.__name__
        if name in _EXEMPT:
            continue
        source = inspect.getsource(route.endpoint)
        if not any(call in source for call in _BUST_CALLS):
            missing.append(f"{sorted(route.methods)} {route.path} ({name})")

    assert not missing, (
        "these routes change the project payload without invalidating its cache "
        "(call bust_project_payloads/bust_geo_cache after the commit, or add an "
        "entry to _EXEMPT explaining why it is safe):\n  " + "\n  ".join(missing)
    )


def test_exemptions_still_refer_to_live_routes():
    """A stale exemption silently un-guards a route that was renamed or split."""
    live = {r.endpoint.__name__ for r in _project_routes()}
    stale = sorted(set(_EXEMPT) - live)
    assert not stale, f"_EXEMPT lists routes that no longer exist: {stale}"

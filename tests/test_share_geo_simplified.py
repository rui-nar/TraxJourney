"""Zoom level of detail for share links — issue #321.

The zoom LOD of #295 landed for the owner path only. Everyone opening a public
or shared link still got the full-resolution geometry it exists to avoid: on
the trip it was measured against, 1,465,345 coordinates and a 4.5 MB payload
against the owner's 13,273 and 448 KB, holding roughly 180 MB of Dart heap —
on the device least likely to have room for it, since a public link is the path
most often opened on a phone the owner has never seen.

``GET /api/share/{token}/geo/simplified`` closes that, keyed on the owner so it
shares every cache entry — and every bust — with the owner route. What these
pin is the part that is not just "call the same helper":

* the two builders agreed *before* one cache key was allowed to serve both;
* the project is read fresh from the DB, not from the 60 s per-token cache;
* the token is resolved before any cache is read;
* no ``aid`` reaches the key, and no visit is recorded.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.share as share_mod
import models.db as db_module
import src.tile_renderer as tile_renderer
from api.deps import get_current_user
from api.geo import _geo_cache, _geo_gen, _track_cache, bust_geo_cache
from api.geo import router as geo_router
from src.project.repo_activities import store_prepared_geometry
from api.projects import router as projects_router
from api.share import router as share_router
from models.project_db import DBActivity, DBProject, DBProjectItem, DBShareVisit
from models.user import UserInfo

TOKEN = "tok-full"
TOKEN_NO_MEM = "tok-nomem"

_POINTS = 3000

# A route no great-circle arc would ever produce: the ferry doubles back south
# before heading north-east. The share builder used the stored route_polyline
# for "rail" only, so before this fix a share viewer saw a straight arc between
# the endpoints while the owner saw this — which is what made one cache entry
# serving both illegitimate.
_FERRY_ROUTE = [
    [7.0, 45.0], [7.2, 44.0], [7.5, 44.6], [7.8, 44.2], [8.0, 45.5],
]


def _track(offset: float = 0.0) -> list[tuple[float, float]]:
    """A wobbling east-west track, so simplification has something to remove."""
    return [
        (45.0 + offset + (0.0002 if i % 2 else 0.0), 7.0 + i * 0.0001)
        for i in range(_POINTS)
    ]


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)

    _geo_cache.clear()
    _geo_gen.clear()
    _track_cache.clear()
    # The share module's per-token TTL caches are module globals; fresh ones
    # per test, so nothing leaks between them and invalidate_share_cache still
    # reaches the ones under test.
    for attr in ("_project_cache", "_project_meta_cache", "_details_cache", "_meta_cache"):
        monkeypatch.setattr(share_mod, attr, share_mod._TTLCache(ttl=60.0))
    # /{token}/geo would otherwise kick off a background raster pre-render of
    # zoom 0-10 on first call. Nothing here is about tiles.
    tile_renderer._feature_cache.clear()
    monkeypatch.setattr(tile_renderer, "_CACHE_ROOT", tmp_path)
    monkeypatch.setattr(tile_renderer, "_submit_prerender", lambda *_a, **_k: None)

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(owner)
        sess.commit()
        sess.refresh(owner)
        uid = owner.id

        project = DBProject(
            user_info_id=uid, name="Trip",
            share_token=TOKEN, share_token_no_memories=TOKEN_NO_MEM,
        )
        sess.add(project)
        sess.commit()
        sess.refresh(project)

        pts = _track()
        sess.add(DBActivity(
            id=111, user_info_id=uid, name="Ride", type="Ride",
            start_date="2026-06-01T00:00:00Z",
            summary_polyline=polyline_lib.encode(pts),
            start_latlng_json=json.dumps(list(pts[0])),
            end_latlng_json=json.dumps(list(pts[-1])),
        ))
        sess.add(DBProjectItem(
            project_id=project.id, position=0, item_type="activity", activity_id=111))
        # Client-side encrypted geometry (issue #29): the server cannot decode
        # it and must never ship the envelope as if it were a track.
        sess.add(DBActivity(
            id=222, user_info_id=uid, name="Secret", type="Ride",
            start_date="2026-06-02T00:00:00Z",
            summary_polyline="v1.YWJj.ZGVm",
        ))
        sess.add(DBProjectItem(
            project_id=project.id, position=1, item_type="activity", activity_id=222))
        sess.add(DBProjectItem(
            project_id=project.id, position=2, item_type="segment",
            segment_id="seg-1",
            segment_json=json.dumps({
                "id": "seg-1",
                "segment_type": "boat",
                "label": "Ferry",
                "start": {"lat": 45.0, "lon": 7.0},
                "end": {"lat": 45.5, "lon": 8.0},
                "route_mode": "ferry",
                "route_polyline": json.dumps(_FERRY_ROUTE),
                "route_status": "resolved",
            }),
        ))
        sess.commit()

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(share_router)
    app.include_router(geo_router)
    # The real rename route, not a stand-in: the share caches it has to drop
    # live in that route, so a test that mirrored it would exercise nothing.
    app.include_router(projects_router)
    yield TestClient(app), uid, engine


# ── helpers ──────────────────────────────────────────────────────────────────

def _simplified(client, zoom, token=TOKEN, **params):
    query = "".join(f"&{k}={v}" for k, v in params.items())
    return client.get(f"/api/share/{token}/geo/simplified?zoom={zoom}{query}")


def _features(resp) -> list[dict]:
    return resp.json()["features"]


def _coords(resp) -> int:
    """Every coordinate in the payload — the number that decides the heap."""
    return sum(len(f["geometry"]["coordinates"]) for f in _features(resp))


def _by_kind(resp, kind: str) -> list[dict]:
    return [f for f in _features(resp) if f["properties"].get("type") == kind]


def _mutate(engine, uid, offset: float) -> None:
    """Move the activity's track, exactly as a mutating route would.

    The DB write — the polyline and, with it, the prepared geometry every
    polyline writer keeps in step (issue #369) — plus ``bust_geo_cache`` is
    what every geometry-changing route does; what it deliberately does *not*
    do is invalidate the share module's per-token project cache, which is
    invalidated from a worker process and in the API process simply ages out.
    """
    with Session(engine) as sess:
        act = sess.exec(select(DBActivity).where(DBActivity.id == 111)).first()
        act.summary_polyline = polyline_lib.encode(_track(offset))
        sess.add(act)
        store_prepared_geometry(sess, act)
        sess.commit()
    bust_geo_cache(uid, "Trip")


# ── the pre-step: one builder, or one cache key is a lie ─────────────────────

def test_the_full_and_simplified_share_payloads_agree_at_zoom_22(env):
    # zoom 22 asks for a pixel of tolerance at street level, so simplification
    # has nothing to remove and the two must be the same geometry. They can
    # only be if they were built by the same code — which they were not: the
    # share builder used a segment's stored route for "rail" only.
    client, *_ = env
    full = client.get(f"/api/share/{TOKEN}/geo")
    near = _simplified(client, 22)
    assert full.status_code == near.status_code == 200

    def geometry(resp):
        return {
            (f["properties"].get("type"), f["properties"].get("activity_id")
             or f["properties"].get("segment_id")): f["geometry"]["coordinates"]
            for f in _features(resp)
        }

    assert geometry(near) == geometry(full)


def test_a_ferry_segment_follows_its_resolved_route_on_both_share_payloads(env):
    # The divergence itself, named: a great-circle arc between the endpoints is
    # 50 points and never goes south of them, the stored route does both.
    client, *_ = env
    for resp in (client.get(f"/api/share/{TOKEN}/geo"), _simplified(client, 22)):
        seg = _by_kind(resp, "segment")
        assert len(seg) == 1
        assert seg[0]["geometry"]["coordinates"] == _FERRY_ROUTE


# ── what the endpoint is for ─────────────────────────────────────────────────

def test_a_coarse_zoom_ships_an_order_of_magnitude_fewer_coordinates(env):
    client, *_ = env
    full = client.get(f"/api/share/{TOKEN}/geo")
    coarse = _simplified(client, 8)
    assert coarse.status_code == 200
    assert _coords(coarse) * 10 < _coords(full), (
        "a public link holding a fraction of the geometry is the whole point"
    )


def test_more_detail_arrives_as_the_viewer_zooms_in(env):
    client, *_ = env
    far = _coords(_simplified(client, 8))
    near = _coords(_simplified(client, 17))
    assert far < near <= _coords(client.get(f"/api/share/{TOKEN}/geo"))


def test_a_repeat_request_for_the_same_level_is_a_cache_hit(env):
    client, *_ = env
    assert _simplified(client, 10).headers["x-cache"] == "MISS"
    assert _simplified(client, 10).headers["x-cache"] == "HIT"


def test_the_share_route_shares_the_owner_route_s_cache(env):
    # Same two layers, same keys — the owner's id and project name. This is
    # what makes one bust per mutation enough for both.
    client, *_ = env
    assert client.get(
        "/api/geo/project/simplified?name=Trip&zoom=10").headers["x-cache"] == "MISS"
    assert _simplified(client, 10).headers["x-cache"] == "HIT"


def test_a_malformed_box_is_rejected_rather_than_ignored(env):
    client, *_ = env
    assert _simplified(client, 12, bbox="9,44,6,46").status_code == 400


def test_a_box_scopes_the_answer_without_dropping_a_feature(env):
    client, *_ = env
    whole = _simplified(client, 17)
    # A box over the ferry's detour, nowhere near the activity's track.
    scoped = _simplified(client, 17, bbox="7.1,44.0,7.9,44.7")
    assert scoped.status_code == 200
    assert len(_features(scoped)) == len(_features(whole))
    assert _coords(scoped) < _coords(whole)


# ── freshness: the 60 s window must not become a 15 minute one ───────────────

def test_a_mutation_inside_the_token_cache_window_still_serves_fresh_geometry(env):
    """The regression the design note is about (issue #321, correction 3).

    ``_get_project_and_type`` hands back a project cached for 60 s per token.
    Preparing a track from *that* would store a copy up to a minute stale into
    a cache held for 900 s — turning a one-minute window into a quarter-hour
    one. So the track cache is filled from a fresh DB read instead.
    """
    client, uid, engine = env
    before = _simplified(client, 12)
    assert before.status_code == 200
    assert _by_kind(before, "activity")[0]["geometry"]["coordinates"][0][1] \
        == pytest.approx(45.0, abs=1e-4)

    _mutate(engine, uid, offset=10.0)

    # The window is genuinely open: the share module still holds its own,
    # now-stale, copy of the project. Nothing about a geo bust evicts it.
    assert share_mod._project_meta_cache.get(TOKEN) is not None \
        or share_mod._project_cache.get(TOKEN) is not None

    after = _simplified(client, 12)
    assert after.status_code == 200
    assert _by_kind(after, "activity")[0]["geometry"]["coordinates"][0][1] \
        == pytest.approx(55.0, abs=1e-4), "served the pre-mutation track"


def test_a_mutation_busts_every_zoom_level(env):
    client, uid, engine = env
    levels = (8, 12, 17)
    assert _simplified(client, 8).headers["x-cache"] == "MISS"
    for zoom in levels:
        # One prepared trip serves every level (issue #369).
        assert _simplified(client, zoom).headers["x-cache"] == "HIT"

    _mutate(engine, uid, offset=10.0)

    assert _simplified(client, 8).headers["x-cache"] == "MISS", "the bust reached the trip"
    for zoom in levels:
        resp = _simplified(client, zoom)
        assert _by_kind(resp, "activity")[0]["geometry"]["coordinates"][0][1] \
            == pytest.approx(55.0, abs=1e-4), f"level {zoom} served a stale entry"


# ── the auth boundary ────────────────────────────────────────────────────────

def test_a_revoked_token_404s_against_a_warm_cache(env):
    """Revocation has to win over a level that is already warm.

    Which it only does because the token is resolved before any cache is read.
    Reading the byte cache first — it is keyed on the owner, not the token —
    would have served the geometry to a link that no longer exists.
    """
    client, _uid, engine = env
    assert _simplified(client, 10).headers["x-cache"] == "MISS"
    assert _simplified(client, 10).headers["x-cache"] == "HIT"  # warm

    with Session(engine) as sess:
        row = sess.exec(select(DBProject).where(DBProject.share_token == TOKEN)).first()
        row.share_token = None
        sess.add(row)
        sess.commit()
    share_mod.invalidate_share_cache(TOKEN)  # what revoke_share_link does

    assert _simplified(client, 10).status_code == 404
    # The other token is untouched, and still warm.
    assert _simplified(client, 10, token=TOKEN_NO_MEM).status_code == 200


def test_an_unknown_token_is_404_at_every_zoom(env):
    client, *_ = env
    for zoom in (0, 8, 12, 22):
        assert _simplified(client, zoom, token="nope").status_code == 404


def test_a_token_grants_exactly_what_it_granted_before_at_every_zoom(env):
    """Both tokens already grant the same geometry; that must not change.

    Memory items are what the no-memories token strips, and they are not
    geometry — so the two share payloads were identical before this endpoint
    existed and stay identical at every level of it.
    """
    client, *_ = env

    def identity(resp):
        return sorted(
            (f["properties"].get("type"),
             str(f["properties"].get("activity_id") or f["properties"].get("segment_id")))
            for f in _features(resp)
        )

    baseline = identity(client.get(f"/api/share/{TOKEN}/geo"))
    assert identity(client.get(f"/api/share/{TOKEN_NO_MEM}/geo")) == baseline
    for zoom in (0, 8, 12, 17, 22):
        assert identity(_simplified(client, zoom)) == baseline
        assert identity(_simplified(client, zoom, token=TOKEN_NO_MEM)) == baseline


def test_encrypted_geometry_is_never_shipped_at_any_zoom(env):
    # Issue #29 geometry is skipped, not passed through as a track. A new
    # payload kind is a new place for the envelope to leak from.
    client, *_ = env
    for resp in [client.get(f"/api/share/{TOKEN}/geo")] + [
            _simplified(client, z) for z in (0, 8, 17, 22)]:
        assert "v1.YWJj.ZGVm" not in resp.text
        assert not [f for f in _features(resp)
                    if f["properties"].get("activity_id") == "222"]


def test_out_of_range_zoom_is_rejected(env):
    client, *_ = env
    for zoom in (-1, 23, 100):
        assert _simplified(client, zoom).status_code == 400


# ── aid and visit recording (issue #321, correction 2) ───────────────────────

def test_the_visitor_id_is_not_part_of_the_cache_key(env):
    """``aid`` is who is looking, not what they asked for.

    The issue proposed keying on it. That would mint an entry per visitor per
    zoom level on exactly the links most people open — the OOM this endpoint
    exists to prevent, self-inflicted.
    """
    client, *_ = env
    assert _simplified(client, 10).headers["x-cache"] == "MISS"
    assert _simplified(client, 10, aid="visitor-a").headers["x-cache"] == "HIT"
    assert _simplified(client, 10, aid="visitor-b").headers["x-cache"] == "HIT"


def test_no_visit_is_recorded_however_much_the_viewer_zooms(env):
    """One shared load is one visit, and /meta already records it.

    This route fires again on every zoom-bucket change and every pan; a DB
    write on each would be pure cost for a number that cannot go up.
    """
    client, _uid, engine = env
    for zoom in (8, 10, 12, 15, 17):
        assert _simplified(client, zoom, aid="visitor-a").status_code == 200

    with Session(engine) as sess:
        assert sess.exec(select(DBShareVisit)).all() == []

    # And the route that does record still does.
    assert client.get(f"/api/share/{TOKEN}/meta?aid=visitor-a").status_code == 200
    with Session(engine) as sess:
        assert len(sess.exec(select(DBShareVisit)).all()) == 1


# ── a rename must not strand the shared link (issue #321 review) ─────────────

def test_a_rename_does_not_break_the_shared_zoom_endpoint(env):
    """The share caches hold the project under the name it had when they warmed.

    ``/{token}/geo`` survives a rename because it builds from the cached project
    *object*, where the name is irrelevant. The zoom-simplified route resolves by
    ``(owner, name)`` instead, so before the rename route learned to drop the
    share caches this 404'd for as long as the 60 s per-token entry lived — and
    the client fell back to the full-resolution payload this endpoint exists to
    avoid. Any level it *did* build would also be keyed under a name no later
    bust would target.
    """
    client, uid, _engine = env
    # Warm both the per-token project cache and a simplified level.
    assert client.get(f"/api/share/{TOKEN}/geo").status_code == 200
    assert _simplified(client, 10).status_code == 200

    renamed = client.put("/api/projects/Trip", json={"new_name": "Trip Renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Trip Renamed"

    # The token is unchanged, so the link must keep working — at every level,
    # including one that was never warmed.
    assert client.get(f"/api/share/{TOKEN}/geo").status_code == 200
    for zoom in (10, 14):
        resp = _simplified(client, zoom)
        assert resp.status_code == 200, f"zoom {zoom}: {resp.status_code} {resp.text}"
        assert _coords(resp) > 0


def test_a_renamed_project_is_still_bustable_for_shared_viewers(env):
    """The level a share viewer gets after a rename must still answer to a bust.

    Guards the second-order half: serving from an entry keyed under the stale
    name would look fine here and then never be invalidated again.
    """
    client, uid, engine = env
    assert _simplified(client, 10).status_code == 200
    assert client.put(
        "/api/projects/Trip", json={"new_name": "Trip Renamed"}).status_code == 200
    before = _coords(_simplified(client, 10))

    _mutate(engine, uid, 5.0)
    bust_geo_cache(uid, "Trip Renamed")
    after = _simplified(client, 10)
    assert after.status_code == 200
    assert _coords(after) == before  # same shape, moved
    assert _features(after)[0]["geometry"]["coordinates"] != []


# ── the low-res placeholder: the last copy of the segment rule (#380) ────────

def test_a_shared_ferry_is_drawn_along_its_route_in_the_low_res_payload(env):
    """The low-res payload is replaced within a second or so — on a fast
    connection. On the slow phone a share link is most often opened on, a
    routed ferry flashed as a straight arc because this endpoint kept a
    rail-only copy of the rule."""
    client, _, _ = env

    (seg,) = _by_kind(client.get(f"/api/share/{TOKEN}/geo/low-res"), "segment")

    assert seg["geometry"]["coordinates"] == _FERRY_ROUTE


def test_the_share_and_owner_low_res_payloads_are_one_builder(env):
    """Every earlier divergence started as a copy patched in one place only."""
    client, _, _ = env

    shared = client.get(f"/api/share/{TOKEN}/geo/low-res").json()
    owner = client.get("/api/geo/project/low-res?name=Trip").json()

    assert shared == owner

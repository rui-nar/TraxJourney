"""Segment features say which route state they draw — issue #278.

The client keeps a resolved route it has just applied until a server fetch
shows the same route, so a fetch that left before the resolve landed cannot put
the arc back. It used to compare ``route_mode`` only, and the segment PUT stores
``rail`` before the resolve runs: the pre-resolve arc already said ``rail``, and
was taken for the route.

Every geo payload a client reconciles against therefore carries
``route_status``, and — for a feature drawn from the stored route — a
``route_hash`` of it, so a re-resolve is told from the route it replaced. What
these pin:

* both properties, on the full, the simplified and the share simplified
  payloads, for pending, resolved and failed segments;
* the hash is the CRC-32 of the very string ``/meta`` hands the client, which
  is what the client hashes;
* a resolve's verdict is visible through the caches the next fetch reads.
"""
from __future__ import annotations

import json
import zlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.share as share_mod
import models.db as db_module
import src.tile_renderer as tile_renderer
from api.deps import get_current_user
from api.geo import _geo_cache, _geo_gen, _repo, _track_cache, bust_geo_cache
from api.geo import router as geo_router
from api.projects import router as projects_router
from api.share import router as share_router
from models.project_db import DBProject, DBProjectItem
from models.user import UserInfo

TOKEN = "tok-route-status"

_ROUTE = json.dumps([[7.0, 45.0], [7.2, 44.0], [7.5, 44.6], [7.8, 44.2], [8.0, 45.5]])
_NEW_ROUTE = json.dumps([[7.0, 45.0], [7.4, 45.9], [7.6, 45.1], [8.0, 45.5]])


def _segment(seg_id: str, status: str, polyline: str | None) -> str:
    return json.dumps({
        "id": seg_id,
        "segment_type": "train",
        "label": seg_id,
        "start": {"lat": 45.0, "lon": 7.0},
        "end": {"lat": 45.5, "lon": 8.0},
        # What the segment PUT stores for a train set to follow its route —
        # before the resolve has run, too.
        "route_mode": "rail",
        "route_polyline": polyline,
        "route_status": status,
    })


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
    for attr in ("_project_cache", "_project_meta_cache", "_details_cache", "_meta_cache"):
        monkeypatch.setattr(share_mod, attr, share_mod._TTLCache(ttl=60.0))
    tile_renderer._feature_cache.clear()
    monkeypatch.setattr(tile_renderer, "_CACHE_ROOT", tmp_path)
    monkeypatch.setattr(tile_renderer, "_submit_prerender", lambda *_a, **_k: None)

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(owner)
        sess.commit()
        sess.refresh(owner)
        uid = owner.id

        project = DBProject(user_info_id=uid, name="Trip", share_token=TOKEN)
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        pid = project.id

        for pos, (seg_id, status, polyline) in enumerate([
            ("pending", "pending", None),
            ("resolved", "resolved", _ROUTE),
            ("failed", "failed", None),
        ]):
            sess.add(DBProjectItem(
                project_id=pid, position=pos, item_type="segment",
                segment_id=seg_id, segment_json=_segment(seg_id, status, polyline),
            ))
        sess.commit()

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(share_router)
    app.include_router(geo_router)
    app.include_router(projects_router)
    yield TestClient(app), uid, pid, engine


_PAYLOADS = {
    "full": lambda c: c.get("/api/geo/project?name=Trip"),
    "simplified": lambda c: c.get("/api/geo/project/simplified?name=Trip&zoom=8"),
    "share simplified": lambda c: c.get(f"/api/share/{TOKEN}/geo/simplified?zoom=8"),
}


def _segments(resp) -> dict[str, dict]:
    assert resp.status_code == 200
    return {f["properties"]["segment_id"]: f["properties"]
            for f in resp.json()["features"]
            if f["properties"].get("type") == "segment"}


@pytest.mark.parametrize("payload", list(_PAYLOADS))
def test_every_segment_feature_carries_its_route_status(env, payload):
    client, *_ = env
    segs = _segments(_PAYLOADS[payload](client))
    assert {k: v["route_status"] for k, v in segs.items()} == {
        "pending": "pending", "resolved": "resolved", "failed": "failed",
    }
    # The pre-resolve arc says 'rail' as well — the reason route_mode alone
    # could not tell it from the route.
    assert segs["pending"]["route_mode"] == "rail"


@pytest.mark.parametrize("payload", list(_PAYLOADS))
def test_only_a_feature_drawn_from_the_stored_route_carries_its_hash(env, payload):
    client, *_ = env
    segs = _segments(_PAYLOADS[payload](client))
    assert segs["resolved"]["route_hash"] == zlib.crc32(_ROUTE.encode("utf-8"))
    assert "route_hash" not in segs["pending"]
    assert "route_hash" not in segs["failed"]


def test_the_hash_is_of_the_string_meta_hands_the_client(env):
    # The client hashes the route_polyline it reads off /meta when it applies
    # a resolve; a hash of anything else would never match, and the patch
    # would never be let go.
    client, *_ = env
    meta = client.get("/api/projects/Trip/meta")
    assert meta.status_code == 200
    polyline = next(
        i["segment"]["route_polyline"] for i in meta.json()["items"]
        if i["item_type"] == "segment" and i["segment"]["id"] == "resolved")
    segs = _segments(_PAYLOADS["full"](client))
    assert segs["resolved"]["route_hash"] == zlib.crc32(polyline.encode("utf-8"))


@pytest.mark.parametrize("payload", list(_PAYLOADS))
def test_a_resolve_verdict_is_served_past_the_caches(env, payload):
    # A resolve job writes its verdict and busts the geo caches; the next
    # fetch must not see the pending state, or the route it drew before.
    client, uid, pid, engine = env
    fetch = _PAYLOADS[payload]
    assert _segments(fetch(client))["pending"]["route_status"] == "pending"
    before = _segments(fetch(client))["resolved"]["route_hash"]

    with Session(engine) as sess:
        _repo.update_segment_fields(sess, pid, "pending", {
            "route_status": "resolved", "route_polyline": _ROUTE})
        _repo.update_segment_fields(sess, pid, "resolved", {
            "route_status": "resolved", "route_polyline": _NEW_ROUTE})
        sess.commit()
    bust_geo_cache(uid, "Trip")

    segs = _segments(fetch(client))
    assert segs["pending"]["route_status"] == "resolved"
    assert segs["pending"]["route_hash"] == before
    assert segs["resolved"]["route_hash"] == zlib.crc32(_NEW_ROUTE.encode("utf-8"))

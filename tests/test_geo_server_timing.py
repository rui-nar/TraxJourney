"""``Server-Timing`` on the simplified geometry endpoints — issue #401.

A slow map refetch on a device could be the server, or the wait around it;
the client cannot tell which without the server's own time. Both simplified
routes — the owner's and the share link's — answer with it, on both of their
return paths: the byte-cache HIT (``cache;desc=hit`` and ``total``) and the
build (``load``, ``build``, ``gzip``, ``total``). It is a header only: the body
must be the very bytes it was before, and ``X-Cache`` stays.
"""
from __future__ import annotations

import gzip
import json
import re

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.share as share_mod
import models.db as db_module
from api.deps import get_current_user
from api.geo import _geo_cache, _geo_gen, _track_cache
from api.geo import router as geo_router
from api.share import router as share_router
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo

TOKEN = "tok-timing"

_OWNER_URL = "/api/geo/project/simplified?name=Trip&zoom=9"
_SHARE_URL = f"/api/share/{TOKEN}/geo/simplified?zoom=9"


@pytest.fixture
def client(monkeypatch):
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
        pts = [(45.0 + (0.0002 if i % 2 else 0.0), 7.0 + i * 0.0001) for i in range(500)]
        sess.add(DBActivity(
            id=111, user_info_id=uid, name="Ride", type="Ride",
            start_date="2026-06-01T00:00:00Z",
            summary_polyline=polyline_lib.encode(pts),
            start_latlng_json=json.dumps(list(pts[0])),
            end_latlng_json=json.dumps(list(pts[-1])),
        ))
        sess.add(DBProjectItem(
            project_id=project.id, position=0, item_type="activity", activity_id=111))
        sess.commit()

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(geo_router)
    app.include_router(share_router)
    yield TestClient(app)
    _geo_cache.clear()
    _geo_gen.clear()
    _track_cache.clear()


_METRIC = re.compile(r"^([A-Za-z0-9_-]+)((?:;[a-z]+=[^;,]+)*)$")


def _parse(header: str) -> dict[str, dict[str, str]]:
    """``Server-Timing`` as ``{metric: {param: value}}``, failing on bad syntax."""
    out: dict[str, dict[str, str]] = {}
    for entry in header.split(", "):
        m = _METRIC.match(entry)
        assert m, f"unparseable Server-Timing entry {entry!r} in {header!r}"
        params = dict(p.split("=", 1) for p in m.group(2).split(";")[1:])
        if "dur" in params:
            # Numbers only: a unit suffix ("12ms") is not valid here.
            assert float(params["dur"]) >= 0.0, header
            assert re.fullmatch(r"\d+(\.\d+)?", params["dur"]), header
        out[m.group(1)] = params
    return out


def _body(resp) -> bytes:
    """The decoded body the client sees."""
    return resp.content


@pytest.mark.parametrize("url", [_OWNER_URL, _SHARE_URL], ids=["owner", "share"])
def test_a_build_reports_each_phase_and_the_total(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.headers["X-Cache"] == "MISS"
    timing = _parse(resp.headers["Server-Timing"])
    assert list(timing) == ["load", "build", "gzip", "total"]
    durs = {k: float(v["dur"]) for k, v in timing.items()}
    # The total spans the phases, plus whatever ran around them. Rounding to
    # 0.1 ms each can cost the sum up to 0.15 ms against the rounded total.
    assert durs["total"] + 0.15 >= durs["load"] + durs["build"] + durs["gzip"]


@pytest.mark.parametrize("url", [_OWNER_URL, _SHARE_URL], ids=["owner", "share"])
def test_a_byte_cache_hit_reports_the_hit_and_the_total(client, url):
    first = client.get(url)
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.headers["X-Cache"] == "HIT"
    timing = _parse(resp.headers["Server-Timing"])
    assert list(timing) == ["cache", "total"]
    assert timing["cache"] == {"desc": "hit"}
    assert set(timing["total"]) == {"dur"}
    # Same bytes on both paths.
    assert _body(resp) == _body(first)


@pytest.mark.parametrize("url", [_OWNER_URL, _SHARE_URL], ids=["owner", "share"])
def test_the_body_is_unchanged_by_the_header(client, url):
    # What goes on the wire is still exactly the gzip the builder made and
    # cached, on the build and on the HIT alike.
    built = client.get(url)
    (gz_bytes, _deadline, _gen), = _geo_cache.values()
    hit = client.get(url)
    for resp in (built, hit):
        assert resp.headers["Content-Encoding"] == "gzip"
        assert _body(resp) == gzip.decompress(gz_bytes)
    assert json.loads(_body(built))["type"] == "FeatureCollection"


def test_both_routes_answer_the_same_body(client):
    assert _body(client.get(_OWNER_URL)) == _body(client.get(_SHARE_URL))


@pytest.mark.parametrize("url", [_OWNER_URL, _SHARE_URL], ids=["owner", "share"])
def test_a_cross_origin_client_may_read_it(client, url):
    # The deployed web client is same-origin; a dev client on another port
    # is not, and CORS hides any header not listed here.
    for resp in (client.get(url), client.get(url)):
        assert "Server-Timing" in resp.headers["Access-Control-Expose-Headers"]

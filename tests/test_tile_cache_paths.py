"""The share-tile cache reads only from inside its own directory.

``get_cached_tile`` is the fast path of ``/api/share/{token}/tiles/...`` and
runs before the token is looked up, so the token it is given comes straight
from the URL.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import models.db as db_module
import src.tile_renderer as tr


@pytest.fixture
def cache(monkeypatch, tmp_path):
    root = tmp_path / "data" / "tiles"
    root.mkdir(parents=True)
    monkeypatch.setattr(tr, "_CACHE_ROOT", root)
    # A tile-shaped file at every place the tokens below resolve to: beside
    # the cache directory, at its top level, and (on Windows, where "\" is a
    # separator) under data/data.
    for where in (tmp_path / "data", root, tmp_path / "data" / "data"):
        (where / "0" / "0").mkdir(parents=True, exist_ok=True)
        (where / "0" / "0" / "0.png").write_bytes(b"not-a-cached-tile")
    # Real directories, so the kernel can walk "a/.." and "x/../..".
    (root / "a").mkdir()
    (root / "x").mkdir()
    return root


@pytest.mark.parametrize("token", [
    "..",        # -> data/0/0/0.png
    ".",         # -> tiles/0/0/0.png
    "",          # -> tiles/0/0/0.png
    "../tiles",  # -> tiles/0/0/0.png
    "a/..",      # -> tiles/0/0/0.png
    "x/../..",   # -> data/0/0/0.png
    "..\\data",  # -> data/data/0/0/0.png on Windows
])
def test_token_that_is_not_one_directory_name_reads_nothing(cache, token):
    assert tr.get_cached_tile(token, 0, 0, 0) is None


def test_cached_tile_is_returned_for_a_real_token(cache):
    token = "0f8fad5b-d9cb-469f-a165-70867728950e"
    (cache / token / "0" / "0").mkdir(parents=True)
    (cache / token / "0" / "0" / "0.png").write_bytes(b"tile")
    assert tr.get_cached_tile(token, 0, 0, 0) == b"tile"


def test_share_tile_route_does_not_answer_from_outside_the_cache(cache, monkeypatch):
    """Through the app, with the raw path so ``..`` reaches the route."""
    from api.router import app

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http",
        "path": "/api/share/../tiles/0/0/0.png",
        "raw_path": b"/api/share/../tiles/0/0/0.png",
        "root_path": "", "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m for m in sent if m["type"] == "http.response.start")["status"]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"not-a-cached-tile" not in body
    # No such share: the lookup that follows a cache miss answers it.
    assert status == 404

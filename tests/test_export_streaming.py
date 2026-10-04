"""Exports are sent whole or in fixed-size pieces, never split on newlines (#484).

The ZIP export was built in a ``BytesIO`` and handed to ``StreamingResponse``,
which iterates a file object line by line: binary photo data then went out in
pieces of whatever size lay between two ``\\n`` bytes, up to the whole archive.
The ``.traxj`` and GPX exports went out the same way.

The ZIP is now written to a spooled temp file and streamed in pieces of at
most 64 KiB. ``.traxj`` and GPX are built in memory anyway, so they are sent
as one complete body, with a ``Content-Length``.
"""

from __future__ import annotations

import asyncio
import io
import json
import random
import zipfile

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.journal as journal_mod
import api.memories as memories_mod
import api.project_shared as project_shared_mod
import api.project_transfer as project_transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from api.project_transfer import (
    export_project_gpx, export_project_traxj, export_project_zip,
)
from models.user import UserInfo
from src.project.project_io import ProjectIO
from tests.test_export_compact import _trip

_CHUNK = 64 * 1024


@pytest.fixture
def env(monkeypatch, tmp_path):
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, project_transfer_mod, storage_mod,
                journal_mod, memories_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="A", email="a@e.com")
        sess.add(user)
        sess.commit()
        uid = user.id
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app, raise_server_exceptions=False), {"sub": str(uid)}
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _noise_jpeg() -> bytes:
    """A photo that barely compresses: a few hundred KB of JPEG, full of
    newline bytes."""
    rng = random.Random(484)
    img = Image.frombytes("RGB", (400, 400), bytes(rng.getrandbits(8) for _ in range(400 * 400 * 3)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _alps_with_a_photo(client) -> bytes:
    r = client.post("/api/projects/import", files={
        "file": (f"Alps{ProjectIO.EXTENSION}", _trip(2, 100), "application/json")})
    assert r.status_code == 201, r.text
    detail = client.get("/api/projects/Alps").json()
    (mid,) = [i["memory"]["id"] for i in detail["items"] if i["item_type"] == "memory"]
    photo = _noise_jpeg()
    r = client.post(f"/api/memories/{mid}/photos",
                    files={"file": ("p.jpg", photo, "image/jpeg")})
    assert r.status_code == 201, r.text
    return photo


def _chunks(response: StreamingResponse) -> list[bytes]:
    async def collect():
        return [chunk async for chunk in response.body_iterator]
    return asyncio.run(collect())


def test_a_zip_export_streams_in_pieces_of_at_most_64_kib(env):
    client, user = env
    photo = _alps_with_a_photo(client)
    assert len(photo) > 2 * _CHUNK and photo.count(b"\n") > 10

    response = export_project_zip("Alps", user)
    chunks = _chunks(response)

    assert len(chunks) > 2
    assert max(len(c) for c in chunks) <= _CHUNK
    # Every piece but the last is full: a fixed size, not a line.
    assert all(len(c) == _CHUNK for c in chunks[:-1])
    zf = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    assert photo in [zf.read(n) for n in zf.namelist() if n.startswith("photos/")]


def test_the_zip_export_downloads_whole(env):
    client, _ = env
    photo = _alps_with_a_photo(client)

    r = client.get("/api/projects/Alps/export-zip")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="Alps.zip"'
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None
    assert photo in [zf.read(n) for n in zf.namelist() if n.startswith("photos/")]


def test_the_traxj_export_is_one_complete_body(env):
    client, user = env
    _alps_with_a_photo(client)

    response = export_project_traxj("Alps", user)

    assert not isinstance(response, StreamingResponse)
    assert json.loads(response.body)["name"] == "Alps"
    # Uncompressed, so Content-Length is the body's own length.
    r = client.get("/api/projects/Alps/export-traxj", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200, r.text
    assert r.content == response.body
    assert r.headers["content-length"] == str(len(r.content))
    assert r.headers["content-type"] == "application/json"
    assert r.headers["content-disposition"] == \
        f'attachment; filename="Alps{ProjectIO.EXTENSION}"'


def test_the_gpx_export_is_one_complete_body(env):
    client, user = env
    _alps_with_a_photo(client)

    response = export_project_gpx("Alps", user)

    assert not isinstance(response, StreamingResponse)
    # GPX is XML over many lines: it goes out whole, not a line at a time.
    assert response.body.count(b"\n") > 1
    # Uncompressed, so Content-Length is the body's own length.
    r = client.get("/api/projects/Alps/export", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200, r.text
    assert r.content == response.body
    assert r.headers["content-length"] == str(len(r.content))
    assert r.headers["content-type"] == "application/gpx+xml"
    assert r.headers["content-disposition"] == 'attachment; filename="Alps.gpx"'

"""``POST /api/projects/import`` has a fixed size cap (issue #434, owner decision).

The import used to be bounded only by the plan's storage quota: off on a
self-hosted instance, so unbounded there, and on a hosted one a refusal that
told the user to upgrade for "storage" the import no longer uses. Now a fixed
cap applies whatever the billing settings, answered with 413 and a plain
"too large" message, and an oversized body is refused as it arrives rather
than after the server has taken it all in.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.project_shared as project_shared_mod
import api.project_transfer as transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from api.project_transfer import router as project_transfer_router
from api.projects import router as projects_router
from models.billing import UserUsage
from models.user import UserInfo

_CAP = 8 * 1024  # stands in for the real cap, so tests stay small


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(project_shared_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(transfer_mod, "MAX_IMPORT_BYTES", _CAP)
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(projects_router)
    app.include_router(project_transfer_router)
    return app, TestClient(app), engine, uid


def _project_file(size: int) -> bytes:
    """A valid project file of exactly ``size`` bytes."""
    doc = {"version": 1, "name": "x", "items": [], "activities": [], "pad": ""}
    base = len(json.dumps(doc).encode())
    doc["pad"] = "x" * (size - base)
    raw = json.dumps(doc).encode()
    assert len(raw) == size
    return raw


def _import(client, content: bytes):
    return client.post(
        "/api/projects/import",
        files={"file": ("Trip.traxj", content, "application/json")},
    )


def _names(client) -> set[str]:
    return {p["name"] for p in client.get("/api/projects/").json()}


def test_a_file_over_the_cap_is_refused_with_413_and_nothing_imported(env):
    _app, client, _engine, _uid = env

    r = _import(client, _project_file(_CAP + 1))

    assert r.status_code == 413, r.text
    detail = r.json()["detail"]
    assert "too large" in detail
    assert "upgrade" not in detail.lower()
    assert _names(client) == set()


def test_a_file_exactly_at_the_cap_is_imported(env):
    _app, client, _engine, _uid = env

    r = _import(client, _project_file(_CAP))

    assert r.status_code == 201, r.text
    assert _names(client) == {"Trip"}


def test_the_cap_applies_with_billing_and_quotas_off(env):
    """The default, self-hosted configuration: no plan limits at all."""
    _app, client, _engine, _uid = env

    assert _import(client, _project_file(_CAP * 4)).status_code == 413


def test_the_storage_quota_no_longer_applies_to_imports(env, monkeypatch):
    """An import stores nothing on disk, so a full storage quota must not stop it."""
    _app, client, engine, uid = env
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    with Session(engine) as sess:
        sess.add(UserUsage(user_info_id=uid, storage_bytes=100 * 1024 ** 3))
        sess.commit()

    r = _import(client, _project_file(1000))

    assert r.status_code == 201, r.text


# ── Refused as it arrives, not after the whole body is in ─────────────────────

def _call(app: FastAPI, headers: list[tuple[bytes, bytes]], chunks: list[bytes]):
    """Drive the ASGI app directly; returns (status, chunks the app consumed)."""
    consumed = 0
    sent: list[dict] = []

    async def receive():
        nonlocal consumed
        if consumed < len(chunks):
            consumed += 1
            return {"type": "http.request", "body": chunks[consumed - 1],
                    "more_body": consumed < len(chunks)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/projects/import",
        "raw_path": b"/api/projects/import", "root_path": "", "query_string": b"",
        "headers": headers, "client": ("test", 1), "server": ("test", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], consumed


_BOUNDARY = b"cap-test-boundary"


def _multipart_chunks(file_bytes: int, chunk: int = 4096) -> list[bytes]:
    head = (b"--" + _BOUNDARY + b"\r\nContent-Disposition: form-data; name=\"file\"; "
            b"filename=\"Trip.traxj\"\r\nContent-Type: application/json\r\n\r\n")
    body = head + b"x" * file_bytes + b"\r\n--" + _BOUNDARY + b"--\r\n"
    return [body[i:i + chunk] for i in range(0, len(body), chunk)]


_CT = (b"content-type", b"multipart/form-data; boundary=" + _BOUNDARY)


def test_a_declared_oversized_body_is_refused_before_any_of_it_is_read(env):
    app, _client, _engine, _uid = env
    chunks = _multipart_chunks(_CAP * 100)
    length = str(sum(len(c) for c in chunks)).encode()

    status, consumed = _call(app, [_CT, (b"content-length", length)], chunks)

    assert status == 413
    assert consumed == 0


def test_an_undeclared_oversized_body_is_cut_off_once_past_the_cap(env):
    """Chunked transfer, no Content-Length: counted as it streams in."""
    app, _client, _engine, _uid = env
    chunks = _multipart_chunks(_CAP * 100)

    status, consumed = _call(app, [_CT], chunks)

    assert status == 413
    assert consumed < len(chunks) // 10

"""One trip import at a time, and no upload received from a stranger (#469).

Both import routes, ``.traxj`` and ZIP, share one guard (Decision 12 of
docs/TRIP_ZIP_IMPORT_PLAN.md). It is taken in the capped upload route's
wrapper, before any of the body is received, so a second import is refused
with 503 at once instead of being spooled to disk first. It is released
however the import ends: success, any error, or a body cut off at the cap.

Before the size check and the guard, the wrapper authenticates the caller
through ``get_current_user`` itself (Decision 13): a request without a valid
token gets the same 401 as any other route, without its body being read and
without taking the guard.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import api.project_shared as project_shared_mod
import api.project_transfer as transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBProject
from models.user import UserInfo
from src.project.project_io import ProjectIO
from tests.test_import_zip import call_asgi

_ROUTES = ("/api/projects/import", "/api/projects/import-zip")
_BUSY = "Another trip import is in progress. Try again in a minute."


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app, one signed-in user, a file-backed DB (threads share it)."""
    import api.router as router

    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, transfer_mod, storage_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id

    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield router.app, TestClient(router.app, raise_server_exceptions=False), engine, uid
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _trip() -> bytes:
    return json.dumps({"version": 1, "name": "x", "items": [], "activities": []}).encode()


def _import(client, name: str, content: bytes | None = None):
    return client.post(
        "/api/projects/import",
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content or _trip(), "application/json")})


def _names(engine, uid) -> set[str]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBProject.name).where(DBProject.user_info_id == uid)))


def _guard_free() -> bool:
    if not transfer_mod._import_guard.acquire(blocking=False):
        return False
    transfer_mod._import_guard.release()
    return True


_BOUNDARY = b"guard-boundary"
_CT = (b"content-type", b"multipart/form-data; boundary=" + _BOUNDARY)


def _chunks(filename: bytes, payload: bytes) -> list[bytes]:
    body = (b"--" + _BOUNDARY + b"\r\nContent-Disposition: form-data; name=\"file\"; "
            b"filename=\"" + filename + b"\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            + payload + b"\r\n--" + _BOUNDARY + b"--\r\n")
    return [body[i:i + 1024] for i in range(0, len(body), 1024)]


def _upload_of(route: str) -> list[bytes]:
    name = b"Trip.zip" if route.endswith("-zip") else b"Trip.traxj"
    return _chunks(name, _trip())


# ── Busy ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route", _ROUTES)
def test_an_import_while_another_holds_the_guard_gets_503_unread(env, route):
    app, _client, engine, uid = env
    assert transfer_mod._import_guard.acquire(blocking=False)
    try:
        status, body, consumed, _ = call_asgi(app, route, [_CT], _upload_of(route))
    finally:
        transfer_mod._import_guard.release()

    assert status == 503
    assert json.loads(body)["detail"] == _BUSY
    assert consumed == 0  # the receive channel was never read
    assert _names(engine, uid) == set()


def test_overlapping_imports_one_succeeds_the_others_get_503(env, monkeypatch):
    """The HTTP side of test_import_name_conflict's concurrent copies: over
    HTTP, overlapping imports no longer race; all but one are refused."""
    _app, client, engine, uid = env
    inside, finish = threading.Event(), threading.Event()
    real = ProjectIO.from_bytes

    def _slow(raw):
        inside.set()
        assert finish.wait(10)
        return real(raw)

    monkeypatch.setattr(transfer_mod.ProjectIO, "from_bytes", staticmethod(_slow))
    first: list = []
    worker = threading.Thread(target=lambda: first.append(_import(client, "First")))
    worker.start()
    try:
        assert inside.wait(10)
        refused = [_import(client, f"Second {n}") for n in range(3)]
    finally:
        finish.set()
        worker.join(10)

    assert first[0].status_code == 201, first[0].text
    assert [r.status_code for r in refused] == [503] * 3
    assert all(r.json()["detail"] == _BUSY for r in refused)
    assert _names(engine, uid) == {"First"}
    assert _guard_free()


# ── Released however the import ends ────────────────────────────────────────

def test_the_guard_is_released_after_a_success(env):
    _app, client, _engine, _uid = env
    assert _import(client, "Trip").status_code == 201
    assert _guard_free()


@pytest.mark.parametrize("make_request,expected", [
    pytest.param(lambda c: _import(c, "Trip", b"{not json"), 400, id="traxj-400"),
    pytest.param(lambda c: c.post("/api/projects/import-zip",
                                  files={"file": ("Trip.zip", b"not a zip", "application/zip")}),
                 400, id="zip-400"),
    pytest.param(lambda c: c.post("/api/projects/import-zip",
                                  files={"file": ("Trip.txt", b"x", "text/plain")}),
                 400, id="zip-wrong-type"),
])
def test_the_guard_is_released_after_a_refusal(env, make_request, expected):
    _app, client, _engine, _uid = env
    assert make_request(client).status_code == expected
    assert _guard_free()


def test_the_guard_is_released_after_a_409(env):
    _app, client, _engine, _uid = env
    assert _import(client, "Trip").status_code == 201
    assert _import(client, "Trip").status_code == 409
    assert _guard_free()


def test_the_guard_is_released_after_a_server_error(env, monkeypatch):
    _app, client, _engine, _uid = env

    def _boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(project_shared_mod._repo, "import_project", _boom)
    assert _import(client, "Trip").status_code == 500
    assert _guard_free()


@pytest.mark.parametrize("route", _ROUTES)
def test_the_guard_is_released_after_a_body_cut_off_mid_stream(env, monkeypatch, route):
    app, _client, _engine, _uid = env
    monkeypatch.setattr(transfer_mod, "MAX_IMPORT_BYTES", 4096)
    monkeypatch.setattr(transfer_mod, "MAX_ZIP_IMPORT_BYTES", 4096)
    name = b"Trip.zip" if route.endswith("-zip") else b"Trip.traxj"
    chunks = _chunks(name, b"x" * 1024 * 1024)

    status, _body, consumed, _ = call_asgi(app, route, [_CT], chunks)

    assert status == 413
    assert 0 < consumed < len(chunks)
    assert _guard_free()


# ── Authentication before any of the body ────────────────────────────────────

class _SpyGuard:
    def __init__(self):
        self.taken = 0

    def acquire(self, blocking=True):
        self.taken += 1
        return True

    def release(self):
        pass


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize("authorization", [None, b"Bearer not-a-valid-token"],
                         ids=["no-token", "bad-token"])
def test_a_stranger_gets_get_current_user_s_401_unread(env, monkeypatch, route, authorization):
    app, client, _engine, _uid = env
    monkeypatch.setenv("JWT_SECRET", "k" * 64)
    app.dependency_overrides.pop(get_current_user, None)
    spy = _SpyGuard()
    monkeypatch.setattr(transfer_mod, "_import_guard", spy)
    headers = [_CT] + ([(b"authorization", authorization)] if authorization else [])

    status, body, consumed, raw_headers = call_asgi(app, route, headers, _upload_of(route))

    # What get_current_user answers on any other route, today.
    expected = client.get(
        "/api/projects/",
        headers={"authorization": authorization.decode()} if authorization else {})
    assert expected.status_code == 401
    assert status == 401
    got = json.loads(body)
    want = expected.json()
    got.pop("request_id", None)
    want.pop("request_id", None)
    assert got == want
    got_headers = {k.decode().lower(): v.decode() for k, v in raw_headers}
    if authorization is None:  # HTTPBearer's own refusal names the scheme
        assert expected.headers.get("www-authenticate")
    assert got_headers.get("www-authenticate") == expected.headers.get("www-authenticate")
    assert consumed == 0
    assert spy.taken == 0

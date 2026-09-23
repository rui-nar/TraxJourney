"""Request correlation + access logging (Unit 0.2, issue #205).

Covers api.middleware.access_log_middleware directly (contextvar isolation
under concurrency, DEBUG-vs-INFO routing) and the full app via TestClient
(X-Request-Id header, request_id in 4xx/5xx bodies, HTTPException and
uncaught-exception paths both producing a correlated log line).
"""
from __future__ import annotations

import asyncio
import logging

from typing import Annotated

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from api.deps import get_current_user, jwt_secret
from api.middleware import DEBUG_ROUTE_TEMPLATES, access_log_middleware, install_middleware
from src.utils.logging import request_id_var, user_id_var


def _make_request(path: str = "/x", headers: dict | None = None) -> Request:
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }
    return Request(scope)


async def _echo_request_id(request: Request) -> Response:
    """A slow-enough call_next that forces the two gathered requests below to
    interleave, so isolation only holds if the ContextVar is actually
    per-task rather than shared mutable state."""
    await asyncio.sleep(0.01)
    seen = request_id_var.get()
    await asyncio.sleep(0.01)
    return Response(seen, status_code=200)


class TestContextVarIsolation:
    def test_concurrent_requests_never_see_each_others_request_id(self):
        async def _run():
            req_a = _make_request()
            req_b = _make_request()
            return await asyncio.gather(
                access_log_middleware(req_a, _echo_request_id),
                access_log_middleware(req_b, _echo_request_id),
            )

        resp_a, resp_b = asyncio.run(_run())

        id_a = resp_a.headers["X-Request-Id"]
        id_b = resp_b.headers["X-Request-Id"]
        assert id_a != id_b
        # The value call_next observed mid-flight (after the interleaving
        # sleep) must match what this same request minted, not the other one.
        assert resp_a.body.decode() == id_a
        assert resp_b.body.decode() == id_b

    def test_request_id_var_does_not_leak_out_of_its_task(self):
        """access_log_middleware deliberately never calls
        request_id_var.reset() (see its docstring — a bare-Exception handler
        needs the value to survive past this middleware). Correctness instead
        relies on each request running in its own asyncio Task, which
        ``asyncio.run`` demonstrates here: the Task started for ``_run()``
        gets its own copy of the context, so the ``.set()`` inside it is
        invisible once back in the calling (real) context."""
        async def _run():
            await access_log_middleware(_make_request(), _echo_request_id)

        assert request_id_var.get() == "-"
        asyncio.run(_run())
        assert request_id_var.get() == "-"

    def test_user_id_resolved_from_bearer_token(self):
        token = pyjwt.encode({"sub": "77"}, jwt_secret(), algorithm="HS256")

        async def _capture(request: Request) -> Response:
            return Response(user_id_var.get(), status_code=200)

        async def _run():
            req = _make_request(headers={"Authorization": f"Bearer {token}"})
            return await access_log_middleware(req, _capture)

        resp = asyncio.run(_run())
        assert resp.body.decode() == "77"

    def test_user_id_defaults_to_dash_without_a_token(self):
        async def _capture(request: Request) -> Response:
            return Response(user_id_var.get(), status_code=200)

        async def _run():
            return await access_log_middleware(_make_request(), _capture)

        resp = asyncio.run(_run())
        assert resp.body.decode() == "-"

    def test_non_jwt_bearer_token_binds_dash_and_logs_nothing(self, caplog):
        """The middleware's decode is best-effort and must stay silent (issue
        #446): a Bearer header is not necessarily a JWT — /metrics is
        authenticated with the opaque METRICS_TOKEN — and the route that
        actually rejects a bad token is the one that warns about it."""
        async def _capture(request: Request) -> Response:
            return Response(user_id_var.get(), status_code=200)

        async def _run():
            req = _make_request(headers={"Authorization": "Bearer opaque-metrics-token"})
            with caplog.at_level(logging.DEBUG, logger="api.deps"):
                return await access_log_middleware(req, _capture)

        resp = asyncio.run(_run())
        assert resp.body.decode() == "-"
        assert [r for r in caplog.records if r.name == "api.deps"] == []


class TestSharedJwtDecode:
    """get_current_user reuses the middleware's decode instead of repeating it
    — a request's JWT is decoded once, not twice."""

    def test_get_current_user_reuses_the_middlewares_decode(self, monkeypatch):
        calls = {"n": 0}
        orig_decode = pyjwt.decode

        def _spy(*args, **kwargs):
            calls["n"] += 1
            return orig_decode(*args, **kwargs)

        monkeypatch.setattr(pyjwt, "decode", _spy)

        app = FastAPI()
        install_middleware(app)

        @app.get("/protected")
        def protected(current_user: Annotated[dict, Depends(get_current_user)]):
            return {"sub": current_user["sub"]}

        token = pyjwt.encode({"sub": "42"}, jwt_secret(), algorithm="HS256")
        client = TestClient(app)
        resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.json() == {"sub": "42"}
        assert calls["n"] == 1, f"expected exactly one JWT decode, got {calls['n']}"

    def test_get_current_user_still_works_without_the_middleware(self):
        """Most unit tests mount routers on a bare FastAPI() with no middleware —
        get_current_user must still decode for itself when there is nothing to
        reuse from request.state."""
        app = FastAPI()

        @app.get("/protected")
        def protected(current_user: Annotated[dict, Depends(get_current_user)]):
            return {"sub": current_user["sub"]}

        token = pyjwt.encode({"sub": "42"}, jwt_secret(), algorithm="HS256")
        client = TestClient(app)
        resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.json() == {"sub": "42"}

    def test_an_invalid_token_still_401s_through_get_current_user(self):
        """Middleware swallows the decode failure for logging purposes; the
        route dependency must still raise its own 401 for a bad token."""
        app = FastAPI()
        install_middleware(app)

        @app.get("/protected")
        def protected(current_user: Annotated[dict, Depends(get_current_user)]):
            return {"sub": current_user["sub"]}

        client = TestClient(app)
        resp = client.get("/protected", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401

    @pytest.mark.parametrize("bad_token", [
        "garbage",  # not even parseable as a JWT
        pyjwt.encode({"sub": "42"}, "a-different-key" * 4, algorithm="HS256"),
    ], ids=["malformed", "forged"])
    def test_a_bad_token_is_warned_about_exactly_once(self, caplog, bad_token):
        """Only a *successful* decode is shared via request.state, so a bad
        token is decoded twice (middleware, then get_current_user). It must
        still be warned about once, by the decode that rejects it (issue
        #446)."""
        app = FastAPI()
        install_middleware(app)

        @app.get("/protected")
        def protected(current_user: Annotated[dict, Depends(get_current_user)]):
            return {"sub": current_user["sub"]}

        client = TestClient(app)
        with caplog.at_level(logging.WARNING, logger="api.deps"):
            resp = client.get(
                "/protected", headers={"Authorization": f"Bearer {bad_token}"}
            )

        assert resp.status_code == 401
        warnings = [
            r for r in caplog.records
            if r.name == "api.deps" and "invalid JWT rejected" in r.message
        ]
        assert len(warnings) == 1, [r.message for r in warnings]
        assert bad_token not in caplog.text


class TestAccessLogLevel:
    def _log_records(self, caplog, template: str, status: int = 200):
        async def _call_next(request: Request) -> Response:
            request.scope["route"] = type("Route", (), {"path": template})()
            return Response("ok", status_code=status)

        async def _run():
            with caplog.at_level(logging.DEBUG, logger="api.middleware"):
                await access_log_middleware(_make_request(), _call_next)

        asyncio.run(_run())
        return [r for r in caplog.records if r.name == "api.middleware"]

    def test_debug_allowlisted_route_logs_at_debug_not_info(self, caplog):
        for template in DEBUG_ROUTE_TEMPLATES:
            caplog.clear()
            records = self._log_records(caplog, template)
            assert len(records) == 1
            assert records[0].levelname == "DEBUG"

    def test_other_route_logs_at_info(self, caplog):
        records = self._log_records(caplog, "/api/projects/{name}")
        assert len(records) == 1
        assert records[0].levelname == "INFO"


# ── Full-app behaviour: headers, response bodies, exception handling ──────────

@pytest.fixture
def client():
    import api.router as router
    return TestClient(router.app)


class TestResponseCorrelation:
    def test_x_request_id_header_on_success(self, client):
        resp = client.get("/api/version")
        assert "X-Request-Id" in resp.headers
        assert len(resp.headers["X-Request-Id"]) == 8

    def test_raised_http_exception_gets_request_id_in_body_and_a_log_line(
        self, client, caplog
    ):
        """An unauthenticated call to a protected route raises HTTPException
        (403, "Not authenticated") via FastAPI's own HTTPBearer dependency —
        exercises the new StarletteHTTPException handler, not a hand-rolled
        typed one."""
        with caplog.at_level(logging.WARNING, logger="api.router"):
            resp = client.get("/api/geo/places?q=paris")

        assert resp.status_code in (401, 403)
        body = resp.json()
        assert body["request_id"] == resp.headers["X-Request-Id"]
        assert any(
            r.name == "api.router" and r.levelname == "WARNING"
            for r in caplog.records
        )

    def test_uncaught_exception_gets_request_id_in_body_and_a_log_line(
        self, client, caplog
    ):
        """Starlette's ServerErrorMiddleware sends our handler's response and
        then always re-raises the original exception too (so a real server
        still logs/crashes-loudly on it) — raise_server_exceptions=False is
        what lets a test observe the response instead of that re-raise."""
        def _boom():
            raise RuntimeError("boom")

        client.app.dependency_overrides[get_current_user] = _boom
        no_raise_client = TestClient(client.app, raise_server_exceptions=False)
        try:
            with caplog.at_level(logging.ERROR, logger="api.router"):
                resp = no_raise_client.get(
                    "/api/geo/places?q=paris",
                    headers={"Authorization": "Bearer irrelevant"},
                )
        finally:
            client.app.dependency_overrides.clear()

        assert resp.status_code == 500
        body = resp.json()
        assert body["request_id"] == resp.headers["X-Request-Id"]
        assert any(
            r.name == "api.router" and r.levelname == "ERROR" and "Unhandled" in r.message
            for r in caplog.records
        )

    def test_http_exception_and_uncaught_exception_share_the_request_id_scheme(
        self, client
    ):
        """Both paths stamp request_id from the same source (request_id_var
        bound by the access-log middleware), so a report referencing either
        id is equally traceable in the access log."""
        resp = client.get("/api/geo/places?q=paris")
        assert resp.json()["request_id"] == resp.headers["X-Request-Id"]

"""What the server may keep about a request (issue #443).

The access log records method, route template, status, duration and a
*truncated* client address (IPv4 to its /24, IPv6 to its /48) — never the
query string (OAuth codes, share tokens and search terms travel there), never
a path parameter (share tokens travel there too), never a full IP. uvicorn's
own access logger printed all three, so entrypoint.sh switches it off and
lets api.middleware's line be the only one.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response
from uvicorn.main import main as uvicorn_cli
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api.deps import get_current_user
from api.middleware import access_log_middleware, client_ip_for_log, install_middleware

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "entrypoint.sh"
# Every documented way of starting the server by hand.
DEV_COMMAND_DOCS = (ROOT / "README.md", ROOT / "docs" / "ANDROID.md", ROOT / ".env.example")

# A value nothing else in a log line could contain by coincidence.
SECRET = "SECRETq7x9v2"


def _make_request(client: tuple[str, int] | None = ("203.0.113.77", 4321)) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/x",
        "raw_path": b"/x",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
    }
    if client is not None:
        scope["client"] = client
    return Request(scope)


async def _ok(request: Request) -> Response:
    return Response("ok", status_code=200)


def _middleware_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "api.middleware"]


class TestClientIpTruncation:
    def test_ipv4_keeps_only_the_first_three_octets(self):
        assert client_ip_for_log("203.0.113.77") == "203.0.113.0/24"

    def test_ipv6_keeps_only_the_48_bit_prefix(self):
        assert client_ip_for_log("2001:db8:1234:5678:9abc:def0:1:2") == "2001:db8:1234::/48"

    def test_ipv4_mapped_ipv6_is_truncated_as_ipv4(self):
        """A dual-stack socket presents an IPv4 client as ::ffff:a.b.c.d; its
        /48 would be ::/48 — everything gone, nothing to investigate with."""
        assert client_ip_for_log("::ffff:203.0.113.77") == "203.0.113.0/24"

    def test_missing_or_unparseable_address_is_a_dash(self):
        assert client_ip_for_log(None) == "-"
        assert client_ip_for_log("") == "-"
        # Starlette's TestClient fills scope["client"] with this literal.
        assert client_ip_for_log("testclient") == "-"


class TestAccessLogLine:
    def _line(self, caplog, client) -> str:
        async def _run():
            with caplog.at_level(logging.INFO, logger="api.middleware"):
                await access_log_middleware(_make_request(client), _ok)

        asyncio.run(_run())
        lines = _middleware_lines(caplog)
        assert len(lines) == 1, lines
        return lines[0]

    def test_ipv4_client_is_logged_truncated(self, caplog):
        line = self._line(caplog, ("203.0.113.77", 4321))
        assert "ip=203.0.113.0/24" in line
        assert "203.0.113.77" not in line

    def test_ipv6_client_is_logged_truncated(self, caplog):
        line = self._line(caplog, ("2001:db8:1234:5678::9", 4321))
        assert "ip=2001:db8:1234::/48" in line
        assert "5678" not in line

    def test_no_client_address_logs_a_dash(self, caplog):
        assert "ip=-" in self._line(caplog, None)


class TestForwardedForTrust:
    """The same uvicorn ProxyHeadersMiddleware entrypoint.sh enables
    (``--proxy-headers --forwarded-allow-ips``) sits outside the app, so
    ``request.client`` is already the resolved client by the time the access
    line is written — but only when the peer is a listed proxy."""

    PROXY = "10.0.0.1"
    STRANGER = "198.51.100.9"
    REAL_CLIENT = "203.0.113.77"

    def _log_line_for(self, caplog, peer: str) -> str:
        app = FastAPI()
        install_middleware(app)

        @app.get("/x")
        def x():
            return {"ok": True}

        wrapped = ProxyHeadersMiddleware(app, trusted_hosts=self.PROXY)

        async def _run():
            transport = httpx.ASGITransport(app=wrapped, client=(peer, 4321))
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                with caplog.at_level(logging.INFO, logger="api.middleware"):
                    resp = await c.get("/x", headers={"X-Forwarded-For": self.REAL_CLIENT})
            assert resp.status_code == 200

        asyncio.run(_run())
        lines = _middleware_lines(caplog)
        assert len(lines) == 1, lines
        return lines[0]

    def test_forwarded_for_from_the_proxy_is_the_client(self, caplog):
        line = self._log_line_for(caplog, peer=self.PROXY)
        assert "ip=203.0.113.0/24" in line
        assert "10.0.0." not in line

    def test_forwarded_for_from_a_stranger_is_ignored(self, caplog):
        """A client talking to the port directly cannot choose what gets
        logged by sending its own X-Forwarded-For."""
        line = self._log_line_for(caplog, peer=self.STRANGER)
        assert "ip=198.51.100.0/24" in line
        assert "203.0.113" not in line


# ── Full app: nothing from the URL beyond the route template reaches a log ────

@pytest.fixture
def client():
    import api.router as router
    return TestClient(router.app, raise_server_exceptions=False)


class _ServerLog:
    """Everything the *server* logged during a request, at any level.

    The app namespaces are pinned at INFO by configure_logging(), so they are
    lifted to DEBUG too or a DEBUG-allowlisted route's access line would go
    unseen. The test client's own loggers (httpx/httpcore) print the URL they
    requested, secret and all — that is the client talking, not the server,
    so they are left out."""

    def __init__(self, caplog):
        self._caplog = caplog

    @property
    def text(self) -> str:
        return "\n".join(
            f"{r.levelname} {r.name} {r.getMessage()}" + (f"\n{r.exc_text}" if r.exc_text else "")
            for r in self._caplog.records
            if not r.name.startswith(("httpx", "httpcore"))
        )


@pytest.fixture
def server_log(caplog):
    with caplog.at_level(logging.DEBUG, logger="api"), caplog.at_level(logging.DEBUG, logger="src"), \
            caplog.at_level(logging.DEBUG):
        yield _ServerLog(caplog)


@pytest.fixture
def empty_db(monkeypatch):
    """A schema with no rows, so a share route reaches its own 404."""
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel, create_engine

    import models.db as db_module

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)


class TestNoSensitiveUrlPartIsLogged:
    def test_oauth_code_in_query_string(self, client, server_log):
        resp = client.get(
            f"/api/strava/callback?code={SECRET}&state={SECRET}", follow_redirects=False
        )
        assert resp.status_code in (302, 307)
        assert "/api/strava/callback" in server_log.text
        assert SECRET not in server_log.text

    def test_query_string_on_a_rejected_request(self, client, server_log):
        """The HTTPException handler's warning line: template, not URL."""
        resp = client.get(f"/api/geo/places?q={SECRET}")
        assert resp.status_code in (401, 403)
        assert re.search(r"WARNING.*/api/geo/places", server_log.text)
        assert SECRET not in server_log.text

    def test_query_string_on_an_unmatched_route(self, client, server_log):
        """No route matched, so there is no template: under /api the area
        segment is kept and everything after it is cut."""
        resp = client.get(f"/api/no-such-route/{SECRET}?token={SECRET}")
        assert resp.status_code == 404
        assert "GET /api/no-such-route/... -> 404" in server_log.text
        assert SECRET not in server_log.text

    def test_share_token_on_a_trailing_slash_redirect(self, client, server_log):
        """Starlette answers ``/meta/`` with a 307 to ``/meta`` before any
        route matches — the concrete path is all the log could see."""
        resp = client.get(f"/api/share/{SECRET}/meta/", follow_redirects=False)
        assert resp.status_code == 307
        assert "GET /api/share/... -> 307" in server_log.text
        assert SECRET not in server_log.text

    def test_share_token_on_a_cors_preflight(self, client, server_log):
        """CORSMiddleware answers the OPTIONS itself, inside the access-log
        middleware but before the router — again no route in the scope."""
        resp = client.options(
            f"/api/share/{SECRET}/meta",
            headers={"Origin": "http://localhost:5500", "Access-Control-Request-Method": "GET"},
        )
        assert resp.status_code == 200
        assert "OPTIONS /api/share/... -> 200" in server_log.text
        assert SECRET not in server_log.text

    def test_share_token_on_a_web_link_with_no_web_build(self, client, server_log):
        """``/share/<token>`` is the web client's route. With no web build
        present nothing claims it and it 404s with the concrete path."""
        if (ROOT / "web_client").is_dir():
            pytest.skip("a web build is present, so the SPA route claims /share/...")
        resp = client.get(f"/share/{SECRET}")
        assert resp.status_code == 404
        assert "GET /share/... -> 404" in server_log.text
        assert SECRET not in server_log.text

    def test_share_token_in_a_failed_statements_parameters(self, client, server_log, monkeypatch):
        """SQLAlchemy's exception text carries the bound parameters unless the
        engine hides them — and the catch-all handler logs that text with
        .exception(). Same engine builder the app uses, over an empty
        database, so the lookup fails on the very statement holding the token."""
        import models.db as db_module

        monkeypatch.setattr(db_module, "engine", db_module._make_engine("sqlite://"))
        resp = client.get(f"/api/share/{SECRET}/meta")
        assert resp.status_code == 500
        assert "no such table" in server_log.text
        assert SECRET not in server_log.text

    def test_query_string_on_an_unhandled_exception(self, client, server_log):
        def _boom():
            raise RuntimeError("boom")

        client.app.dependency_overrides[get_current_user] = _boom
        try:
            resp = client.get(
                f"/api/geo/places?q={SECRET}", headers={"Authorization": "Bearer irrelevant"}
            )
        finally:
            client.app.dependency_overrides.clear()
        assert resp.status_code == 500
        assert re.search(r"ERROR.*Unhandled exception on GET /api/geo/places", server_log.text)
        assert SECRET not in server_log.text

    def test_share_token_in_the_path(self, client, server_log, empty_db):
        """Share tokens are path parameters; every line logs the template
        ``/api/share/{token}/meta``, so the token never appears — on the
        access line or the 404's warning line."""
        resp = client.get(f"/api/share/{SECRET}/meta")
        assert resp.status_code == 404
        assert "/api/share/{token}/meta" in server_log.text
        assert SECRET not in server_log.text


# ── entrypoint.sh: uvicorn's own access logger stays off ──────────────────────

def _uvicorn_line() -> str:
    lines = [l for l in ENTRYPOINT.read_text().splitlines() if l.lstrip().startswith("exec uvicorn")]
    assert len(lines) == 1, lines
    return lines[0]


_SHELL_DEFAULT_RE = re.compile(r"\$\{FORWARDED_ALLOW_IPS:-([^}]+)\}")


def _config_for(command: str) -> uvicorn.Config:
    """Feed a documented `uvicorn ...` command line to uvicorn's own CLI parser
    and build the Config it would run with — so the assertion is on what
    uvicorn understands, not on the presence of a substring. The shell
    `${VAR:-default}` is resolved the way an unset variable would resolve it."""
    argv = shlex.split(_SHELL_DEFAULT_RE.sub(r"\1", command))
    assert argv[0] in ("exec", "uvicorn")
    if argv[0] == "exec":
        argv = argv[1:]
    params = uvicorn_cli.make_context("uvicorn", argv[1:]).params
    return uvicorn.Config(
        params.pop("app"),
        **{k: v for k, v in params.items() if k in ("access_log", "proxy_headers", "forwarded_allow_ips")},
    )


def _dev_commands():
    found = []
    for doc in DEV_COMMAND_DOCS:
        for m in re.finditer(r"uvicorn api\.router:app[^`\n]*", doc.read_text(encoding="utf-8")):
            found.append((doc.name, m.group(0)))
    return found


class TestEntrypoint:
    def test_uvicorn_access_log_is_off(self):
        assert _config_for(_uvicorn_line()).access_log is False, (
            "uvicorn's access log prints the full client IP and the raw request "
            "line, query string included (#443)"
        )

    def test_forwarded_for_is_trusted_from_configured_proxies_only(self):
        config = _config_for(_uvicorn_line())
        assert config.proxy_headers is True
        # Empty FORWARDED_ALLOW_IPS (a blank .env line) must mean uvicorn's own
        # default, not "trust nobody" — hence the shell fallback, not a bare
        # env passthrough.
        assert _SHELL_DEFAULT_RE.search(_uvicorn_line()).group(1) == "127.0.0.1"
        assert config.forwarded_allow_ips == "127.0.0.1"

    def test_documented_dev_commands_keep_the_access_log_off(self):
        commands = _dev_commands()
        assert {name for name, _ in commands} == {d.name for d in DEV_COMMAND_DOCS}, commands
        for name, command in commands:
            assert _config_for(command).access_log is False, (name, command)

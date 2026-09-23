"""Request correlation + access logging (Unit 0.2, issue #205).

Mints a short ``request_id`` for every request, resolves ``user_id`` from the
Authorization header ahead of route dependencies, and logs one access-log
line per request — DEBUG for the chatty/polled routes listed in
``DEBUG_ROUTE_TEMPLATES`` (docs/LOGGING_OBSERVABILITY_PLAN.md Section 3),
INFO for everything else.

That line is the only access log the server keeps (uvicorn's own is off, see
entrypoint.sh), and it deliberately records the route template rather than
the URL — query strings carry OAuth codes, share tokens and search terms —
and the client address truncated to its /24 (IPv6: /48), never in full
(issue #443, docs/LOGGING.md).
"""
from __future__ import annotations

import ipaddress
import time
import uuid
from typing import Awaitable, Callable

from fastapi import Request, Response

from api.deps import decode_token_quietly
from src.utils.logging import get_logger, request_id_var, user_id_var

_log = get_logger(__name__)

# Chatty, read-only-or-polled routes logged at DEBUG instead of INFO. Matched
# against the *resolved route template*, not the concrete request path, so
# path parameters (job ids, tile coordinates, ...) don't matter.
DEBUG_ROUTE_TEMPLATES = frozenset(
    {
        "/api/projects/{name}/poster/{job_id}",  # poster job-status polling
        "/api/share/{token}/tiles/{z}/{x}/{y}.png",  # raster tile serving
        "/api/geo/project",  # full-res project GeoJSON
        "/api/geo/project/low-res",  # low-res project GeoJSON
        "/api/version",  # client staleness probe
        "/metrics",  # Prometheus scrape
    }
)


def route_template(request: Request) -> str:
    """The path template the request matched, e.g. "/api/projects/{name}".

    What every log line names a request by — api.router's exception handlers
    included — so a share token or other path parameter never lands in a
    log. Falls back to the raw path (never the query string) for anything
    that never matched a route (a 404) — those aren't in the allowlist either
    way, so they still log at INFO.
    """
    route = request.scope.get("route")
    return route.path if route is not None else request.url.path


def client_ip_for_log(host: str | None) -> str:
    """*host* reduced to what the access log may keep (issue #443): an IPv4
    address to its /24, an IPv6 address to its /48 — enough to see which
    network a burst of abuse came from, not enough to single out a client.
    "-" when there is no usable address (no peer, or a non-IP literal such
    as the TestClient's "testclient")."""
    if not host:
        return "-"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "-"
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def _resolve_user_id(request: Request) -> str:
    """Best-effort JWT decode straight from the Authorization header.

    Runs ahead of FastAPI's ``get_current_user`` dependency (this is
    middleware, not a route dependency), so it shares api.deps' JWT
    verification rather than duplicating it — through its quiet variant,
    because a missing, malformed or expired token just means the request logs
    with user_id="-" and nothing is said about it here: it's an
    unauthenticated route, a request that will itself 401 (and be warned
    about, once) further down the stack, or a Bearer token that was never a
    JWT, like the opaque one ``/metrics`` takes (issue #446).

    Stashes the decoded payload on ``request.state`` so ``get_current_user``/
    ``get_optional_current_user`` (api.deps) can reuse it instead of decoding
    the same token a second time moments later. Only a
    *successful* decode is stashed — a failure here means the token is bad,
    and the route dependency below still needs to run its own decode to raise
    the right 401.
    """
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return "-"
    payload = decode_token_quietly(token.strip())
    if payload is None:
        return "-"
    request.state.jwt_payload = payload
    sub = payload.get("sub")
    return str(sub) if sub is not None else "-"


async def access_log_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind request_id/user_id for the duration of one request and log it.

    Registered via ``app.middleware("http")``, so it wraps every route
    including ones that raise — FastAPI's exception handling for *typed*
    exceptions (StaleWriteError, HTTPException, ...) runs inside
    ``call_next``, so by the time it returns here there is already a
    Response (including error responses) to attach ``X-Request-Id`` to and
    log the final status of.

    Deliberately does **not** reset the contextvars afterwards (no
    ``.set()``/token/``reset()`` dance): a handler registered for the bare
    ``Exception`` type (api.router's catch-all) is special-cased by Starlette
    onto ``ServerErrorMiddleware``, which sits *outside* this middleware —
    resetting in a ``finally`` here would clear request_id before that
    handler ever runs, breaking correlation for exactly the case it matters
    most for. Each request already gets its own asyncio Task upstream (one
    per ASGI call), so the bound values simply go out of scope with it —
    nothing to leak into the next request.
    """
    req_id = uuid.uuid4().hex[:8]
    request_id_var.set(req_id)
    user_id_var.set(_resolve_user_id(request))
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    template = route_template(request)
    log = _log.debug if template in DEBUG_ROUTE_TEMPLATES else _log.info
    # request.client is already the real client behind the reverse proxy:
    # uvicorn's ProxyHeadersMiddleware (--proxy-headers, entrypoint.sh) wraps
    # the whole app and rewrites scope["client"] from X-Forwarded-For — but
    # only when the peer is one --forwarded-allow-ips lists, so a stranger
    # sending that header is logged by its own address.
    log(
        "%s %s -> %d (%.1fms) ip=%s",
        request.method, template, response.status_code, duration_ms,
        client_ip_for_log(request.client.host if request.client else None),
    )
    response.headers["X-Request-Id"] = req_id
    return response


def install_middleware(app) -> None:
    """Attach the access-log/correlation middleware to *app*."""
    app.middleware("http")(access_log_middleware)

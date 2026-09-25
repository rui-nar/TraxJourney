"""Prometheus scrape endpoint (issue #125).

Not under ``/api`` — ``/metrics`` is the conventional path scrapers default to.

Caddy reverse-proxies *every* path of the public domain to this app, so an
unauthenticated ``/metrics`` would publish the app's internals (user counts,
route names, error rates, DB file size) to anyone who asks. It therefore
requires a bearer token, and is switched off entirely until one is configured —
no token, no endpoint.
"""
import hmac
import os

from fastapi import APIRouter, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

router = APIRouter(tags=["metrics"])


def _configured_token() -> str:
    """Read METRICS_TOKEN at request time, not import time, so a deployment can
    be flipped on without a rebuild (and so tests can monkeypatch it)."""
    return os.environ.get("METRICS_TOKEN", "").strip()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Expose the process' metrics in Prometheus text format.

    Returns 404 when ``METRICS_TOKEN`` is unset: the endpoint is disabled, and
    a probe can't distinguish it from a route that was never deployed.
    """
    expected = _configured_token()
    if not expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    # compare_digest over a constant-time comparison of the token itself; the
    # scheme check is not secret. Compared as bytes: compare_digest raises on
    # non-ASCII str, and Starlette hands headers over decoded as latin-1, so
    # re-encoding latin-1 recovers the bytes the client sent (issue #450).
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        presented.strip().encode("latin-1"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid metrics token"
        )

    return Response(content=generate_latest(_registry()), media_type=CONTENT_TYPE_LATEST)


def _registry():
    """The registry to scrape: multiprocess when configured, else the default.

    With a worker container (issue #173) the API process' default registry no
    longer sees everything — DB-session timings and stale-write counters from
    queued jobs are recorded in the worker and would simply never be scraped.
    Setting ``PROMETHEUS_MULTIPROC_DIR`` to a directory both containers mount
    makes every process write its samples there and this endpoint aggregate
    them.

    Unset (the default, and correct for a single-container deployment) this is
    the plain default registry and nothing changes.
    """
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return REGISTRY

    from prometheus_client import CollectorRegistry, multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry

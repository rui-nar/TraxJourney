"""Combines all REST API sub-routers into a single FastAPI app."""
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from scalar_fastapi import get_scalar_api_reference
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.brand import APP_NAME
from src.exceptions.errors import APIError, AuthenticationError, QuotaExceeded
from src.jobs.prepared_geo_jobs import sweep_unprepared_geometry
from src.jobs.route_jobs import (
    sweep_degraded_segments,
    sweep_orphaned_jobs,
    sweep_stale_resolver_segments,
)
from src.poster.poster_job_runner import sweep_orphaned_poster_jobs
from src.project.project_repo import StaleWriteError

from api.activities import router as activities_router, activity_fields_router
from api.admin import router as admin_router
from api.auth import router as auth_router
from api.backup import router as backup_router
from api.billing import router as billing_router
from api.deps import jwt_secret
from api.encounters import router as encounters_router
from api.encryption import router as encryption_router
from api.geo import router as geo_router
from api.groups import router as groups_router
from api.immich import router as immich_router
from api.journal import router as journal_router
from api.members import router as members_router, invites_router
from api.memories import router as memories_router
from api.metrics import router as metrics_router
from api.middleware import install_middleware
from api.people import router as people_router
from api.polarsteps import router as polarsteps_router
from api.poster import poster_public_router, router as poster_router
from api.project_items import router as project_items_router
from api.project_shares import router as project_shares_router
from api.project_transfer import router as project_transfer_router
from api.projects import router as projects_router
from api.segments import router as segments_router
from api.share import router as share_router
from api.strava import router as strava_router
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from models.db import checkpoint_wal, engine
from models.project_db import _check_schema_contract
from src.admin.bootstrap import seed_admin
from src.backup.backup_service import backup_db
from src.billing.usage import reconcile_all_usage
from src.utils.logging import configure_logging, env_level, get_logger, request_id_var
from src.utils.metrics import (
    JOB_EVENT_MASK,
    STALE_WRITES,
    install_db_metrics,
    instrument_http,
    record_job_event,
)

# Wire app loggers (api.*, src.*) to a console handler as early as possible —
# this is the process entry point (uvicorn api.router:app), so configuring here
# means INFO+ logs surface from import time onward, not just inside the lifespan.
# LOG_LEVEL (issue #208) is the restart-persistent baseline; the admin dashboard
# can additionally apply a live, process-memory-only override on top of it.
configure_logging(level=env_level())

_log = get_logger(__name__)
_scheduler = AsyncIOScheduler()

# Which role this process plays (issue #173). Only the API process may run
# migrations and the scheduler: a worker container that also ran them would
# checkpoint the WAL twice a minute, take two daily backups, and race
# `alembic upgrade head` against the API container at boot.
#
# Today a worker never imports this module, so the guard is belt-and-braces —
# but the failure it prevents is silent and periodic, which is exactly the kind
# that survives a code review. Set TRAXJOURNEY_ROLE=worker in the worker image.
_ROLE = os.environ.get("TRAXJOURNEY_ROLE", "api").strip().lower()
_IS_API_PROCESS = _ROLE != "worker"

# Single source of truth for the running version: the git tag baked in at build
# time (Dockerfile ARG/ENV APP_VERSION, set from the tag by CI). Defaults to
# "dev" locally. Used for both the OpenAPI `version` and the /api/version probe.
_APP_VERSION = os.environ.get("APP_VERSION", "dev")

# Logged at import — this module IS the process entry point, so the line lands at
# the top of every log, before migrations or any request. Without it there is no
# way to tell from a log file which build produced it (issue #179).
_log.info("%s API starting — version %s", APP_NAME, _APP_VERSION)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Before anything else, and before the database is touched: an instance with
    # no JWT_SECRET signs sessions with a key anyone can read, and the old
    # failure mode was silence (issue #156). Fail at boot rather than on the
    # first login, so a bad deploy is obvious immediately.
    jwt_secret()
    if not _IS_API_PROCESS:
        # A worker shares this codebase but must not own schema or schedules.
        _log.info("TRAXJOURNEY_ROLE=%s — skipping migrations, admin seed and scheduler", _ROLE)
        yield
        return
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_command.upgrade(cfg, "head")
    _check_schema_contract()
    seed_admin()
    # Anything left non-terminal is orphaned by definition: nothing was
    # executing it a moment ago, because nothing was running at all (issue
    # #173). This is what replaces the Flutter client's stale-pending recovery —
    # a server-side crash is now recovered by the server, whether or not anyone
    # reopens the project.
    sweep_orphaned_jobs()
    # A poster work-horse OOM-kill is normally caught the instant it happens
    # (src.jobs.worker's work_horse_killed_handler) — this is the backstop for
    # the rarer case where the whole worker container went down with it, so
    # nothing was left alive to run that handler (issue #14 follow-up).
    sweep_orphaned_poster_jobs()
    _scheduler.add_job(backup_db, "cron", hour=2, minute=0, id="daily_backup", replace_existing=True)
    _scheduler.add_job(checkpoint_wal, "interval", seconds=60, id="wal_checkpoint", replace_existing=True)
    # Correct any drift between the per-user storage counters used for quota
    # checks and what is actually on disk (issue #121). No-op unless billing is
    # enabled, so a self-hosted instance never pays for the walk.
    _scheduler.add_job(reconcile_all_usage, "cron", hour=3, minute=30,
                       id="usage_reconcile", replace_existing=True)
    # A degraded (straight-line) resolve is often transient Overpass mirror
    # flakiness, not permanent — retry it on a schedule instead of leaving it
    # stuck until the user notices and taps to retry manually (issue #207).
    # Hourly, not more often: resolve is concurrency-bounded to be polite to a
    # free public API, and this competes with real user-triggered resolves for
    # that same budget.
    _scheduler.add_job(sweep_degraded_segments, "interval", hours=1,
                       id="degraded_segment_retry", replace_existing=True)
    # The other half of the same problem: a resolve that *succeeded* and was
    # wrong is invisible to the sweep above, so a resolver fix never reaches the
    # geometry it already drew (issue #364). Segments stamped by an older
    # resolver generation are re-resolved here, hard-capped per run because a
    # RESOLVER_VERSION bump makes every rail segment a candidate at once.
    #
    # Cron on a fixed minute rather than "interval", so it does not sit
    # permanently in step with the degraded sweep: an interval trigger counts
    # from when it was added, and these two are added microseconds apart, so
    # both would fire together every hour and compete for the single Overpass
    # slot (src.jobs.upstream_slots) on every single run rather than by chance.
    _scheduler.add_job(sweep_stale_resolver_segments, "cron", minute=20,
                       id="stale_resolver_retry", replace_existing=True)
    # Prepared geometry for activities that predate issue #369: everything
    # written since derives its blob beside the polyline, so this only ever
    # drains a backlog and then no-ops. Five minutes rather than hourly because
    # it is purely local CPU in bounded slices with nothing upstream to
    # compete for, and the sooner it drains the sooner legacy trips stop paying
    # the cost on their first open.
    _scheduler.add_job(sweep_unprepared_geometry, "interval", minutes=5,
                       id="prepared_geometry_backfill", replace_existing=True)
    # One listener covers every job — current and future — with run counts,
    # duration and a last-success timestamp (issue #125).
    _scheduler.add_listener(record_job_event, JOB_EVENT_MASK)
    _scheduler.start()
    _log.info("Backup scheduler started — daily at 02:00 UTC")
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(
    title=f"{APP_NAME} API",
    description=(
        f"REST API consumed by the {APP_NAME} Flutter client (web, Android, iOS).\n\n"
        "Authentication uses JWT bearer tokens obtained via `/api/auth/token` "
        "(email + password), `/api/auth/register`, or `/api/auth/google`.\n\n"
        "Interactive docs: [`/docs`](/docs) (Swagger) · [`/scalar`](/scalar) (Scalar)"
    ),
    version=_APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# Request rate / latency / error rate (issue #125). The scrape endpoint itself
# is ours (api/metrics.py) so it can require a token.
instrument_http(app)

# Query volume/latency, pool saturation (issue #35's early warning) and SQLite
# file + WAL growth (silent failure mode of wal_autocheckpoint=0).
install_db_metrics(engine)

# Allow Flutter dev clients (and web) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# request_id/user_id correlation + one access-log line per request (issue #205).
install_middleware(app)

app.include_router(activities_router)
app.include_router(activity_fields_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(backup_router)
app.include_router(billing_router)
app.include_router(encounters_router)
app.include_router(encryption_router)
app.include_router(geo_router)
app.include_router(groups_router)
app.include_router(immich_router)
app.include_router(journal_router)
app.include_router(members_router)
app.include_router(invites_router)
app.include_router(memories_router)
app.include_router(metrics_router)
app.include_router(people_router)
app.include_router(polarsteps_router)
app.include_router(poster_router)
app.include_router(poster_public_router)
app.include_router(project_items_router)
app.include_router(project_shares_router)
app.include_router(project_transfer_router)
app.include_router(projects_router)
app.include_router(segments_router)
app.include_router(share_router)
app.include_router(strava_router)


@app.exception_handler(StaleWriteError)
async def _stale_write_handler(_request, exc: StaleWriteError):
    """Map an optimistic-lock conflict to 409 so clients can refetch and retry."""
    STALE_WRITES.inc()  # write contention signal — spikes when writers collide
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "request_id": request_id_var.get()},
    )


@app.exception_handler(QuotaExceeded)
async def _quota_handler(_request, exc: QuotaExceeded):
    """Map a plan limit to 402 Payment Required, with the numbers to render it.

    402 rather than 403: the request is well-formed and the caller is allowed to
    do this in principle — they need a bigger plan, and the client turns this
    into an upgrade prompt rather than an error toast.
    """
    return JSONResponse(
        status_code=402,
        content={
            "detail": str(exc),
            "code": "quota_exceeded",
            "resource": exc.resource,
            "plan": exc.plan,
            "limit": exc.limit,
            "used": exc.used,
            "needed": exc.needed,
            "request_id": request_id_var.get(),
        },
    )


@app.exception_handler(AuthenticationError)
async def _auth_error_handler(_request, exc: AuthenticationError):
    """Map an upstream (Strava) auth failure to 401 so the client can re-authenticate.

    The messages carried by AuthenticationError are already user-facing and
    actionable (e.g. "re-authenticate via Add track → From Strava…").
    """
    return JSONResponse(
        status_code=401,
        content={"detail": str(exc), "request_id": request_id_var.get()},
    )


@app.exception_handler(APIError)
async def _upstream_api_error_handler(_request, exc: APIError):
    """Map an upstream third-party API failure (e.g. Strava) to 502, not a bare 500.

    The raw upstream response is logged for diagnostics but not leaked to the
    client; the client sees a stable, actionable message instead.
    """
    _log.warning("Upstream API error: %s", exc)
    return JSONResponse(
        status_code=502,
        content={
            "detail": "The Strava integration is currently unavailable. Please try again later.",
            "request_id": request_id_var.get(),
        },
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(_request, exc: StarletteHTTPException):
    """Replace FastAPI's default HTTPException handler with one that logs and
    stamps request_id — the ~200 call sites across the app that raise
    HTTPException directly (auth failures, 404s, validation-style 4xx, ...)
    get correlation and a log line for free instead of each needing its own.

    Logged at WARNING, not ``.exception()``: this is expected control flow (a
    route deliberately raising 401/403/404/...), not a bug — the catch-all
    below is for the unexpected kind.
    """
    _log.warning(
        "%s %s -> %d: %s", _request.method, _request.url.path, exc.status_code, exc.detail
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": request_id_var.get()},
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(_request, exc: Exception):
    """Last-resort handler for anything no typed handler above claims.

    Without this, an uncaught exception fell through to FastAPI's own bare
    500 with no server-side record beyond whatever uvicorn's crash log
    happened to catch (issue #205). Registering a handler for the bare
    ``Exception`` type is safe alongside the typed handlers above — FastAPI
    dispatches by the most specific exception class, so StaleWriteError,
    QuotaExceeded, AuthenticationError, APIError and HTTPException still hit
    their own handlers first.

    Starlette special-cases a bare-``Exception``/500 handler onto
    ``ServerErrorMiddleware``, which sits *outside* our own access-log
    middleware (api/middleware.py) — that middleware's own
    ``response.headers["X-Request-Id"] = ...`` line never runs for this path,
    so the header is set here directly instead.
    """
    _log.exception("Unhandled exception on %s %s", _request.method, _request.url.path)
    request_id = request_id_var.get()
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
        headers={"X-Request-Id": request_id},
    )


@app.get("/scalar", include_in_schema=False)
async def scalar_docs() -> HTMLResponse:
    """Scalar API reference UI."""
    return get_scalar_api_reference(
        openapi_url="/openapi.json",
        title=f"{APP_NAME} API",
    )


@app.get("/api/version", include_in_schema=False)
async def app_version():
    """Version this image was built from (the git tag baked in at build time).

    The web client compares its own baked APP_VERSION against this and prompts a
    reload when they differ, so a returning user never stays stuck on a stale
    cached bundle. Defaults to "dev" locally (matching the client default) so the
    check never fires outside a real deployment.
    """
    return {"version": _APP_VERSION}


# ── Android App Links ─────────────────────────────────────────────────────────
# Must be registered before the SPA fallback below, which would otherwise answer
# it with index.html and quietly fail link verification.
ANDROID_PACKAGE_NAME = "com.traxjourney.app"


def _android_cert_fingerprints() -> list[str]:
    """SHA-256 fingerprints of the release signing certificate, from the env.

    Comma-separated so a key rotation can list old and new at once. Set from the
    keystore that CI signs with — `scripts/new-android-keystore.ps1` prints it.
    """
    raw = os.getenv("ANDROID_CERT_FINGERPRINTS", "")
    return [fp.strip().upper() for fp in raw.split(",") if fp.strip()]


@app.get("/.well-known/assetlinks.json", include_in_schema=False)
async def android_assetlinks():
    """Digital Asset Links statement that lets the Android app claim our URLs.

    Android fetches this over https when the app is installed and, if it lists
    the app's package and signing fingerprint, stops offering a browser chooser
    for traxjourney.com/share|join|verify-email links — they open in the app.

    404s when no fingerprint is configured: publishing an empty statement is
    worse than publishing none, since Android caches the failed verification.
    """
    fingerprints = _android_cert_fingerprints()
    if not fingerprints:
        return JSONResponse(status_code=404, content={"detail": "Not configured"})
    return JSONResponse(
        content=[
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": ANDROID_PACKAGE_NAME,
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        ],
        media_type="application/json",
    )


# ── Flutter web SPA — must be registered last so /api/... routes take priority ─
_web_dir = os.path.join(os.path.dirname(__file__), "..", "web_client")

_NO_CACHE = "no-cache"
_LONG_CACHE = "public, max-age=86400"


def _cache_control_for(full_path: str) -> str:
    """Cache-Control policy for a Flutter-web asset path.

    Flutter does NOT content-hash its entry-point filenames (``main.dart.js``,
    ``flutter.js``, ``flutter_bootstrap.js``, deferred ``*.part.js``, the service
    worker, ``index.html``, ``manifest.json``, ``version.json``). If any of those
    is allowed to sit in the browser cache, a returning user keeps running the
    *old* build after a deploy — including a stale ``main.dart.js`` that may carry
    a wrong baked API URL — until the cache expires. So every entry point is
    served ``no-cache`` (the browser must revalidate each load; a 304 keeps it
    cheap), guaranteeing the latest build is picked up immediately.

    Only the large, rarely-changing static trees (``assets/`` bundle data,
    ``canvaskit/`` runtime, tied to the Flutter SDK version) get a day of caching.
    """
    if full_path.startswith(("assets/", "canvaskit/")):
        return _LONG_CACHE
    return _NO_CACHE


if os.path.isdir(_web_dir):
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """Serve the Flutter web build; fall back to index.html for SPA routing."""
        # An unknown /api/... path is a missing endpoint, never a client route.
        # Answering it with index.html and a 200 lets an out-of-date client
        # take the page for a real response — e.g. save it as a project backup
        # after an export route was renamed. Raise the same 404 an unmatched
        # route gets when no web build is present.
        if full_path == "api" or full_path.startswith("api/"):
            raise StarletteHTTPException(status_code=404)
        candidate = os.path.join(_web_dir, full_path)
        if full_path and os.path.isfile(candidate):
            resp = FileResponse(candidate)
            resp.headers["Cache-Control"] = _cache_control_for(full_path)
            return resp
        resp = FileResponse(os.path.join(_web_dir, "index.html"))
        resp.headers["Cache-Control"] = _NO_CACHE
        return resp

"""Prometheus metrics for TraxJourney — the single place metric objects are defined.

Business code imports the counters/histograms it needs (or one of the helpers
below); nothing outside this module touches ``prometheus_client`` directly.

Everything lives on ``prometheus_client``'s default registry, which is process
local. The API container runs a single uvicorn worker (``entrypoint.sh``), so
that registry sees every request it serves.

It does not, however, see another *process*. With a job worker running (issue
#173) the DB-session timings and stale-write counters recorded while executing a
queued job live in the worker's registry. Set ``PROMETHEUS_MULTIPROC_DIR`` to a
directory both containers mount and every process writes its samples there for
``/metrics`` to aggregate — the same switch multiple gunicorn workers would
need. See docs/METRICS.md.

Label values are always drawn from closed sets (a status class, a normalised
endpoint, a SQL keyword). A metric label whose values come from user data —
a path with an ID in it, a raw SQL statement — creates one time series per
distinct value and will eventually take Prometheus down with it, so paths go
through ``normalise_path`` and statements through ``_operation_of`` first.
"""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from typing import Iterator, Optional

import sqlalchemy.exc
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
)
from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import event

from src.utils.logging import get_logger

_log = get_logger(__name__)

# ── Authentication ────────────────────────────────────────────────────────────

LOGINS = Counter(
    "traxjourney_logins_total",
    "Login attempts by auth provider and outcome.",
    ["provider", "result"],
)

REGISTRATIONS = Counter(
    "traxjourney_registrations_total",
    "Accounts created, by auth provider.",
    ["provider"],
)

APP_OPENS = Counter(
    "traxjourney_app_opens_total",
    "App launches, by whether the cached session was still valid.",
    ["session_state"],
)

# ── Third-party APIs (Strava, Polarsteps, Google Translate, SMTP) ──────────────

EXTERNAL_REQUESTS = Counter(
    "traxjourney_external_requests_total",
    "Calls to third-party services by outcome.",
    ["service", "endpoint", "outcome"],
)

EXTERNAL_DURATION = Histogram(
    "traxjourney_external_request_duration_seconds",
    "Wall-clock duration of calls to third-party services.",
    ["service", "endpoint"],
)

# Strava's quotas belong to the application, and the limiters enforcing them are
# process-wide (issue #130), which is what makes these worth exporting.
STRAVA_RATE_LIMIT_USAGE = Gauge(
    "traxjourney_strava_rate_limit_usage",
    "Strava requests made in the current quota window.",
    ["window"],
)

STRAVA_RATE_LIMIT_CAPACITY = Gauge(
    "traxjourney_strava_rate_limit_capacity",
    "Strava requests allowed per quota window.",
    ["window"],
)

STRAVA_THROTTLED = Counter(
    "traxjourney_strava_throttled_total",
    "Calls refused by our own limiter before reaching Strava.",
    ["window"],
)

# ── Background jobs (APScheduler) ─────────────────────────────────────────────

JOB_RUNS = Counter(
    "traxjourney_job_runs_total",
    "Scheduled job executions by outcome.",
    ["job", "result"],
)

JOB_DURATION = Histogram(
    "traxjourney_job_duration_seconds",
    "Wall-clock duration of scheduled job executions.",
    ["job"],
)

JOB_LAST_SUCCESS = Gauge(
    "traxjourney_job_last_success_timestamp_seconds",
    "Unix timestamp of the last successful run of each scheduled job.",
    ["job"],
)

# The backfill sweep for prepared geometry (issue #369). A gauge rather than a
# log line, because the failure it exists to expose — a sweep that runs, reports
# success, and makes no progress — is invisible to JOB_RUNS and JOB_LAST_SUCCESS,
# and every log-based signal for it either spams once the backlog is drained or
# goes silent exactly when it matters. A flat non-zero line here is the signal.
PREPARED_GEOMETRY_BACKLOG = Gauge(
    "traxjourney_prepared_geometry_backlog",
    "Activities with a polyline and no current prepared row, including any that "
    "can never be prepared. Should fall to a small constant and stay there.",
)

PREPARED_GEOMETRY_OUTCOMES = Counter(
    "traxjourney_prepared_geometry_outcomes_total",
    "Rows the backfill sweep examined, by outcome.",
    ["outcome"],  # prepared | unpreparable | error
)

# ── Database ──────────────────────────────────────────────────────────────────

DB_QUERIES = Counter(
    "traxjourney_db_queries_total",
    "SQL statements executed, by operation.",
    ["operation"],
)

DB_QUERY_DURATION = Histogram(
    "traxjourney_db_query_duration_seconds",
    "Wall-clock duration of individual SQL statements.",
    ["operation"],
)

DB_SESSION_DURATION = Histogram(
    "traxjourney_db_session_duration_seconds",
    "Wall-clock lifetime of a get_session() scope.",
)

DB_ERRORS = Counter(
    "traxjourney_db_errors_total",
    "Database errors escaping a get_session() scope, by kind.",
    ["kind"],
)

DB_POOL_CONNECTIONS = Gauge(
    "traxjourney_db_pool_connections",
    "Connections in the SQLAlchemy pool, by state.",
    ["state"],
)

DB_POOL_OVERFLOW = Gauge(
    "traxjourney_db_pool_overflow",
    "Connections currently open beyond pool_size.",
)

DB_POOL_CAPACITY = Gauge(
    "traxjourney_db_pool_capacity",
    "Maximum connections the pool will hand out (pool_size + max_overflow).",
)

DB_FILE_SIZE = Gauge(
    "traxjourney_db_file_size_bytes",
    "Size of the SQLite database file and its write-ahead log.",
    ["file"],
)

STALE_WRITES = Counter(
    "traxjourney_stale_writes_total",
    "Optimistic-lock conflicts (StaleWriteError) surfaced to clients as 409.",
)


# ── HTTP requests ─────────────────────────────────────────────────────────────

_HTTP_METRIC_PREFIX = "traxjourney_http_"


def instrument_http(app) -> None:
    """Attach request rate / latency / error-rate metrics to *app*.

    Labels come from the route template, so path parameters can't multiply the
    series count. Status codes are deliberately **not** grouped into
    ``2xx``/``4xx``/``5xx``: this API's meaningful statuses are a small closed
    set, and 409 (optimistic-lock conflict) and 401-vs-404 are individually
    worth watching. The in-progress gauge is what tells a slow request apart
    from a stuck one (issue #45).

    Any HTTP collector left over from an earlier call is dropped first.
    Re-instrumenting only happens when ``api.router`` is reloaded — which
    ``tests/test_version_endpoint.py`` does — and prometheus_client refuses to
    register the same metric name twice, so without this a reload raises.
    """
    for name, collector in list(REGISTRY._names_to_collectors.items()):
        if name.startswith(_HTTP_METRIC_PREFIX):
            try:
                REGISTRY.unregister(collector)
            except KeyError:  # already gone with a sibling name
                pass

    Instrumentator(
        should_group_status_codes=False,
        should_instrument_requests_inprogress=True,
        inprogress_name=_HTTP_METRIC_PREFIX + "requests_inprogress",
        excluded_handlers=["/metrics"],
    ).instrument(app, metric_namespace="traxjourney")


# ── Third-party call tracking ─────────────────────────────────────────────────

_ID_SEGMENT = re.compile(r"/\d+(?=/|$)")


def normalise_path(path: str) -> str:
    """Replace numeric path segments with ``{id}``.

    ``/activities/12345/streams`` → ``/activities/{id}/streams``. Every path
    used as an ``endpoint`` label must go through this: a Strava activity ID in
    a label value would mint a new time series per activity ever imported.
    """
    return _ID_SEGMENT.sub("/{id}", path)


class ExternalCall:
    """Handle yielded by :func:`track_external` so callers can set the outcome.

    Left alone, a block that completes records ``success`` and a block that
    raises records ``exception``. Setting ``outcome`` wins over both — that is
    how an HTTP status the caller inspects itself (``429`` → ``rate_limited``)
    is recorded even though it then raises.
    """

    __slots__ = ("outcome",)

    def __init__(self) -> None:
        self.outcome: Optional[str] = None


@contextmanager
def track_external(service: str, endpoint: str) -> Iterator[ExternalCall]:
    """Time a third-party call, record its outcome, and log it (issue #205).

    The single choke point for external-call logging: every caller gets an
    INFO line on success and a WARNING on failure/exception for free, with no
    per-site logging code needed.
    """
    call = ExternalCall()
    start = time.perf_counter()
    try:
        yield call
    except BaseException:
        if call.outcome is None:
            call.outcome = "exception"
        raise
    finally:
        duration = time.perf_counter() - start
        EXTERNAL_DURATION.labels(service, endpoint).observe(duration)
        outcome = call.outcome or "success"
        EXTERNAL_REQUESTS.labels(service, endpoint, outcome).inc()
        if outcome == "success":
            _log.info("external call succeeded service=%s endpoint=%s duration=%.3fs",
                       service, endpoint, duration)
        else:
            _log.warning("external call failed service=%s endpoint=%s outcome=%s duration=%.3fs",
                          service, endpoint, outcome, duration)


def outcome_for_status(status_code: int) -> str:
    """Map an upstream HTTP status onto the closed ``outcome`` label set."""
    if status_code < 400:
        return "success"
    if status_code in (401, 403):
        return "auth_error"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    return "client_error"


# ── Scheduled jobs ────────────────────────────────────────────────────────────

JOB_EVENT_MASK = (
    EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
)

# job_id → perf_counter() at submission. APScheduler's execution events carry no
# duration, so it is measured across the submitted→executed pair. Safe with the
# default max_instances=1: at most one run per job is ever in flight.
_job_starts: dict[str, float] = {}


def record_job_event(event) -> None:  # apscheduler.events.JobEvent
    """APScheduler listener recording run counts, duration and last success.

    Registered once against the scheduler with :data:`JOB_EVENT_MASK`, so every
    job — the daily backup, the WAL checkpoint, anything added later — is
    covered without per-job wiring.

    ``EVENT_JOB_MISSED`` is included deliberately: a job whose run was skipped
    because the previous one was still going only ever showed up as a log line
    (see ``configure_logging``'s docstring), which is invisible after the fact.
    """
    job = getattr(event, "job_id", None) or "unknown"

    if event.code == EVENT_JOB_SUBMITTED:
        _job_starts[job] = time.perf_counter()
        return

    if event.code == EVENT_JOB_MISSED:
        JOB_RUNS.labels(job, "missed").inc()
        return

    start = _job_starts.pop(job, None)
    if start is not None:
        JOB_DURATION.labels(job).observe(time.perf_counter() - start)

    if getattr(event, "exception", None) is not None:
        JOB_RUNS.labels(job, "error").inc()
    else:
        JOB_RUNS.labels(job, "success").inc()
        JOB_LAST_SUCCESS.labels(job).set(time.time())


# ── Database ──────────────────────────────────────────────────────────────────

_SQL_OPERATIONS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "PRAGMA"})


def _operation_of(statement: str) -> str:
    """First SQL keyword, folded onto a closed set (never the statement text)."""
    head = statement.strip().split(None, 1)
    if not head:
        return "OTHER"
    keyword = head[0].upper()
    return keyword if keyword in _SQL_OPERATIONS else "OTHER"


def _pool_stat(engine, name: str) -> float:
    """Read a pool statistic, degrading to 0 when the pool doesn't expose it.

    Read through ``engine.pool`` at call time — ``engine.dispose()`` swaps the
    pool object out. In-memory SQLite (tests) uses a pool that has no
    ``checkedout``/``overflow``, and a scrape must never raise.
    """
    fn = getattr(engine.pool, name, None)
    if fn is None:
        return 0.0
    try:
        return float(fn())
    except Exception:
        return 0.0


def _pool_capacity(engine) -> float:
    """pool_size + max_overflow — the ceiling that issue #35 hit."""
    size = _pool_stat(engine, "size")
    # No public accessor for max_overflow; the private attribute is stable and
    # a wrong reading here costs a slightly-off gauge, never a failed scrape.
    overflow = getattr(engine.pool, "_max_overflow", 0)
    if not isinstance(overflow, int) or overflow < 0:
        return size
    return size + overflow


def _db_file_size(engine, suffix: str = "") -> float:
    """Size of the SQLite file (or its ``-wal`` sidecar); 0 if not applicable."""
    path = engine.url.database
    if not path or path == ":memory:":
        return 0.0
    try:
        return float(os.path.getsize(path + suffix))
    except OSError:
        return 0.0


_DB_METRICS_MARK = "_traxjourney_db_metrics_installed"


def install_db_metrics(engine) -> None:
    """Wire query, pool and file-size metrics onto *engine* (idempotent).

    Pool and file-size gauges are computed at scrape time via ``set_function``:
    no background thread, and no cost between scrapes. They are per-process
    singletons, so the last engine installed wins — the app installs exactly
    one, and tests install their own deliberately.
    """
    if getattr(engine, _DB_METRICS_MARK, False):
        return
    setattr(engine, _DB_METRICS_MARK, True)

    @event.listens_for(engine, "before_cursor_execute")
    def _before(_conn, _cursor, _statement, _params, context, _executemany):
        context._traxjourney_query_start = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(_conn, _cursor, statement, _params, context, _executemany):
        operation = _operation_of(statement)
        start = getattr(context, "_traxjourney_query_start", None)
        if start is not None:
            DB_QUERY_DURATION.labels(operation).observe(time.perf_counter() - start)
        DB_QUERIES.labels(operation).inc()

    DB_POOL_CONNECTIONS.labels("in_use").set_function(
        lambda: _pool_stat(engine, "checkedout")
    )
    DB_POOL_CONNECTIONS.labels("idle").set_function(
        lambda: _pool_stat(engine, "checkedin")
    )
    DB_POOL_OVERFLOW.set_function(lambda: _pool_stat(engine, "overflow"))
    DB_POOL_CAPACITY.set_function(lambda: _pool_capacity(engine))
    DB_FILE_SIZE.labels("main").set_function(lambda: _db_file_size(engine))
    DB_FILE_SIZE.labels("wal").set_function(lambda: _db_file_size(engine, "-wal"))


def db_error_kind(exc: BaseException) -> Optional[str]:
    """Classify a database failure, or return None if it isn't one.

    Application exceptions raised *inside* a ``get_session()`` block (an
    ``HTTPException`` from a failed login, say) travel out through the same
    context manager, and counting those as database errors would make the
    metric meaningless — so anything that isn't a SQLAlchemy error is ignored.
    """
    if isinstance(exc, sqlalchemy.exc.TimeoutError):
        return "pool_timeout"  # pool exhausted — the issue #35 signature
    if isinstance(exc, sqlalchemy.exc.OperationalError):
        if "database is locked" in str(exc).lower():
            return "locked"
        return "operational"
    if isinstance(exc, sqlalchemy.exc.SQLAlchemyError):
        return "other"
    return None


@contextmanager
def track_db_session() -> Iterator[None]:
    """Time a ``get_session()`` scope and classify database errors leaving it."""
    start = time.perf_counter()
    try:
        yield
    except BaseException as exc:
        kind = db_error_kind(exc)
        if kind is not None:
            DB_ERRORS.labels(kind).inc()
        raise
    finally:
        DB_SESSION_DURATION.observe(time.perf_counter() - start)

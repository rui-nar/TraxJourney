"""Logging utilities for TraxJourney."""

import contextvars
import logging
import logging.handlers
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def setup_logging(
    name: str,
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    log_dir: str = "logs",
) -> logging.Logger:
    """
    Set up logging for a module.

    Args:
        name: Logger name (typically __name__)
        level: Logging level (default: INFO)
        log_file: Optional log file name
        log_dir: Directory for log files (default: logs/)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        log_path = Path(log_dir)
        log_path.mkdir(exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            log_path / log_file, maxBytes=10485760, backupCount=5
        )
        file_handler.setLevel(level)
        file_format = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get an existing logger by name.

    Args:
        name: Logger name

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


# Top-level logger namespaces used by app modules via get_logger(__name__):
# everything under `api.*` (routers) and `src.*` (business logic / services).
# ``apscheduler`` (background jobs — WAL checkpoint, daily backup) has no
# handler of its own either, so it gets the same treatment.
_APP_LOGGER_NAMES = ("api", "src", "apscheduler")
_APP_HANDLER_MARK = "_traxjourney_app_handler"

# uvicorn wires its own handlers onto these before the app module is imported
# (Config.__init__ calls configure_logging() ahead of Config.load()), but its
# default formatters omit the timestamp entirely — restyled in place below
# rather than replaced, so the record's %(message)s (built by uvicorn's
# ColourizedFormatter subclasses from client_addr/request_line/status_code)
# still renders correctly.
_UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Request/user correlation (issue #205). Populated by api.middleware's
# access-log middleware for the lifetime of one request; every other caller —
# APScheduler jobs, the "TraxJourney API starting..." import-time line, a worker
# process — never sets these, so the explicit "-" default is what actually
# shows up for them. ContextVar.get() with no default raises LookupError, and
# a logging.Filter running on every record can't be allowed to raise.
request_id_var: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "request_id", default="-"
)
user_id_var: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "user_id", default="-"
)


class _RequestContextFilter(logging.Filter):
    """Stamps every LogRecord with the current request_id/user_id.

    Attached at the handler level (both the shared app handler and each
    restyled uvicorn handler below) so it covers every path a record can take
    to the console, not just ``api.*``/``src.*``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        return True


_context_filter = _RequestContextFilter()

# Names accepted by LOG_LEVEL and the admin live-override endpoint (issue #208).
# Global on/off only — no per-module control, see the issue's stated non-goals.
LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR")

# The env-configured baseline (set by configure_logging()'s *level* arg) and
# the live, process-memory-only override an admin can apply on top of it
# (issue #208). Neither survives a restart — LOG_LEVEL is what does; a
# restart always drops back to it. Module-level rather than a class because
# there is exactly one process-wide logging configuration, same reasoning as
# _APP_LOGGER_NAMES above.
_env_level: int = logging.INFO
_override_level: Optional[int] = None
_override_expires_at: Optional[float] = None


def env_level() -> int:
    """``LOG_LEVEL`` parsed to a logging level, defaulting to INFO.

    Read by the two process entry points (``api/router.py``,
    ``src/jobs/worker.py``) before their ``configure_logging()`` call. An
    unset or unrecognized value falls back to INFO rather than raising — a
    typo'd env var must not crash boot.
    """
    raw = os.environ.get("LOG_LEVEL", "").strip().upper()
    if not raw:
        return logging.INFO
    if raw not in LEVEL_NAMES:
        logging.getLogger(__name__).warning(
            "LOG_LEVEL=%r is not one of %s — defaulting to INFO", raw, LEVEL_NAMES
        )
        return logging.INFO
    return getattr(logging, raw)


def apply_level(level: int) -> None:
    """Set every app logger namespace — and their handlers — to *level*.

    The one place that actually mutates logger/handler levels. Both
    ``configure_logging()`` and the override functions below call through
    this so the env baseline and a live override can never apply the level
    two different ways. Handlers need their own ``setLevel`` touched too:
    a handler created at INFO (configure_logging()'s initial level) would
    otherwise keep filtering out DEBUG records even after the logger itself
    is turned down.
    """
    for name in _APP_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        for handler in logger.handlers:
            handler.setLevel(level)


@dataclass(frozen=True)
class LevelInfo:
    """The level actually in effect right now, plus enough to explain why —
    what the admin UI's status chip renders."""
    effective_level: int
    source: str  # "env" or "override"
    env_level: int
    override_level: Optional[int]
    override_expires_at: Optional[float]


def _override_is_live() -> bool:
    return _override_level is not None and (
        _override_expires_at is None or _override_expires_at > time.time()
    )


def current_level_info() -> LevelInfo:
    if _override_is_live():
        return LevelInfo(
            effective_level=_override_level,
            source="override",
            env_level=_env_level,
            override_level=_override_level,
            override_expires_at=_override_expires_at,
        )
    return LevelInfo(
        effective_level=_env_level,
        source="env",
        env_level=_env_level,
        override_level=None,
        override_expires_at=None,
    )


def set_level_override(level: int, expires_at: Optional[float]) -> None:
    """Apply a live override on this process, on top of the env baseline.

    Process-memory only (issue #208): a restart drops back to ``LOG_LEVEL``.
    Does not touch the shared (Redis) copy other processes read from — call
    sites that need cross-process propagation go through
    ``src.utils.log_level_store`` themselves (see ``api/admin.py``).
    """
    global _override_level, _override_expires_at
    _override_level = level
    _override_expires_at = expires_at
    apply_level(level)


def clear_level_override() -> None:
    """Drop this process's live override; the env baseline takes back over."""
    global _override_level, _override_expires_at
    _override_level = None
    _override_expires_at = None
    apply_level(_env_level)


def publish_level_override(level: int, expires_at: Optional[float]) -> None:
    """Apply a live override on this process *and* publish it for other
    processes (the worker) to pick up. What ``PUT /api/admin/log-level``
    calls — the two-step apply-then-write split (``set_level_override`` /
    ``log_level_store.write``) exists only so each half can also be used on
    its own (e.g. a process re-applying a store value it just read)."""
    from src.utils.log_level_store import write as _write_store

    set_level_override(level, expires_at)
    _write_store(level, expires_at)


def revert_log_level_override() -> None:
    """Undo a live override everywhere: this process's in-memory state and
    the shared Redis copy other processes (the worker) read from. Used by
    ``DELETE /api/admin/log-level`` and by the scheduled auto-revert job."""
    from src.utils.log_level_store import clear as _clear_store

    clear_level_override()
    _clear_store()


def refresh_level_from_store() -> None:
    """Pull the shared (Redis-backed) override into this process, if any.

    Called once per background job (``src.jobs.queue.enqueue``'s queued
    path) so a worker process — which never sees the admin API's in-memory
    state directly — still picks up a live override an admin just applied,
    or drops one that was just reverted. A no-op wherever Redis isn't
    configured, matching ``log_level_store``'s existing no-Redis contract.
    """
    global _override_level, _override_expires_at
    from src.utils.log_level_store import read as _read_store

    stored = _read_store()
    if stored is None:
        if _override_level is not None:
            clear_level_override()
        return
    level, expires_at = stored
    if expires_at is not None and expires_at <= time.time():
        clear_level_override()
        return
    if level != _override_level or expires_at != _override_expires_at:
        set_level_override(level, expires_at)


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a millisecond-timestamped console handler across every logger
    this process emits through (idempotent).

    App modules obtain loggers via ``get_logger(__name__)`` under the ``api.*``
    and ``src.*`` namespaces, and APScheduler logs under ``apscheduler``. Those
    loggers have no handlers of their own, so without this their records
    propagate to an unconfigured root and are dropped (only WARNING+ survives,
    via logging's last-resort handler) — which is why early auth diagnostics
    were invisible when running under uvicorn, and why APScheduler's "job
    missed" warnings showed up with no timestamp at all (issue #45 investigation
    needed exact timing to distinguish a slow request from a stuck one).

    uvicorn's own ``uvicorn``/``uvicorn.error``/``uvicorn.access`` loggers do
    have handlers (uvicorn wires those up before this app module is even
    imported), but uvicorn's default formatters print no timestamp either —
    those get their existing handler's formatter swapped in place instead of a
    second handler added, so access/error lines aren't duplicated.

    ``%(asctime)s`` includes milliseconds by default (``,mmm``) with no
    ``datefmt`` override, which is what every one of these needed to correlate
    a request's actual server-side duration against the client's own timeout.

    Safe to call more than once (e.g. on module reload): a handler is added
    only if one isn't already there.

    *level* becomes the env baseline (issue #208) that a live admin override
    applies on top of, and that ``clear_level_override()`` reverts to.
    """
    global _env_level
    _env_level = level

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - "
        "request_id=%(request_id)s user_id=%(user_id)s - %(message)s"
    )
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(_context_filter)
    setattr(handler, _APP_HANDLER_MARK, True)

    for name in _APP_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        already = any(
            getattr(h, _APP_HANDLER_MARK, False) for h in logger.handlers
        )
        if not already:
            logger.addHandler(handler)

    for name in _UVICORN_LOGGER_NAMES:
        for h in logging.getLogger(name).handlers:
            h.setFormatter(formatter)
            if _context_filter not in h.filters:
                h.addFilter(_context_filter)

    # A second call (e.g. module reload) must not silently drop a live
    # override back to the just-passed env level — re-apply whichever is
    # actually in effect.
    apply_level(current_level_info().effective_level)
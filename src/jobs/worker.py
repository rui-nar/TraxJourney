"""RQ worker entry point — ``python -m src.jobs.worker`` (issue #173).

Run one process per unit of concurrency you want. For the ``resolve`` queue that
count *is* the Overpass politeness bound, so raising it is a deliberate decision
about load on a free public API, not a throughput knob.

The worker deliberately does not run migrations, the APScheduler jobs, or the
admin seed — see ``TRAXJOURNEY_ROLE`` in ``api/router.py`` for why.
"""
from __future__ import annotations

import os
import sys
import time

from src.jobs.queue import ALL_QUEUES
from src.jobs.redis_client import get_redis, redis_url, reset_redis
from src.utils.logging import configure_logging, env_level, get_logger

_log = get_logger(__name__)

# Backoff between reconnect attempts once REDIS_URL is set but the broker isn't
# answering. Capped, not unbounded — a worker idling at a 60s ping is cheap to
# leave running.
#
# Previously a single failed probe made the worker exit(1) immediately, and
# `restart: unless-stopped` relaunched it a few seconds later. That is fine for
# a broker that is actually down, but when Redis is merely *slow* — starved by
# whatever else is loading the host — every relaunch itself burns CPU/memory,
# deepening the exact contention that made Redis slow in the first place. Two
# workers doing that in lockstep turned one overloaded host into a ~15s crash
# loop that outlasted the job that triggered it (issue #209). Retrying in place
# costs nothing extra and gives the underlying pressure a chance to clear.
_RECONNECT_INTERVALS_S = [2, 5, 10, 30, 60]


def _work_horse_killed_handler(job, retpid, ret_val, rusage) -> None:
    """RQ callback for a work-horse that died without raising a catchable
    exception — a SIGKILL (almost always the OOM killer) is the common case
    for the memory-heavy ``poster`` queue. Called synchronously by the still-
    alive parent worker process the moment the death is detected, which is
    what makes this the fast path: a poster render is otherwise left
    "running" forever until the next API restart's orphan sweep (see
    ``src.poster.poster_job_runner.sweep_orphaned_poster_jobs``), and the
    user who was promised an email hears nothing in the meantime.

    ``job.args`` is ``(func, *args)`` — every queued job goes through
    ``src.jobs.queue._run_with_level_refresh(func, *args)``, so *func* is the
    real callable regardless of which queue it came from. Only a poster job
    (``run_poster_job``) is handled here: a route job's own resolve failure
    is already recoverable losslessly by the next resolve trigger, and the
    default queue's jobs (share tiles, stats) aren't user-facing durable work.
    Never raises — a broken handler must not take the worker process down.
    """
    try:
        from src.poster.poster_job_runner import mark_job_interrupted, run_poster_job

        func = job.args[0] if job.args else None
        if func is not run_poster_job:
            return
        job_id = job.args[1]
        mark_job_interrupted(
            job_id,
            "The poster generation process was terminated unexpectedly "
            "(likely out of memory) — try a smaller region or fewer "
            "photos, or try again.",
        )
    except Exception:
        _log.exception("work_horse_killed_handler failed for job %s", getattr(job, "id", "?"))


def _connect_with_retry(sleep=time.sleep):
    """Block until Redis answers, retrying with backoff instead of giving up."""
    attempt = 0
    while True:
        client = get_redis()
        if client is not None:
            return client
        delay = _RECONNECT_INTERVALS_S[min(attempt, len(_RECONNECT_INTERVALS_S) - 1)]
        _log.warning("Redis unreachable (%s) — retrying in %ss", redis_url(), delay)
        sleep(delay)
        reset_redis()
        attempt += 1


def main(argv: list[str] | None = None) -> int:
    """Consume the named queues (default: all) until killed."""
    # LOG_LEVEL (issue #208) is the restart-persistent baseline. A live admin
    # override applies on top of it per-job (see src.jobs.queue.enqueue).
    configure_logging(level=env_level())
    os.environ.setdefault("TRAXJOURNEY_ROLE", "worker")

    queues = argv if argv else list(ALL_QUEUES)

    if not redis_url():
        _log.error(
            "cannot start a worker: REDIS_URL is not set. Without a broker "
            "there is no queue to consume — the API process runs background "
            "work itself.")
        return 1

    client = _connect_with_retry()

    from rq import Worker

    _log.info("worker starting on queues: %s", ", ".join(queues))
    Worker(
        queues, connection=client,
        work_horse_killed_handler=_work_horse_killed_handler,
    ).work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

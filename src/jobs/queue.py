"""RQ queues and the enqueue helpers every background job goes through (#173).

Before this, background work ran as FastAPI ``BackgroundTasks`` in the API
process. That has two problems the queue fixes:

* **Durability.** A restart loses every in-flight job. For route resolution the
  segment stayed ``pending`` and recovery was implemented in the Flutter client,
  which only ran when someone reopened the project.
* **Concurrency control.** An RQ worker runs one job at a time, so the number of
  workers on a queue bounds how many of that queue's jobs run at once — a
  deployment parameter, enforced regardless of how many API processes exist.
  (This was originally described as the bound on *Overpass* requests too. It is
  not, and cannot be: one resolve makes several queries. That bound lives in
  ``src.jobs.upstream_slots``, applied per request.)

``enqueue`` falls back to running in-process when no broker is configured, so a
self-hosted instance and the test suite need no Redis. That fallback is also the
rollback lever: unset ``REDIS_URL`` and the deployment reverts to the old
behaviour with no image change.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from src.jobs.redis_client import get_redis
from src.utils.logging import get_logger

_log = get_logger(__name__)

# Queue names. Split by workload profile rather than by feature: what matters is
# how many may run at once and how long each holds a worker.
QUEUE_RESOLVE = "resolve"   # HAFAS + Overpass; long-running, a few at a time
QUEUE_POSTER = "poster"     # A0 rendering — memory-heavy, keep it to one
QUEUE_DEFAULT = "default"   # share tiles, stats: short and cheap
QUEUE_VIDEO = "video"       # trip video renders — minutes each, never in-process

ALL_QUEUES = (QUEUE_RESOLVE, QUEUE_POSTER, QUEUE_DEFAULT, QUEUE_VIDEO)

# How many jobs from each queue may run at once. An RQ worker takes one job at a
# time, so a queue's bound *is* the number of worker processes listening on it —
# which makes this a property of the deployment topology, not of any code path
# here. It lives in this module anyway because the numbers are properties of the
# jobs, not of a host: 2 on `resolve` bounds how many rail resolves are in
# flight, and 1 on `poster` (and on `video`) is the memory footprint of one
# render.
#
# NOTE this is *not* the Overpass politeness bound, though it was once described
# as one. A worker count cannot be: a single resolve makes four or five Overpass
# queries and jobs on the other queues make none, so the two numbers are only
# coincidentally related. That bound is enforced per request in
# src.jobs.upstream_slots, against the host's own advertised per-IP limit.
#
# Nothing at runtime reads this — a worker cannot know how many siblings it has.
# `tests/test_worker_topology.py` checks the shipped compose file against it, so
# that changing one without the other fails CI instead of silently doubling the
# load on a free public API (issue #188).
QUEUE_MAX_CONCURRENCY = {
    QUEUE_RESOLVE: 2,
    QUEUE_POSTER: 1,
    QUEUE_DEFAULT: 2,
    QUEUE_VIDEO: 1,
}

# Generous: a rail resolve makes several Overpass queries, each with a 45 s HTTP
# timeout. Well past the point where the client has stopped polling, but the
# result is still worth persisting for the next page load.
_JOB_TIMEOUT_S = 600
_RESULT_TTL_S = 3600

# Retries are a queue property here rather than something each job hand-rolls.
# The intervals are minutes, not milliseconds: these failures are rate limits
# and network trouble, not write contention.
_RETRY_INTERVALS = [10, 30, 60]


def _run_with_level_refresh(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Job wrapper actually handed to RQ (issue #208).

    Must be a plain module-level function, not a closure — RQ serializes a
    job by the importable name of its callable, and *func* travels as a
    plain argument alongside it (also importable-by-reference, same
    requirement every queued job already meets).

    Refreshes this worker process's log level from the shared store before
    running *func*, so a live override an admin applied through the API
    process reaches whichever job happens to execute next — not just route
    resolution, since every queue (`resolve`/`poster`/`default`) goes
    through this same wrapper.
    """
    from src.utils.logging import refresh_level_from_store

    refresh_level_from_store()
    return func(*args, **kwargs)


def queue_available() -> bool:
    """Whether jobs will be queued rather than run in-process."""
    return get_redis() is not None


def queue_has_workers(name: str) -> bool:
    """Whether a broker is reachable and at least one RQ worker listens on *name*.

    For queues with no in-process fallback (``video``): a job queued with no
    worker would sit pending until a sweep fails it. One Redis call (the
    queue's worker set, which a worker joins at birth and leaves at death).
    Any broker error counts as "no worker" — never an exception to the caller.
    """
    try:
        queue = get_queue(name)
        if queue is None:
            return False
        from rq import Worker

        return Worker.count(queue=queue) > 0
    except Exception as exc:  # noqa: BLE001 — a broker error means "unavailable"
        _log.warning("could not count workers on %r (%s)", name, exc)
        return False


def get_queue(name: str):
    """The RQ queue *name*, or ``None`` when running without a broker."""
    client = get_redis()
    if client is None:
        return None
    from rq import Queue  # imported lazily: unused in the no-Redis deployment

    return Queue(name, connection=client, default_timeout=_JOB_TIMEOUT_S)


def enqueue(
    queue_name: str,
    func: Callable[..., Any],
    *args: Any,
    background_tasks: Optional[Any] = None,
    max_retries: int = len(_RETRY_INTERVALS),
    job_timeout: Optional[int] = None,
    allow_inline: bool = True,
    **kwargs: Any,
) -> bool:
    """Run *func* out of process if possible; otherwise in this one.

    Returns True when the job was queued. Pass *background_tasks* from a request
    handler so the fallback still defers the work until after the response —
    without it the fallback runs *inline*, which for a resolve would block the
    request for the tens of seconds this whole design exists to avoid.

    A broker that fails at enqueue time falls back rather than 500s: losing
    durability is better than losing the request.

    *max_retries* of 0 queues the job with no retry at all. *job_timeout*
    overrides the queue's default (seconds). With *allow_inline* False — and
    always on the ``video`` queue, whose renders take minutes — there is no
    fallback: a missing or failing broker returns False and *func* is not run,
    so the caller can fail the job instead of starving the API process.
    """
    inline = allow_inline and queue_name != QUEUE_VIDEO
    queue = get_queue(queue_name)
    if queue is not None:
        try:
            options: dict[str, Any] = {"result_ttl": _RESULT_TTL_S}
            if max_retries > 0:
                # RQ's Retry refuses max=0, so "never retry" is no Retry at all.
                from rq import Retry

                options["retry"] = Retry(
                    max=max_retries, interval=_RETRY_INTERVALS[:max_retries])
            if job_timeout is not None:
                options["job_timeout"] = job_timeout
            queue.enqueue(_run_with_level_refresh, func, *args, **options, **kwargs)
            return True
        except Exception as exc:  # noqa: BLE001 — degrade to in-process
            _log.warning("enqueue to %r failed (%s)%s", queue_name, exc,
                         " — running in-process" if inline else "")

    if not inline:
        _log.error(
            "job for queue %r not run: no broker to queue it on and running it "
            "in-process is not allowed (REDIS_URL unset or Redis unreachable).",
            queue_name,
        )
        return False

    if queue_name == QUEUE_POSTER:
        # Every other queue degrading to in-process just delays that one job.
        # A poster render degrading this way is worse: it's a CPU-heavy Pillow
        # composite (up to ~140M pixels) plus blocking sequential Mapbox tile
        # fetches, run inside the API's own process/threadpool — it starves
        # every other request for the duration of the render, not just this
        # job's caller. Louder than the generic warning above on purpose.
        _log.error(
            "poster job has no broker to run on (queue_name=%r, REDIS_URL unset "
            "or Redis unreachable) — rendering in-process, which will block the "
            "API process for every other request until the render finishes. "
            "Configure REDIS_URL and the worker-poster service — see "
            "docs/DEPLOYMENT_VPS.md.",
            queue_name,
        )

    if background_tasks is not None:
        background_tasks.add_task(func, *args, **kwargs)
    else:
        func(*args, **kwargs)
    return False

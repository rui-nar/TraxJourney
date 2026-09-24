"""Housekeeping for ``PROMETHEUS_MULTIPROC_DIR`` (issue #437).

With the directory set, every process writes its samples to its own files
there (``counter_<id>.db``, ``gauge_all_<id>.db``, ...) and ``/metrics`` sums
them. The API container and both worker containers mount the same directory.
prometheus_client leaves three things to the application, all handled here:

**Unique file names across containers.** prometheus_client names the files by
PID. Each container has its own PID namespace: the API and every worker's
parent process are all PID 1, and the work-horses RQ forks in the two worker
containers get the same small PIDs. Two live processes on one file overwrite
each other's samples, so the identifier is ``<hostname>-<pid>``. Docker sets
the hostname to the container ID, which is unique per container.

**Clearing files left by an earlier run.** A dead process' counters and gauges
are otherwise exported, and summed, forever. The directory is cleared by the
first process to start writing while no other writer is alive, which is what
"once per stack start" means here. Every writer holds a shared ``flock`` on
one lock file in the directory for its whole life (a forked child inherits it).
A starting process tries to take that lock exclusively without waiting: if it
gets it, no writer is alive, so it deletes every ``*.db`` before creating its
own; if it doesn't, the files belong to live processes (or to dead ones whose
counters those live processes' totals already include) and are kept. Either
way it then takes the shared lock, waiting for a wipe in progress to finish.

**Forgetting an exited process.** ``forget_process`` is prometheus_client's
``mark_process_dead``: it deletes that process' live-mode gauge files
(``livesum``/``liveall``/...). Counter, histogram and ``all``-mode gauge files
stay until the next clear. Deleting a dead process' counters while others keep
counting would make the summed counter drop, and ``rate()`` reads a drop as a
reset.

``flock`` needs the directory on a local filesystem shared by one kernel: a
bind mount on the Docker host, as in ``docker-compose.yml.example``. Not NFS
or SMB.
"""
from __future__ import annotations

import glob
import os
import re
import socket
from typing import Optional

from src.utils.logging import get_logger

_log = get_logger(__name__)

WRITERS_LOCK = ".writers.lock"

# prometheus_client splits file names on "_", so the identifier must not hold one.
_HOST = re.sub(r"[^A-Za-z0-9.-]", "-", socket.gethostname()) or "host"


def multiproc_dir() -> Optional[str]:
    """The configured directory, or None. Empty counts as unset, as in ``/metrics``."""
    return os.environ.get("PROMETHEUS_MULTIPROC_DIR") or None


def process_identifier(pid: Optional[int] = None) -> str:
    """This process' (or *pid*'s, in this container) name in the file names."""
    return f"{_HOST}-{os.getpid() if pid is None else pid}"


def claim_multiproc_dir(path: str) -> Optional[int]:
    """Register this process as a writer of *path*, clearing it first when no
    other writer is alive. Call before the first sample is written.

    Returns the lock's file descriptor, which must stay open for the life of
    the process: closing it is what tells the next starter this one is gone.
    Returns None where ``flock`` doesn't exist (Windows development), and
    clears nothing there.
    """
    try:
        import fcntl
    except ImportError:
        return None

    os.makedirs(path, exist_ok=True)
    fd = os.open(os.path.join(path, WRITERS_LOCK), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass  # a live writer holds it: these files are in use
    else:
        stale = glob.glob(os.path.join(path, "*.db"))
        for name in stale:
            try:
                os.remove(name)
            except FileNotFoundError:
                pass
        if stale:
            _log.info("cleared %d metric files left by an earlier run from %s",
                      len(stale), path)
    # Converting EX to SH is not atomic: another starter may take EX in the gap
    # and clear the directory. Nothing is lost, since this process has written
    # nothing yet, and this call waits until that clear is done.
    fcntl.flock(fd, fcntl.LOCK_SH)
    return fd


def forget_process(pid: int) -> None:
    """Drop the live-mode gauge files of *pid*, a process of this container
    that has exited. No-op when multiprocess mode is off."""
    path = multiproc_dir()
    if not path or not pid:
        return
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(process_identifier(pid), path)

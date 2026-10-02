"""Fetch a render's map tiles ahead of the frame loop (docs/VIDEO_RENDER_TIME_PLAN.md
D2–D7, D9).

A video render asks for its tiles one at a time, on the thread that draws the
frames, and the order is known before frame 0 (``basemap_bands.plan_requests``).
:class:`TilePrefetcher` walks that order on a few background threads, so the
frame loop waits only for a tile that isn't fetched yet.

It is a drop-in ``TileFetcher``: each call is matched to a pending result for
its ``(z, x, y)``, never trusted to be the next one. A call with no pending
result is a *miss* and is fetched synchronously, as without prefetching, so a
wrong order costs speed, never correctness.

There is no producer thread, and nothing waits on the window: the consumer's
own call submits more work, up to ``window`` submitted but unconsumed results,
each time it returns. The pool is a few daemon threads on a queue, with a stop
event, rather than ``ThreadPoolExecutor`` (whose atexit hook joins its
threads): a process exits at once after a failed render, whatever a fetch
thread is doing. Closing drops queued work and doesn't wait for in-flight
fetches; their results are discarded.

Everything but the pool's queue is touched only by the consumer's thread, so
the counters (``fetched``, ``net_seconds``, ``misses``) need no lock.
"""
from __future__ import annotations

import bisect
import itertools
import queue
import threading
import time
import weakref
from collections import deque
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from src.poster.tile_stitcher import TileFetcher

Key = Tuple[int, int, int]

# Concurrent fetches (D6, raised from 4 to 8). At 271 ms a tile on a slow
# network, 4 threads left the frames waiting; 8 cover about 30 tiles a
# second. A burst may briefly pass 100 requests a second, but Mapbox's limit
# is per minute (6,000) and the window bounds a burst. 0 disables prefetching.
PREFETCH_THREADS = 8
# Tiles fetched or in flight but not yet consumed: about two sheets.
PREFETCH_WINDOW = 32

_pool_ids = itertools.count(1)


class _Slot:
    """One submitted request: its result, once a pool thread has run it."""

    __slots__ = ("key", "index", "done", "data", "error", "net", "cancelled")

    def __init__(self, key: Key, index: int) -> None:
        self.key = key
        self.index = index          # position in the request sequence
        self.done = threading.Event()
        self.data: Optional[bytes] = None
        self.error: Optional[BaseException] = None
        self.net = 0.0              # network seconds, measured on the pool thread
        # Not to be fetched (discarded, or dropped by close). Set before
        # ``done`` when a waiter must be woken.
        self.cancelled = False


class _Pool:
    """The fetch threads and their queue. Holds no reference to the
    prefetcher, so a dropped prefetcher can be collected, and its finalizer
    stops the threads."""

    def __init__(self, fetcher: TileFetcher, threads: int) -> None:
        self._fetcher = fetcher
        self._size = threads
        self._queue: "queue.Queue[Optional[_Slot]]" = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()     # orders submit() against close()
        self.closed = False
        self.threads: Tuple[threading.Thread, ...] = ()

    def start(self) -> None:
        with self._lock:
            if self.closed:     # never started after close(): no sentinel would reach it
                return
            name = f"tile-prefetch-{next(_pool_ids)}"
            self.threads = tuple(
                threading.Thread(target=self._work, name=f"{name}-{i}", daemon=True)
                for i in range(self._size))
            for t in self.threads:
                t.start()

    def submit(self, slot: _Slot) -> bool:
        with self._lock:
            if self.closed:
                return False
            self._queue.put(slot)
            return True

    def close(self) -> None:
        """Drop queued work, wake anyone waiting on it, and tell the threads to
        exit. In-flight fetches are not waited for."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._stop.set()
            while True:
                try:
                    slot = self._queue.get_nowait()
                except queue.Empty:
                    break
                if slot is not None:
                    slot.cancelled = True
                    slot.done.set()
            for _ in self.threads:
                self._queue.put(None)     # wakes an idle thread so it exits

    def _work(self) -> None:
        while True:
            slot = self._queue.get()
            if slot is None:
                return
            if self._stop.is_set() or slot.cancelled:
                slot.cancelled = True
                slot.done.set()
                if self._stop.is_set():
                    return
                continue
            t0 = time.perf_counter()
            try:
                slot.data = self._fetcher(*slot.key)
            except BaseException as exc:    # raised at the consumer's call
                slot.error = exc
            slot.net = time.perf_counter() - t0
            slot.done.set()
            if self._stop.is_set():
                return


class TilePrefetcher:
    """A ``TileFetcher`` that fetches *requests* ahead on *threads* daemon
    threads, keeping at most *window* results submitted but unconsumed.

    Starts on its first call. ``threads == 0`` makes every call a direct,
    synchronous fetch, and so does every call after :meth:`close`.

    ``fetched`` and ``net_seconds`` count every tile a call returns (from the
    pool, a miss, or a direct fetch) and its network time; ``misses`` counts
    calls fetched synchronously while prefetching because nothing was pending
    for their key. A failed fetch counts in neither: its exception is raised.
    """

    def __init__(self, fetcher: TileFetcher, requests: Sequence[Key], *,
                 threads: int = PREFETCH_THREADS, window: int = PREFETCH_WINDOW) -> None:
        self._fetcher = fetcher
        self._requests = list(requests)
        self._window = max(1, window)
        self._pool: Optional[_Pool] = _Pool(fetcher, threads) if threads > 0 else None
        # Closes the pool when the prefetcher is closed or collected; the
        # callback refers to the pool only.
        self._finalizer = (weakref.finalize(self, self._pool.close)
                           if self._pool is not None else None)
        self._started = False
        # Each key's positions in *requests*, to tell where a miss sits.
        self._positions: Dict[Key, List[int]] = {}
        for i, key in enumerate(self._requests):
            self._positions.setdefault(key, []).append(i)
        self._next = 0                                  # next request to submit
        self._pending: Dict[Key, Deque[_Slot]] = {}     # per key, in request order
        self._order: Deque[_Slot] = deque()             # every live slot, in request order
        self._live = 0                                  # submitted, not consumed or discarded
        self.fetched = 0
        self.net_seconds = 0.0
        self.misses = 0

    @property
    def threads(self) -> Tuple[threading.Thread, ...]:
        """This prefetcher's own pool threads (empty until its first call)."""
        return self._pool.threads if self._pool is not None else ()

    def close(self) -> None:
        if self._finalizer is not None:
            self._finalizer()

    def __enter__(self) -> "TilePrefetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __call__(self, z: int, x: int, y: int) -> bytes:
        key = (z, x, y)
        if self._pool is None or self._pool.closed:
            return self._direct(key)
        if not self._started:
            self._started = True
            self._pool.start()
            self._top_up()
        slots = self._pending.get(key)
        if not slots:
            self.misses += 1
            self._skip_to(key)
            self._top_up()
            return self._direct(key)
        slot = slots[0]
        self._discard_before(slot.index)
        self._take(slot)
        slot.done.wait()
        self._top_up()
        if slot.cancelled:          # dropped by a close() from another thread
            return self._direct(key)
        if slot.error is not None:
            error, slot.error = slot.error, None
            raise error
        self.fetched += 1
        self.net_seconds += slot.net
        return slot.data

    def _direct(self, key: Key) -> bytes:
        t0 = time.perf_counter()
        data = self._fetcher(*key)
        self.net_seconds += time.perf_counter() - t0
        self.fetched += 1
        return data

    def _top_up(self) -> None:
        while self._live < self._window and self._next < len(self._requests):
            slot = _Slot(self._requests[self._next], self._next)
            if not self._pool.submit(slot):
                return
            self._next += 1
            self._pending.setdefault(slot.key, deque()).append(slot)
            self._order.append(slot)
            self._live += 1

    def _take(self, slot: _Slot) -> None:
        """Remove *slot*, the first pending one for its key, from the table."""
        slots = self._pending[slot.key]
        slots.popleft()
        if not slots:
            del self._pending[slot.key]
        self._live -= 1

    def _discard_before(self, index: int) -> None:
        """Drop the live results for requests before *index*: the render has
        moved past them, and they would hold the window forever. A queued one
        is not fetched at all; an in-flight one finishes and is dropped."""
        while self._order and self._order[0].index < index:
            slot = self._order.popleft()
            slots = self._pending.get(slot.key)
            if slots and slots[0] is slot:
                slot.cancelled = True
                self._take(slot)
        # Consumed slots are left in _order and skipped lazily here.
        while self._order and self._order[0].index == index:
            self._order.popleft()

    def _skip_to(self, key: Key) -> None:
        """On a miss for *key*: if *key* is still to come in the sequence, the
        render has jumped ahead to it. Discard everything pending, which is
        behind it, and resume submitting after it. A key not ahead (off-plan,
        or one already passed) changes nothing."""
        positions = self._positions.get(key)
        if not positions:
            return
        j = bisect.bisect_left(positions, self._next)
        if j == len(positions):
            return
        self._discard_before(positions[j])
        self._next = positions[j] + 1

"""Tests for the tile prefetcher (src/video/tile_prefetch.py) and the request
order it walks (``Sheet.requests``, ``plan_requests``), docs/VIDEO_RENDER_TIME_PLAN.md
U2a.

The fakes here are called from several threads, so each counts and records
under its own lock. A test that waits on threads always does so with a bound,
so a deadlock fails the test instead of hanging the suite.
"""
from __future__ import annotations

import gc
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from src.models.project import ConnectingSegment, Project, ProjectItem, SegmentEndpoint
from src.video.basemap_bands import Basemaps, plan_bands, plan_requests
from src.video.camera import CAMERA_MODES, camera_path
from src.video.legs import build_legs
from src.video.tile_prefetch import PREFETCH_THREADS, PREFETCH_WINDOW, TilePrefetcher
from src.video.timeline import build_timeline
from tests.test_video_renderer import SMALL, fake_tile, timeline30, timeline30_ny

ROOT = Path(__file__).resolve().parent.parent


def tile_bytes(z: int, x: int, y: int) -> bytes:
    return f"{z}/{x}/{y}".encode()


class FakeFetcher:
    """Thread-safe: counts calls, tracks how many run at once, and can sleep,
    block on a gate, or fail once for given keys."""

    def __init__(self, delay: float = 0.0, gate: threading.Event = None,
                 gate_keys: set = None, fail: dict = None) -> None:
        self.delay = delay
        self.gate = gate
        self.gate_keys = gate_keys      # the keys that wait on *gate*; None: all
        self.fail = dict(fail or {})
        self.lock = threading.Lock()
        self.calls = []             # (key, thread name), in start order
        self.active = 0
        self.max_active = 0
        self.on_start = None        # called under the lock with the key

    def __call__(self, z: int, x: int, y: int) -> bytes:
        key = (z, x, y)
        with self.lock:
            self.calls.append((key, threading.current_thread().name))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.on_start is not None:
                self.on_start(key)
            error = self.fail.pop(key, None)
        try:
            if self.gate is not None and (self.gate_keys is None or key in self.gate_keys):
                self.gate.wait(10)
            if self.delay:
                time.sleep(self.delay)
            if error is not None:
                raise error
            return tile_bytes(z, x, y)
        finally:
            with self.lock:
                self.active -= 1

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.calls)


def keys(n: int, z: int = 5):
    return [(z, i % 32, i // 32) for i in range(n)]


def wait_until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def join_all(threads, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))


def run_bounded(fn, timeout: float = 10.0):
    """Run *fn* on a helper thread; fail (rather than hang) if it doesn't end."""
    out = {}

    def target():
        try:
            out["value"] = fn()
        except BaseException as exc:    # re-raised below
            out["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "deadlocked"
    if "error" in out:
        raise out["error"]
    return out.get("value")


# ── request order ────────────────────────────────────────────────────────────

def recorded_render(shots, size):
    """The plan, and the tile requests a sequential ``Basemaps`` render of
    every frame makes, in order."""
    plan = plan_bands(shots, size)
    requested = []

    def fetcher(z, x, y):
        requested.append((z, x, y))
        return fake_tile(z, x, y)

    basemaps = Basemaps(shots, size, plan, tile_fetcher=fetcher)
    for n in range(len(shots)):
        basemaps.frame(n)
    return plan, requested


def assert_plan_order(shots, size):
    plan, requested = recorded_render(shots, size)
    for s in plan.sheets:
        assert len(s.requests) == s.tiles
    assert requested
    assert plan_requests(plan) == requested
    assert len(requested) == plan.tiles


@pytest.mark.parametrize("mode", CAMERA_MODES)
@pytest.mark.parametrize("timeline", [timeline30, timeline30_ny], ids=["paris_lyon", "paris_ny"])
def test_plan_requests_is_the_order_a_sequential_render_makes(timeline, mode):
    tl = timeline()
    assert_plan_order(camera_path(tl, tl.fps, SMALL, mode), SMALL)


def test_plan_requests_matches_a_sheet_split_across_the_antimeridian():
    seg = ConnectingSegment(id="f", segment_type="flight", date="2026-05-01",
                            start=SegmentEndpoint(35.55, 139.78),
                            end=SegmentEndpoint(33.94, -118.41))
    tl = build_timeline(build_legs(Project(name="Pacific", items=[
        ProjectItem(item_type="segment", segment=seg)])), 30.0)
    size = (640, 360)
    shots = camera_path(tl, tl.fps, size, "variable")
    assert any(len(s.pieces) > 1 for s in plan_bands(shots, size).sheets)
    assert_plan_order(shots, size)


# ── serving ──────────────────────────────────────────────────────────────────

def test_in_order_consumption_makes_no_miss_and_returns_each_keys_bytes():
    seq = keys(20)
    seq.insert(10, seq[3])                      # a key requested twice
    fake = FakeFetcher(delay=0.002)
    with TilePrefetcher(fake, seq, threads=4, window=8) as pf:
        got = run_bounded(lambda: [pf(*k) for k in seq])
    assert got == [tile_bytes(*k) for k in seq]
    assert pf.misses == 0
    assert sorted(k for k, _ in fake.calls) == sorted(seq)


def test_fetched_equals_the_fetchers_call_count_after_an_in_order_run():
    """R1-3 guard: the counters are only touched on the consumer's thread."""
    seq = keys(120)
    fake = FakeFetcher(delay=0.001)
    with TilePrefetcher(fake, seq, threads=4, window=32) as pf:
        run_bounded(lambda: [pf(*k) for k in seq])
    assert pf.fetched == fake.count == len(seq)
    assert pf.misses == 0
    assert pf.net_seconds > 0


def test_fetches_run_on_the_pool_threads_concurrently_within_the_window():
    seq = keys(40)
    index = {k: i for i, k in enumerate(seq)}
    window, threads = 6, 4
    entered = [0]                    # calls made by the consumer so far
    ahead = []                       # per pool fetch: how far past the consumer

    fake = FakeFetcher(delay=0.02)
    fake.on_start = lambda key: ahead.append(index[key] + 1 - entered[0])

    def consume():
        for k in seq:
            entered[0] += 1
            assert pf(*k) == tile_bytes(*k)

    with TilePrefetcher(fake, seq, threads=threads, window=window) as pf:
        run_bounded(consume)
    assert 2 <= fake.max_active <= threads
    assert max(ahead) <= window
    assert {name for _, name in fake.calls} <= {t.name for t in pf.threads}
    assert all(t.name.startswith("tile-prefetch-") for t in pf.threads)
    assert len(pf.threads) == threads


def test_the_default_pool_runs_eight_fetches_at_once_within_the_window():
    """D6 as amended: 8 threads by default, and the 32-tile window still bounds
    what is fetched ahead. Every fetch waits on a gate until all 8 are in."""
    assert (PREFETCH_THREADS, PREFETCH_WINDOW) == (8, 32)
    seq = keys(100)
    index = {k: i for i, k in enumerate(seq)}
    entered = [0]                    # calls made by the consumer so far
    ahead = []                       # per pool fetch: how far past the consumer
    gate = threading.Event()
    fake = FakeFetcher(gate=gate)
    fake.on_start = lambda key: ahead.append(index[key] + 1 - entered[0])

    def consume():
        for k in seq:
            entered[0] += 1
            assert pf(*k) == tile_bytes(*k)

    with TilePrefetcher(fake, seq) as pf:
        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        try:
            assert wait_until(lambda: fake.active == PREFETCH_THREADS)
        finally:
            gate.set()
        consumer.join(10)
        assert not consumer.is_alive(), "deadlocked"
    assert fake.max_active == PREFETCH_THREADS
    assert max(ahead) <= PREFETCH_WINDOW
    assert len(pf.threads) == PREFETCH_THREADS
    assert pf.misses == 0
    assert fake.count == len(seq)


def test_zero_threads_fetches_every_call_directly():
    seq = keys(5)
    fake = FakeFetcher()
    with TilePrefetcher(fake, seq, threads=0) as pf:
        assert [pf(*k) for k in seq] == [tile_bytes(*k) for k in seq]
    assert pf.threads == ()
    assert pf.misses == 0 and pf.fetched == 5
    assert {name for _, name in fake.calls} == {threading.current_thread().name}


# ── misses and out-of-order consumers ────────────────────────────────────────

def test_an_unknown_key_is_a_miss_and_still_returns_its_bytes():
    seq = keys(10)
    fake = FakeFetcher()
    with TilePrefetcher(fake, seq, threads=2, window=4) as pf:
        assert run_bounded(lambda: pf(9, 1, 2)) == tile_bytes(9, 1, 2)
        assert pf.misses == 1
        # Prefetching carries on: the planned keys are still hits.
        assert run_bounded(lambda: [pf(*k) for k in seq]) == [tile_bytes(*k) for k in seq]
    assert pf.misses == 1


def test_a_consumer_that_jumps_ahead_neither_deadlocks_nor_stops_prefetching():
    seq = keys(60)
    fake = FakeFetcher(delay=0.002)
    with TilePrefetcher(fake, seq, threads=2, window=4) as pf:
        # Past the window: misses, each moving the prefetch on past its key.
        order = [seq[20], seq[0], seq[40]] + seq[41:]
        got = run_bounded(lambda: [pf(*k) for k in order])
    assert got == [tile_bytes(*k) for k in order]
    assert pf.misses == 3                       # 20, 0 (already passed), 40
    # Nothing between the jumps was fetched beyond what one window allows.
    assert fake.count <= len(order) + 3 * 4


def test_results_the_render_skipped_do_not_hold_the_window():
    """Hits on later keys discard the earlier, unconsumed results, so the
    window moves on and the rest are still hits."""
    seq = keys(30)
    fake = FakeFetcher(delay=0.002)
    with TilePrefetcher(fake, seq, threads=2, window=4) as pf:
        order = [k for i, k in enumerate(seq) if i % 3 != 0]    # skips every third
        got = run_bounded(lambda: [pf(*k) for k in order])
    assert got == [tile_bytes(*k) for k in order]
    assert pf.misses == 0


# ── errors ───────────────────────────────────────────────────────────────────

def test_a_failed_fetch_raises_its_exception_once_at_the_call_for_its_key():
    seq = keys(10)
    boom = RuntimeError("tile 4 failed")
    fake = FakeFetcher(fail={seq[4]: boom})
    with TilePrefetcher(fake, seq, threads=2, window=8) as pf:
        assert run_bounded(lambda: pf(*seq[0])) == tile_bytes(*seq[0])
        # The first call took request 0 and topped the window up to 8 more.
        assert wait_until(lambda: fake.count == 9 and fake.active == 0)
        # The failure is stored, not raised early.
        assert run_bounded(lambda: [pf(*k) for k in seq[1:4]]) == [tile_bytes(*k) for k in seq[1:4]]
        with pytest.raises(RuntimeError) as info:
            run_bounded(lambda: pf(*seq[4]))
        assert info.value is boom
        # Asked again, it is fetched afresh (a miss), not raised again.
        assert run_bounded(lambda: pf(*seq[4])) == tile_bytes(*seq[4])
        assert run_bounded(lambda: [pf(*k) for k in seq[5:]]) == [tile_bytes(*k) for k in seq[5:]]
    assert pf.misses == 1


# ── close, abandonment, exit ─────────────────────────────────────────────────

def test_after_close_no_new_fetch_starts_and_calls_fetch_directly():
    seq = keys(20)
    gate = threading.Event()
    fake = FakeFetcher(gate=gate)
    pf = TilePrefetcher(fake, seq, threads=2, window=8)
    consumer = threading.Thread(target=lambda: pf(*seq[0]), daemon=True)
    consumer.start()
    assert wait_until(lambda: fake.active == 2)
    pf.close()
    gate.set()                      # the in-flight fetches finish
    consumer.join(5)
    assert not consumer.is_alive()
    join_all(pf.threads)
    assert not any(t.is_alive() for t in pf.threads)
    time.sleep(0.05)
    assert fake.count == 2          # only the two that were in flight
    pf.close()                      # idempotent
    assert pf(*seq[5]) == tile_bytes(*seq[5])
    assert fake.calls[-1] == (seq[5], threading.current_thread().name)


def test_a_close_from_another_thread_wakes_a_consumer_waiting_on_dropped_work():
    seq = keys(20)
    gate = threading.Event()
    fake = FakeFetcher(gate=gate, gate_keys={seq[1]})
    pf = TilePrefetcher(fake, seq, threads=1, window=8)
    run_bounded(lambda: pf(*seq[0]))
    # The one pool thread is stuck on seq[1]; seq[2] is queued behind it.
    assert wait_until(lambda: fake.active == 1)
    got = {}
    waiter = threading.Thread(target=lambda: got.update(v=pf(*seq[2])), daemon=True)
    waiter.start()
    time.sleep(0.05)
    assert waiter.is_alive()
    pf.close()
    # Woken by close() while seq[1] is still stuck, and fetched directly.
    waiter.join(5)
    assert not waiter.is_alive()
    assert got["v"] == tile_bytes(*seq[2])
    assert fake.calls[-1] == (seq[2], waiter.name)
    gate.set()
    join_all(pf.threads)
    assert not any(t.is_alive() for t in pf.threads)


def test_an_abandoned_prefetcher_leaves_no_thread_blocked():
    """Served a few calls, then dropped without close(): once the in-flight
    fetches end, its threads are gone, and it never fetched past its window."""
    seq = keys(200)
    window = 8
    fake = FakeFetcher(delay=0.01)
    pf = TilePrefetcher(fake, seq, threads=4, window=window)
    run_bounded(lambda: [pf(*k) for k in seq[:3]])
    threads = pf.threads
    assert threads
    del pf
    gc.collect()
    join_all(threads)
    assert not any(t.is_alive() for t in threads)
    assert fake.count <= 3 + window


def test_a_process_exits_at_once_when_the_consumer_raises_during_a_slow_fetch():
    script = textwrap.dedent("""
        import sys, threading, time
        from src.video.tile_prefetch import TilePrefetcher

        started = threading.Event()

        def fetcher(z, x, y):
            if x == 1:
                started.set()
                time.sleep(30)
            return b"tile"

        with TilePrefetcher(fetcher, [(1, 0, 0), (1, 1, 0), (1, 0, 1)], threads=2) as pf:
            pf(1, 0, 0)
            started.wait(10)
            print(time.time(), flush=True)
            raise RuntimeError("render failed")
    """)
    proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=25)
    ended = time.time()
    assert proc.returncode != 0
    assert "render failed" in proc.stderr
    raised_at = float(proc.stdout.strip().splitlines()[-1])
    assert ended - raised_at < 3.0

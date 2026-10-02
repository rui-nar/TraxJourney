# Trip video render time: tile fetching — Plan for #517

## Problem

A 90 s 1080p trip video takes 11–22 minutes to render on the VPS. The video
job timeout is 30 minutes (`JOB_TIMEOUT_S = 1800`). `worker-video` renders one
job at a time, so one render holds every other user's video for that long.

The per-stage profile (docs/VIDEO.md, gates G1 and G2 of #518; same trip,
variable camera, 90 s at 1080p, 2,700 frames, 2,414 tiles):

| Stage (ms per frame) | G1 | G2 (current code) |
|---|---|---|
| Tile fetch | 128.3 | 44.6 |
| Sheet stitch | 10.0 | 9.9 |
| Basemap crop, scale and blend | 79.6 | 48.6 |
| Overlay | 75.3 | 129.4 |
| Write to ffmpeg | 14.1 | 13.8 |
| **Total** | **317.4** (857 s) | **257.4** (695 s) |

Tile fetching is the stage that varies most: 50 to 143 ms per tile depending
on the network. The render waits for every tile, one after another, on the
same thread that draws the frames. The CPU stages (basemap, overlay) have
never been profiled below the stage level, so it is not yet known what in
them to make faster.

## Current state

- `src/poster/tile_stitcher.py`: `MapboxTileClient.fetch_tile` calls
  `requests.get` for every tile. There is no `Session`, so every tile opens
  a new TCP and TLS connection to `api.mapbox.com`. 5xx and network errors are
  retried 3 times with backoff 1 s, 2 s. Every 4xx, including **429**, raises
  `APIError` immediately and fails the render. `_default_tile_fetcher` builds
  the client lazily from `MAPBOX_TOKEN`. Posters use the same client.
- `render_basemap` (same file) fetches its tile range row by row
  (`y_min..y_max`, then `x_min..x_max`), decodes each tile and pastes it.
- `src/video/basemap_bands.py`: `plan_bands` plans every sheet of the video
  before frame 0 (`BandPlan.sheets`, each with `first`/`last` frame and
  `pieces`). `Sheet.tiles` counts each piece's
  `tile_range_for_bounds(bounds, band)`. `Basemaps._sheet` stitches a sheet
  at its first frame, calling `render_basemap(..., max_zoom=band)` once per
  piece. The order of tile requests over a whole render is therefore known
  in advance.
- `src/video/renderer.py`: `FrameRenderer` wraps the fetcher in
  `_TimedFetcher`, which adds the time spent in each fetch to the `fetch`
  stage. `encode` (MP4) and `_write_preview_webp` (#519 preview) drive the
  frame loop. `src/video/bench.py` runs either on a real trip and prints the
  render summary line.
- Mapbox Static Tiles API: the default rate limit is 6,000 requests per
  minute per account, and over the limit Mapbox answers 429
  (docs.mapbox.com/api/maps/static-tiles).

## Decisions

- **D1 — Reuse connections.** `MapboxTileClient` keeps one
  `requests.Session` per thread (thread-local), so tiles reuse keep-alive
  connections. One session per thread, not one shared, because `Session`
  is not documented as thread-safe. Posters get this too, for free. Sessions
  are not closed explicitly: every RQ job runs in a forked work horse that
  exits after the job, and the bench is a one-shot process.
- **D2 — Fetch ahead, in the background.** A new tile prefetcher fetches the
  render's tiles on a small thread pool, in the order the render will ask
  for them, while the main thread draws frames. The render then waits only
  for a tile that isn't fetched yet. This rules out two alternatives:
  - **Fetching each sheet's tiles in parallel at stitch time.** It is
    simpler, but the render still stops at every new sheet for its slowest
    tile.
  - **Asyncio or a separate fetch process.** Threads suffice for network
    waits: `requests` releases the GIL while it waits on the socket.
- **D3 — The order comes from the plan.** `Sheet.requests` lists a sheet's
  `(z, x, y)` requests in exactly the order `render_basemap` makes them:
  pieces in order, then rows, then columns, at zoom `band`. The prefetcher
  walks the sheets in first-use order (frames 0..N, each frame's sheet
  refs in `plan.frames[n]` order, each sheet once) and queues their requests.
- **D4 — Matching, not trusting.** The prefetcher's fetcher callable looks
  up a pending result for `(z, x, y)`; results for one key queue in request
  order. When a request has no pending result (a *miss*), it is fetched
  synchronously, exactly as today. A wrong order therefore costs speed,
  never correctness. Misses are counted and logged.
- **D5 — Same tiles, same count, same frames.** The prefetcher requests the
  same multiset of tiles as today: no deduplication across sheets and no
  cache (owner decision: no tile cache in #517). The Mapbox cost per render
  is unchanged, and frames are bit-identical with prefetching on or off.
- **D6 — Bounded.** At most `PREFETCH_THREADS = 4` concurrent fetches. At
  most `PREFETCH_WINDOW = 32` tiles fetched or in flight but not yet consumed,
  about two sheets (G2 averaged 16 tiles per sheet) and at most about 32 MB of
  PNG bytes. At ~50 ms per tile, 4 threads top out near 80 requests per
  second, under Mapbox's 100 per second. That can only happen in bursts
  while the window fills: a render consumes about 4 tiles per second on
  average. `PREFETCH_THREADS = 0` disables prefetching, for tests that need
  the sequential path.

  **Raised to 8 threads during delivery** (owner, 2026-10-02, after #525's
  gate). Once #525 made the render faster, a slow-network run (271 ms per
  tile) waited 10.3 ms per frame for tiles with 4 threads. 8 threads cover
  about 30 tiles per second at that speed. The 32-tile window is unchanged,
  so memory and the size of a burst are too. A burst can briefly exceed 100
  requests per second, but Mapbox's limit is per minute (6,000), and bursts
  are bounded by the window.
- **D7 — Errors surface where they do today.** A failed prefetch stores its
  exception, which is raised when the render asks for that tile, so the
  render fails at the same point with the same exception. When the frame
  loop ends, for any reason, the prefetcher is closed: queued fetches are
  dropped, a stop event tells the pool threads to exit, and in-flight
  requests are not waited for. The pool is the prefetcher's own few
  **daemon threads**, not `concurrent.futures.ThreadPoolExecutor`, whose
  atexit hook joins its threads. A process therefore exits at once after a
  failed render, whatever a fetch thread is doing, including the bench
  (owner, review round 1, envelope question 2). A thread still inside a
  request or a retry sleep when the prefetcher closes finishes that fetch
  in the background and then exits; its result is discarded. The
  prefetcher
  starts on the first `frame()` call, so building a `FrameRenderer` starts no
  threads. A render that needs no tile still never needs `MAPBOX_TOKEN`.
  **No thread ever waits on the window** (review R1-1): there is no producer
  thread. The consumer's own call tops up the submissions, up to the window,
  each time it takes a result. A renderer that draws a few frames and is then
  dropped without `close()` (many tests and the bench's `--dump-frames`)
  leaves at most `window` fetches to finish, then idle threads. A
  `weakref.finalize` on `FrameRenderer` closes the prefetcher when the
  renderer is collected, which ends its threads. It is registered when the
  prefetcher is created, with the prefetcher's own bound `close` as its
  callback, so it holds no reference to the renderer (review R2-2). A closed
  prefetcher is never restarted, and calls after `close()` fetch
  synchronously.
- **D8 — 429 is retried.** `MapboxTileClient` treats 429 like a transient
  error, with its own budget rather than `MAX_RETRIES` (review R2-3). Mapbox
  documents
  `X-Rate-Limit-Reset` (a Unix timestamp) on rate-limited responses, not
  `Retry-After` (review R1-2), so the wait is, in order of preference:
  1. `X-Rate-Limit-Reset` minus now;
  2. `Retry-After` in seconds;
  3. the existing `2 ** attempt` backoff.

  A reset-based wait adds 1 s of margin past the reset. Each wait is
  clamped to [1, 30] s. A missing, malformed or past header counts as absent,
  so parsing never raises anything but `APIError` out of the client.

  A tile keeps retrying 429s until its total wait would pass
  `MAX_RATE_LIMIT_WAIT_S = 65`, so a window used up at its start (reset
  ~60 s ahead) still ends with an attempt after the reset (review R2-3).
  5xx and network errors keep `MAX_RETRIES`.

  **A deadline bounds every wait** (review R2-1). The client takes an
  optional `deadline` (a `time.monotonic()` instant). A wait that would end
  past it is not taken: the client raises `APIError` at once. A request's
  own timeout is also cut to what is left before the deadline. When less
  than `MIN_REQUEST_S = 0.5` is left, the deadline counts as passed and the
  client raises `APIError` without sending a request, so it never passes
  `requests` a timeout ≤ 0 (review R3-2).
  `render_basemap` passes its `deadline` to the default client it builds.
  The synchronous poster preview (10 s budget, `poster_renderer.py`)
  therefore still falls back to its grey map within budget instead of
  sleeping past the app's ~20 s timeout. Video renders and full posters pass
  no deadline and get the full 65 s.

  Today a single 429 fails the render, and parallel fetching makes one more
  likely.
- **D9 — What "fetch" means in the summary.** The `fetch` stage stays "time
  the frame loop spends waiting for tiles". With prefetching that is
  blocked time, not network time. The summary line gains two fields:
  `tile_ms`, the mean network time per tile measured inside the pool (so a
  slow network is still visible), and `prefetch_misses`. `tile_ms` is a
  diagnostic only: gate G1 decides on `fetch_ms` and `prefetch_misses`. The
  pool thread stores each fetch's network time with its result. The
  counters (`fetched`, `net_seconds`, `misses`) are updated only on the
  consumer's thread, when it takes a result or makes a miss, so they need no
  lock and can't race (review R2-4, which supersedes the warning added for
  R1-3). The R1-3 guard keeps its test: after an in-order run with 4
  threads, `fetched` equals the fake fetcher's thread-safe call count.
- **D10 — CPU stages: profile now, change later** (owner decision). The bench
  gains `--profile FILE`, which runs the frame loop under `cProfile`, writes
  the stats and prints the top 30 functions by own time. The owner runs it
  on the VPS at gate G1. The findings become a follow-up issue. No change to
  the overlay, basemap or encoder in #517.
- **D11 — The timeout and the worker layout are unchanged.** `JOB_TIMEOUT_S`
  stays at 1800 s: after this change, a 90 s 1080p render is expected at
  roughly 600–700 s. The worker layout belongs to #520.

## Review envelope

REVIEW.md §2 defaults apply, plus:

- **Concurrency inside a render:** up to 4 fetch threads beside the frame
  loop, in one process (the RQ work horse, or the bench). Shared state is the
  prefetcher's pending-results table and window. Required properties: no
  deadlock when the render asks for tiles out of order or stops early, no
  thread still fetching after the prefetcher is closed (beyond the in-flight
  requests), no fetch thread delaying process exit (daemon threads, D7),
  and a stored exception never lost.
- **External service:** Mapbox's per-account rate limit (6,000 per minute).
  The render shares it with the prod and val stacks, with posters (same
  client), and with users browsing the interactive satellite map in the
  Flutter clients, which draws the same style on the same account (owner,
  review round 1). The render's own peak is bounded (D6) and a 429 is waited
  out (D8). Nothing coordinates across processes or with clients, so a
  render must survive the budget being used up by others for up to a minute.
- **RQ timeout:** the work horse is killed, or the frame loop raises
  `JobTimeoutException`. Either way, no thread may keep the horse alive.
- **Scale:** at most `MAX_TILES = 3000` tiles per render, as today.

## Boundaries crossed

None. No API, schema, stored data or export format changes. The render
summary log line gains two fields (`tile_ms`, `prefetch_misses`). Only
humans read it, and docs/VIDEO.md documents it.

## Conventions

- Frames are bit-identical with and without prefetching, for every camera
  mode. Existing goldens don't change.
- Test fakes that count or record tile requests must be safe when called
  from several threads, or the test sets `PREFETCH_THREADS = 0`.
- No new dependency.

## Open decisions

- **CPU follow-up scope:** decided from the G1 profile, after this plan is
  delivered. It does not block any unit.

## Execution units

### Wave 1 — tile client, prefetcher, bench profile (disjoint)

**U1 — Tile client: connection reuse and 429 retry**
- **Goal:** `MapboxTileClient` reuses connections per thread and retries 429.
- **Scope:** `src/poster/tile_stitcher.py` (`MapboxTileClient`,
  `_default_tile_fetcher`, and passing `deadline` to the default client in
  `render_basemap`); `tests/test_tile_stitcher.py`.
- **Context:** D1, D8. The existing client tests
  (`test_mapbox_tile_client_*`) are the example. They monkeypatch
  `requests.get` and must move to patching the session.
- **Do:**
  1. Give the client a thread-local `requests.Session`, created on first
     use in each thread, and fetch through it.
  2. Treat 429 as retryable, as D8 says: choose each wait as
     `X-Rate-Limit-Reset` minus now plus 1 s, then `Retry-After`, then
     `2 ** attempt`, and clamp it to [1, 30] s. Keep retrying 429s while the
     total wait stays within `MAX_RATE_LIMIT_WAIT_S = 65`. A missing,
     malformed or past header counts as absent (reviews R1-2, R2-3).
  3. Add an optional `deadline` to the client and to `_default_tile_fetcher`.
     A wait that would end past it raises `APIError` at once, and a
     request's timeout is cut to the time left. When less than
     `MIN_REQUEST_S = 0.5` is left, raise `APIError` without sending
     (review R3-2). `render_basemap` passes its
     own `deadline` when it builds the default client (review R2-1).
  4. Leave every other behaviour unchanged: 5xx and network retries, other
     4xx raising at once, error messages and the URL.
- **Acceptance:** `pytest tests/test_tile_stitcher.py tests/test_poster*.py`
  passes. New tests:
  - two fetches on one thread use one session object; two threads use two;
  - a 429 with `X-Rate-Limit-Reset` 10 s ahead, then a 200, returns the
    tile after sleeping 11 s (with the clock faked);
  - a reset 120 s ahead sleeps 30;
  - a 429 with the reset 60 s ahead every time until the reset passes, then
    a 200: the tile is returned, and the total sleep is ≤ 65 s (R2-3);
  - with a `deadline` 10 s ahead, a 429 whose reset is 60 s ahead raises
    `APIError` without sleeping. `render_basemap` with a deadline and no
    injected fetcher hands that deadline to the client (R2-1);
  - with 0.2 s left before the deadline, or the deadline already past, the
    client raises `APIError` and makes no request (R3-2);
  - a 429 with `Retry-After: 5` only sleeps 5;
  - a 429 with a malformed or past reset falls back to the backoff and
    raises only `APIError`;
  - persistent 429 raises `APIError` once the 65 s budget is spent;
  - a 404 still raises at once without sleeping.
- **Out of scope:** prefetching, caching, the poster renderer.
- **Latitude:** local design
- **Escalate if:** X3; a poster test depends on `requests.get` being called
  directly.
- **Depends on:** —

**U2a — Tile prefetcher and `Sheet.requests`**
- **Goal:** a thread-pool prefetcher that serves a known sequence of tile
  requests ahead of time, and the sequence for a band plan.
- **Scope:** `src/video/basemap_bands.py` (`Sheet.requests`,
  `plan_requests(plan)`); new `src/video/tile_prefetch.py`; new
  `tests/test_video_tile_prefetch.py`.
- **Context:** D2–D7, D9. `Sheet.tiles` and `Basemaps._sheet`/`frame` in
  basemap_bands.py, and the fetch loop of `render_basemap` in
  `src/poster/tile_stitcher.py`: `Sheet.requests` must reproduce its
  zoom choice (`zoom_for_target_size(..., max_zoom=band)`, which yields
  `band`) and its loop order exactly. `src/jobs/upstream_slots.py` is the
  repo's example of a small concurrency helper with focused tests.
- **Do:**
  1. `Sheet.requests -> List[Tuple[int, int, int]]`, in `render_basemap`'s
     order for each piece in turn. Assert `len(sheet.requests) ==
     sheet.tiles`.
  2. `plan_requests(plan)`: the requests of every sheet in first-use order
     (D3).
  3. `TilePrefetcher(fetcher, requests, threads=PREFETCH_THREADS,
     window=PREFETCH_WINDOW)` is callable as a `TileFetcher`. It has
     `close()`, is a context manager, and exposes `misses`, `fetched` and
     `net_seconds`.
     - There is no producer thread, and no thread ever waits on the
       window (review R1-1). The first call submits up to `window`
       requests. Each time a call takes a pending result, it submits more,
       up to `window` submitted but unconsumed.
     - A call with a pending result for its key waits for it and returns its
       bytes, or raises its stored exception. Any other call is a miss: it is
       fetched synchronously on the calling thread and counted.
     - `threads == 0` makes every call a direct, synchronous fetch.
     - The pool is `threads` daemon threads taking work from a queue, with
       a stop event (D7), not `ThreadPoolExecutor`. Closing it drops queued
       work, sets the event and doesn't wait for in-flight requests.
       Calling it after `close()` fetches synchronously.
     - Results that are never consumed (out-of-order renders) must not keep
       the window full forever. When a miss shows the render has moved past
       them, discard them (local design: say how).
     - Each pool fetch returns its network time with its bytes. `fetched`,
       `net_seconds` and `misses` are updated only on the consumer's thread
       (D9, review R2-4).
     - Name the pool threads with a common prefix (`tile-prefetch-`), and
       expose the prefetcher's own threads (e.g. a `threads` property), so a
       test can check a given prefetcher's threads. Other tests in the same
       process may leave idle threads of their own (review R3-1).
- **Acceptance:** `pytest tests/test_video_tile_prefetch.py
  tests/test_video_renderer.py tests/test_video_camera.py` passes. New tests:
  - **plan order:** for the renderer's synthetic trip in every camera mode,
    and for a sheet split across the antimeridian, `plan_requests` equals
    the exact request sequence a sequential `Basemaps` render makes, recorded
    with a fake fetcher;
  - **concurrency and window:** with a slow fake fetcher, at most `threads`
    calls run at once, and at most `window` results are ever unconsumed;
  - **serving:** in-order consumption makes zero misses and returns the
    right bytes per key, including a key requested twice;
  - **misses:** an unknown key is a miss and still returns the right bytes;
    an out-of-order consumer neither deadlocks nor stops prefetching
    (bounded test timeout);
  - **abandoned:** a prefetcher that served a few calls and is then dropped
    without `close()` has every pool thread idle or gone once its last
    in-flight fetch ends: no thread is blocked on the window;
  - **counters (R1-3 guard):** after an in-order run with 4 threads,
    `fetched` equals the fake fetcher's thread-safe call count;
  - **errors:** a failing fetch raises the same exception, once, at the
    consumer's call for that key;
  - **close:** after `close()`, no new fetch starts. A test proves this by
    counting fake calls after close, with in-flight calls allowed to finish;
  - **exit:** a subprocess whose consumer raises while a fake fetch sleeps
    for 30 s exits within 3 s;
- **Out of scope:** wiring into `FrameRenderer`, logging, docs.
- **Latitude:** local design
- **Escalate if:** X3; `Sheet.requests` can't be made to match
  `render_basemap`'s order without changing `render_basemap`.
- **Depends on:** —

**U3 — Bench `--profile`**
- **Goal:** the bench can profile the frame loop's CPU time on the VPS.
- **Scope:** `src/video/bench.py`; `tests/test_video_bench.py`;
  docs/VIDEO.md ("Profiling on the server" section only).
- **Context:** D10. The existing bench flags (`--dump-frames`,
  `--preview`) and their tests are the example.
- **Do:**
  1. Add `--profile FILE`, which runs the render (MP4 or `--preview`) under
     `cProfile.Profile` and dumps the stats to *FILE*.
  2. Print the top 30 functions by `tottime` after the summary line.
  3. Document it, including that profiled timings are inflated and the
     gate's ms-per-frame figures come from a run without `--profile`.
- **Acceptance:** `pytest tests/test_video_bench.py` passes (Linux image,
  CI=1). New tests: `--profile` writes a file that `pstats.Stats` loads and
  that names a function from `src/video/overlay.py`; without the flag, no
  profiler is created.
- **Out of scope:** any change to what is profiled; renderer code.
- **Latitude:** local design
- **Escalate if:** X3.
- **Depends on:** —

### Wave 2 — wiring

**U2b — Prefetch in every render**
- **Goal:** the video render, the preview and the bench fetch tiles
  through the prefetcher, and the summary reports it.
- **Scope:** `src/video/renderer.py`; `tests/test_video_renderer.py`,
  `tests/test_video_preview_render.py`, `tests/test_video_bench.py` (only
  where a fake fetcher needs to become thread-safe); docs/VIDEO.md ("Stage
  timings" and "Basemap" sections).
- **Context:** D7, D9, Conventions. `_TimedFetcher`, `FrameRenderer`, `encode`,
  `_write_preview_webp` and `_log_render_summary`; the U2a prefetcher.
- **Do:**
  1. `FrameRenderer` builds a `TilePrefetcher` over the raw fetcher. The raw
     fetcher is the injected one, or the default client built lazily on the
     first fetch, so no token is needed when no tile is.
  2. Chain the fetchers as Basemaps → `_TimedFetcher` → prefetcher, so
     `fetch` is the frame loop's wait (D9).
  3. Start the prefetcher on the first `frame()` call. Add
     `FrameRenderer.close()`, which is idempotent and called in a `finally`
     by `encode` and `_write_preview_webp`. When the prefetcher is created,
     register `weakref.finalize(self, prefetcher.close)`. The callback is
     the prefetcher's own bound method, never one that refers to `self`
     (reviews R1-1, R2-2). A closed prefetcher is never restarted.
  4. Add `tile_ms` and `prefetch_misses` to the summary line for both kinds.
  5. Update docs/VIDEO.md.
- **Acceptance:** in the Linux image (CI=1), `pytest tests/test_video_*.py`
  passes, with existing goldens unchanged. New tests:
  - **frames:** frames are bit-identical with `PREFETCH_THREADS` at 4 and at
    0, for a full render and for a preview, in every camera mode;
  - **tiles:** the tile request multiset equals the sequential one, and
    `prefetch_misses == 0` on the synthetic trip;
  - **no token:** a render whose plan has no tiles runs without
    `MAPBOX_TOKEN`;
  - **overlap:** with a fake fetcher that sleeps 20 ms per tile, the `fetch`
    stage is under 25% of the summed sleep;
  - **failure cleanup:** after a failing fetch, the render raises the
    fetcher's exception, and none of **its own** prefetcher's threads is
    still alive afterwards (bounded wait). The check uses the prefetcher's
    `threads`, or the `tile-prefetch-` threads that are new since a
    `threading.enumerate()` snapshot taken before the renderer was built.
    Never check every thread in the process (R3-1);
  - **dropped renderer:** a `FrameRenderer` draws a few frames directly,
    with no `encode`. After `del` and `gc.collect()`, a weak reference to it
    is dead, and none of the `tile-prefetch-` threads that are new since a
    snapshot taken before the renderer was built is alive (bounded wait).
    Shown to fail with a finalizer that closes over the renderer (R2-2,
    R3-1);
  - **summary:** the line carries `tile_ms=` and `prefetch_misses=`.
- **Out of scope:** CPU stages, the poster renderer, caching, timeouts.
- **Latitude:** local design
- **Escalate if:** X3; any golden changes; prefetching makes a memory test
  (`tests/test_video_memory.py`) exceed its bound.
- **Depends on:** U1 (thread-safe default client), U2a.

**Gate G1 (owner, after wave 2, before the PR is merged):** push the branch
to `validation`. On val, run:

```
docker compose run --rm worker-video python -m src.video.bench --project "<G2 trip>" --owner <uid> --length 90 --height 1080
```

Run it once without `--profile`, then once with `--profile /tmp/p.prof`,
and paste both outputs. To pass, `fetch_ms` must be ≤ 10 ms per frame and
`prefetch_misses` 0 in the unprofiled run. The orchestrator records the run
in docs/VIDEO.md beside G1 and G2 of #518, and files the CPU follow-up issue
from the profile.

## Definition of done

- On the VPS bench (90 s, 1080p, variable camera, the G2 trip),
  `fetch_ms` is ≤ 10 ms per frame and `prefetch_misses` is 0.
- The tile count per render is unchanged, and frames are bit-identical with
  and without prefetching; existing goldens pass unchanged.
- A Mapbox 429, including one that lasts until the end of the current minute,
  no longer fails a render on its own.
- The poster preview still falls back to its grey map within its 10 s
  budget when Mapbox answers 429.
- A failed or timed-out render leaves no prefetch thread fetching.
- The render summary reports `tile_ms` and `prefetch_misses`;
  docs/VIDEO.md documents them and records the gate run.
- `python -m src.video.bench --profile` works, and a follow-up issue for the
  CPU stages exists, based on the VPS profile.

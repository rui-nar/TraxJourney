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
- **D7 — Errors surface where they do today.** A failed prefetch stores its
  exception, which is raised when the render asks for that tile, so the
  render fails at the same point with the same exception. When the frame
  loop ends, for any reason, the prefetcher is closed: queued fetches are
  cancelled and the pool is shut down without waiting for in-flight
  requests (`shutdown(wait=False, cancel_futures=True)`). The prefetcher
  starts on the first `frame()` call, so building a `FrameRenderer` starts no
  threads. A render that needs no tile still never needs `MAPBOX_TOKEN`.
- **D8 — 429 is retried.** `MapboxTileClient` treats 429 like a transient
  error: it waits `Retry-After` seconds when the header is present (capped
  at 30 s), otherwise the existing backoff, within the same `MAX_RETRIES`.
  Today a single 429 fails the render, and parallel fetching makes one more
  likely.
- **D9 — What "fetch" means in the summary.** The `fetch` stage stays "time
  the frame loop spends waiting for tiles". With prefetching that is
  blocked time, not network time. The summary line gains two fields:
  `tile_ms`, the mean network time per tile measured inside the pool (so a
  slow network is still visible), and `prefetch_misses`.
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
  requests), and a stored exception never lost.
- **External service:** Mapbox's per-account rate limit (6,000 per minute).
  The prod and val stacks may share an account, and posters fetch through the
  same client. The design bounds concurrency (D6) and retries 429 (D8); it
  does not coordinate across processes.
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
- **Scope:** `src/poster/tile_stitcher.py` (`MapboxTileClient` only);
  `tests/test_tile_stitcher.py`.
- **Context:** D1, D8. The existing client tests
  (`test_mapbox_tile_client_*`) are the example. They monkeypatch
  `requests.get` and must move to patching the session.
- **Do:**
  1. Give the client a thread-local `requests.Session`, created on first
     use in each thread, and fetch through it.
  2. Treat 429 as retryable within `MAX_RETRIES`. Sleep for `Retry-After`
     (integer seconds) capped at 30 s when the header is present, otherwise
     `2 ** attempt`.
  3. Leave every other behaviour unchanged: 5xx and network retries, other
     4xx raising at once, error messages and the URL.
- **Acceptance:** `pytest tests/test_tile_stitcher.py tests/test_poster*.py`
  passes. New tests:
  - two fetches on one thread use one session object; two threads use two;
  - a 429 followed by 200 returns the tile and slept `Retry-After`;
  - a 429 with `Retry-After: 120` sleeps 30;
  - persistent 429 raises `APIError` after `MAX_RETRIES`;
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
     - A producer keeps at most `window` requests submitted but not yet
       consumed. Consuming a pending result frees a slot.
     - A call with a pending result for its key waits for it and returns its
       bytes, or raises its stored exception. Any other call is a miss: it is
       fetched synchronously on the calling thread and counted.
     - `threads == 0` makes every call a direct, synchronous fetch.
     - Closing it cancels queued work and doesn't wait for in-flight
       requests. Calling it after `close()` fetches synchronously.
     - Results that are never consumed (out-of-order or stopped renders)
       must not stall the producer forever. When a miss shows the render has
       moved past them, discard them (local design: say how).
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
    an out-of-order and a stopped-early consumer neither deadlock nor
    stall the producer (bounded test timeout);
  - **errors:** a failing fetch raises the same exception, once, at the
    consumer's call for that key;
  - **close:** after `close()`, no new fetch starts. A test proves this by
    counting fake calls after close, with in-flight calls allowed to finish.
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
     by `encode` and `_write_preview_webp`.
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
    fetcher's exception and no prefetch thread is still alive afterwards
    (`threading.enumerate()`, bounded wait);
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
- A 429 from Mapbox no longer fails a render on its own.
- A failed or timed-out render leaves no prefetch thread fetching.
- The render summary reports `tile_ms` and `prefetch_misses`;
  docs/VIDEO.md documents them and records the gate run.
- `python -m src.video.bench --profile` works, and a follow-up issue for the
  CPU stages exists, based on the VPS profile.

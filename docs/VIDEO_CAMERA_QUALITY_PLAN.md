# Trip video camera options and sharper overlay — Plan for #518

## Problem

The first trip videos rendered on the server (after #501) show two things users
want changed:

1. **One camera for every trip.** Today's camera follows the marker and zooms
   per transport mode (close on foot, far out for flights). That reads well for
   a short trip, but a long one "pumps" in and out, and there is no way to see
   the whole trip at once while it plays. The owner wants two more camera
   options, each selectable on its own:
   - **Overview** — a still map fitted to the whole trip; only the marker and
     the travelled line move.
   - **Fixed zoom** — the camera still follows the marker, at one zoom for the
     whole video, chosen by the server.
2. **Pixelated track line and transport chips.** The owner reports the route
   line and the mode icons (the marker and the icons in the counter panels) look
   pixelated in the MP4.

Render time is the constraint. A 90 s 1080p video took 21 min 53 s on the VPS
(about 486 ms per frame, #517), against a 62 ms per frame benchmark on a 10-core
development machine and a 30-minute job timeout. Better anti-aliasing costs
time, so this plan measures on the VPS before it chooses how much sharpening to
buy (owner decision, 2026-09-29). Making rendering faster in general stays in
#517.

## Current state

- **Camera** — `src/video/camera.py`: `camera_path(timeline, fps, size)` →
  `list[Shot(lon, lat, zoom, flying)]`, `@lru_cache(maxsize=4)` on
  `(Timeline, fps, size)`. Cards use `overview()` (whole trip at
  `OVERVIEW_FILL = 0.85`); clips aim at the marker at the clip's
  `CLIP_FILL = 0.6` fit, raised to `MODE_MIN_ZOOM` (hike/run 12, ride/other 10,
  train/bus/boat 6, flight 0), smoothed by `_zoom_plan`, a critically damped
  spring and a leash, with `_fly` (van Wijk & Nuij) before each cut. Its only
  production caller is `src/video/renderer.py:59`.
- **Basemap** — `src/video/basemap_bands.py`: bands per `floor(zoom)`, sheets =
  union of viewports (`SHEET_MAX_FRAMES = 12`, `GAP_FRAMES = 15`),
  `MAX_TILES = 3000` enforced by lowering the top band (coarser, upscaled
  tiles), `PIXEL_RATIO = 2`. `Basemaps.frame(n)` crops and scales a sheet on
  every frame, even when the shot doesn't change.
- **Overlay** — `src/video/overlay.py`:
  - `_draw_route` (≈300-333) uses plain `ImageDraw.line` on a colour layer and
    an L mask over the route's bounding box, then `BoxBlur(max(0.5, h/1080))`.
    That is 0.67 px at 720p, so there is almost no anti-aliasing and no
    supersampling. Widths are `line_w = h/160`, `casing_w = h/540` and
    `faint_w = h/270`.
  - `_marker` is drawn at 4× and LANCZOS'd down, but the icon is pasted at 1×.
  - `_icon` loads 128 px Noto Color Emoji PNGs from `assets/video/icons/` and
    resizes them with LANCZOS. HUD panels (`_panel`) use rounded rectangles
    that aren't supersampled.
  - The route, marker and HUD are redrawn every frame.
- **Poster precedent** — `src/poster/poster_renderer.py:490-533`
  `_draw_route(..., supersample)`: draws at `ss`× on an RGBA layer, then
  `resize(BOX)` and `alpha_composite`. `_ROUTE_SUPERSAMPLE = 4` carries a memory
  caveat at 105-110.
- **Encoder** — `renderer.py:41-42, 93-95`: `libx264 -preset veryfast -crf 23
  -threads 4 -pix_fmt yuv420p -movflags +faststart`, no `-tune`.
- **Request contract** — `api/video.py`:
  - `VideoPlanRequest(length_s=60, height=None, decrypted_geometry)` and
    `VideoRequest(length_s, height=720, decrypted_geometry)`.
  - Stored as `request_json = {"length_s", "height", "width"}` (≈419-427) and
    handed to `render_video(request=...)`, which reads those keys.
  - `tests/test_video_api.py:180` pins that exact dict.
- **Client** — `flutter_client/lib/src/api/video_api.dart`
  `createVideoJob(lengthS, height)`; `video_job_notifier.dart` state `lengthS`,
  `height`, `setLength`/`setHeight`; `video_config_dialog.dart` Length and
  Resolution `SegmentedButton`s (≈211-250).
- **App icons** — the Flutter client already uses Material icons for modes
  (`Icons.directions_bike`, `train_rounded`, `hiking_rounded`, `flight_rounded`,
  …; `activity_panel.dart:120`, `welcome_screen.dart`).
- **Tests pinned by the change:**
  - `tests/test_video_camera.py`, whose tests all call
    `camera_path(tl, FPS, size)` with three arguments.
  - `tests/test_video_renderer.py` golden frames (`title`, `follow`, `end` at
    320×180; tolerance mean ≤ 2.0 and ≤ 1% of pixels off by > 48; regenerate
    with `VIDEO_UPDATE_GOLDEN=1` in the Linux image), plus the manual benchmark
    (`VIDEO_BENCH=1`: ≤ 150 ms per frame, ≤ 896 MB).
- **Libraries** — Pillow only; no aggdraw, Cairo or Skia in `requirements.txt`.

## Decisions

| # | Decision | Reason | Rules out |
|---|---|---|---|
| D1 | Four camera values, `camera: "variable" \| "overview" \| "fixed" \| "fixed_strict"`, default `"variable"` (today's camera, unchanged frame for frame). `fixed` and `fixed_strict` are the two fixed-zoom behaviours of D3; the dialog shows them as "Fixed zoom" plus a "Zoom out for flights and long legs" switch (on = `fixed`). | Owner decisions (2026-09-29): both new options, separately selectable; for fixed zoom, both approach A and approach B, selectable (review R1-1). One enum field keeps the contract, storage and tests simple. | Replacing today's camera; one merged "constant" option; a second request field that only matters with one value of the first. |
| D2 | **Overview:** every frame is the whole-trip `overview()` shot, `flying` always false; the marker, travelled line, HUD and cards animate as today. | That's what "see the whole trip while it plays" means; a still camera needs one basemap sheet. | Slow pans or zooms in overview mode. |
| D3 | **Fixed zoom**, zoom **chosen by the server** (owner decision). The camera follows the marker with variable mode's machinery (spring, leash, `_fly` across jumps and to or from the cards) at one **integer** zoom `Z` (floored, so no frame sits in a band cross-fade, review R1-5). Per-mode floors don't apply. A followed sub-leg is **fast** at zoom `z` if following it would move the aim faster than `FIXED_MAX_PAN_PER_S = 1.5` frames per second, measured per axis as the larger of the speed in frame widths and in frame heights, like `_off_frame` (0.05 per frame at 30 fps; on 16:9 the vertical axis is the binding one; review R3-1). That is what the critically damped spring can track without the leash taking over: its lag is 2v/ω, and `LEASH·ω/2 = 1.5` per second with `LEASH = 0.3`, `SPRING_OMEGA = 10` (review R2-1). It is also well inside the follow bound of `test_consecutive_frames_change_little` (0.1 per frame while following, 0.25 for any pair of frames). **`Z` is chosen by one fixed procedure, from the top down** (review R2-2). For each integer `z` from 14 down to the mode's floor, recompute at that `z` which followed sub-legs are fast, and accept the first (highest) `z` that meets every condition of its mode. **`fixed` (approach A, pull out for fast legs):** (1) `z` ≤ the floor of the median `CLIP_FILL` fit zoom over the sub-legs **not** fast at `z`; (2) the path's estimated tiles at `z` ≤ `MAX_TILES`, with the fast legs framed as in variable mode (clip fit zoom, flown to and from). The floor is 4. Every non-card frame that isn't on a fast leg is at `Z`. **`fixed_strict` (approach B, one zoom throughout):** (1) `z` ≤ the floor of the median `CLIP_FILL` fit zoom over **all** followed sub-legs; (2) no followed sub-leg is fast at `z`; (3) estimated tiles at `z` ≤ `MAX_TILES`. The floor is 2, raised so that no frame is wider than 180° of longitude: `max(2, ceil(log2(frame_width_px / 256)))` (3 at 1920 px, 2 at 320 px; delivery escalation of U2, owner decision 2026-09-29). Every non-card frame is at `Z`. If no `z` qualifies, `Z` is the floor. | Automatic keeps the dialog simple. The median suits the typical leg. Floors and the pan bound keep the map from strobing, and the tile bound gives a sharp map at a lower zoom rather than a blurry capped one. Both approaches are owner-requested. | A user zoom slider or presets (not wanted); keeping mode floors (they would reintroduce zoom changes); following a leg faster than the spring can track (strobing, R1-1; leash-pinned marker, R2-1); an unspecified search order (R2-2). |
| D4 | **Measure first, and again after.** Wave 1 adds per-stage render timings and a benchmark and frame-dump CLI. It takes `--camera` (review R1-3) and `--crf`/`--tune` (review R1-7), and runs in its own container via `docker compose run --rm worker-video …`, never `exec` in the live worker (review R1-6). **Gate G1** (before wave 2): the owner runs it **once** on the VPS, in variable mode with today's encoder settings, for timing only. The agents can't reach the VPS. Encoder candidates are compared on the dev box instead, because frames are the same on any machine (Convention 2; review R2-3). **Gate G2** (after wave 2, before wave 3): the owner runs it on the new code in variable and overview mode, two runs (review R1-4). The numbers go into `docs/VIDEO.md` and #517. | Owner decision: sharpening costs time, and the dev-box benchmark was 8× off. G2 is what checks the definition of done's "stays under the job timeout on the VPS". | Choosing a supersample factor blind; certifying VPS render time from dev-box numbers. |
| D5 | **Route line:** draw the route layer supersampled (factor `ROUTE_SS`, default 3, allowed 2–4) over the route's bounding box only, then `resize(BOX)`, following the poster's `_draw_route`. Replace the box blur. The factor is set from G1's numbers within the render-time budget (D8). | Proven in the repo (poster), and costs pixels only where the route is. | New native dependencies (aggdraw, Cairo, Skia) at runtime; a full-frame supersampled layer (≈132 MB at 4× 1080p). |
| D6 | **Chips:** replace the 128 px emoji bitmaps with **vector-sourced icons matching the app's Material icons** (Material Symbols, Apache-2.0), pre-rendered offline to 512 px PNGs by a dev-only script run in Docker, with the PNGs committed. At runtime the icon is rendered once per size (cached) from the 512 px source into the 4× marker sprite before it's scaled down. HUD rounded panels are drawn at 4× the same way. | A bitmap emoji font can't be rendered larger than about 136 px. Vector sources scale cleanly, and matching the app's icons keeps the video and app consistent. Keeping the runtime Pillow-only means no Cairo in the image. | Runtime SVG rasterising (Cairo in the image); keeping the emoji. |
| D7 | **Encoder:** candidates `-crf` 18–21, with and without `-tune animation`, keeping `yuv420p` (plays on every phone), `veryfast`, `-threads 4`. The orchestrator runs the bench **on the dev box** (Linux image, real tiles, the same trip as G1) with `--dump-frames` for each candidate (reviews R1-7, R2-3), and picks the one whose decoded frames keep both the route line and the map labels sharpest (the tune is dropped if it softens the map imagery). Files may grow about 1.5–2× (owner accepted a moderate increase). | Encoding softens thin coloured lines; animation tuning suits flat colours but can soften photographic map texture, so it's measured, not assumed. | `yuv444p` (poor phone support); `-crf ≤ 16` (files too large). |
| D8 | **Render-time budget** (owner confirmed 2026-09-29): the overlay and encoder changes together must not add more than **25%** to the per-frame time, measured on the benchmark in **both variable and overview mode**. Overview (and the cards) are the worst case for route drawing, because the route box covers ~85% of the frame (review R1-3). The budget applies on the dev box, and on the VPS at G2 against G1. **Overview has no "before" of its own** (it can't render before U3), so its baseline is the **variable-mode per-frame time before the change**: the dev-box benchmark from wave 1 and G1 on the VPS. Overview after the change must stay ≤ 1.25 × that (review R2-4). The 1080p benchmark must also stay within its existing assertions. If D5, D6 and D7 at their defaults exceed it, the unit lowers `ROUTE_SS` before anything else. | Keeps #518 from making #517 worse. | Unbounded quality work; a budget checked only in the mode where it's cheapest. |
| D9 | **Overview is also cheaper:** a still camera reuses the cropped and scaled basemap image instead of recomputing it every frame, and hands the overlay a **copy** each frame (the overlay draws in place; review R1-2). | Free speed for the new mode; `Basemaps.frame` recomputes today. | Handing the cached image itself to the overlay (frames would accumulate). |

## Review envelope

`docs/REVIEW.md` defaults apply, plus the trip video envelope in
`docs/TRIP_VIDEO_PLAN.md`: the E6 exception for per-job consent geometry,
`ffmpeg` as a local binary, Mapbox as the existing basemap provider, and one
`video` worker running one job at a time. This change adds no trust boundary
and no external service. New points:

- **Render time and tile cost are in scope.** A change that pushes a
  90 s 1080p render past the 1,800 s job timeout on the VPS (per G1's numbers),
  or raises Mapbox tiles per video beyond `MAX_TILES`, is a defect.
- **The dev-only icon script** (D6) runs offline in Docker and is not part of
  the image or the app.

## Boundaries crossed

- **API contract:** an optional `camera` field (`"variable" | "overview" |
  "fixed" | "fixed_strict"`, default `"variable"`) on `POST /api/projects/{name}/video` and
  `POST …/video/plan`. The change is additive: installed apps (E5) don't send
  it and get today's videos. Unknown values are a 422 (Pydantic `Literal`). No
  response shape changes.
- **Stored data:** `videojob.request_json` gains `"camera"`. Rows written before
  this change have no key and are read as `"variable"` (`request.get("camera",
  "variable")`). This matters for pending jobs during a deploy. There is no
  schema change and no migration.
- **Export format:** still MP4 H.264 `yuv420p`. Only the CRF and tune change, so
  files get larger (D7).
- **Golden images:** `tests/golden/video/*.png` are regenerated for the new
  drawing, and new goldens are added for the two new modes.
- **Icons:** `assets/video/icons/*.png` are replaced (the licence README is
  updated).

## Conventions

1. **Variable mode doesn't move.** With `camera="variable"`, `camera_path`
   returns the same shots as before this plan (a test pins this against the
   current implementation). Only the overlay and encoder may change pixels in
   that mode.
2. **Determinism holds.** Frame *n* is a pure function of (timeline, fps, size,
   camera mode, n) in every mode. The camera mode is part of the `camera_path`
   cache key.
3. **The pure modules stay pure.** `src/video/camera.py` imports no Pillow,
   ffmpeg, DB or FastAPI.
4. **No new runtime dependency.** The image stays Pillow plus ffmpeg (D5, D6).
5. **Performance numbers are recorded.** Every unit that changes the overlay,
   basemap or encoder reports benchmark ms per frame and peak RSS before and
   after, measured in the Linux image.

## Open decisions

None. G1 and G2 are measurement gates, not decisions. G1 sets `ROUTE_SS` (2–4),
the CRF (18–21) and whether `-tune animation` is used, within the ranges above.
G2 confirms the result on the VPS or lowers `ROUTE_SS`.

## Execution units

### Wave 1 — measure, and the camera modes

**U1 — Stage timings and the VPS benchmark CLI**
- **Goal:** Every render logs where its time went, and one command measures a
  real render on the VPS in any camera mode and with candidate encoder settings,
  and dumps frames for the sharpness comparison.
- **Scope:** `src/video/renderer.py` (timing instrumentation, plus plumbing the
  encoder CRF and tune as overridable parameters with today's values as the
  default — no behaviour change by default); new `src/video/bench.py` (CLI:
  `python -m src.video.bench --project <name> --owner <uid> --length 90
  --height 1080 [--camera variable|overview|fixed|fixed_strict] [--crf N]
  [--tune animation|none] [--dump-frames 450,1350,2250] [--out DIR]`); new
  `tests/test_video_bench.py`; `docs/VIDEO.md` (a "Profiling on the server"
  section).
- **Context:** the existing manual benchmark in `tests/test_video_renderer.py`
  (`VIDEO_BENCH=1`, ≈563-582) — the example to follow for timing and RSS; the
  `src/video/__main__.py` CLI (U1 of #501) for argument style and trip loading;
  `render_video` / `FrameRenderer` / `_FFMPEG_ARGS` in `renderer.py`;
  `src/utils/metrics` for how external calls are timed.
- **Do:**
  1. Accumulate wall time per stage per job: tile fetching, sheet stitching,
     basemap frame (crop, scale, blend), overlay draw, and write-to-ffmpeg
     (pipe wait). Log one INFO summary line per job with totals, ms per frame
     per stage, frames, sheets, tiles, and peak RSS of the renderer and ffmpeg.
  2. The bench CLI renders a real trip with the real tile fetcher (it uses the
     server's `MAPBOX_TOKEN`) to a temporary file and prints the same summary.
     `--camera` is passed through to the renderer; until U2 and U3 land, only
     `variable` is accepted and the flag errors clearly for the others. `--crf`
     and `--tune` override the encoder. With `--dump-frames`, it writes each
     listed frame twice: as a PNG before encoding, and as a PNG decoded back
     from the MP4 with `ffmpeg -vf select`.
  3. Document how to run it on the server in **its own container**, never
     inside the live worker: `docker compose run --rm worker-video python -m
     src.video.bench …` (a separate container with its own memory limit and no
     RQ worker, so it can't starve or be starved by a user's render).
- **Acceptance:** `pytest tests/test_video_bench.py tests/test_video_renderer.py
  tests/test_video_runner.py` passes in the Linux image (CI=1). New tests:
  - a small render with a fake tile fetcher logs one summary line whose stage
    totals add up to within 10% of the measured wall time;
  - `--dump-frames` writes two PNGs per frame, of the right size;
  - `--crf`/`--tune` reach the ffmpeg command line, and the defaults are
    unchanged;
  - the CLI refuses a trip the owner can't see.
  Existing renderer tests and goldens are unchanged.
- **Out of scope:** any speed-up; changing what is drawn; changing the default
  encoder settings (U3).
- **Latitude:** local design
- **Escalate if:** X3; timing the ffmpeg stage needs changes to the job runner.
- **Depends on:** —

**U2 — Overview and fixed-zoom camera paths**
- **Goal:** `camera_path` gains a `mode` parameter with `"overview"`, `"fixed"`
  and `"fixed_strict"` implementations per D2 and D3, while `"variable"` stays
  unchanged.
- **Scope:** `src/video/camera.py`; `tests/test_video_camera.py`.
- **Context:** `camera.py` in full — `_path`, `_clip_aims`, `_zoom_plan`,
  `overview`, `_fly`, `_off_frame`, `CUT`, `is_followed`, `mode_min_zoom`.
  `basemap_bands.py` `plan_bands` / `band_weights` / `FADE` / `MAX_TILES` for
  how tiles are counted (camera.py may not import it: Convention 3 — re-derive
  the estimate from viewport rects, `TILE_SIZE` and `PIXEL_RATIO`, duplicated
  as constants, with a test pinning it to `plan_bands` on sample paths).
- **Do:**
  1. `camera_path(timeline, fps, size, mode="variable")`, with the mode in the
     cache key, and `camera(...)` likewise.
  2. Overview: all shots = `overview()`, `flying=False`.
  3. Fixed (`fixed`, approach A): per D3, find the fast sub-legs, choose integer
     `Z` from the median of the non-fast followed sub-legs' `CLIP_FILL` fit
     zooms (floor, clamp [4, 14]), frame the legs that are fast at `Z` as
     variable mode does, and lower `Z` until the estimated tiles ≤ `MAX_TILES`.
     Run variable mode's spring, leash, cut and `_fly` machinery with those
     targets. Cards stay overview with fly in and out.
  4. Strict (`fixed_strict`, approach B): per D3, the largest integer
     `Z` ≤ that median, clamped to [2, 14], at which no followed sub-leg is
     fast and the estimated tiles ≤ `MAX_TILES`; every non-card frame targets
     `Z`.
  5. Expose `fixed_zoom(timeline, size, mode)` for tests and logs.
- **Acceptance:** `pytest tests/test_video_camera.py tests/test_video_timeline.py`
  passes. New tests:
  - variable mode returns exactly the shots of the pre-change implementation on
    the existing test trips (Convention 1);
  - overview: every frame's viewport contains the whole trip, and all shots are
    identical;
  - fixed and fixed_strict: the chosen `Z` is an integer; the existing
    `test_consecutive_frames_change_little` bounds (≤ 0.1 frame per frame while
    following, ≤ 0.25 for any pair) hold in both; no clip frame is a cut caused
    by marker speed; while a sub-leg is followed, the marker stays within
    `LEASH` of the centre **without the leash clamping** on more than 5% of its
    frames;
  - a mostly north–south leg just under the bound (by the larger-axis measure)
    is followed without the leash clamping on more than 5% of its frames, and
    one just over it is treated as fast (review R3-1);
  - the search order: on a trip of short walks (fit ≈ 14) plus several slow long
    legs (fit ≈ 6, fast only above about 9), `fixed` picks the higher `Z` (≥ 10,
    long legs flown) and `fixed_strict` picks a lower one, and the test asserts
    that the two differ (review R2-2);
  - fixed: on a trip of walks plus one 1,000 km flight, every walk frame is at
    `Z`, the flight is framed at its fit zoom, and `Z` equals the value for the
    same trip without the flight;
  - fixed_strict: on that same trip, every non-card, non-flying frame is at the
    same `Z`, and the flight is followed at it without exceeding
    `FIXED_MAX_PAN_PER_S`;
  - both fixed modes on a long synthetic trip (365 days, 1,000 legs) end with
    estimated tiles ≤ `MAX_TILES`;
  - the tile estimate matches `plan_bands` within 10% on sample paths in every
    mode;
  - all modes are deterministic, and the antimeridian test holds in every mode.
- **Out of scope:** the renderer, API, client.
- **Latitude:** local design
- **Escalate if:** X3; `fixed_strict` can't satisfy the pan and tile bounds at
  zoom ≥ 2 on the long synthetic trip.
- **Depends on:** —

**Gate G1 (owner, between waves 1 and 2):** run U1's bench **once** on the VPS in
its own container (`docker compose run --rm worker-video python -m src.video.bench
…`) for one plaintext real trip at 90 s and 1080p, in variable mode, with
today's encoder settings, preferably when no user video job is queued or
running. Paste the summary into #517. That run is about 20 minutes of CPU on
the shared host (review R2-3). In parallel, the orchestrator runs the D7
candidate comparison **on the dev box**: Linux image, real tiles, the same trip
(via the owner's export of it), `--dump-frames` per candidate. It records G1's
numbers and the chosen encoder settings in `docs/VIDEO.md`, then sets
`ROUTE_SS` and the encoder settings for wave 2 within D5 and D7's ranges and
D8's budget.

### Wave 2 — thread the mode through; sharpen

**U3 — Camera mode through API, job and renderer; still-camera basemap reuse; encoder settings**
- **Goal:** A requested camera mode reaches the renderer; still-camera frames
  reuse the basemap without the overlay ever drawing on the cached image; the
  encoder uses the settings chosen at G1.
- **Scope:** `api/video.py`; `src/video/renderer.py`;
  `src/video/basemap_bands.py`; `src/video/bench.py` (lift the variable-only
  guard on `--camera`); `tests/test_video_api.py`;
  `tests/test_video_runner.py`; new `tests/test_video_camera_render.py`.
- **Context:** the request models and `request_json` dict in `api/video.py`
  (≈100-111, 419-427); `render_video` / `FrameRenderer` in `renderer.py`;
  `Basemaps.frame` in `basemap_bands.py`; `Overlay.draw` in `overlay.py`
  (read-only here: it draws on the frame it is given, in place); the fake tile
  fetcher in `tests/test_video_renderer.py` (example for render tests); G1's
  chosen encoder settings.
- **Do:**
  1. `camera: Literal["variable","overview","fixed","fixed_strict"] =
     "variable"` on `VideoPlanRequest` and `VideoRequest`, stored in
     `request_json`.
  2. `render_video` reads `request.get("camera", "variable")` and passes it to
     `camera_path`, and the render summary log includes the mode.
  3. `Basemaps.frame` keeps the cropped and scaled image for an unchanged shot
     and returns a **copy** each frame, so the overlay's in-place drawing never
     touches the cached image (D9).
  4. ffmpeg: the G1-chosen CRF and tune become the defaults.
  5. Update the `test_video_api.py:180` expectation.
- **Acceptance:** `pytest tests/test_video_api.py tests/test_video_runner.py
  tests/test_video_camera_render.py tests/test_video_renderer.py
  tests/test_video_camera.py` passes in the Linux image (CI=1), with the
  existing goldens unchanged. New tests:
  - `camera` is stored and reaches `camera_path` in each of the four modes;
  - an unknown mode is a 422;
  - a stored row without `camera` renders as variable;
  - an overview render stitches one sheet and calls the crop and scale step
    once per distinct shot;
  - frame n rendered on its own equals frame n rendered after frames 0..n-1, in
    overview mode and on the title and end cards of variable mode (no
    accumulation);
  - the ffmpeg command line carries the chosen flags.
  The benchmark reports variable mode before and after, and overview mode after,
  against D8's overview baseline (variable before × 1.25) (Convention 5).
- **Out of scope:** the overlay; the client; golden changes (U4, U6).
- **Latitude:** local design
- **Escalate if:** X3; D8's budget is exceeded by the encoder change alone.
- **Depends on:** U1, U2, G1

**U4 — Sharper route, chips and panels**
- **Goal:** The route line, the transport chips (marker and panel icons) and the
  HUD panels are drawn anti-aliased per D5 and D6, within D8's budget in both
  variable and overview mode.
- **Scope:** `src/video/overlay.py`; `assets/video/icons/` (new PNGs +
  README/licence); new `scripts/make_video_icons.py` (dev-only, run in Docker);
  new `tests/test_video_overlay.py`; `tests/golden/video/` (regenerate the
  existing `title`, `follow` and `end` goldens only).
- **Context:** `src/poster/poster_renderer.py:490-533` `_draw_route(...,
  supersample)` and its memory note at ≈105-110 — the example to follow;
  `overlay.py` `_draw_route`, `_marker`, `_icon`, `_panel`; the Flutter icons for
  each mode (`activity_panel.dart:120`, the segment icons) to pick the matching
  Material Symbols; G1's frame pairs and `ROUTE_SS`.
- **Do:**
  1. Draw the route's colour layer and mask at `ROUTE_SS`× over its bounding box
     and `resize(BOX)` down; remove the box blur.
  2. Make `scripts/make_video_icons.py` render the Material Symbols SVG for each
     mode to a 512 px PNG (white glyph on transparent, tinted at runtime or
     pre-coloured, matching today's mode colours), and document it in the icons
     README with the Apache-2.0 licence.
  3. `_icon` downsizes from 512 px, once per size (cached); the marker composes
     its icon into the 4× sprite before scaling.
  4. Draw `_panel` rounded rectangles at 4× and scale them down, cached per size.
- **Acceptance:** `pytest tests/test_video_overlay.py tests/test_video_renderer.py`
  passes in the Linux image (CI=1) with the goldens regenerated there
  (`VIDEO_UPDATE_GOLDEN=1`), before and after frames attached to the report.
  New tests:
  - **edge straightness:** render a single 45° route segment at 320×180; along
    its edge, find the sub-pixel edge position in each row from the mask's
    coverage; the RMS residual from a straight line must fall below a threshold
    that **today's code is shown to fail first** (run against the pre-change
    overlay and record both numbers in the test's docstring);
  - icons are loaded from 512 px sources;
  - no runtime import outside Pillow.
  The benchmark reports variable mode before and after. For overview, which
  the renderer can't produce until U3 is merged, U4 times `Overlay.draw` on its
  own at an overview shot built with `camera.overview()` (merged in wave 1),
  before and after (review R2-4). The full overview render check against D8
  happens at wave-2 integration (the orchestrator runs the bench in overview
  mode after merging U3 and U4) and at G2.
- **Out of scope:** camera; encoder; client.
- **Latitude:** local design
- **Escalate if:** X3; D8's budget can't be met at `ROUTE_SS` = 2 in overview
  mode; a mode has no suitable Material Symbol.
- **Depends on:** U1, U2, G1

**Gate G2 (owner, between waves 2 and 3):** run the bench again on the VPS in
its own container, on the wave-2 code, for the same trip at 90 s and 1080p, in
**variable** and **overview** mode. The orchestrator records the numbers next
to G1's in `docs/VIDEO.md` and #517 and checks D8's +25% on the VPS. If the
projected 90 s 1080p render time reaches 1,600 s (about 11% under the
1,800 s timeout), `ROUTE_SS` is lowered before wave 3.

### Wave 3 — client, goldens and docs

**U5 — Camera choice in the video dialog**
- **Goal:** Users pick Variable, Overview or Fixed zoom, and for Fixed zoom
  whether to zoom out for flights and long legs, in the video dialog; the
  choice is sent with the plan and create requests.
- **Scope:** `flutter_client/lib/src/api/video_api.dart`,
  `flutter_client/lib/src/projects/video_job_notifier.dart`,
  `flutter_client/lib/src/projects/video_config_dialog.dart`;
  `flutter_client/test/video_job_notifier_test.dart`,
  `flutter_client/test/video_config_dialog_test.dart`.
- **Context:** the Length and Resolution `SegmentedButton`s in
  `video_config_dialog.dart` (≈211-250), and `setLength`/`setHeight` in the
  notifier — the examples to follow; `design_tokens.dart`; the widget-test traps
  (pump fixed frames while a spinner shows; swap the global `api`).
- **Do:** a "Camera" `SegmentedButton<String>` (Variable · Overview · Fixed
  zoom), defaulting to Variable, with one-line helper text per option; when
  Fixed zoom is selected, a switch "Zoom out for flights and long legs",
  default on. The notifier maps the choice to `camera`: Variable →
  `"variable"`, Overview → `"overview"`, Fixed zoom with the switch on →
  `"fixed"`, off → `"fixed_strict"`. `camera` is sent on `fetchVideoPlan` and
  `createVideoJob`.
- **Acceptance:** `flutter analyze` is clean. In the Flutter container, the video
  tests and then the full suite pass. New tests: each of the four values is
  sent in the create request; the default sends `"variable"`; the switch only
  appears with Fixed zoom; the choice survives a consent round-trip.
- **Out of scope:** server; previews of the camera.
- **Latitude:** none
- **Escalate if:** X3; the API field differs from U3's.
- **Depends on:** U3, G2

**U6 — Golden frames for the new modes, docs**
- **Goal:** Each camera mode is pinned by golden frames, and the docs describe
  the modes, sharpening and the measured numbers.
- **Scope:** `tests/test_video_renderer.py` (golden cases only);
  `tests/golden/video/` (new `overview_*`, `fixed_*`, `fixed_strict_*`);
  `docs/VIDEO.md`.
- **Context:** `test_frames_match_golden_images` and
  `test_golden_comparison_catches_a_moved_route` (≈291-320) — the example to
  follow; G1's and G2's numbers; U3's and U4's before and after benchmark
  reports.
- **Do:** add a dedicated golden fixture with a leg that is **fast at `fixed`'s
  `Z`**, e.g. short walks in Paris, a Paris → New York flight and short walks in
  New York, so `fixed` flies the flight while `fixed_strict` drops its zoom
  (review R2-5). Its test first asserts `fixed_zoom(…, "fixed") >
  fixed_zoom(…, "fixed_strict")` as a precondition. Add golden cases generated
  in the Linux image: overview (a mid-video frame and the end card, on the
  existing fixture), fixed (a walk frame) and fixed_strict (a frame during the
  flight), on the new fixture. Document the four camera values, D3's zoom choice,
  D5–D7 with the chosen values, and a benchmark table (dev box and VPS, G1 and
  G2, variable and overview).
- **Acceptance:** `pytest tests/test_video_renderer.py` passes in the Linux image
  (CI=1). Negative controls: the overview **mid-video** frame fails when
  rendered in variable mode, and the fixed and fixed_strict follow frames fail
  when rendered in each other's mode and in variable mode. The overview end-card
  golden is not negative-controlled, because the end card is identical in every
  mode by design; a test asserts that identity instead. `docs/VIDEO.md` states
  the chosen `ROUTE_SS`, the encoder settings and the numbers.
- **Out of scope:** code changes outside tests.
- **Latitude:** none
- **Escalate if:** the new goldens aren't stable across two runs.
- **Depends on:** U3, U4

## Definition of done

- The video dialog offers Variable, Overview and Fixed zoom, and for Fixed zoom
  a "zoom out for flights and long legs" switch. Variable renders the same
  camera path as before. Overview shows the whole trip with only the marker and
  line moving. Fixed zoom follows the marker at one server-chosen integer zoom,
  pulling out for fast legs when the switch is on, and at one zoom throughout
  when it's off. In every mode, the map never moves more than 0.25 frame widths
  per frame outside flights between views, and never more than 0.1 while
  following the marker.
- Older installed apps and pending jobs without `camera` render in Variable
  mode.
- On a 45° segment, the route's edge straightness beats today's by the recorded
  margin, and the chips come from 512 px vector-sourced icons. Before and after
  frames are attached to the PR.
- VPS numbers from G1 and G2 are recorded in `docs/VIDEO.md` and #517. U3 and U4
  together add ≤ 25% per frame in variable and overview mode, on the dev box and
  on the VPS. The projected 90 s 1080p render on the VPS stays under 1,600 s.
- Mapbox tiles per video stay ≤ `MAX_TILES` in every mode.
- The server and Flutter suites pass; the video tests pass in the Linux image;
  CI is green.
- The PR carries a `Release-Note:` trailer. There is no `Upgrade-Note:`: no
  operator action is needed beyond the usual image update.

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
| D1 | Three camera modes, `camera: "variable" \| "overview" \| "fixed"`, default `"variable"` (today's camera, unchanged frame for frame). | Owner decision (2026-09-29): both new options, separately selectable. | Replacing today's camera; one merged "constant" option. |
| D2 | **Overview:** every frame is the whole-trip `overview()` shot, `flying` always false; the marker, travelled line, HUD and cards animate as today. | That's what "see the whole trip while it plays" means; a still camera needs one basemap sheet. | Slow pans or zooms in overview mode. |
| D3 | **Fixed zoom:** the camera follows the marker exactly as in variable mode (spring, leash, fly across jumps and to or from the cards) but at **one zoom** for all clips, **chosen by the server** (owner decision). The zoom is the median over followed sub-legs of their `CLIP_FILL` fit zoom, clamped to [4, 14], then lowered one level at a time until the estimated tile count for the path is ≤ `MAX_TILES`. Per-mode floors don't apply. | Automatic keeps the dialog simple. The median suits the typical leg. Lowering the zoom up front gives a sharp map at a lower zoom rather than a high zoom made blurry by the band cap. | A user zoom slider or presets (not wanted); keeping mode floors (they would reintroduce zoom changes). |
| D4 | **Measure first.** Wave 1 adds per-stage render timings and a benchmark and frame-dump CLI. **Gate G1:** the owner runs it on the VPS (the agents can't reach the VPS), and the numbers go into `docs/VIDEO.md` and #517 before wave 2 sets the sharpening parameters. | Owner decision: sharpening costs time, and the dev-box benchmark was 8× off. | Choosing a supersample factor blind. |
| D5 | **Route line:** draw the route layer supersampled (factor `ROUTE_SS`, default 3, allowed 2–4) over the route's bounding box only, then `resize(BOX)`, following the poster's `_draw_route`. Replace the box blur. The factor is set from G1's numbers within the render-time budget (D8). | Proven in the repo (poster), and costs pixels only where the route is. | New native dependencies (aggdraw, Cairo, Skia) at runtime; a full-frame supersampled layer (≈132 MB at 4× 1080p). |
| D6 | **Chips:** replace the 128 px emoji bitmaps with **vector-sourced icons matching the app's Material icons** (Material Symbols, Apache-2.0), pre-rendered offline to 512 px PNGs by a dev-only script run in Docker, with the PNGs committed. At runtime the icon is rendered once per size (cached) from the 512 px source into the 4× marker sprite before it's scaled down. HUD rounded panels are drawn at 4× the same way. | A bitmap emoji font can't be rendered larger than about 136 px. Vector sources scale cleanly, and matching the app's icons keeps the video and app consistent. Keeping the runtime Pillow-only means no Cairo in the image. | Runtime SVG rasterising (Cairo in the image); keeping the emoji. |
| D7 | **Encoder:** `-crf 20 -tune animation`, keeping `yuv420p` (plays on every phone), `veryfast`, `-threads 4`. Files may grow about 1.5–2× (owner accepted a moderate increase). The final CRF is confirmed from G1's raw-frame versus MP4 comparison (18–21). | Encoding softens thin coloured lines; animation tuning suits flat colours. | `yuv444p` (poor phone support); `-crf ≤ 16` (files too large). |
| D8 | **Render-time budget:** the overlay and encoder changes together must not add more than **25%** to the per-frame time on the benchmark (dev box and G1's VPS run alike), and the 1080p benchmark must stay within its existing assertions. If D5, D6 and D7 at their defaults exceed it, the unit lowers `ROUTE_SS` before anything else. | Keeps #518 from making #517 worse. | Unbounded quality work. |
| D9 | **Overview is also cheaper:** a still camera reuses the cropped and scaled basemap frame instead of recomputing it every frame. | Free speed for the new mode; `Basemaps.frame` recomputes today. | — |

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
  "fixed"`, default `"variable"`) on `POST /api/projects/{name}/video` and
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

None. G1 is a measurement gate, not a decision: it sets `ROUTE_SS` (2–4) and
the CRF (18–21) within the ranges above.

## Execution units

### Wave 1 — measure, and the camera modes

**U1 — Stage timings and the VPS benchmark CLI**
- **Goal:** Every render logs where its time went, and one command measures a
  real render on the VPS and dumps frames for the sharpness comparison.
- **Scope:** `src/video/renderer.py` (timing instrumentation only — no
  behaviour change); new `src/video/bench.py` (CLI:
  `python -m src.video.bench --project <name> --owner <uid> --length 90
  --height 1080 [--dump-frames 450,1350,2250] [--out DIR]`); new
  `tests/test_video_bench.py`; `docs/VIDEO.md` (a "Profiling on the server"
  section).
- **Context:** the existing manual benchmark in `tests/test_video_renderer.py`
  (`VIDEO_BENCH=1`, ≈563-582) — the example to follow for timing and RSS; the
  `src/video/__main__.py` CLI (U1 of #501) for argument style and trip loading;
  `render_video` / `FrameRenderer` in `renderer.py`; `src/utils/metrics` for how
  external calls are timed.
- **Do:**
  1. Accumulate wall time per stage per job: tile fetching, sheet stitching,
     basemap frame (crop, scale, blend), overlay draw, and write-to-ffmpeg
     (pipe wait). Log one INFO summary line per job with totals, ms per frame
     per stage, frames, sheets, tiles, and peak RSS of the renderer and ffmpeg.
  2. The bench CLI renders a real trip with the real tile fetcher (it uses the
     server's `MAPBOX_TOKEN`) to a temporary file and prints the same summary.
     With `--dump-frames`, it writes each listed frame twice: as a PNG before
     encoding, and as a PNG decoded back from the MP4 with `ffmpeg -vf select`.
  3. Document how to run it inside `worker-video`: `docker compose exec
     worker-video python -m src.video.bench …`.
- **Acceptance:** `pytest tests/test_video_bench.py tests/test_video_renderer.py
  tests/test_video_runner.py` passes in the Linux image (CI=1). New tests:
  - a small render with a fake tile fetcher logs one summary line whose stage
    totals add up to within 10% of the measured wall time;
  - `--dump-frames` writes two PNGs per frame, of the right size;
  - the CLI refuses a trip the owner can't see.
  Existing renderer tests and goldens are unchanged.
- **Out of scope:** any speed-up; changing what is drawn.
- **Latitude:** local design
- **Escalate if:** X3; timing the ffmpeg stage needs changes to the job runner.
- **Depends on:** —

**U2 — Overview and fixed-zoom camera paths**
- **Goal:** `camera_path` gains a `mode` parameter with `"overview"` and
  `"fixed"` implementations per D2 and D3, while `"variable"` stays unchanged.
- **Scope:** `src/video/camera.py`; `tests/test_video_camera.py`.
- **Context:** `camera.py` in full — `_path`, `_clip_aims`, `_zoom_plan`,
  `overview`, `_fly`, `is_followed`, `mode_min_zoom`. `basemap_bands.py`
  `plan_bands` / `MAX_TILES` for how the tile count is estimated (camera.py may
  not import it: Convention 3 — re-derive the estimate from viewport rects,
  `TILE_SIZE` and `PIXEL_RATIO` passed in or duplicated as constants, with a test
  pinning it to `plan_bands` on a sample path).
- **Do:**
  1. `camera_path(timeline, fps, size, mode="variable")`, with the mode in the
     cache key, and `camera(...)` likewise.
  2. Overview: all shots = `overview()`, `flying=False`.
  3. Fixed: choose the zoom per D3 (median of followed sub-legs' `CLIP_FILL` fit
     zooms, clamp [4, 14], lower until the estimated tiles ≤ `MAX_TILES`), then
     run the variable path's spring, leash, cut and `_fly` machinery with that
     zoom for every clip frame. Cards stay overview with fly in and out.
  4. Expose `fixed_zoom(timeline, size)` for tests and logs.
- **Acceptance:** `pytest tests/test_video_camera.py tests/test_video_timeline.py`
  passes. New tests:
  - variable mode returns exactly the shots of the pre-change implementation on
    the existing test trips (Convention 1);
  - overview: every frame's viewport contains the whole trip, and all shots are
    identical;
  - fixed: the zoom is constant across all non-card, non-flying frames; the
    marker stays within the leash; a long synthetic trip (365 days, 1,000 legs)
    lowers the zoom until the estimated tiles ≤ `MAX_TILES`; the estimate
    matches `plan_bands` within 10% on a sample path;
  - all modes are deterministic, and the antimeridian test holds in every mode.
- **Out of scope:** the renderer, API, client.
- **Latitude:** local design
- **Escalate if:** X3; the fixed zoom can't satisfy the tile budget above zoom 4
  on the long synthetic trip.
- **Depends on:** —

**Gate G1 (owner, between waves 1 and 2):** run U1's bench in `worker-video` on
the VPS for one real trip at 90 s and 1080p, with `--dump-frames`. Paste the
summary and attach the frame pairs to #517. The orchestrator records the numbers
in `docs/VIDEO.md` and sets `ROUTE_SS` and CRF for wave 2 within D5 and D7's
ranges and D8's budget.

### Wave 2 — thread the mode through; sharpen

**U3 — Camera mode through API, job and renderer; still-camera basemap reuse; encoder settings**
- **Goal:** A requested camera mode reaches the renderer; overview renders reuse
  the basemap frame; the encoder uses D7's settings.
- **Scope:** `api/video.py`; `src/video/renderer.py`;
  `src/video/basemap_bands.py`; `tests/test_video_api.py`;
  `tests/test_video_runner.py`; new `tests/test_video_camera_render.py`.
- **Context:** the request models and `request_json` dict in `api/video.py`
  (≈100-111, 419-427); `render_video` / `FrameRenderer` in `renderer.py`;
  `Basemaps.frame` in `basemap_bands.py`; the fake tile fetcher in
  `tests/test_video_renderer.py` (example for render tests); G1's numbers (CRF).
- **Do:**
  1. `camera: Literal["variable","overview","fixed"] = "variable"` on
     `VideoPlanRequest` and `VideoRequest`, stored in `request_json`.
  2. `render_video` reads `request.get("camera", "variable")` and passes it to
     `camera_path`, and the render summary log includes the mode.
  3. `Basemaps.frame` reuses the previous frame's cropped and scaled image when
     the shot is identical (D9).
  4. ffmpeg: `-crf <G1 value> -tune animation`.
  5. Update the `test_video_api.py:180` expectation.
- **Acceptance:** `pytest tests/test_video_api.py tests/test_video_runner.py
  tests/test_video_camera_render.py tests/test_video_renderer.py
  tests/test_video_camera.py` passes in the Linux image (CI=1). New tests:
  - `camera` is stored and reaches `camera_path` in each mode;
  - an unknown mode is a 422;
  - a stored row without `camera` renders as variable;
  - an overview render stitches one sheet (or two in the fade zone) and calls the
    crop and scale step once per distinct shot;
  - the ffmpeg command line carries the new flags.
  The benchmark reports before and after (Convention 5), within D8.
- **Out of scope:** the overlay; the client; golden regeneration (U6).
- **Latitude:** local design
- **Escalate if:** X3; D8's budget is exceeded by the encoder change alone.
- **Depends on:** U1, U2, G1

**U4 — Sharper route, chips and panels**
- **Goal:** The route line, the transport chips (marker and panel icons) and the
  HUD panels are drawn anti-aliased per D5 and D6, within D8's budget.
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
  - on a diagonal route segment, the count of distinct alpha levels along a
    cross-section is ≥ `ROUTE_SS`² (it is 2 today);
  - icons are loaded from 512 px sources;
  - no runtime import outside Pillow.
  The benchmark reports before and after (Convention 5); U3 and U4 together stay
  within D8's +25%.
- **Out of scope:** camera; encoder; client.
- **Latitude:** local design
- **Escalate if:** X3; D8's budget can't be met at `ROUTE_SS` = 2; a mode has no
  suitable Material Symbol.
- **Depends on:** U1, G1

### Wave 3 — client, goldens and docs

**U5 — Camera choice in the video dialog**
- **Goal:** Users pick Variable, Overview or Fixed zoom in the video dialog, and
  it is sent with the plan and create requests.
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
  zoom), defaulting to Variable, with one-line helper text per option; a
  `camera` field and setter in the notifier; `camera` sent on `fetchVideoPlan`
  and `createVideoJob`.
- **Acceptance:** `flutter analyze` is clean. In the Flutter container, the video
  tests and then the full suite pass. New tests: each choice is sent in the
  create request; the default sends `"variable"`; the choice survives a consent
  round-trip.
- **Out of scope:** server; previews of the camera.
- **Latitude:** none
- **Escalate if:** X3; the API field differs from U3's.
- **Depends on:** U3

**U6 — Golden frames for the new modes, docs**
- **Goal:** Each camera mode is pinned by golden frames, and the docs describe
  the modes, sharpening and the measured numbers.
- **Scope:** `tests/test_video_renderer.py` (golden cases only);
  `tests/golden/video/` (new `overview_*`, `fixed_*`); `docs/VIDEO.md`.
- **Context:** `test_frames_match_golden_images` and
  `test_golden_comparison_catches_a_moved_route` (≈291-320) — the example to
  follow; G1's numbers; U3's and U4's before and after benchmark reports.
- **Do:** add golden cases for overview (a mid-video frame and the end card) and
  fixed (a follow frame), generated in the Linux image. Document the three modes,
  D3's zoom choice, D5–D7 with the chosen values, and a benchmark table (dev box
  and VPS, before and after).
- **Acceptance:** `pytest tests/test_video_renderer.py` passes in the Linux image
  (CI=1); the new golden cases fail when the camera mode is swapped (a negative
  control in the test); `docs/VIDEO.md` states the chosen `ROUTE_SS`, CRF and
  the numbers.
- **Out of scope:** code changes outside tests.
- **Latitude:** none
- **Escalate if:** the new goldens aren't stable across two runs.
- **Depends on:** U3, U4

## Definition of done

- The video dialog offers Variable, Overview and Fixed zoom. Variable renders
  the same camera path as before. Overview shows the whole trip with only the
  marker and line moving. Fixed follows the marker at one server-chosen zoom.
- Older installed apps and pending jobs without `camera` render in Variable
  mode.
- On a diagonal segment, the route line has smooth edges (measured by the alpha
  cross-section test), and the chips come from 512 px vector-sourced icons.
  Before and after frames are attached to the PR.
- VPS numbers from G1 are recorded in `docs/VIDEO.md` and #517. U3 and U4
  together add ≤ 25% per frame. A 90 s 1080p render stays under the job
  timeout on the VPS.
- Mapbox tiles per video stay ≤ `MAX_TILES` in every mode.
- The server and Flutter suites pass; the video tests pass in the Linux image;
  CI is green.
- The PR carries a `Release-Note:` trailer. There is no `Upgrade-Note:`: no
  operator action is needed beyond the usual image update.

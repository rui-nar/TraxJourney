# Trip video rendering

How the `video` worker turns a trip into an MP4 (docs/TRIP_VIDEO_PLAN.md, D1,
D8, D10; unit U5). The job lifecycle, quota and consent are in
`src/video/job_runner.py` and `api/video.py`; this page is about the pixels.

## Pipeline

`render_video` (`src/video/renderer.py`, Convention 5) loads the trip through
its owner (the requester may be a companion, D12), builds the timeline
(`timeline.py`) and the camera path (`camera.py`), and pipes each frame to
ffmpeg as raw RGB:

```
ffmpeg -f rawvideo -pix_fmt rgb24 -s WxH -r 30 -i - -an
       -c:v libx264 -preset veryfast -crf 20 -threads 4
       -pix_fmt yuv420p -movflags +faststart  video.part.mp4
```

`-crf 20`, no `-tune`, is D7's choice: sharper than the earlier `-crf 23`
(files come out about 1.5× larger), with `-tune animation` dropped because it
softened the map imagery at the same CRF — see [Gate G1
measurements](#gate-g1-measurements-518-2026-09-29) for the comparison.

The MP4 is written under a `.part` name and moved into place only when
ffmpeg exits 0. A missing ffmpeg, a broken pipe or a non-zero exit raises
`VideoEncodeError` with a fixed message; ffmpeg's stderr goes to the server
log only. The runner reports every such failure as its generic "could not be
rendered" reason. Progress is written every 30 frames (stages `loading trip`,
`planning`, `rendering`).

`-threads 4` is deliberate: by default x264 runs about 1.5 frame threads per
core, and each one buffers frames. On a 10-core host that took about 560 MB
at 1080p on its own, and four threads still keep up with the frames Python
draws.

## Camera (`src/video/camera.py`)

`camera_path(timeline, fps, size, mode)` offers four camera values
(docs/VIDEO_CAMERA_QUALITY_PLAN.md D1), cached per those four arguments and
returned frame by frame:

- **`variable`** (default) — today's camera, unchanged by this plan: a clip
  follows the marker at a zoom fit to its route, raised to its mode's floor,
  cut and flown between clips and cards.
- **`overview`** — a still whole-trip view (the title card's `overview()`) on
  every frame; only the marker and the travelled line move.
- **`fixed`** and **`fixed_strict`** — the marker followed with `variable`'s
  own spring, leash and fly-to machinery, at one server-chosen **integer**
  zoom `Z` for the whole video (`fixed_zoom`, D3). `fixed` pulls a sub-leg out
  to its own fit zoom when following it at `Z` would be **fast** — faster
  than the spring can track without the leash pinning the marker,
  `FIXED_MAX_PAN_PER_S` frame widths/heights per second (the larger axis).
  `fixed_strict` instead picks a lower `Z` at which no sub-leg is fast, so the
  whole video stays at one zoom throughout.

  `Z` is chosen by one fixed top-down search: from zoom 14 down to the mode's
  floor, integer by integer (an integer zoom never sits in a basemap band
  cross-fade), the highest `z` at which (a) `z` is at most the floor of the
  median `CLIP_FILL` fit zoom of the relevant followed sub-legs — every one of
  them for `fixed_strict`, only those not fast at `z` for `fixed` — and (b)
  the path's estimated tiles stay within `MAX_TILES`, else the floor.
  `fixed`'s floor is 4; `fixed_strict`'s is 2, raised so a frame never spans
  more than 180° of longitude: `max(2, ceil(log2(width / 256)))` (3 at
  1920 px, 2 at 320 px).

  The video dialog shows these two as one "Fixed zoom" choice plus a "Zoom
  out for flights and long legs" switch: on sends `fixed`, off sends
  `fixed_strict`.

## Basemap: bands and sheets (`src/video/basemap_bands.py`)

- **Bands.** A frame at camera zoom *z* is drawn from the tiles of integer
  zoom `floor(z)`, stitched at the tiles' @2x resolution. That gives one to
  two sheet pixels per frame pixel: the map is only ever scaled down, for a
  quarter of the tiles that `ceil(z)` would need. In the last 0.2 of a zoom
  level, the frame cross-fades to the next band, so detail doesn't pop. The
  camera's mode floors (12, 10, 6) are integers, so a clip followed at its
  floor reads from one band.
- **Sheets.** A sheet is one `render_basemap` image: the union of the
  viewports that consecutive frames need from one band. A sheet closes when
  it would exceed 12 frames' area, or when its band has gone unused for 15
  frames. Every sheet is planned before frame 1, stitched at its first frame
  and dropped after its last, so memory depends on a few frames of map, not
  on the trip's extent.
- **Tile budget.** The planned tile count is checked before frame 1 against
  `MAX_TILES` (3000). If the plan is over budget, the highest band is lowered
  one level at a time until the plan fits: coarser tiles, scaled up. The
  camera's framing never changes, and the render never fails for the budget.

## The antimeridian

A route across the ±180° meridian (a Tokyo to Los Angeles flight, a ferry or
a track in Fiji) is animated, framed and drawn along its short way.

- **Unwrapped longitudes.** `build_legs` (`legs.py`) shifts each point's
  longitude by a whole number of turns so it lies within 180° of the point
  before it. This carries on from one leg to the next, so the Los Angeles
  walk after the flight continues at about 241.6° rather than -118.4°. The
  interpolation, bearings, bounding boxes, camera and overlay all work on
  these continuous longitudes, which may lie past ±180 (world x below 0 or
  above 1). Distances don't change, because haversine is periodic in
  longitude.
- **Wrapped basemap.** A sheet whose x range runs past the world's edge is
  stitched from one `render_basemap` call per world copy that it overlaps.
  Each piece is shifted back into [-180, 180], so only valid tile indices
  are requested and `render_basemap` itself is unchanged.
- **One world copy per frame.** A frame shows at most one copy of the world,
  centred on the frame. A frame wider than the world (only at band 0) is
  black beyond that copy, as it was before.

## Overlay (`src/video/overlay.py`)

- **Route.** The whole route is drawn as a faint white line. The travelled
  part is drawn in each mode's colour with a dark casing, cut exactly at the
  marker. Colours come from `design_tokens.dart`: ride #4FC3F7, run #EF5350,
  hike/walk #66BB6A, other #AB47BC, flight #42A5F5, train #8D6E63,
  bus #FFB300, boat #26C6DA, and #FFA726 for anything else.
- **Anti-aliasing.** The route is drawn as one colour layer plus one coverage
  mask, `ROUTE_SS` times larger than the frame, over the route's bounding box
  (`poster_renderer._draw_route`'s supersampling, docs/VIDEO_CAMERA_QUALITY_PLAN.md
  D5). Only the grid cells the route actually touches are reduced back down
  and pasted, since most of that box is empty. `ROUTE_SS = 3`, chosen at gate
  G1 within its allowed 2–4 range; see [Gate G2
  measurements](#gate-g2-measurements-518-2026-09-29) for what it costs per
  frame.
- **Marker.** The mode icon on a white disc ringed in the mode colour, inside
  a soft halo, composed into a 4× sprite and scaled down for smooth edges. The
  icon itself is downsized once per size from a 512 px source (D6): the same
  Material Symbols glyphs the app uses for each mode (Apache-2.0), tinted at
  runtime rather than the old 128 px emoji bitmaps. The icons are in
  `assets/video/icons/`; see that folder's README for where they come from and
  `scripts/make_video_icons.py` for how they're regenerated. HUD panels
  (`_panel`) are rounded rectangles drawn the same way, at 4× and scaled down.
- **HUD** (clips only). The date ticker is top left, the speed badge bottom
  left, and the per-mode distance counters bottom right, in the order the
  modes first appear in the trip.
- **Cards.** The title card shows the trip name, date range and total
  distance, and fades out over its last 0.5 s. The end card shows per-mode
  totals and fades in over 0.5 s. Text falls back to the emoji face, as the
  poster's does.

## Benchmark

Run it manually on Linux:
`VIDEO_BENCH=1 pytest tests/test_video_renderer.py -k benchmark -s`. It
renders a 60 s 1080p video of a synthetic two-week, 24-leg trip, with a fake
fetcher serving textured 1024 px JPEG tiles, so tile decoding is counted but
the network is not.

Measured 2026-09-28 in the app image (`python:3.14-slim`, Pillow 12.3,
ffmpeg 7.1) under Docker Desktop, 10 CPUs, with `--memory 896m` (the
`worker-video` limit):

| | |
|---|---|
| Frames | 1800 (60 s × 30 fps) |
| Time per frame | **62.4 ms** (60.2 ms without the memory limit) |
| Peak RSS, renderer (Python) | **363 MB** |
| Peak RSS, ffmpeg | **318 MB** |
| cgroup `memory.peak` | 855 MB, including page cache from writing the MP4 |
| Sheets / tiles | 86 sheets, 1479 tiles |

- **Output size.** The fake tiles are grain noise, the worst case for x264:
  the MP4 came out at 214 MB. Real imagery compresses far better.
- **Earlier runs.** Before the thread cap, ffmpeg's peak was 557 MB. Under
  the limit, the render then slowed to 259.6 ms/frame, most likely from
  memory pressure (not re-measured).
- **Fewer cores.** Python and x264 run in parallel. On a host with fewer
  cores than this one, expect a slower render.
- **Mapbox cost.** 1479 tiles for one 60 s trip is the real per-render
  Mapbox cost; `MAX_TILES` bounds it.

## Stage timings

Every render logs one INFO summary line when it finishes encoding — frame
count, wall time, setup time, ms per frame for each stage, sheets, tiles and
peak RSS of the renderer and of ffmpeg:

```
video render summary: frames=1800 elapsed_s=112.32 setup_s=0.05 ms_per_frame=62.4
fetch_ms=18.10 stitch_ms=9.40 basemap_ms=21.60 overlay_ms=11.20 write_ms=2.10
sheets=86 tiles=1479 peak_rss_renderer_mb=363 peak_rss_ffmpeg_mb=318
```

The five stages are mutually exclusive (they add up to `elapsed_s`, not past
it): `fetch` is time inside the tile fetcher; `stitch` is a new sheet's own
decode/paste/resize, net of any `fetch` it did; `basemap` is a frame's crop,
scale and cross-fade blend, net of any `fetch`/`stitch` a new sheet needed;
`overlay` is drawing the route, marker and HUD; `write` is time blocked
writing a frame to ffmpeg's stdin (its own encoding work happens
concurrently, in the ffmpeg process, and isn't part of any of these).
`setup_s` is the time before `elapsed_s` starts, in neither it nor any stage:
building the camera path, basemap band plan, basemaps and overlay, and
starting ffmpeg (a preview has no ffmpeg; its setup includes the video's own
camera path it samples).

## Profiling on the server

`python -m src.video.bench` renders one real trip end to end — the real
tile fetcher, using the server's `MAPBOX_TOKEN` — and prints the same
summary line, without going through a job row, quota or the queue:

```
python -m src.video.bench --project "Tour de France" --owner 3 \
    --length 90 --height 1080 [--camera variable] [--crf 20] \
    [--tune animation] [--dump-frames 450,1350,2250] [--out DIR]
```

- `--camera` is passed to the renderer as-is: `variable` (default),
  `overview`, `fixed` or `fixed_strict`.
- `--crf`/`--tune` override the encoder for one run, to compare candidates
  (D7) without touching the shipped defaults.
- `--dump-frames` (comma-separated frame numbers) writes each of those frames
  twice into `--out`: `frame_NNNNNN_pre.png`, rendered directly (what was fed
  to ffmpeg), and `frame_NNNNNN_post.png`, decoded back from the encoded MP4
  with `ffmpeg -vf select` — the pair the sharpness comparison (D5–D7) is
  judged from.
- `--out` is where the MP4 and any dumped frames land; omitted, it's a fresh
  temp directory (printed at the end).

**Run it in its own container, never inside the live worker** — a
`docker compose run` gets its own process and memory limit, with no RQ
worker listening, so a benchmark run can neither starve nor be starved by a
user's render:

```
docker compose run --rm worker-video python -m src.video.bench \
    --project "Tour de France" --owner 3 --length 90 --height 1080
```

Encoder candidates (D7: `-crf` 18–21, with and without `-tune animation`) are
compared on a dev box instead of the VPS, because frames are the same on any
machine — only render *time* needs the real server.

## Gate G1 measurements (#518, 2026-09-29)

Measured before any #518 drawing or encoder change: variable camera, 90 s at 1080p (2,700 frames), one plaintext trip, 2,414 Mapbox tiles.

**VPS** (OVH VPS-1, 2 vCPU, shared by the prod and val stacks), run with `docker compose run --rm worker-video python -m src.video.bench`:

| Stage | ms per frame |
|---|---|
| Tile fetch | 128.3 |
| Sheet stitch | 10.0 |
| Basemap crop, scale and blend | 79.6 |
| Overlay | 75.3 |
| Write to ffmpeg | 14.1 |
| **Total** | **317.4** (857 s) |

The peak memory figures of that run are not valid: the renderer's own memory was counted as ffmpeg's. Fix unit U1a corrects the measurement.

**Encoder comparison** (dev box, Linux image, same trip and tiles; three sampled frames; decoded frame against the raw frame):

| Candidate | MP4 size | Full frame PSNR / SSIM | Route PSNR / SSIM | Map PSNR / SSIM |
|---|---|---|---|---|
| crf 23, no tune (before) | 113.7 MB (1.00×) | 35.30 / 0.9559 | 31.85 / 0.9506 | 33.58 / 0.9640 |
| **crf 20, no tune (chosen)** | 175.7 MB (1.55×) | 36.89 / **0.9732** | 32.96 / **0.9692** | 35.38 / **0.9787** |
| crf 20, animation | 193.7 MB (1.70×) | 37.03 / 0.9708 | 33.16 / 0.9656 | 35.79 / 0.9780 |

The animation tune added size and lowered SSIM (structure) on the route and the map at the same CRF, so it isn't used (D7). crf 18 wasn't run: at crf 20 the file is already 1.55× the baseline, and 18 would likely pass the ~2× the owner accepted.

**Settings chosen for wave 2:** `-crf 20`, no `-tune`, `ROUTE_SS = 3`.

## Gate G2 measurements (#518, 2026-09-29)

Same VPS, same trip and settings as G1, on the wave-2 code (sharper overlay, CRF 20, camera modes):

| Stage (ms per frame) | G1 variable (before) | G2 variable | G2 overview |
|---|---|---|---|
| Tile fetch | 128.3 | 44.6 | 0.1 |
| Sheet stitch | 10.0 | 9.9 | 0.0 |
| Basemap | 79.6 | 48.6 | 4.7 |
| Overlay | 75.3 | 129.4 | 83.5 |
| Write to ffmpeg | 14.1 | 13.8 | 14.2 |
| **Total** | **317.4** (857 s) | **257.4** (695 s) | **110.6** (299 s) |
| Tiles / sheets | 2,414 / 152 | 2,414 / 152 | 9 / 1 |
| Peak RSS renderer / ffmpeg | invalid | 725 / 299 MB | 725 / 321 MB |

- **Render-time budget (D8):** the stages #518 changed (overlay and write) went from 89.4 to 143.2 ms per frame. That is +53.8 ms, or +17% of G1's total, within D8's +25%. Tile fetch and basemap vary with the network and host load, and #518 didn't touch them.
- **Supersampling is much dearer on the VPS:** the overlay alone costs +72% there, against about +10% on the dev box, because of the 2 slow vCPUs.
- **Projected 90 s 1080p render:** 695 s as measured, about 920 s if tile fetching is as slow as at G1. Both are well under the 1,600 s threshold (D4, G2).
- **Overview is 2.9× faster than variable** and fetches 9 tiles instead of 2,414.
- **Memory is dominated by loading the trip:** the renderer peaks at 725 MB in both modes, because loading the trip dominates. The peaks add up to more than `worker-video`'s 896 MB limit, but they didn't coincide and the runs completed. This concerns worker sizing (#520), not this change.

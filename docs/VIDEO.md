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
       -c:v libx264 -preset veryfast -crf 23 -threads 4
       -pix_fmt yuv420p -movflags +faststart  video.part.mp4
```

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
- **Anti-aliasing.** The route is drawn as one colour layer plus one
  coverage mask, over the route's bounding box only. The mask is box-blurred
  for anti-aliasing and the result pasted once.
- **Marker.** The mode icon on a white disc ringed in the mode colour, inside
  a soft halo. The icons are in `assets/video/icons/`; see that folder's
  README for where they come from.
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

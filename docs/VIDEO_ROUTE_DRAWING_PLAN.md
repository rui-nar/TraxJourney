# Trip video route drawing: speed and memory — Plan for #525 (with #523)

## Problem

After #517, the frame loop no longer waits on the network. A 90 s 1080p trip
video (variable camera, 2,700 frames) still takes **200.8 ms per frame
(542 s)** on the VPS, and the overlay alone is 122.3 ms of that
(docs/VIDEO.md, "Gate G1 measurements (#517)").

The VPS profile (`bench --profile`, 748 s profiled) puts about 490 s in
`overlay._draw_route`. 378 s of that is Pillow's
`ImageDraw.line(..., joint="curve")`, which works out every rounded corner in
Python: 7.9 M pie slices, about 2,900 per frame.

#523 adds a memory problem in the same function. Every point of every
visible leg is converted to pixels and drawn, including the parts of the leg
that are off screen. At zoom 16, one 500 k-point leg peaks at about 180 MB
(361 B per point) for a single frame.

## Current state

- `src/video/overlay.py`:
  - `Overlay._lines(shot, state)` picks the visible legs (bbox test in
    `_visible`). Each leg contributes its full kept-index array at the frame's
    integer zoom level (`_Route.kept(level)`, decimated at 1.5 px), plus the
    travelled count and the marker splice.
  - `Overlay._draw_route` computes the route's pixel bbox from every kept
    point, then builds a `ROUTE_SS = 3` supersampled colour layer and a
    coverage mask over that bbox. It converts every kept point to layer
    pixels, then draws:
    - the faint line (colour drawn one pixel wider than the mask);
    - the casing;
    - the mode colour, which with the casing uses `joint="curve"`.

    It then reduces and pastes only the `_ROUTE_CELL` cells the route crosses
    (`_route_cells`).
- The goldens (`tests/test_video_renderer.py`
  `test_frames_match_golden_images` and the camera-mode goldens) compare
  320×180 frames with mean ≤ 2.0 and at most 1% of pixels off by more than 48.
- The memory tests are in `tests/test_video_memory.py`.

**Measured on the dev box** (Linux app image, `--cpus 2`, the owner's real
test trip `Trip 2026 - test.traxj`: 273 legs, 1.59 M points; paired runs;
prototypes in the session scratchpad `probe525/variants.py`):

| Option | Overlay speed-up (variable / overview / fixed) | Pixels vs today at 1080p |
|---|---|---|
| A: clip legs to the frame | 1.49 / 0.95 / 1.30 | identical on 300/300 sampled frames (2 of 900 fixed frames had 1 pixel off, see D2) |
| B: one disk per vertex instead of `joint="curve"` | 2.31 / 1.72 / 2.28 | max 247, mean 0.007, 0.032% differ, 51.7 dB |
| **A + B** | **2.76 / 1.61 / 2.50** | as B |
| C: coarser decimation (2–3 px) | 1.17–1.48 | 0.24–0.27% differ, visibly more angular |
| E: skia-python | 2.94 / 1.96 / 2.92 | 0.41% differ; +108 MB image |
| E: aggdraw | 3.49 | miter joints only: spikes on sharp turns |

With A, the 500 k-point leg at zoom 16 peaks at 0.21 MB instead of 180 MB.
The cost no longer grows with the leg's length.

## Decisions

- **D1 — A and B, in Pillow** (owner, 2026-10-02). The estimated VPS overlay
  falls from 122.3 to about 45–48 ms per frame, and the total from 200.8 to
  about 126 ms per frame. The owner ruled out:
  - **C (coarser decimation):** small gain for a visibly more angular route;
  - **E (a native library):** aggdraw can't draw round joints; skia-python adds
    ~108 MB for ~10 ms more;
  - **scaling the basemap on a worker thread:** about −11% on the dev box,
    uncertain on 2 shared vCPUs.
- **D2 — Clipping is pixel-neutral (#523).** For each visible leg,
  `_draw_route` keeps only the runs of kept points whose segments come within
  a margin of the frame. The margin covers half the widest stroke (faint line
  plus 2 px, and the casing), the supersampling, and the joint disks.
  - **Runs, not points:** a run keeps its first and last segment through the
    margin, so every segment that touches the frame is drawn exactly as
    today.
  - **Marker splice:** it is preserved inside the run that holds it.
  - **Layer origin:** clipping must not move it. The probe saw 1-pixel
    differences when the origin came from the clipped bbox, because the
    float coordinates then differ. The origin and size of the supersampled
    layers must come from something independent of which points are
    clipped. For example: the frame-clamped bbox computed from the legs'
    stored world bboxes and the run endpoints, or a fixed anchor such as
    the frame origin, with the layer covering only the cells needed.
    Implementer's choice, with the acceptance below.
  - **The criterion:** frames are byte-identical with clipping on and off, for
    every frame of the test trips in every camera mode.
- **D3 — Clipping is cheap.** Finding a leg's runs must not convert every
  kept point to pixels. The leg's kept points are scanned in world units
  against the frame's world rectangle, and the scan may skip ahead in blocks
  using per-block world bboxes cached with each zoom level's kept indices.
  The probe measured about 1.6 B per kept point per level. Memory and time
  for a frame then grow with what is on screen, not with the leg's length
  (#523).
- **D4 — Round joints as disks** (owner: a disk at every vertex). Each line
  that uses `joint="curve"` today (the casing and the mode colour, on both
  the colour layer and the mask) is drawn as one `line(..., joint=None)`
  call. Then a filled disk of that
  line's own width is drawn at every interior vertex, with
  `ImageDraw.ellipse` centred on the vertex in the same layer coordinates.
  No `pieslice` is drawn any more.
  - **Where the disks go:** line ends get no disk; today's ends are flat,
    with no joint at an end.
  - **Faint line:** today it is drawn without `joint="curve"`, so it stays
    exactly as it is.
  - **Fewer notches:** at 1080p the disks also remove notches that Pillow's
    pie-slice joints leave in the casing (probe crops).
- **D5 — Fidelity bounds, not goldens.** The goldens can't tell good
  fidelity from bad here: every variant, skia included, passed them. So B is
  held to measured bounds at 1080p against today's code, on the renderer's
  synthetic trips in every camera mode:
  - mean absolute difference ≤ 0.02;
  - ≤ 0.1% of pixels differ;
  - PSNR ≥ 48 dB.

  The probe measured 0.007, 0.032% and 51.7 dB on the real trip. The
  existing goldens must still pass unchanged, without re-recording.
- **D6 — Gate on the VPS.** Gate G1 is the same bench as #517's G1 (90 s,
  1080p, variable, the same trip). It passes when overlay ms per frame is at
  most half of 122.3 (≤ 61), the issue's acceptance. The total is recorded
  in docs/VIDEO.md.

## Review envelope

REVIEW.md §2 defaults apply. No new concurrency, external service or trust
boundary. The trip size is as in #519 (D11): up to ~1.6 M points, single
legs up to 500 k points. The renderer must stay within `worker-video`'s
memory limit.

## Boundaries crossed

None. No API, schema, stored data or export format change. Newly rendered
videos and previews differ from today's by the D5 bounds; videos already
rendered are not touched.

## Conventions

- Only `src/video/overlay.py` changes in the renderer. The marker, HUD,
  cards and basemap are untouched.
- Existing goldens pass unchanged; nothing is re-recorded.
- No new dependency.

## Open decisions

None.

## Execution units

### Wave 1 — clipping (#523)

**U1 — Draw only the on-screen runs of each leg**
- **Goal:** `_draw_route` converts and draws only the runs of each visible
  leg that can touch the frame. Frames stay byte-identical.
- **Scope:** `src/video/overlay.py` (`_Route`, `_lines`, `_draw_route`,
  new helpers); `tests/test_video_memory.py`; new
  `tests/test_video_route_clip.py`.
- **Context:**
  - D2 and D3; issue #523 (`gh issue view 523`).
  - `_Route.kept` and `_decimate` are the existing per-level cache to extend.
    `tests/test_video_memory.py` is the example for the memory test.
  - Prototype: the session scratchpad
    `C:\Users\rui_n\AppData\Local\Temp\claude\e--Dev-TraxJourney\0d219349-08db-49cb-bfcc-45805eaa8227\scratchpad\probe525\variants.py`
    (option A) and `mem523.py`. It is a sketch: its layer origin is the one
    that broke byte identity.
- **Do:**
  1. Cache per-block world bboxes with each level's kept indices (D3).
  2. In `_lines`/`_draw_route`, cut each visible leg's kept indices to the
     runs that come within the margin of the frame, keeping the travelled
     count and the marker splice right for the leg being travelled.
  3. Keep the layer origin and size independent of clipping (D2).
  4. Compute the route bbox and `_route_cells` from the runs.
- **Acceptance:** in the Linux image (CI=1),
  `pytest tests/test_video_route_clip.py tests/test_video_memory.py tests/test_video_renderer.py tests/test_video_preview_render.py`
  passes, with goldens unchanged. New tests:
  - **identity:** with clipping on and off (a module switch the test flips),
    every frame of the renderer's synthetic trips is byte-identical in every
    camera mode, at 1080p and at the 320×180 preview size. Include a leg
    that leaves and re-enters the frame, a marker on an off-screen-crossing
    segment, and the antimeridian trip;
  - **memory (#523):** one 500 k-point leg at zoom 16, fully travelled, then
    a 100 k-point one. The traced peak of one `_draw_route` is under 5 MB
    for both, so it does not grow with the leg's length;
  - **runs:** a leg that crosses the frame twice gives two runs, and a leg
    entirely off screen inside a visible bbox gives none.
- **Out of scope:** joints (U2), decimation, the basemap.
- **Latitude:** local design.
- **Escalate if:** X3; byte identity can't be reached on some frame without
  also changing today's output.
- **Depends on:** —

### Wave 2 — joints

**U2 — Round joints as disks**
- **Goal:** no `pieslice` per vertex: lines are drawn with `joint=None`,
  plus a disk at each interior vertex.
- **Scope:** `src/video/overlay.py` (`_draw_route` only); new
  `tests/test_video_route_joints.py`; `docs/VIDEO.md` (the "Overlay"
  section).
- **Context:** D4 and D5. The probe's option B in `probe525/variants.py`, as
  above. Today's `_draw_route` after U1.
- **Do:**
  1. Draw the casing and the mode colour, on both the colour layer and the
     mask, each as one `line(..., joint=None)`, plus filled disks of that
     line's own width at its interior vertices (D4). Leave the faint line
     unchanged.
  2. Keep the colour layer's "one pixel wider than the mask" rule for the
     disks too.
  3. Update the Overlay section of docs/VIDEO.md.
- **Acceptance:** in the Linux image (CI=1),
  `pytest tests/test_video_route_joints.py tests/test_video_route_clip.py tests/test_video_renderer.py tests/test_video_preview_render.py tests/test_video_memory.py`
  passes, with goldens unchanged (not re-recorded). New tests:
  - **bounds:** against the pre-U2 drawing (kept in the test as a reference
    function, or as frames recorded at the start of the unit), 1080p frames
    of the synthetic trips in every camera mode meet D5. Shown to fail if
    joints are dropped entirely (no disks).
  - **no pie slices:** `ImageDraw.ImageDraw.pieslice` is patched to raise,
    and a full render still succeeds.
  - **clipping still identical:** U1's identity test passes with the new
    joints.
- **Out of scope:** clipping, decimation, the marker and HUD.
- **Latitude:** local design.
- **Escalate if:** X3; D5's bounds are not met on a synthetic trip.
- **Depends on:** U1.

**Gate G1 (owner, after wave 2):** push the branch to `validation`. On val,
run the #517 G1 bench once more:

```
docker compose run --rm worker-video python -m src.video.bench --project "Trip 2026 - test" --owner 1 --length 90 --height 1080
```

Paste the output. It passes when `overlay_ms` is ≤ 61. The orchestrator
records it in docs/VIDEO.md. The owner also watches the rendered MP4, or the
`--dump-frames` PNGs, to check the route's look.

## Definition of done

- On the VPS bench (90 s, 1080p, variable, the test trip), `overlay_ms` is
  ≤ 61 ms per frame, against 122.3 before. The total is recorded in
  docs/VIDEO.md.
- Drawing one frame of a 500 k-point leg at zoom 16 peaks below 5 MB, and
  the peak does not grow with the leg's length (#523).
- Frames are byte-identical with clipping on and off. Against today's code,
  joint changes stay within D5's bounds. Existing goldens pass unchanged.
- No `pieslice` is drawn per vertex; no new dependency.

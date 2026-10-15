# Map load performance: audit and staged plan (issue #276)

Why the map still stutters for several seconds after the loading spinner
clears, what actually causes it, and the order in which to fix it.

Written after the round of ANR fixes that landed through #256–#290. Those
removed the hard blockages; what remains is a rhythm of short stalls during
the first ~10 s of a trip. This document is the diagnosis and the plan, not
a changelog.

## The real timeline of a cold open

`ProjectNotifier.load()` is a three-phase pipeline. Phase 1 clears the
spinner; phases 2 and 3 keep running afterwards, and that is where the
remaining jank lives.

| t | What happens | Runs on |
|---|---|---|
| 0 | `load()` clears state, notifies | UI isolate |
| ~1–2 s | `/meta` + `/geo/project/low-res` land, `jsonDecode` each | **UI isolate** |
| ~2 s | `isLoading = false` — spinner disappears | — |
| | **the user believes loading is finished here** | |
| ~2–6 s | `getGeo` → full-res GeoJSON `jsonDecode` + `expandEncodedActivities` (Google-polyline decode of every activity) | **UI isolate** |
| | ~8 progressive batches: geo swap → `notifyListeners()` → full map + panel + chart rebuild, 80 ms apart | UI isolate |
| | final pass: authoritative geo swap → another full rebuild | UI isolate |
| ~6–15 s | `getDetails` → the **~12 MB** details payload, `jsonDecode` | **UI isolate** |
| | `_buildFullTrack` / `computeElevationSpots` | worker isolate |
| | final `notifyListeners()` → full rebuild | UI isolate |

Issue #276 reports "I see the elevation curve go low-res to high-res and
after that I can move around" — that is the last row. Everything above it is
the residual jerkiness.

## Root causes, ranked

### 1. The decodes were never moved off the UI isolate — only the derived computations were

`ApiClient._handle` does a bare `jsonDecode(res.body)` for *every* endpoint,
including the ~12 MB details payload and the full-res geo. `ProjectService.getGeo`
then runs `expandEncodedActivities` — a polyline decode over potentially
300k+ points — inline immediately after.

Previous rounds correctly moved `buildFullTrackResult`, `hitTestMapTap`,
`computeElevationSpots` and `decimatePolylinePoints` behind `compute()`.
Those are the cheap half. The parse that *produces their input* is the
largest single main-thread block in the session and has no threshold guard
at all.

Corollary worth remembering: a **warm** open is smoother than a cold one,
because `project_cache_store_native.dart` gunzips and decodes via
`compute()`. The disk path is protected; the network path is not.

### 2. The progressive geo reveal is now pure cost

`_loadFullGeoProgressively` stages ~8 geo swaps 80 ms apart. That made sense
when the upgrade genuinely trickled in. Today the whole payload has already
arrived *and been parsed* before the first batch fires, so the staging buys
no perceived progress and costs 8 full-tree rebuilds — every polyline spec,
every marker spec, a `compute()` decimation hop, `ActivityPanel`,
`ElevationChart`.

This is the specific cause of the "small local blocks and unblocks" symptom:
it is a metronome of hitches roughly 640 ms wide, because the cause literally
is one.

### 3. `_waitForCameraIdle` defers jank rather than removing it

It polls every 100 ms and gives up after 2 s. Pan continuously and the whole
rebuild lands mid-gesture at t+2 s anyway. Deferral without a cheaper rebuild
only moves the stall — and the polling is itself work competing with the
gesture it is trying to stay out of the way of.

*(Fixed in Phase 2a: the wait is now event-driven off the camera-idle
callback, with the same cap.)*

### 4. One god-notifier makes every update a whole-screen update

`ProjectNotifier` is ~2700 lines with ~90 `notifyListeners()` calls, and
`app_screen.dart` wraps `ManageMapPanel` in a plain `Consumer`, so *any*
notification rebuilds the map.

The `identical()`-guard cache in `map_panel.dart` — roughly 20 `_lastX`
fields — exists purely to undo that over-broad notification. It is
compensation, not architecture, and it fails exactly when it matters: a geo
swap changes `geo`'s identity, so `geoOrStyleChanged` is true and everything
rebuilds from raw GeoJSON.

### 5. Geometry lives as untyped GeoJSON and is re-walked on every geo swap

`_buildPolylineSpecs`, `_buildActivityMarkerSpecs` and `buildFullTrackResult`
each walk `f['geometry']['coordinates']` with `as num` casts per coordinate
and allocate a `LatLng` per point.

**Correction to this document's first draft.** It originally claimed every
*rebuild* is O(total points). That is wrong: `memoCoordsToLatLng` already
memoizes the conversion on the identity of each feature's coords list, so
only a *changed* feature is re-converted. Measured on a 100-activity /
500k-point trip: a cold conversion costs ~83 ms, a warm rebuild ~0.1 ms.

The real cost is therefore one cold conversion per geo swap, not per rebuild
— which is exactly why it mattered that Phase 2a cut the number of swaps from
nine to one, and why Phase 2b is about moving that single conversion off the
UI isolate rather than restructuring the storage.

### 6. No zoom-adaptive level of detail

`kMaxTotalPolylinePoints` decimates once to a flat 6000-point budget for the
whole trip, viewport-independent. Zoomed out we render detail nobody can see;
zoomed in we have permanently lost detail. Memory markers are full widgets
with network thumbnails, built for the entire trip regardless of viewport.

### 7. Two platform caveats

- **`compute()` runs inline on Flutter web.** Every isolate guard in this
  codebase is a no-op there, and `project_cache_store_stub.dart` means web
  has no disk cache either. Web is the unprotected platform.
- The manage basemap is parked on `VectorTileLayerMode.vector`
  (`basemaps.dart`), which by that file's own comment repaints the whole
  basemap on every pumped frame at ~70 ms/frame on CanvasKit.

## The architectural take

One invariant is missing, and everything above is a symptom of its absence:

> **The client should never fetch, parse, or hold geometry at higher
> resolution than the current viewport can display.**

Today it is inverted: download the entire trip at full resolution, parse all
of it on the UI thread, then throw ~95% of it away (decimate to 6000 points,
downsample elevation to N spots). Every fix so far has been about making the
throwing-away cheaper. The long game is to not fetch it.

The machinery for that already exists. `src/tile_renderer.py` renders and
caches raster track tiles with background pre-render and edit invalidation.
`MapPanel` already accepts `trackTileUrlTemplate`, and `selectedOnly:
tilesActive` is already wired into `_stylePolylines`. It is only exposed to
share links. Promoting it to authenticated projects turns the base track into
tiles — constant client memory, instant pan, no client geometry cost — with
full-res vector geometry fetched only for the *selected* activity or day,
which is the only thing that needs hit-testing.

## Staged plan

Each phase is independently shippable and independently verifiable. The
ordering is deliberate: the later phases are much cheaper to build once the
earlier ones have simplified what they act on.

### Phase 0 — make it measurable (prerequisite)

Extend `perf_timing.dart` from percentile dumps to **named phase spans**:
`decode_meta`, `decode_geo`, `expand_polylines`, `decode_details`,
`apply_geo`, `build_specs`, `style_markers`.

*Verify:* a synthetic large-trip fixture, and a test asserting no single
main-isolate span exceeds the frame budget. Without this, every phase below
is unfalsifiable and the whole area regresses again silently.

**Reading the numbers on a real device.** Recording is on in every build, not
just `--dart-define=PERF_TIMING` ones — each span wraps a multi-millisecond
fetch, decode or build, so a Stopwatch and a map insert are far below the
noise floor of what they measure. After opening a trip, the last load's
report is in **Settings → Performance**, with a Copy button. A
dart-define-gated `debugPrint` cannot diagnose the builds that actually need
it: the ones installed on a phone with no debugger attached.

Read it as two separate things. **Blocking** spans are UI-isolate stalls —
those are jank, and anything over 16.7 ms dropped a frame. **Stage** spans
are wall clock and include isolate hops, so a multi-second `decode_geo` that
ran on a worker costs zero frames. The report says explicitly which blocking
spans, if any, went over budget.

### Phase 1 — move decode off the UI isolate

Fetch heavy payloads as bytes and do `utf8.decode → jsonDecode → domain
transform` in **one** isolate hop, returning typed data rather than a `Map`
tree where practical (a `Map<String, dynamic>` crossing an isolate boundary
is deep-copied; `Uint8List`/`Float64List` transfer near-free). Route
`getGeo`, `getDetails` and `getLowResGeo` through it, folding
`expandEncodedActivities` into the same hop.

*Verify:* `decode_geo` and `decode_details` spans disappear from the UI
thread; #276's repro — pan immediately after the spinner clears — is smooth.

### Phase 2a — drop the progressive reveal *(done)*

Delete the 8-batch reveal in favour of the single atomic swap the code
already performed as its "final pass", and replace `_waitForCameraIdle`'s
poll loop with an event-driven wait on the camera-idle callback (same cap, no
timer storm).

*Verified:* a test asserting listeners are handed at most two distinct `geo`
objects across a whole load — the low-res one, then the full-res one. On the
batched implementation the same test reports **9**.

**Rejected: `SchedulerBinding.scheduleTask(..., Priority.idle)`.** That was
the original plan — let the framework place the swap in a frame that has
spare time. Measured behaviour says no: an idle task resolves in ~3 ms in a
plain `test()` but **does not run at all under a pumped `testWidgets`
pipeline**, so gating the upgrade on one hangs the background load in widget
tests. The deeper objection is the same one in production: a busy map is
exactly the situation with no spare frame time, so the upgrade would be
starved precisely when it is most wanted. Don't retry this without new
evidence.

### Phase 2b — convert coordinates on the worker, not the UI isolate *(done)*

**Rejected: the `TrackGeometry` / `Float64List` model this phase originally
specified.** Two measurements killed it. First, the conversion is already
memoized per feature (see the correction under root cause 5), so the
"O(points) on every rebuild" problem it was designed to solve does not exist.
Second, flutter_map's `Polyline` takes a `List<LatLng>`, so typed buffers
would have to be materialised back into exactly that list on the render path
— moving the cost, not removing it. That is a large refactor across the spec
builders, `buildFullTrackResult`, `hitTestMapTap`, `extractSelectedPoints`,
the segment-overlay merge and the on-device cache, for no measured gain.

**What shipped instead**, which is the part of the original idea that
survives contact with the measurements: the decode hop from Phase 1 now
produces the coordinate→LatLng conversion alongside the parse and seeds
`map_geometry_memo.dart`'s cache with the result. Both halves ride back on
one zero-copy hop, and because the coords lists and their converted points
transfer as a single object graph, identity-keyed caching still works across
the isolate boundary. On native platforms the UI isolate therefore never pays
the cold conversion at all.

**Not on web**, where `compute` runs its callback inline on the main thread:
there the conversion is front-loaded into the decode rather than removed from
it. Web's answer is Phase 4's payload reduction, not more `compute` calls —
the same caveat as root cause 7.

*Verified:* after `decodeGeoOffIsolate`, walking every feature through
`memoCoordsToLatLng` performs **zero** conversions (counted, not timed),
against a baseline test showing an unseeded geo pays one per feature.

### Phase 3 — stop the selection path doing geometry work *(partly done)*

The phase as written was: decompose `ProjectNotifier` into independently
listenable facets, then delete `map_panel.dart`'s `_lastX` guard fields as
compensation that is no longer needed.

**Measuring first changed this too.** Instrumenting `build_specs` /
`style_markers` / `all_points` and driving a 100-activity / 500k-point trip
through a real widget pump showed the selection path costing **~24 ms per
selection change** — over the frame budget on its own. But none of it was
notification scope, and none of it was the spec builders: it was
`_cachedAllPoints`, a list of every point in the trip, derived from `geo`
alone and consumed only by the one-shot fit-to-bounds, sitting in the
*selection*-dependent half of `build()`. Every day or activity tap rebuilt it
for nothing.

Moving that one assignment into the geo-dependent half:

| | before | after |
|---|---|---|
| `style_markers` per selection change | 27.2 ms | **0.7 ms** |

Also folded in: `arcMidpoint` now rides the Phase 2b seeding path alongside
the coordinate conversion, and its per-segment `pow(x, 0.5)` became `sqrt(x)`.

**The `_lastX` guards should stay.** They are the reason a selection change
never re-enters the spec builders at all. This document previously called
them "compensation, not architecture" — they are in fact load-bearing, and
deleting them in favour of finer-grained notification would have made the
selection path slower, not faster. Splitting the notifier remains defensible
as a *maintainability* change; it is not a performance one and should not be
sold as one.

*Verified:* across six selection changes the `all_points` span records one
sample; on the previous placement it records seven.

### Phase 3b — decimate on the worker *(done — issue #299)*

`_maybeDecimatePolylines` marshalled every rendered point into
`(double, double)` records on the UI isolate and handed the result to
`compute()`. Only a `compute()`'s **return** value is zero-copy; its argument
is copied. The first real-device report made the cost of that plain:

```
build_specs       n=3  worst=2402.3ms
  decimate_marshal     worst= 788.4ms
```

788 ms of marshalling plus roughly 1.6 s of argument copy — a 2.4 s UI-isolate
stall. And because the camera-idle gate releases the geo swap after at most a
couple of seconds, that stall landed *in the middle of an active pan*, which
is what tripped Android's ANR watchdog on aggressive panning. This is exactly
the failure root cause 3 predicted and Phase 2a did not fix: deferral without
a cheaper rebuild only moves the stall.

The decode hop now produces the decimated geometry itself, using the same
aggregate budget and the same `>= 2 point` filter the spec builder applies, so
the shares come out identical. At most `kMaxTotalPolylinePoints` points come
back, so the return costs nothing. The map looks the result up per feature
instead of marshalling anything.

The async fallback stays for geometry the worker never saw: client-built E2EE
geo, and locally patched segments merged in after a load.

**The on-disk cache was a second door, and it was missed.** The first fix
seeded only geometry that arrived over the network. A trip served from
`projectDataCache`'s L2 — i.e. any open after an app restart — was decoded
straight to a Map, producing fresh coordinate lists that none of the
identity-keyed caches had ever seen, so it paid the entire cold derivation on
the UI isolate. That is the same 2.4 s stall, on the more common path, and it
is why aggressive panning still tripped the ANR watchdog after the first fix
shipped.

The stored blob is `gzip(utf8(jsonEncode(geo)))`, so gunzipping it without
decoding yields precisely the bytes the network path receives. The cache now
returns those bytes and they go through the identical parse-derive-seed hop.
L1 is still preferred when present: that Map is the very object the decode hop
seeded earlier in the session.

The lesson worth keeping: **derived-geometry seeding has to be a property of
where geo enters the app, not of one code path.** Both entry points now share
`ProjectService.readCachedGeo`.

**That fix was itself wrong, in an instructive way.** It skipped the bytes
path whenever L1 already held the ref, on the reasoning that an L1 Map is the
object the decode hop seeded. But `ProjectDataCache._readDisk` promotes an
*entire disk row* into L1, and a load reads low-res geo first — so an
unseeded full geo is already sitting in L1 by the time the check runs, and
the fix never engaged on the path it was written for. Seeding is now asked of
the geometry itself (`geoGeometrySeeded`) instead of inferred from cache
residency. Low-res geo takes the same hop, since on a long trip it is over
the render budget too.

## Measuring the gesture, not just the load

Three rounds of fixes were aimed at load-time work on the strength of
load-time numbers, and the ANR outlived all of them. On a 180-day trip the
device reported a worst blocking span of 102 ms — with Android's watchdog at
~5 s, that is an order of magnitude too small to be the ANR. Every span in
this app measured *loading*; nothing measured the pan.

`PerfSpans` now captures frame build/raster times while the map camera is
moving (driven from `setMapCameraActive`, which all three map screens already
call), and the map records what it hands the renderer each frame:
`rendered_points` and `markers`. Settings → Performance renders live rather
than from the load-end snapshot, so panning and then opening it shows the
gesture.

Those two counts matter because `flutter_map` re-walks every point of every
polyline overlapping the viewport, and repositions every marker, **on every
camera frame**. On a 180-day trip the marker count is the untested suspect:
every memory is a widget holding a network thumbnail, and nothing about that
scales with the viewport.

*Verified:* a widget test asserting the `decimate_marshal` span is **never
recorded** for geo that arrived through the decode hop, that the render budget
is still applied, and that the fallback still runs for geo that did not.

### Phase 4 — server-side level of detail (the long game)

1. **Track tiles for authenticated projects.** Generalise
   `src/tile_renderer.py` beyond share tokens; serve
   `/api/geo/project/tiles/{z}/{x}/{y}.png`. The client renders tiles as the
   base track and fetches full-res vector geometry only for the selection.
   The client plumbing already exists.
2. **Precomputed elevation profile endpoint.** Stop shipping raw per-point
   `elevation_profile` inside the ~12 MB details blob. Serve a
   distance-indexed profile downsampled to ~2000 points — the chart needs
   roughly one point per horizontal pixel. This removes the 12 MB payload,
   its parse, `buildFullTrackResult` and `computeElevationSpots` in one move:
   the entire back half of the load timeline.

*Verify:* the full-res `getDetails` disappears from the cold-open path;
measure payload bytes and time-to-interactive before and after.

**Compatibility note:** 4.2 changes the details payload contract, and there
are Android/iOS builds in the wild. Ship it as a version-gated additional
endpoint, not a breaking change to the existing one.

### Phase 5 — marker layer

Viewport-scoped marker construction, and a single `CustomPainter` layer for
un-selected memory pins instead of N widgets each holding a network
thumbnail. Revisit `MAP_TILE_MODE=raster` with phases 1–3 in place, on real
measurements rather than the currently parked assumption.

## Where this started

Phases 0 and 1 together carry the least architectural risk and address the
largest unguarded main-thread work left in the app. Phase 2's reveal deletion
is the targeted fix for the "small local blocks and unblocks" symptom. Phase
4 is where the ceiling actually lifts.


## The straight-lines report, and what a duration cannot tell you

The first gesture-instrumented run cleared the ANR: 4000 frames across 80
pans, `janky=82/4000`, worst frame 79 ms, and no UI-isolate span over budget.
But the map showed only straight lines, and the report said why once read
carefully:

```
fetch_geo      n=2  total=5119.8ms  worst=5081.5ms
fetch_details  n=2  total=5411.1ms  worst=5367.1ms
```

with no `decode_geo`, no `decode_details`, and no `geo`/`details` payload
notes. Those notes sit on the line immediately after the fetch, so they can
only be skipped if the fetch threw. `n=2` is not two fetches; it is one
failure at ~5 s plus `_loadFullGeoProgressively`'s retry failing fast.

**A span timed in a `finally` is recorded whether the body returned or
threw.** That made a failing fetch and a slow one indistinguishable, which is
the most misleading thing an instrument can do. `PerfSpans.stage` now records
failures separately and the report prints a `FAILURES` section.

### Why the two largest payloads failed

The API container is capped at 768 MB (`docker-compose.yml.example`), and
`api/geo.py`'s payload cache, shared by `/meta`, full details and both geo
variants, was bounded by an **entry count of 200** rather than by bytes.
Entries are whole gzipped project payloads and are nowhere near uniform: a
small trip's `/meta` is tens of KB, while a 180-day trip's details payload
serialises to ~35 MB before compression. Two hundred of those is gigabytes.

Issue #209's third incident was already this failure mode, the container
OOM-killed at ~779 MB with no single request to blame. Here it presented from
the client side instead: for one trip both large requests failed after ~5 s
while smaller payloads on the same project succeeded, and the retry failed
instantly, which is what an OOM-killed container looks like from outside.

The cache is now bounded by the thing that actually runs out, bytes (64 MB),
with the entry cap kept only as a guard against a flood of tiny entries and a
per-entry ceiling so one oversized payload cannot evict everything else.

This is a mitigation, not the cure. The cure is Phase 4.2: stop building a
35 MB payload at all.


## The ANR was never on the Dart isolate

Six rounds of fixes targeted the Dart UI isolate, and the ANR outlived all of
them. The instrument that finally settled it was the event-loop stall
watchdog, by reporting **nothing**: across two device runs, one of which
ANR'd, the Dart event loop was never blocked for more than ~250 ms. Every
blocking span agreed — worst was `all_points` at 7.9 ms, and
`activity_panel_build` did not even appear.

The thing to have questioned much earlier: **an Android ANR is the platform
main thread failing to handle input for 5 s, and Flutter's Dart code does not
run on that thread.** It runs on the engine's UI thread. Instruments that
only watch the Dart isolate can report all-clear through an ANR, and did.

The second run made it sharper still: an ANR with **one** map gesture, zero
frames captured, and no user interaction at all. Not gesture work, not
geometry, not marker count — `rendered_points` and `markers` stayed flat
across every run where behaviour changed drastically.

### What was actually consuming the device

`_MarkerThumbImage` drew each marker with:

```dart
Image.memory(bytes, width: widget.size, height: widget.size, fit: BoxFit.cover)
```

`width`/`height` are **layout** constraints and do not affect decoding.
Without `cacheWidth`/`cacheHeight` Flutter decodes at native resolution, and
`api/memories.py`'s `_THUMB_SIZE` is `(400, 400)`. So every marker held
roughly 400x400x4 = ~640 KB of decoded bitmap to draw a ~30-pixel circle. At
~600 memory markers that is on the order of **375 MB of native memory** —
outside the Dart heap, outside every span in this codebase, and invisible to
the stall watchdog because decoding does not happen on the UI isolate.

Passing `cacheWidth`/`cacheHeight` sized to the display box times the device
pixel ratio takes each decode to roughly 40 KB: about a 16x reduction.

Alongside it, the encoded-bytes cache behind those thumbnails was a
`static final Map<String, Uint8List>` with **no eviction whatsoever** — the
same mistake as the server payload cache, and the reason hiding the memories
layer freed nothing. It is now a `BoundedByteCache` with a byte budget.

`image_cache` and `thumb_bytes` are reported as payload notes so the next
device run measures this rather than inferring it.


## Phase 4 design rationale: reducing the payload without losing the track

Two payloads get conflated, and the distinction is the whole answer.

| | size (180-day trip) | what it is | used for |
|---|---|---|---|
| `geo` | 4.5 MB | GPS coordinates | drawing the track on the map |
| `details` | 33 MB | activity metadata + `elevation_profile` | the elevation chart, and cursor sync |

**Phase 4.2 targets `details`. Track geometry is untouched** — it stays
full-resolution, encoded as Google polylines at ~1 m precision. Shrinking
33 MB costs nothing on the map line, because the map line is not in there.

### Why `details` is 33 MB

`elevation_profile` is an array of `[distance, elevation]` pairs, one per GPS
sample, serialised as JSON text. `[12.345678, 1234.5]` is ~20 bytes to carry
two numbers that fit in 8 bytes binary, or 2-3 as deltas.

Three levers, in increasing order of how much judgement they need:

1. **Encoding — lossless.** Delta + varint (exactly what the polyline encoding
   already does for coordinates). Elevation to 1 m and distance to 1 m is more
   precision than the data actually has. Typically 5-10x smaller with zero
   fidelity change, and it is most of the win.

   The response is already gzipped, so the wire is smaller than 33 MB — but
   the client still materialises 33 MB of JSON into hundreds of MB of Dart
   objects. Binary encoding shrinks the wire *and* the decode *and* the heap.
   The last of those is what matters if the remaining freeze is GC.

2. **Resolution matched to purpose.** The chart is capped at 300 points
   (`_kMaxChartPoints`) and LTTB-downsamples whatever arrives, so megabytes
   are shipped and all but 300 discarded.

   The real caveat: the profile *is* used at higher resolution than 300, for
   the map-to-chart cursor sync. But the cursor maps distance to position, and
   the position comes from `geo`, which stays full-resolution — so the profile
   only needs enough density to resolve distance-to-elevation smoothly. One
   sample per ~25 m loses nothing visible.

3. **On demand.** A coarse whole-trip profile at load; a single activity's
   full profile fetched only when it is selected.

### The coarse tracks are a separate problem, and LOD makes them sharper

Tracks currently look like straight lines because `kMaxTotalPolylinePoints`
(6000) is shared across ~600 activities — roughly 10 points each. That is a
client *render budget*, not a payload limit: the full-resolution geometry is
already on the device.

- **Today:** 6000 points spread over the whole trip regardless of zoom. Zoomed
  into one day, that day still gets ~10 points.
- **Viewport/zoom-aware LOD:** only activities intersecting the viewport get
  budget, at the density the zoom actually resolves. Zoomed into one day, that
  day gets the whole 6000.

Which is the principle behind all of this: **LOD is not "less detail", it is
"detail where you are looking, instead of detail spread so thin it is useless
everywhere."** Server-side track tiles (Phase 4.1) are the same idea taken
further — the basemap already works this way, which is why it stays sharp at
every zoom without downloading the planet.

### Compatibility

Both halves ship as **additional endpoints**; the existing ones are not
touched. `GET /{name}` keeps serving `elevation_profile` inside the full
details payload, so the Android and iOS builds in the wild carry on working
unchanged — there is no version gate and none is needed, because nothing a
shipped client reads has changed shape.

### What already exists, and what that means for the scope

Two things found while building this, both of which shrink the work:

* **A low-res profile is already stored and already served.**
  `Activity.elevation_profile_low_res` (~300 points) lives in its own column,
  and because `/meta` defers `elevation_profile_json`, `ProjectIO._ep_pairs`
  already falls back to it. So `/meta` — 3.4 MB — *already* carries a
  chart-ready profile, and the chart renders from it before the 33 MB details
  payload arrives at all.
* **The 33 MB fetch therefore buys only cursor precision.** Its one remaining
  consumer is `buildFullTrackResult`, which pairs profile samples with geo
  coordinates to build the distance-indexed track behind the map-to-chart
  cursor. And when the profile is shorter than the coordinate list — which is
  exactly the low-res case — that function already takes its haversine
  fallback and derives distances from the geometry instead.

Which raised the question the next slice had to answer honestly: **does the
client need the full-resolution profile at all?**

**It never did — and the reason it appeared to is a bug.**
`buildFullTrackResult` paired `profile[i]` with `coords[i]` whenever there
were at least as many coordinates as profile samples. That is only correct
when the two are index-aligned, i.e. at full GPS resolution. Given a
*downsampled* profile it mapped the entire distance range onto the leading
fraction of the geometry: measured on a straight 10 km track with a 10-sample
profile, the track ended at lat 0.009 instead of 0.099 — the map cursor
pointing about a tenth of the way along.

So the low-res profile `/meta` already ships could not be used, and the 33 MB
payload existed to paper over that. The builder now always derives distances
from the geometry and scales them to the profile's total (what
`buildTrackFromPolyline` already did as a "fallback"), which needs exactly
*one* number from the profile at any resolution. Cursor accuracy follows the
geometry, which was never the limiting factor.

With that fixed, the client fetches `/elevation` instead of the details
payload, and the 33 MB, its ~9 s fetch, its ~5 s decode and its retention all
go together. E2EE trips still take the old path: their profiles are opaque
envelopes the endpoint cannot open.

One consequence worth recording: the work is now O(geometry coordinates)
rather than O(profile samples), so `_buildFullTrack`'s inline-vs-isolate
threshold had to start measuring coordinates. Gating on profile size alone
would have let a trip carrying the 300-point low-res profile walk hundreds of
thousands of coordinates on the UI isolate — precisely the pattern that guard
exists to prevent.


## Measured: the compact endpoint, and the fetch it did not remove

First device run with the compact path live:

| | before | after |
|---|---|---|
| elevation payload | 33.0 MB | **2.8 MB** |
| decode | 4884 ms | **57 ms** |

But `fetch_details` was *still there*, at 14.1 s. **View mode ran its own
Phase 2**, fetching the full details payload for elevation, entirely
independently of the inherited background upgrade — so a view-mode load
fetched elevation twice: once cheaply through the new endpoint, and once
expensively through the old one.

That Phase 2 predates the low-res profile now carried by `/meta` and the
compact endpoint, and both of the things it existed to do are now done
elsewhere. It is gone, along with the `_ViewProjectService.fetchFullDetails`
that only it called.

Worth recording as a pattern: the report named the problem in one line, but
only because `fetch_elevation` and `fetch_details` appear as *separate* spans.
Aggregate load time would have shown an improvement and hidden a duplicated
14-second fetch completely.

**Shared mode still has the equivalent Phase 2.** Its service talks to
`/api/share/{token}` and there is no share-scoped elevation endpoint yet, so
it keeps fetching the full payload; the compact endpoint 404s there and falls
back. That is the next thing to close.

## Device profiling: what 1.5 GB actually was

Five `dumpsys meminfo` captures over ~7 minutes settled a question three
rounds of inference had not.

| | A | B | C | D | E |
|---|---|---|---|---|---|
| Dart heap (`Unknown`) | 505 | 804 | 622 | 627 | 625 MB |
| `GL mtrack` | 102 | 250 | 273 | 265 | 234 MB |
| Native heap | 49 | 61 | 63 | 72 | 52 MB |
| TOTAL PSS | 798 | 1235 | 1079 | 1085 | 1037 MB |

**Nothing leaks.** Graphics fills a bounded cache and then declines; the
native heap does the same. The app reaches a stable ~1.04 GB PSS.

Two readings along the way were wrong, and both came from too few samples: at
three rising points graphics looked unbounded, and it is not. Four points was
the minimum that distinguished a filling cache from a leak.

`Unknown` is the Dart heap — it *shrank* 169 MB between captures, which fixed
mappings do not do. The Dart VM allocates through anonymous `mmap`, so
`dumpsys` cannot attribute it; it never appears under `Native Heap` (malloc,
and only ~50 MB here).

So the budget is: **Dart heap ~625 MB steady and ~804 MB during load**,
graphics ~320 MB at its plateau, everything else ~120 MB. The Dart heap is
both the largest component and the one that spikes, and GC over a heap that
size is what produces the multi-second frames. Graphics is map surfaces and
is not reducible.

That rules out raster track tiles (Phase 4.1) outright: they would move
geometry out of the Dart heap and into the one pool already at its ceiling.
Zoom level of detail on vector geometry is the answer instead.

## Phase 4.1 replaced: zoom level of detail

`GET /api/geo/project/simplified?name=…&zoom=…` returns geometry
Ramer-Douglas-Peucker-simplified to about one screen pixel at that zoom. The
client holds what it asks for, so the size of the client's geometry becomes a
function of what is on screen rather than of trip length.

The algorithm is the one already used to trim MOTIS shape points, moved to
`src/models/simplify.py` and shared — including its equirectangular
correction, which matters here for the same reason it mattered there: a
degree of longitude is ~63% of a degree of latitude at European latitudes, so
an unprojected epsilon over-simplifies north-south.

Additional endpoint; `/project` is unchanged, for shipped clients and for
anything that genuinely needs every point (track editing, export).

### The client half

The load fetches geometry for the zoom about to be shown, and refetches when
the zoom *bucket* changes — quantised to whole levels, so a pinch through
several of them causes one request rather than one per frame, and a pan within
a level causes none. The refetch waits for the camera to settle, for the same
reason every other background swap does.

Three things that had to be got right, all of which review caught:

* **Stamp the bucket that was requested, not the one in effect afterwards.**
  The fit-bounds animation runs during the fetch and the camera-idle wait, so
  reading the current zoom after the awaits records a level that was never
  fetched — and then never refetches it. That is the common path on every
  trip open, not an edge case.
* **Never carry a bucket across projects.** `ProjectNotifier` is a single
  app-wide provider. A bucket left from the previous trip arms refetching for
  geometry it has nothing to do with, and on an E2EE trip — whose geo is built
  client-side and which the server cannot serve at all — the refetch would
  replace the route with an empty FeatureCollection. Zoom refetching is now
  disarmed on `load()` and `clear()`, and never armed while encryption is
  unlocked.
* **A zoom change is not an authoritative snapshot.** The load path reconciles
  the segment overlay against the server's view; a zoom refetch must not, or a
  cached payload mentioning a segment id clears a pending patch and reverts an
  optimistic edit on screen.

Shared/public viewers keep the full-resolution path: there is no share-scoped
simplified endpoint, and the owner one is auth-gated on a real project name,
so calling it with a share token would 401 on every shared load.

### Still to do

* **A bounding box** — *done, see Round 9 below.*
* **Two known regressions**, filed as #317: the on-device full-geo cache is no
  longer seeded (offline reopen degrades to low-res), and export/share
  rendering reads the now-simplified geometry from the notifier rather than
  fetching full resolution.

## Measured: zoom LOD works, and the freeze does not care

First device run with the client half live:

| | before | after |
|---|---|---|
| `geo_coords` | 1,465,345 | **515** |
| geo payload | 4.5 MB | **284 KB** |
| Dart heap (`Unknown`) | 625 MB | **210 MB** |
| TOTAL PSS | ~1,037 MB | **582 MB** |
| peak RSS | 1733 MB | **847 MB** |

The memory problem is solved: the client no longer holds a trip's worth of
geometry to draw a screen's worth.

**And the freeze survived it** — `worst build 2640ms` at 779 MB, roughly half
the memory at which it used to happen. Garbage collection over a large heap
was the leading hypothesis for three rounds and the data has now falsified it.
No instrumented span exceeds 16.7 ms, and `buildDuration` covers build, layout
*and* paint, so the time is inside the framework's own work on a tree this app
does not time.

### Two regressions the same run exposed

* **21.6 s worst fetch**, 4 fetches totalling 55.8 s. Simplification ran per
  request over the whole trip, and the 60 s cache TTL added earlier — to stop
  these entries evicting everyone else's — guaranteed repeated cold builds.
  Measured: Ramer-Douglas-Peucker over 200k wiggly points takes 1.29 s at zoom
  13, so ~10 s across this trip. Bounded now by striding to at most 4,000
  points per line before the RDP pass: 0.04 s for the same input, and the
  guarantee it costs (every dropped point provably within tolerance) is not a
  distinction the screen can render at a GPS sample every few metres.
* **515 coordinates for 219 activities** — 2.4 per activity, a straight line
  per leg. Correct by the tolerance and useless as a map. There is now a floor
  of 32 points per line, chosen against what the client renders rather than
  against the tolerance: its own 6,000-point budget worked out at ~27 per
  activity for this trip, and a floor below what the renderer would have drawn
  anyway is a regression however defensible the arithmetic.

### Instrumenting the frame itself

A multi-second frame with no span over budget says the time went somewhere
unmeasured — but not whether that frame was doing any of our work at all. The
report now names the instrumented spans that ran between one frame's timings
and the next, for the worst frame:

```
[perf] worst frame ran: (no instrumented work)
```

An empty context means the cost is entirely framework layout and paint —
which, with everything else now cheap and 586 markers on screen, points at
`flutter_map`'s per-frame marker work and issue #296. A named span means
something of ours is running alongside it. Either way it is the first
instrument that distinguishes the two.
## Round 8 — what the frame context caught: `compute()` copies its argument

The instrument paid for itself on the first run. The worst frame came back
named:

```
[perf] worst frame ran: elevation_chart_build
[perf] frames=678  build p50=6.0 p90=13.2 p99=42.7 max=2436.6ms
                 | raster p50=5.8 p90=10.7 p99=15.4 max=25.4ms
[perf] no UI-isolate span over 16.7ms
```

Three facts, together, locate the bug exactly:

* The frame ran `elevation_chart_build`, and **only** that — no `build_specs`,
  no `style_markers`. So this is the frame in which the elevation data lands
  and the chart rebuilds.
* That span measured **0.3 ms**. The cost is in the same frame but outside it.
* Raster peaked at 25.4 ms, so it is not the GPU. It is UI-thread work.

`_compute` is called from `didUpdateWidget`, which runs in that frame but
*before* `build()` — outside the span that wraps `build()`. That is precisely
how a frame reads 2437 ms while its only named span reads 0.3 ms.

### The mechanism

`compute()` runs a function on another isolate, and **copies its argument to
get it there**. The copy is done by the *calling* isolate: the UI one.

Two per-trip computations were being handed the whole `activities` list:

```dart
compute(computeElevationSpots, (activities: activities, selectedId: null))
compute(buildFullTrackResult,  (geo: geo, activities: activities))
```

Every activity map carries `elevation_profile`, shaped `[[distKm, elevM], …]`
— **one Dart list object per sample**. This trip's profiles are ~700,000
samples, so each call serialised on the order of a million small objects
before any work began. Measured: **2437 ms**, on the UI isolate, to move work
*off* the UI isolate.

This is the same trap already hit and fixed once in this investigation, in
`build_specs` (2402 ms of marshalling for the decimation hop) — the same
magnitude, which is not a coincidence. Moving a computation to an isolate is
not free, and the price is the size of its argument, not the size of its work.

### Why nothing caught it

Three separate blind spots lined up:

* `blocking()` spans wrap the *body* of the work. The copy happens at the hop,
  outside any of them.
* `stage()` spans are wall clock, so an isolate hop looks the same whether it
  blocked the UI isolate or not.
* The payload report counted `full_track_points`, `geo_coords` and
  `dart_structs_est` — but **never elevation samples**. `dart_structs_est`
  read `~3 MB` while ~700k profile points sat outside the estimate entirely.

The report now counts them (`elevation_points`), because the thing that isn't
measured is the thing that grows.

### The fix

The two calls needed opposite treatments, and the difference is the point.

**The track builder needed almost nothing.** Since the zoom-LOD work of issue
#295 it derives distances from the *geometry* and scales them to the profile's
total — so from each profile it reads exactly **one number**, `elevTotalKm`.
It was copying 700,000 points to read 219 doubles. It is now given an
`ActivityTotals`: one `(id, elevTotalKm)` record per activity. The profiles
never cross the boundary at all.

**The chart genuinely needs the samples** — it concatenates them all and
LTTB-downsamples to 300 spots. So they still cross, but as a `Float64List`
per activity instead of nested lists: the same numbers as one typed buffer,
which copies as a memcpy rather than object by object.

Both nested entry points (`computeElevationSpots`, `buildFullTrackResult`)
were kept as thin delegating wrappers, since they are the readable contract
the existing suite pins. `test/isolate_payload_test.dart` compares the flat
implementations against a reference implementation of the original nested
walk, because comparing them against the wrappers that now delegate to them
would prove nothing.

The subtle invariant worth naming: an activity whose samples are *partly*
malformed contributes no spots for them but **still advances the running
distance offset** by its raw last entry. Flattening had to preserve that, or
every activity after it would shift along the x axis.

### What this does and does not claim

It removes a measured 2437 ms of UI-isolate work, and it is the first
explanation in this investigation that accounts for every number in the report
rather than being consistent with some of them. It should also fix the
"panning blocks when the low-res tracks are replaced" symptom, since every
zoom-bucket change calls `_buildFullTrack` and therefore paid this copy again.

It is not yet proof the freeze is gone. The next run says: `elevation_flatten`
and `elevation_totals` are now spans, so their residual cost is visible, and
the worst-frame context will name whatever is left.

### The adversarial review of that fix, and what it caught

Two blockers, both from one structural mistake: the nested wrapper flattened
**before** filtering by `selectedId`.

The original walk filtered first and then walked only the surviving profile.
Flattening first inverted that, and the selected-activity path is the *inline*
branch — the one deliberately taken without any size threshold, on the
assumption that "a single selected activity's profile is bounded". That
assumption was still true of the resulting *series* and no longer true of the
*work*: tapping an activity on the measured trip would have walked all ~700k
samples and allocated ~11 MB of `Float64List` across 219 buffers to keep one,
synchronously, inside `didUpdateWidget`. The fix moved a 2437 ms frame off the
elevation-landing path and would have created a new one on every activity tap.

The second blocker is the same bug seen from a different angle, and the reason
it is a correctness issue rather than only a performance one: flattening
activities the filter would have excluded means *reading* their samples, so a
malformed entry in an activity the user did not select now throws — out of
`initState`/`didUpdateWidget`, synchronously, into the build phase. The suite
already pinned that such input must not throw.

Both are fixed by filtering before flattening, and `computeElevationSpotsFlat`
no longer takes a `selectedId` at all: selection is the caller's business, and
removing the parameter removes the chance of getting the order wrong again.
`test/isolate_payload_test.dart` pins the ordering with a *poisoned
neighbour* — an activity whose samples would throw if read — which makes "did
not touch the other activities" observable rather than merely faster.

The review also caught a **pre-existing** bug in the same method:
`_computeGen` was bumped only on the isolate branch, so an inline call could
not supersede a hop already in flight. Select an activity while the full-trip
hop is running and the stale full-trip series lands afterwards and overwrites
it — leaving the chart showing the whole trip while `widget.track` is the
per-activity track, so the cursor mapping disagrees too. This is the exact bug
already fixed in `ProjectNotifier._buildFullTrack`, whose comment names the
scenario; the chart was its un-fixed twin. The counter is now bumped before
the branch.

One more, worth stating because it cuts against the headline: **on web this
change would have been a pure cost.** `compute` has no isolate to hop to
there and runs its callback inline, so there is no argument copy to save, and
flattening would simply have added a second full pass over every sample on the
main thread. Web now takes the single-pass inline path explicitly. The 2437 ms
saving is Android/iOS only.

#### One finding rejected

The review argued that `decodeElevationProfiles`' *result* — a
`Map<String, List<List<double>>>`, i.e. the same ~700k nested lists — is
copied back onto the UI isolate by `compute`, and that this is the larger half
of the same bug.

It isn't. `compute` is `Isolate.run`, whose result "is sent using `exit`,
which means it's sent to this isolate without copying"; `Isolate.exit`'s
object graph is reassigned to the receiving isolate, "in most cases … in
constant time". Arguments are copied, results are not — which is exactly why
this whole class of bug is asymmetric and easy to miss.

What survives from that finding is a *memory* point, not a frame-time one:
those ~700k two-element lists really are allocated and really do end up in the
UI isolate's heap. That is what the new `elevation_points` counter measures,
and a flat representation for `elevation_profile` would remove it — but it is
a heap question for the 16 call sites that read `[[distKm, elevM], …]`, not
something to smuggle into this fix.

Also deferred, and now tracked as **issue #321**: viewers arriving through a
share link have no simplified endpoint, so `geo` crosses the boundary at full
resolution for them (~1.46M nested lists — more than the elevation payload
removed here). The claim that this fix addresses the "panning blocks when
low-res tracks are replaced" symptom holds for the owner path only.

The scope there is narrower than "unauthenticated means no LOD":
`/api/geo/project/simplified` already takes the same `owner` parameter and
auth dependency as `/api/geo/project`, so a signed-in collaborator opening
someone else's trip by name is already covered. It is the token-scoped routes
in `api/share.py` — the public ones — that have no zoom-aware equivalent.

## Round 9 — the map's own layout and paint, and the extent the zoom never bounded

Three changes that only make sense together, in this order.

### 1. Nothing measured flutter_map

The report's most useful line was also its most frustrating:

```
[perf] worst frame ran: (no instrumented work)
```

An empty context means the 2437 ms went somewhere none of this app's spans
wrap. But "somewhere" was as far as it went, because
`FrameTiming.buildDuration` covers build, layout *and* paint as one number,
and every `perfSpans.blocking()` span in this codebase wraps a **build**
callback we wrote. Layout and paint — where flutter_map re-walks every point
of every polyline overlapping the viewport, and repositions every marker, on
every camera frame — were structurally invisible.

`PerfSubtree` is a `RenderProxyBox` that times `performLayout` and `paint` of
the subtree it wraps. It is layout-transparent (same constraints, same size,
same paint offset) and costs one bool read when recording is off. The map
subtree (`map`), the track `PolylineLayer` (`map_lines`) and all five marker
layers together (`map_markers`) are wrapped in both map panels.

Two things it had to get right:

* **Aggregate per frame, not per call.** `paint` can run several times in one
  frame. A per-call sample list recorded at 60 Hz would also have been the
  third unbounded cache in this investigation, after the server payload cache
  and the thumbnail cache — so only `(count, total, worst)` is kept per name,
  which is O(names) forever.
* **Print it next to what was being drawn.** `rendered_points` and
  `map_lines_paint` mean very little apart and quite a lot together. The
  report now puts them on the same line.

The worst-frame context can now name `map_paint` instead of saying nothing.

### 2. The client cap was undoing the server's work

```
geo_coords       13273   <- what the server sent, pixel-accurate at that zoom
rendered_points   6029   <- what kMaxTotalPolylinePoints let through
```

Since the zoom LOD of #295 the server has been simplifying to about one screen
pixel and the client has been throwing away 55% of the result — so the drawn
track was roughly twice as coarse as the line that arrived, and the server's
own floor (`min_points`) had to be justified against the *client's* budget
rather than against what the map needs. Two resolution policies were
disagreeing and the coarser one won.

Raised to 40,000, against the server's guarantee rather than a round number:
at most 4,000 points per line at ~1 px, and the largest trip measured came
back as 13,273 for its entire length. It still catches what it exists for —
the full-resolution fallback path hands the renderer 1,465,345 points on that
trip.

Kept as one trip-wide budget. A per-activity split was considered (6000 over
219 activities is ~27 each whether the leg is 2 km or 200 km) and rejected:
that allocation is exactly what the server now performs, with the geometry,
the zoom and the viewport to go on.

### 3. Zoom bounds the detail; it never bounded the extent

This was already written down as "still to do" after #295, and it is what
makes the deep-zoom case bounded in both directions. `GET
/api/geo/project/simplified` now takes `bbox=minLon,minLat,maxLon,maxLat`.

Measured on a synthetic 219-activity, 1,467,300-coordinate trip strung across
Europe, with a viewport one screen wide:

| zoom | no box | with box |
|---|---|---|
| 9 | 0.25 s / 7,008 coords | 0.10 s / 7,008 coords |
| 12 | 3.81 s / 19,949 coords | 0.17 s / 7,124 coords |
| 15 | 4.02 s / 66,418 coords | 0.18 s / 7,544 coords |

That reproduces the shape the device reported — `fetch_geo_lod worst 6841 ms`
— and it is issue #324 directly: simplification cost rises with zoom while the
saving falls, because the endpoint was simplifying a whole trip to draw one
screen. Note also the 66,418 at zoom 15: **above the raised client cap.** The
box is what keeps the payload under the valve, which is why raising the cap
without this would have been reckless.

Four decisions worth keeping:

* **A feature is never dropped.** An off-box line is reduced to the
  `min_points` floor — *exactly* what a whole-trip zoom already gives it — and
  skips the Ramer-Douglas-Peucker pass. Dropping it instead would have broken
  three things that read `geo` as a description of the whole trip: the
  segment-overlay reconciliation (a tombstone is cleared when the server geo
  no longer mentions its id), fit-to-bounds, and the export path. The
  equivalence to a coarser zoom is the entire safety argument, and it is
  asserted rather than described.
* **The box is snapped to the tile grid at the level, by the server.** Raw
  viewport floats mean a cache entry per pan pixel, and this project has
  already OOM-killed its API container once with a payload cache bounded by
  the wrong thing (#209's third incident, and again from the client side in
  #276). The client snaps too, and uses its own snapped box to decide when to
  refetch; the server snapping again is what makes an older or hostile client
  unable to mint entries. Snapping is outward, so the answer is always a
  superset and pads for free.
* **The intersection test short-circuits on the first point.** `line_bbox`
  walks every coordinate, and at whole-trip zoom — where every line is on
  screen and the box removes nothing — that walk is pure added cost: measured
  0.24 s to 0.34 s before the short-circuit, 0.25 s after, i.e. free. A line
  whose first point is outside still pays the walk, and there it buys skipping
  RDP.
* **The initial load sends no box, and that falls out rather than being a
  special case.** The notifier has no camera box until the map's first event,
  so the first fetch is the whole trip at the load zoom — which is what
  fit-to-bounds and the whole-trip elevation cursor are built from. Scoping
  engages from the first zoom-bucket change onwards.

Refetching now triggers on the camera leaving the fetched box as well as on
the level changing, debounced and camera-idle-gated exactly as the zoom
refetch already was. The previous geometry stays on screen throughout; a
failed refetch means slightly wrong detail, never a blank map.

### What this does NOT fix

* **Issue #317 is not resolved, and is marginally deepened.** The on-device
  full-geo cache is still not seeded by the simplified path (offline reopen
  still degrades to low-res) — unchanged, nothing new is written or skipped.
  The export/share path still reads the simplified geometry out of the
  notifier, and now, when the user is zoomed in, off-screen activities in it
  are at the `min_points` floor rather than at the current zoom's detail. That
  is bounded by the equivalence above — it is the resolution a whole-trip zoom
  already produces, which is roughly the zoom an export renders at — but it is
  a deepening and should be stated as one. The fix is #317's: those paths
  should fetch full resolution rather than read the map's copy.
* **The first fetch of a deep-zoom URL is still whole-trip.** Arriving on
  `/app?...&zoom=15` fetches the entire trip at zoom 15, because no camera box
  exists yet. Seeding one from the route's centre and zoom plus the map's size
  would close it; it needs the map to be laid out first, so it is a separate
  change.
* **Shared/public viewers still have no simplified endpoint** (issue #321), so
  none of this reaches them.

### What the next device run should say

* `map_lines_paint` / `map_markers_layout` / `map_paint` worst values, against
  `rendered_points` and `markers` on the same line. This is the number that
  says whether 40,000 is affordable — and if it is not, the fix is the
  server's tolerance, not the client's valve.
* `worst frame ran:` naming something instead of `(no instrumented work)`.
* `fetch_geo_lod` worst, which should stop scaling with zoom.
* `geo_coords` after a zoom-in, which should stop scaling with trip length.

## Round 9 — the LOD endpoint, and measuring the right half

Two runs after the viewport work, the client was healthy (worst frame 176 ms,
zero over 500 ms, `dart_structs_est` ~3 MB) and the load was not:

```
fetch_geo_lod  n=3  total=16549.0ms  worst=6869.5ms
[perf] FAILURES
  fetch_low_res_geo  x1  ApiException(502)
  fetch_meta         x1  ApiException(502)
```

No OOM kills on the server. The explanation is simpler and was in the
Dockerfile all along:

```
exec uvicorn api.router:app --host 0.0.0.0 --port 8000
```

**One process, no `--workers`.** The geo endpoints are sync `def`, so FastAPI
runs them in a threadpool — but they are CPU-bound *Python*, so the GIL means
a multi-second build starves everything else in the process. That is one
mechanism for both symptoms: the slow fetches are the builds, and the 502s are
unrelated requests arriving while a build holds the GIL.

### Where the time actually goes

Measured for a 219-activity trip at ~6,700 points each:

| phase | cost |
|---|---|
| decode every activity polyline | **1.83 s** |
| build the GeoJSON features | 0.33 s |
| simplify to a level | 0.3 s – 7 s (with track wiggliness and zoom) |
| gzip the *simplified* result | ~0.06 s |

The decode is a fixed floor paid on every cache miss, before anything is
simplified. Which means the cost is **per build**, and the number of builds is
what matters.

### What issue #325 got wrong

The viewport work made each build cheaper by skipping the RDP pass for
off-screen lines — but it put the box in the cache key, so it also made builds
*more numerous*: every pan to a new tile range was a fresh miss, paying the
1.83 s decode again. It optimised the minority half and multiplied the
majority.

This is the second time in this investigation that a fix targeted the wrong
half of a cost. The first (`compute()` copying its argument) was caught by an
instrument; this one was caught by profiling the phases before designing —
which is the cheaper order.

### The split

A **level** is expensive and box-independent. A **box** is cheap and
box-specific. So the level is what gets cached, and every box is served from
it:

* `simplify_geo_features(features, zoom)` builds a level with no box in it.
* `restrict_to_bbox` is a separate pass that floors off-box lines, run over
  the already-simplified result.

Flooring an already-simplified line rather than the original yields the same
point count and the same endpoints, from geometry that is off screen by
definition; visible lines are untouched, so what the user sees is identical.

Measured on the same synthetic trip:

| | cost |
|---|---|
| cold level build (decode + simplify) | 5.27 s |
| serving another box from that level | **0.056 s** |

**94× for a pan**, and the box no longer multiplies cache entries — there is
one entry per zoom level, bounded in coordinates because that is what both the
memory and the cost scale with, and generation-checked like the byte cache so
an edit still busts it.

### What this does not fix

The *first* visit to a zoom level still pays the full build, and zooming
through levels still pays one per level. Removing that needs either the decode
cached in a compact form (~23 MB as typed arrays for this trip, against ~150 MB
as Python lists) or the levels precomputed off the request path — and
precomputing inside this process would starve it exactly as the request-time
build does, so it belongs in the worker role the image already has (#173).
Tracked in #324.
## Round 10 — the fetch that already had its answer

```
elevation_points   1465345
elevation          2.8 MB
fetch_elevation    4360.0ms
elevation_flatten  n=2 total=115.5ms worst=113.1ms   <- the last span over 16.7ms
```

1,465,345 samples to draw 300 spots and read 219 doubles. Issue #323 proposed
downsampling `GET /{name}/elevation` to chart resolution, mirroring #295.

**It did not need writing.** `/meta` has carried a ~300-point profile per
activity since the low-res-first work: `ProjectIO.to_dict`'s `_ep_pairs` falls
back to `elevation_profile_low_res_json`, and the lightweight load never
defers that column. The client was already rendering from it — `loadView` and
`loadShared` say so in as many words — and then spending 2.8 MB and 4.3 s
replacing it with data no consumer can tell apart:

* the chart LTTB-downsamples whatever it is given to `_kMaxChartPoints` = 300,
  and a *selected* activity is capped at 300 too;
* `buildFullTrackFromTotals` reads one number per activity, the profile's last
  distance — which `downsample_elevation` keeps exactly, along with the first
  point and both global extremes.

The reason it used to be necessary is gone. Before #295 the track builder
paired profile samples with geometry *index-wise*, so a 300-point profile
mapped the whole distance range onto the leading 300 coordinates of the track.
It now derives distances from the geometry and scales them to the profile's
total, and a total is all it takes.

So the fix is one guard on the Phase-2 upgrade: **skip it when the meta load
already supplied profiles.** No server change, no new downsampler, and the
2.8 MB fetch and its decode disappear rather than shrinking.

Measured in the test container (desktop-class, so roughly half the device's
times — the device read 113 ms where this reads 54):

| | 219 × 6,690 samples | 219 × 300 samples |
|---|---|---|
| `flattenProfiles` | 54.2 ms | **1.2 ms** |
| `computeElevationSpotsFlat` | 205.7 ms | **4.8 ms** |

`elevation_flatten` lands ~2.5 ms on the device: under budget by 6x.

**The isolate hop stays.** Inline it would now be 1.2 + 4.8 = 6 ms here, so
~13 ms on the device — under 16.7 ms, but not by enough to be worth spending
the whole margin on, and `_kInlineComputeThreshold` = 5,000 keeps 65,700
samples on the isolate anyway. The hop's *argument* was the problem, and at
this size it is a 1 MB `Float64List` memcpy.

### What this deliberately does not touch

* **E2EE trips still fetch the details payload.** Their low-res profile does
  arrive through `/meta` (as the ciphertext `_revealActivities` decrypts), so
  elevation alone would not need it — but that payload is also the only
  load-path source of a *decrypted* `map.summary_polyline`, which
  `_openTrackEditor` reads off the panel's copy. Skipping it would send the
  editor to `/activities/{id}/track`, which answers with the envelope, and an
  encrypted trip would quietly lose track editing. That is a separate change
  with a separate argument.
* **`GET /{name}/elevation` is unchanged**, and still serves full resolution.
  It remains the fallback for a server with no low-res column, for a trip
  whose profiles failed to decrypt, and for one that genuinely has no
  elevation — all three of which send no profiles through `/meta` and so still
  fall through.
* **`_silentReload` still re-fetches the full details payload** after an
  activity CRUD, so `elevation_points` goes back to full resolution until the
  next load. Pre-existing, and unchanged by this.

## Round 11 — the unit of caching was the level, and it should have been the line

```
fetch_geo_lod  n=6  total=37636.4ms  worst=8577.6ms   (geo_lod 233 KB)
geo_refetches  6
```

Six distinct zoom levels in one session, each a cold whole-trip build at 6 to
8.5 s, for a payload of 233 KB. The client half was healthy — `geo_refetches`
matches `fetch_geo_lod` exactly, so the box contract from #334 held and there
was no refetch loop. All of it was server build cost.

### The trade that produced it

* **#325** put the viewport box in the cache key. Each build was **cheap** — it
  skipped the Ramer-Douglas-Peucker pass for every line the box could not show
  — but no build was reusable, so every pan to a new tile range paid a fresh
  one, decode included.
* **#331** took the box out of the key. One level then served every viewport,
  and a pan cost 0.056 s instead of a rebuild. But a box-free build cannot skip
  anything, so every cold level ran RDP over the whole trip.

Panning got much better; zooming got worse; the measured session was
zoom-heavy. Both rounds cached the same wrong thing: a *level*.

### The line is the right unit

A **line** simplified to a level is box-independent, so it is shareable the way
a level is. And which lines are worth simplifying is exactly what the box
decides, so a build is cheap the way a per-box one was. Both properties at
once, from the same object.

`api/geo.py` now caches a **prepared trip** per project instead of a level per
zoom. Per line it holds:

* the **working set** — the line already strided to `max_input_points`, which
  is all `simplify_for_zoom` ever reads, so simplifying from it is *identical*
  to simplifying from the original at every zoom. Held as a flat `array("d")`:
  16 bytes per coordinate against the 128 a `[lon, lat]` list costs, which is
  15 MB rather than 112 MB for the 219-activity trip;
* its **bounding box**, so "can this box show it" is O(1) rather than a walk
  over every coordinate;
* its **floor**, the reduction an off-screen line is served at.

Simplification results are memoised per `(level, line)`. A level therefore
fills in incrementally as boxes bring lines on screen, and the decode — the
fixed 1.83 s floor paid before anything is simplified — happens once per trip
rather than once per level.

`_LEVEL_CACHE_MAX_ENTRY_COORDS` is gone with the level cache. It existed
because a whole-trip level at deep zoom approaches
`len(activities) * max_input_points`; at 219 × 4,000 = 876,000 coordinates the
trip this document measures exceeded the 250,000 cap and so was **never cached
at all** at deep zoom. Nothing holds a whole trip at deep zoom now.

### Measured

Synthetic 219-activity trip at 6,700 points each, viewport tracking the zoom,
same machine, back to back. Every request is a distinct viewport or level; the
byte cache is never the thing being measured.

| scenario | before (#331) | after | |
|---|---|---|---|
| six distinct levels, one session | 22,736 ms | **2,209 ms** | 10.3× |
| six boxes at one level, off the trip | 3,310 ms | **2,015 ms** | 1.6× |
| six boxes along the trip, each revealing new activities | 3,330 ms | **1,884 ms** | 1.8× |
| three levels with the whole trip on screen | 6,644 ms | **4,040 ms** | 1.6× |
| three levels, no box at all (older client) | 7,880 ms | **5,511 ms** | 1.4× |
| cache held after the zoom sweep | 16.6 MB | 15.5 MB | |

Per request, the second and later levels of a sweep fall from 3.1–4.3 s to
**36–63 ms**. The pan-along-the-trip case is the adversarial one — every step
brings new activities on screen, so every step does real work — and it costs
34–46 ms, indistinguishable from the pan that reveals nothing.

**What got worse.** The first, cold request now also packs the working sets and
precomputes the bounding boxes and floors: 135 ms of new work for this trip
(104 ms packing, 31 ms width checking), against ~55 ms of `line_bbox` walking
it removes. With the whole trip on screen — where the box saves nothing and the
extra preparation is pure cost — the cold build measured 2,000 ms before and
2,258 ms after, about 13% worse. With a box that scopes anything, the cold
build is *faster* (3,363 → 1,999 ms at zoom 9), because the box now skips RDP
on a cold build too, which #331 could not.

### Memory

Bounded in bytes rather than coordinates, since the cache now mixes typed
arrays with Python lists:

* 48 MB across the whole cache, checked after every store and every memoise;
* 32 MB of working sets and floors for one trip — 2,000,000 coordinates, i.e.
  500 activities at the 4,000-point cap. A trip past that is not cached, and
  logged rather than silently rebuilt forever;
* 16 MB of memoised results per trip, bounded separately because that is the
  part that grows *after* the entry is stored, as a session visits levels.

The lone-entry escape means one entry may reach 32 + 16 = 48 MB, so the steady
ceiling is 48 MB either way, and ~96 MB for the instant of a store, since the
new entry is inserted before eviction brings the total back inside budget.
Against the level cache's 400,000 × 128 = 51 MB steady and ~103 MB transient.
Per process; `--workers N` multiplies it.

### What this does not fix

* The **first** request on a trip still decodes it. That cost is now paid once
  per trip rather than once per level, but it is still on the request path, on
  a single-process uvicorn where it holds the GIL. Precomputing in the worker
  role (#173) is still the only thing that removes it.
* `restrict_to_bbox` and `simplify_for_zoom`'s `bbox` argument are no longer on
  the request path — the box is applied by choosing which lines to simplify,
  not by reducing an already-simplified level. They are kept as the library
  statement of what flooring means, and their tests still pin it.

## Round 12 — the flatten that replaced the copy

```
geo_flatten  n=2  total=22.9ms  worst=22.6ms
[perf] OVER BUDGET (>16.7ms on the UI isolate): geo_flatten
```

Round 8's lesson applied once more: `compute()` copies its *argument*, so
issue #332's last fix stopped handing `_buildFullTrack`'s isolate the raw
GeoJSON — one Dart list object per point — and handed it one `Float64List` per
activity instead. That removed a ~150 MB transient copy and left the flatten
itself on the UI isolate, where it became the only span over the frame budget.

The trade was still favourable. It was also unnecessary: `geo` is *already*
decoded on a worker (`decodeGeoOffIsolate`), which already derives the map's
points, arc midpoints and decimated geometry there and seeds them back into
the identity-keyed caches in `map_geometry_memo.dart`. A `compute()` return
value is not copied — `Isolate.exit` reassigns the object graph — so anything
that worker produces arrives free. The flatten is now produced there too and
seeded the same way; `flattenGeoCoords` on the UI isolate became O(features)
cache lookups instead of O(points) of arithmetic.

Measured in the test container (desktop-class, so roughly half the device's
times — the device read 113 ms where this reads 54 in Round 10):

| coordinates | flatten, cold | flatten, seeded |
|---|---|---|
| 7,000 | 0.72 ms | 0.04 ms |
| 100,000 | 9.5 ms | 0.07 ms |
| 1,465,110 | 117 ms | 0.07 ms |

The seeded column is flat because it no longer depends on the number of
points at all — 219 map lookups, whatever is behind them.

### Where the skips live now

`flattenGeoCoords` skipped non-Map features, segments, features with no
`activity_id`, and empty coordinate lists. The worker has to skip *exactly*
the same ones: one it wrongly skips costs a cache miss, one it wrongly
flattens retains a buffer nothing ever reads. So the predicate moved into
`trackFeatureCoords`, called by both, returning the coords list as well as the
id so neither side re-derives it.

A miss is a fallback, not a failure: geometry that never went through that
worker — client-built E2EE geo, a locally patched feature — flattens on demand
exactly as before. Slower, never empty.

### `kInlineFullTrackThreshold` re-checked, and left alone

The worry on #337 was that viewport scoping has brought a typical `geo` to
~7,000 coordinates, just over the 5,000 threshold, so the hop is taken for a
payload it may no longer pay for. Measured on a seeded geo, per branch, in the
container:

| coordinates | inline branch, UI stall | isolate branch, UI stall | hop, wall clock |
|---|---|---|---|
| 5,000 | 4.78 ms | 0.065 ms | 6.6 ms |
| 7,000 | 5.69 ms | 0.014 ms | 7.1 ms |
| 10,000 | 7.98 ms | 0.012 ms | 9.9 ms |
| 20,000 | 19.01 ms | 0.010 ms | 28.4 ms |

The hypothesis is false, and it is this change that falsifies it. The hop's
wall clock is longer than the inline build at every size — it always was — but
almost none of it lands on the UI isolate any more: what used to make the hop
expensive *there* was the flatten in front of it, and that is now ~0.01 ms.
Doubling the threshold to keep ~7,000-point trips inline would move ~5.7 ms
here (~11 ms on the device) back onto the UI isolate to save an isolate spawn.

So 5,000 stays. It is not a tuned number and does not need to be: above it the
isolate branch is nearly free on the UI isolate, and below it the inline build
is a few milliseconds and an isolate spawn is not worth its latency.

### What this costs

The seeded buffers are retained for as long as their coordinate lists are —
they hang off the same `Expando`s as the points and the decimated geometry, so
they are collected with the feature. That is 16 bytes per point on top of a
GeoJSON point that already costs ~100, so ~15% more geometry memory, and it
was previously paid transiently (twice, since the hop copied it) on every
build rather than once per decode.

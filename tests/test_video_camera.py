"""Trip video follow camera (docs/TRIP_VIDEO_PLAN.md, U2) and its camera
modes (docs/VIDEO_CAMERA_QUALITY_PLAN.md, U2)."""
from __future__ import annotations

import math
import subprocess
import sys
from datetime import date, timedelta

import pytest

from src.video import camera as cam
from src.video.camera import (
    CAMERA_MODES,
    CLIP_FILL,
    FIXED_MAX_PAN_PER_S,
    LEASH,
    MAX_TILES,
    MAX_ZOOM,
    MIN_ZOOM,
    TRANSITION_S,
    Shot,
    camera,
    camera_path,
    estimate_tiles,
    fixed_zoom,
    frame_count,
    is_followed,
    lonlat_to_world,
    mode_min_zoom,
    viewport_bounds,
    world_to_lonlat,
)
from src.models.project import ConnectingSegment, Project, ProjectItem, SegmentEndpoint
from src.video.legs import Leg, LegSet, build_legs, prefix_km
from src.video.pacing import Clip, clip_budget_s
from src.video.timeline import Timeline, build_timeline

FPS = 30
HD = (1920, 1080)
SMALL = (320, 180)
RAMP = round(TRANSITION_S * FPS)
FIXED_MODES = ("fixed", "fixed_strict")


# ── builders ─────────────────────────────────────────────────────────────────

def _leg(i, mode, start, end, real_s, day, n=40) -> Leg:
    """A gently wavy line from *start* to *end* ((lon, lat) each)."""
    (lon0, lat0), (lon1, lat1) = start, end
    pts = tuple((lon0 + (lon1 - lon0) * k / (n - 1),
                 lat0 + (lat1 - lat0) * k / (n - 1) + 0.002 * math.sin(k))
                for k in range(n))
    cum = prefix_km(pts)
    return Leg(index=i, kind="activity", ref=i, label=f"leg {i}", mode=mode,
               coords=pts, cum_km=cum, km=cum[-1], real_s=real_s,
               speed_kmh=cum[-1] / real_s * 3600.0, date=day)


def _legset(specs) -> LegSet:
    return LegSet(tuple(_leg(i, *spec) for i, spec in enumerate(specs)), ())


def _tour() -> LegSet:
    """Every mode, each leg starting where the last one ended, one per day."""
    d = date(2026, 5, 1)
    specs = [
        ("hike", (6.00, 45.00), (6.05, 45.03), 7200),
        ("ride", (6.05, 45.03), (6.80, 45.40), 10800),
        ("train", (6.80, 45.40), (2.35, 48.85), 4 * 3600),
        ("hike", (2.35, 48.85), (2.37, 48.86), 3600),
        ("flight", (2.37, 48.86), (12.50, 41.90), 2 * 3600),
        ("run", (12.50, 41.90), (12.52, 41.91), 1800),
        ("boat", (12.52, 41.91), (14.20, 40.80), 3 * 3600),
        ("other", (14.20, 40.80), (14.25, 40.82), 2000),
        ("bus", (14.25, 40.82), (15.50, 40.60), 2 * 3600),
        ("ride", (15.50, 40.60), (16.20, 40.40), 9000),
    ]
    return _legset([(m, a, b, r, d + timedelta(days=i)) for i, (m, a, b, r) in enumerate(specs)])


def _scattered() -> LegSet:
    """1,000 legs over a year, every mode, the legs not joined up: at 30 s
    most clips hold several modes and the marker jumps between legs."""
    modes = ("ride", "hike", "run", "train", "ride", "bus", "flight", "boat")
    specs = []
    for i in range(1000):
        mode = modes[(i * 7) % len(modes)]
        lon, lat = -5.0 + (i % 97) * 0.2, 40.0 + (i % 50) * 0.1
        far = mode in ("train", "bus", "flight", "boat")
        end = (lon + 0.7, lat + 0.5) if far else (lon + 0.04, lat + 0.03)
        specs.append((mode, (lon, lat), end, 600 + (i * 131) % 20_000,
                      date(2026, 1, 1) + timedelta(days=i * 365 // 1000), 12))
    return _legset(specs)


TRIPS = {"tour": _tour, "scattered": _scattered}


def _world(shot):
    return lonlat_to_world(shot.lon, shot.lat)


def _marker_offset(shot, state, size):
    """The marker's distance from the centre in frame widths/heights."""
    (cx, cy), (mx, my) = _world(shot), lonlat_to_world(state.lon, state.lat)
    px = 512 * 2 ** shot.zoom
    return max(abs(mx - cx) * px / size[0], abs(my - cy) * px / size[1])


# ── Web Mercator ─────────────────────────────────────────────────────────────

def test_world_coordinates_match_the_poster_tile_math():
    from src.poster.tile_stitcher import lonlat_to_pixel
    for lon, lat in [(0.0, 0.0), (2.35, 48.85), (-122.4, 37.8), (151.2, -33.9)]:
        x, y = lonlat_to_world(lon, lat)
        px, py = lonlat_to_pixel(lon, lat, 7)
        assert (x * 512 * 2 ** 7, y * 512 * 2 ** 7) == pytest.approx((px, py))
        assert world_to_lonlat(x, y) == pytest.approx((lon, lat))


def test_viewport_bounds_grow_as_zoom_drops():
    near = viewport_bounds(2.35, 48.85, 12, HD)
    far = viewport_bounds(2.35, 48.85, 11, HD)
    assert far["west"] < near["west"] < 2.35 < near["east"] < far["east"]
    assert far["south"] < near["south"] < 48.85 < near["north"] < far["north"]
    assert (far["east"] - far["west"]) == pytest.approx(2 * (near["east"] - near["west"]))


# ── determinism ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", CAMERA_MODES)
@pytest.mark.parametrize("trip", sorted(TRIPS))
def test_identical_output_twice(trip, mode):
    """Convention 3: two timelines built alike give the same path, and a
    frame asked for out of order, after the cache is gone, is the same."""
    a = build_timeline(TRIPS[trip](), 30)
    b = build_timeline(TRIPS[trip](), 30)
    first = camera_path(a, FPS, HD, mode)
    assert camera_path(b, FPS, HD, mode) == first
    for cache in (cam._path, cam._samples, cam._pan_speeds, cam._fixed_zoom):
        cache.cache_clear()
    n = frame_count(a, FPS)
    backwards = [camera(a, i, FPS, HD, mode) for i in reversed(range(n))][::-1]
    assert backwards == [(s.lon, s.lat, s.zoom) for s in first]


def test_the_mode_is_part_of_the_cache_key():
    tl = build_timeline(_tour(), 30)
    paths = {mode: camera_path(tl, FPS, HD, mode) for mode in CAMERA_MODES}
    assert len({paths[m] for m in CAMERA_MODES}) == len(CAMERA_MODES)
    assert camera_path(tl, FPS, HD) == paths["variable"]


def test_an_unknown_mode_is_refused():
    tl = build_timeline(_tour(), 30)
    with pytest.raises(ValueError):
        camera_path(tl, FPS, HD, "orbit")
    with pytest.raises(ValueError):
        camera(tl, 0, FPS, HD, "orbit")
    for mode in ("variable", "overview"):
        with pytest.raises(ValueError):
            fixed_zoom(tl, HD, mode)


def test_frame_count_and_out_of_range():
    tl = build_timeline(_tour(), 30)
    assert frame_count(tl, FPS) == 900 == len(camera_path(tl, FPS, HD))
    camera(tl, 899, FPS, HD)
    for n in (-1, 900):
        with pytest.raises(ValueError):
            camera(tl, n, FPS, HD)


def test_ninety_seconds_of_a_long_trip_is_fast():
    import time
    tl = build_timeline(_scattered(), 90)
    cam._path.cache_clear()
    t0 = time.perf_counter()
    camera_path(tl, FPS, HD)
    assert time.perf_counter() - t0 < 1.0


# ── zoom clamps ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trip", sorted(TRIPS))
@pytest.mark.parametrize("total", [30, 90])
@pytest.mark.parametrize("size", [HD, SMALL])
def test_zoom_within_per_mode_clamps(trip, total, size):
    """Every frame's zoom is within [MIN_ZOOM, MAX_ZOOM]; outside a fly-to,
    never below the floor of a sub-leg being followed."""
    tl = build_timeline(TRIPS[trip](), total)
    checked = set()
    for n, shot in enumerate(camera_path(tl, FPS, size)):
        assert MIN_ZOOM - 1e-9 <= shot.zoom <= MAX_ZOOM + 1e-9
        state = tl.sample(n / FPS)
        if state.kind != "clip" or shot.flying:
            continue
        if not is_followed(tl.clips[state.clip_index].subs[state.sub_index]):
            continue      # too short to follow: the whole clip is shown
        assert shot.zoom >= mode_min_zoom(state.mode) - 1e-9, (n, state.mode, shot.zoom)
        checked.add(state.mode)
    if trip == "tour":
        assert checked == set(cam.MODE_MIN_ZOOM)


def test_mode_floor_holds_a_long_ride_close_and_leaves_a_flight_free():
    d = date(2026, 5, 1)
    tl = build_timeline(_legset([
        ("ride", (6.0, 45.0), (8.0, 46.0), 8 * 3600, d),                     # ~190 km
        ("flight", (8.0, 46.0), (37.6, 55.7), 4 * 3600, d + timedelta(1)),
    ]), 30)
    path = camera_path(tl, FPS, HD)
    ride, flight = tl.clips
    mid = lambda c: path[round((c.start_s + c.end_s) / 2 * FPS)]
    assert mid(ride).zoom == pytest.approx(10.0)     # the fit alone would be lower
    assert mid(flight).zoom < 6.0


def test_zoom_rises_before_a_higher_floor_starts():
    """A flight then a walk in one clip: the camera is already at the walk's
    floor on the walk's first frame, and got there while the flight was
    drawn."""
    d = date(2026, 5, 1)
    legs = _legset([
        ("flight", (2.35, 48.85), (12.5, 41.9), 2 * 3600, d),
        ("hike", (12.5, 41.9), (12.51, 41.91), 2 * 3600, d),
    ]).legs
    tl = Timeline(legs, [Clip(legs)], [clip_budget_s(30)], 30)
    path = camera_path(tl, FPS, HD)
    first = next(n for n in range(len(path)) if tl.sample(n / FPS).sub_index == 1)
    assert not path[first].flying
    assert path[first].zoom >= 12.0 - 1e-9
    assert path[first - RAMP].zoom < path[first - RAMP // 2].zoom < path[first].zoom
    assert max(z for _, z, _ in _steps(path, HD)) <= 0.6


# ── smoothness ───────────────────────────────────────────────────────────────

def _steps(path, size):
    """Per consecutive pair: (centre move in frame widths at the lower zoom,
    zoom change, both frames following rather than flying)."""
    out = []
    for a, b in zip(path, path[1:]):
        (ax, ay), (bx, by) = _world(a), _world(b)
        px = 512 * 2 ** min(a.zoom, b.zoom)
        move = max(abs(bx - ax) * px / size[0], abs(by - ay) * px / size[1])
        out.append((move, abs(b.zoom - a.zoom), not (a.flying or b.flying)))
    return out


@pytest.mark.parametrize("mode", CAMERA_MODES)
@pytest.mark.parametrize("trip", sorted(TRIPS))
@pytest.mark.parametrize("total", [30, 90])
@pytest.mark.parametrize("size", [HD, SMALL])
def test_consecutive_frames_change_little(trip, total, size, mode):
    """No frame jumps: at most a quarter of the frame and one zoom level per
    frame anywhere (fly-tos included), a tenth of the frame and 0.6 zoom
    levels while following."""
    tl = build_timeline(TRIPS[trip](), total)
    steps = _steps(camera_path(tl, FPS, size, mode), size)
    assert max(m for m, _, _ in steps) <= 0.25
    assert max(z for _, z, _ in steps) <= 1.0
    follow = [(m, z) for m, z, f in steps if f]
    assert follow
    assert max(m for m, _ in follow) <= 0.1
    assert max(z for _, z in follow) <= 0.6


def test_card_to_clip_is_a_fly_to_not_a_cut():
    """The title card doesn't jump into the first clip: the camera flies in
    over the title's last TRANSITION_S and is in place as the clip starts."""
    tl = build_timeline(_tour(), 30)
    path = camera_path(tl, FPS, HD)
    first = round(tl.clips[0].start_s * FPS)
    assert [s.flying for s in path[first - RAMP: first + 1]] == [False] + [True] * (RAMP - 1) + [False]
    zooms = [s.zoom for s in path[first - RAMP: first + 1]]
    assert zooms == sorted(zooms)                       # zooms in steadily
    assert zooms[1] - zooms[0] < (zooms[-1] - zooms[0]) / 4
    assert zooms[-1] - zooms[-2] < (zooms[-1] - zooms[0]) / 4


# ── following ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("total", [30, 90])
def test_marker_stays_on_screen(total):
    """On a trip whose legs join up, outside the fly-tos the marker is in
    the frame, and within LEASH of the centre while it is followed."""
    tl = build_timeline(_tour(), total)
    for n, shot in enumerate(camera_path(tl, FPS, HD)):
        state = tl.sample(n / FPS)
        if state.kind != "clip" or shot.flying:
            continue
        off = _marker_offset(shot, state, HD)
        assert off < 0.5, n
        if is_followed(tl.clips[state.clip_index].subs[state.sub_index]):
            assert off <= LEASH + 1e-9, n


def test_the_camera_pulls_out_before_a_flight_takes_off():
    """A walk then a flight: the camera is at the flight's zoom when the
    flight starts, having pulled out over the walk's last TRANSITION_S,
    while the plane is still on the ground."""
    tl = build_timeline(_tour(), 30)
    path = camera_path(tl, FPS, HD)
    flight = next(c for c in tl.clips if c.clip.mode == "flight")
    first = next(n for n in range(len(path)) if tl.sample(n / FPS).clip_index == flight.index)
    assert not path[first].flying and path[first - 1].flying
    assert path[first].zoom < 8 < 12 < path[first - RAMP].zoom
    for n in range(first - RAMP, first):
        assert tl.sample(n / FPS).mode == "hike"
        assert _marker_offset(path[n], tl.sample(n / FPS), HD) < 0.5


def test_leash_keeps_a_fast_marker_close():
    """A 400 km ride held at the ride floor races across the frame faster
    than the spring settles: the leash still keeps it within LEASH."""
    d = date(2026, 5, 1)
    tl = build_timeline(_legset([
        ("ride", (6.0, 45.0), (11.0, 45.5), 16 * 3600, d),
        ("hike", (11.0, 45.5), (11.02, 45.51), 3600, d + timedelta(1)),
    ]), 30)
    path = camera_path(tl, FPS, SMALL)
    offsets = [_marker_offset(shot, tl.sample(n / FPS), SMALL)
               for n, shot in enumerate(path)
               if tl.sample(n / FPS).clip_index == 0 and not shot.flying]
    assert max(offsets) == pytest.approx(LEASH)


def test_camera_follows_the_marker():
    tl = build_timeline(_tour(), 90)
    path = camera_path(tl, FPS, HD)
    clip = tl.clips[1]                                   # the ride
    a, b = (round(t * FPS) for t in (clip.start_s + 1.0, clip.end_s - 0.5))
    assert _world(path[a]) != pytest.approx(_world(path[b]))
    for n in (a, b):
        assert _marker_offset(path[n], tl.sample(n / FPS), HD) < 0.1


def test_a_jump_between_legs_is_flown_to():
    """Two walks of one day far apart share a clip; the marker jumps from
    one to the other and the camera flies there instead of following."""
    d = date(2026, 5, 1)
    tl = build_timeline(_legset([
        ("hike", (6.0, 45.0), (6.02, 45.01), 3600, d),
        ("hike", (7.0, 45.5), (7.02, 45.51), 3600, d),
    ]), 30)
    assert len(tl.clips) == 1
    path = camera_path(tl, FPS, HD)
    # A cut is the first frame after a fly-to.
    cuts = [n for n in range(1, len(path)) if path[n - 1].flying and not path[n].flying]
    states = [tl.sample(n / FPS) for n in cuts]
    assert [(s.kind, s.sub_index) for s in states] == [("clip", 0), ("clip", 1), ("end", None)]
    assert max(m for m, _, _ in _steps(path, HD)) <= 0.25


# ── cards ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trip", sorted(TRIPS))
@pytest.mark.parametrize("size", [HD, SMALL])
def test_overview_frames_contain_the_whole_trip(trip, size):
    """Title frames until the fly-in, and every end frame, show every point
    of the trip."""
    tl = build_timeline(TRIPS[trip](), 30)
    lons = [lon for leg in tl.legs for lon, _ in leg.coords]
    lats = [lat for leg in tl.legs for _, lat in leg.coords]
    overview = 0
    for n, shot in enumerate(camera_path(tl, FPS, size)):
        kind = tl.sample(n / FPS).kind
        if kind == "clip" or shot.flying:
            continue
        box = viewport_bounds(shot.lon, shot.lat, shot.zoom, size)
        assert box["west"] <= min(lons) and max(lons) <= box["east"], n
        assert box["south"] <= min(lats) and max(lats) <= box["north"], n
        overview += 1
    assert overview >= (2.5 + 3.0 - TRANSITION_S) * FPS - 1


# ── the antimeridian (F1-5) ──────────────────────────────────────────────────

def _pacific_flight() -> LegSet:
    """Tokyo to Los Angeles: a great-circle arc across the ±180° meridian."""
    seg = ConnectingSegment(id="f", segment_type="flight", date="2026-05-01",
                            start=SegmentEndpoint(35.55, 139.78),
                            end=SegmentEndpoint(33.94, -118.41))
    return build_legs(Project(name="Pacific", items=[ProjectItem(item_type="segment", segment=seg)]))


@pytest.mark.parametrize("mode", CAMERA_MODES)
@pytest.mark.parametrize("size", [HD, SMALL])
def test_a_pacific_flight_is_framed_the_short_way(size, mode):
    """Every frame's viewport spans well under the whole world, the overview
    holds the whole route, and the followed marker stays on screen."""
    tl = build_timeline(_pacific_flight(), 30)
    for n, shot in enumerate(camera_path(tl, FPS, size, mode)):
        box = viewport_bounds(shot.lon, shot.lat, shot.zoom, size)
        assert box["east"] - box["west"] < 180, n
        state = tl.sample(n / FPS)
        if state.kind == "clip" and not shot.flying:
            assert _marker_offset(shot, state, size) < 0.5, n
        elif state.kind == "end":
            assert box["west"] < 139.78 and -118.41 + 360 < box["east"], n


# ── Convention 1 (#518): variable mode doesn't move ──────────────────────────
#
# A frozen copy of the variable camera as it was before the camera modes
# (98f35930), constants and motion included, so any change to what variable
# mode produces fails here — not only a change to the code that picks a mode.

_REF_MODE_MIN_ZOOM = {"hike": 12.0, "run": 12.0, "ride": 10.0, "other": 10.0,
                      "train": 6.0, "bus": 6.0, "boat": 6.0, "flight": 0.0}


def _ref_px(zoom):
    return 512 * 2.0 ** zoom


def _ref_fit(points, size, fill):
    xs, ys = zip(*points)
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    zoom = 16.0
    if w > 0:
        zoom = min(zoom, math.log2(fill * size[0] / (512 * w)))
    if h > 0:
        zoom = min(zoom, math.log2(fill * size[1] / (512 * h)))
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, max(0.0, zoom)


def _ref_smoothstep(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3.0 - 2.0 * u)


def _ref_spring(pos, vel, target, dt):
    d = pos - target
    e = math.exp(-10.0 * dt)
    k = (vel + 10.0 * d) * dt
    return target + (d + k) * e, (vel - 10.0 * k) * e


def _ref_fly(a, b, s, size):
    w0, w1 = size[0] / _ref_px(a[2]), size[0] / _ref_px(b[2])
    dx, dy = b[0] - a[0], b[1] - a[1]
    d1 = math.hypot(dx, dy)
    if d1 < 1e-12:
        return a[0] + s * dx, a[1] + s * dy, a[2] + s * (b[2] - a[2])
    rho2 = 2.0 ** 0.5 * 2.0 ** 0.5
    b0 = (w1 * w1 - w0 * w0 + rho2 * rho2 * d1 * d1) / (2 * w0 * rho2 * d1)
    b1 = (w1 * w1 - w0 * w0 - rho2 * rho2 * d1 * d1) / (2 * w1 * rho2 * d1)
    r0, r1 = -math.asinh(b0), -math.asinh(b1)
    r = r0 + s * (r1 - r0)
    u = w0 / (rho2 * d1) * (math.cosh(r0) * math.tanh(r) - math.sinh(r0))
    w = w0 * math.cosh(r0) / math.cosh(r)
    return a[0] + u * dx, a[1] + u * dy, math.log2(size[0] / (512 * w))


def _ref_zoom_plan(levels, ramp):
    low = min(levels)
    out = []
    for n in range(len(levels)):
        best = levels[n]
        for m in range(max(0, n - ramp), min(len(levels), n + ramp + 1)):
            if levels[m] > best:
                best = max(best, levels[m] - (levels[m] - low) * _ref_smoothstep(abs(n - m) / ramp))
        out.append(best)
    return out


def _ref_followed(sub):
    return not sub.instant and sub.end_s - sub.start_s >= 1.0


def _ref_clip_aims(clip, size):
    cx, cy, fit = _ref_fit([lonlat_to_world(lon, lat) for sub in clip.subs
                            for lon, lat in sub.leg.coords], size, 0.6)
    followed = [_ref_followed(sub) for sub in clip.subs]
    level = [min(16.0, max(fit, _REF_MODE_MIN_ZOOM.get(sub.leg.mode, 10.0))) for sub in clip.subs]
    aims, i = [], 0
    while i < len(clip.subs):
        if followed[i]:
            aims.append((None, level[i]))
            i += 1
            continue
        j = i
        while j < len(clip.subs) and not followed[j]:
            j += 1
        after = next((k for k in range(j, len(clip.subs)) if followed[k]), None)
        before = next((k for k in range(i - 1, -1, -1) if followed[k]), None)
        if clip.subs[j - 1].end_s - clip.subs[i].start_s >= 0.6 or (after is None and before is None):
            aim = ((cx, cy), fit)
        elif after is not None:
            aim = (lonlat_to_world(*clip.subs[after].leg.coords[0]), level[after])
        else:
            aim = (lonlat_to_world(*clip.subs[before].leg.coords[-1]), level[before])
        aims.extend([aim] * (j - i))
        i = j
    return aims


def _ref_path(timeline, fps, size):
    n_frames = int(round(timeline.total_s * fps))
    dt = 1.0 / fps
    ramp = max(1, int(round(0.6 * fps)))
    home = _ref_fit([lonlat_to_world(lon, lat) for leg in timeline.legs for lon, lat in leg.coords],
                    size, 0.85)
    aims = [_ref_clip_aims(clip, size) for clip in timeline.clips]
    n_clips = len(timeline.clips)
    samples = [timeline.sample(n * dt) for n in range(n_frames)]
    segs = [-1 if s.kind == "title" else n_clips if s.kind == "end" else s.clip_index
            for s in samples]
    target = []
    for s, seg in zip(samples, segs):
        if 0 <= seg < n_clips:
            point, level = aims[seg][s.sub_index]
            x, y = point if point is not None else lonlat_to_world(s.lon, s.lat)
            target.append((x, y, level))
        else:
            target.append(home)
    zoom = [v[2] for v in target]
    start = 0
    for n in range(1, n_frames + 1):
        if n == n_frames or segs[n] != segs[start]:
            if 0 <= segs[start] < n_clips:
                zoom[start:n] = _ref_zoom_plan(zoom[start:n], ramp)
            start = n

    def off(p, v):
        px = _ref_px(v[2])
        return max(abs(p[0] - v[0]) * px / size[0], abs(p[1] - v[1]) * px / size[1])

    cuts = [n for n in range(1, n_frames) if segs[n] != segs[n - 1] or off(
        target[n - 1][:2], (*target[n][:2], min(zoom[n - 1], zoom[n]))) > 0.5]
    follow = []
    x = y = vx = vy = 0.0
    cut_set = set(cuts)
    for n in range(n_frames):
        tx, ty, _ = target[n]
        if n == 0 or n in cut_set:
            x, y, vx, vy = tx, ty, 0.0, 0.0
        else:
            x, vx = _ref_spring(x, vx, tx, dt)
            y, vy = _ref_spring(y, vy, ty, dt)
            hw, hh = 0.3 * size[0] / _ref_px(zoom[n]), 0.3 * size[1] / _ref_px(zoom[n])
            x, y = min(max(x, tx - hw), tx + hw), min(max(y, ty - hh), ty + hh)
        follow.append((x, y, zoom[n]))
    views, flying, prev = list(follow), [False] * n_frames, 0
    for c in cuts:
        w0 = max(prev, c - ramp)
        for n in range(w0 + 1, c):
            views[n] = _ref_fly(follow[n], follow[c], _ref_smoothstep((n - w0) / (c - w0)), size)
            flying[n] = True
        prev = c
    return tuple(Shot(*world_to_lonlat(wx, wy), wz, f) for (wx, wy, wz), f in zip(views, flying))


def _floor_trip():
    d = date(2026, 5, 1)
    return _legset([("ride", (6.0, 45.0), (8.0, 46.0), 8 * 3600, d),
                    ("flight", (8.0, 46.0), (37.6, 55.7), 4 * 3600, d + timedelta(1))])


def _jump_trip():
    d = date(2026, 5, 1)
    return _legset([("hike", (6.0, 45.0), (6.02, 45.01), 3600, d),
                    ("hike", (7.0, 45.5), (7.02, 45.51), 3600, d)])


@pytest.mark.parametrize("trip", ["tour", "scattered", "floor", "jump", "pacific"])
@pytest.mark.parametrize("total", [30, 90])
@pytest.mark.parametrize("size", [HD, SMALL])
def test_variable_mode_is_the_camera_before_the_modes(trip, total, size):
    make = {**TRIPS, "floor": _floor_trip, "jump": _jump_trip, "pacific": _pacific_flight}[trip]
    tl = build_timeline(make(), total)
    assert camera_path(tl, FPS, size, "variable") == _ref_path(tl, FPS, size)


# ── overview mode (#518 D2) ──────────────────────────────────────────────────

@pytest.mark.parametrize("trip", sorted(TRIPS))
@pytest.mark.parametrize("size", [HD, SMALL])
def test_overview_mode_holds_the_whole_trip_on_every_frame(trip, size):
    tl = build_timeline(TRIPS[trip](), 30)
    path = camera_path(tl, FPS, size, "overview")
    assert len(path) == frame_count(tl, FPS)
    assert set(path) == {path[0]} and not path[0].flying
    lons = [lon for leg in tl.legs for lon, _ in leg.coords]
    lats = [lat for leg in tl.legs for _, lat in leg.coords]
    box = viewport_bounds(path[0].lon, path[0].lat, path[0].zoom, size)
    assert box["west"] <= min(lons) and max(lons) <= box["east"]
    assert box["south"] <= min(lats) and max(lats) <= box["north"]
    # The cards of every other mode show this same view.
    assert camera_path(tl, FPS, size)[0] == path[0]


# ── fixed zoom (#518 D3) ─────────────────────────────────────────────────────

def _days(*specs) -> LegSet:
    """One leg a day, so each is its own clip. A spec is (mode, start, end,
    real_s[, points])."""
    d = date(2026, 5, 1)
    return _legset([(*spec[:4], d + timedelta(days=i), *spec[4:]) for i, spec in enumerate(specs)])


def _walk(lon, lat):
    """A short walk: its CLIP_FILL fit zoom is about 14.6 at HD."""
    return ("hike", (lon, lat), (lon + 0.02, lat + 0.01), 3600)


def _sub_fit(sub, size):
    return cam._fit([lonlat_to_world(*p) for p in sub.leg.coords], size, CLIP_FILL)[2]


def _followed_frames(path, tl, key):
    """The non-flying frames of sub-leg *key* = (clip index, sub index)."""
    return [n for n, shot in enumerate(path) if not shot.flying
            and (lambda s: s.kind == "clip" and (s.clip_index, s.sub_index) == key)(tl.sample(n / FPS))]


def _leash_clamps(path, tl, size):
    """Per followed sub-leg: (its non-flying frames, those where the leash
    holds the marker at LEASH). Asserts the marker is never further out."""
    out = {}
    for n, shot in enumerate(path):
        state = tl.sample(n / FPS)
        if state.kind != "clip" or shot.flying:
            continue
        if not is_followed(tl.clips[state.clip_index].subs[state.sub_index]):
            continue
        off = _marker_offset(shot, state, size)
        assert off <= LEASH + 1e-9, n
        frames, clamped = out.get((state.clip_index, state.sub_index), (0, 0))
        out[(state.clip_index, state.sub_index)] = (frames + 1, clamped + (off >= LEASH - 1e-6))
    return out


@pytest.mark.parametrize("mode", FIXED_MODES)
@pytest.mark.parametrize("trip", sorted(TRIPS))
@pytest.mark.parametrize("total", [30, 90])
@pytest.mark.parametrize("size", [HD, SMALL])
def test_fixed_modes_follow_at_one_integer_zoom(trip, total, size, mode):
    """Z is an integer and every frame outside the cards, the fly-tos and
    (in "fixed") the fast sub-legs is at Z; no cut inside a sub-leg (the
    marker never outruns the camera); a followed marker stays within LEASH
    and is held there by the leash on at most 5% of its sub-leg's frames."""
    tl = build_timeline(TRIPS[trip](), total)
    z = fixed_zoom(tl, size, mode)
    assert isinstance(z, int) and cam.fixed_floor(mode, size) <= z <= cam.FIXED_TOP_ZOOM
    path = camera_path(tl, FPS, size, mode)
    speeds = cam._pan_speeds(tl, size)
    for n, shot in enumerate(path):
        state = tl.sample(n / FPS)
        if state.kind != "clip" or shot.flying:
            continue
        fast = mode == "fixed" and speeds.get((state.clip_index, state.sub_index), 0) * 2 ** z > \
            FIXED_MAX_PAN_PER_S
        if not fast:
            assert shot.zoom == z, n
        if n and not path[n - 1].flying:
            before = tl.sample((n - 1) / FPS)
            if (before.clip_index, before.sub_index) == (state.clip_index, state.sub_index):
                # Only a gap between legs or a change of framing is a cut.
                move = cam._off_frame(lonlat_to_world(before.lon, before.lat),
                                      (*lonlat_to_world(state.lon, state.lat), shot.zoom), size)
                assert move <= cam.CUT, n
    for key, (frames, clamped) in _leash_clamps(path, tl, size).items():
        assert clamped <= 0.05 * frames, (key, clamped, frames)


def test_the_strict_floor_keeps_frames_within_half_the_world():
    """D3 (amended): fixed_strict's floor is max(2, ceil(log2(width / 256))),
    so its zoom never shows more than 180° of longitude across the frame."""
    assert cam.fixed_floor("fixed_strict", HD) == 3
    assert cam.fixed_floor("fixed_strict", SMALL) == 2
    assert cam.fixed_floor("fixed", HD) == cam.fixed_floor("fixed", SMALL) == 4
    trips = {**TRIPS, "pacific": _pacific_flight}
    for size in (HD, SMALL):
        for trip in sorted(trips):
            for total in (30, 90):
                tl = build_timeline(trips[trip](), total)
                z = fixed_zoom(tl, size, "fixed_strict")
                assert size[0] / (512 * 2 ** z) * 360 <= 180, (trip, total, size, z)
    # The Pacific flight's fit (2.99 at HD) would pick 2 without the raise.
    assert fixed_zoom(build_timeline(_pacific_flight(), 30), HD, "fixed_strict") == 3


def test_the_pan_bound_is_what_the_spring_can_track():
    """R2-1: a steady pan at v lags the spring by 2v/ω; at the bound that is
    exactly LEASH."""
    assert FIXED_MAX_PAN_PER_S == pytest.approx(LEASH * cam.SPRING_OMEGA / 2)
    assert FIXED_MAX_PAN_PER_S / FPS <= 0.1 / 2        # the follow bound, with room


def _north_south_trip(scale):
    """Three walks, then a straight, mostly north–south ride *scale*° of
    latitude long, on its own day."""
    return _days(_walk(6.0, 45.0), _walk(6.1, 45.0), _walk(6.2, 45.0),
                 ("ride", (6.3, 45.0), (6.3 + 0.1 * scale, 45.0 + scale), 3 * 3600, 2))


def _tuned_north_south(ratio):
    """The north–south trip with its ride moving *ratio* × the pan bound at
    the fixed zoom, by the larger-axis measure; and that Z."""
    scale, key = 0.2, (3, 0)
    for _ in range(6):     # the Mercator stretch isn't linear in latitude
        tl = build_timeline(_north_south_trip(scale), 90)
        z = fixed_zoom(tl, HD, "fixed")
        speed = cam._pan_speeds(tl, HD)[key] * 2 ** z
        scale *= ratio * FIXED_MAX_PAN_PER_S / speed
    tl = build_timeline(_north_south_trip(scale), 90)
    z = fixed_zoom(tl, HD, "fixed")
    assert cam._pan_speeds(tl, HD)[key] * 2 ** z == pytest.approx(ratio * FIXED_MAX_PAN_PER_S,
                                                                  rel=1e-3)
    return tl, z


def test_a_north_south_leg_is_measured_on_the_frame_height():
    """R3-1: just under the bound, a mostly north–south ride is followed at
    Z and the leash hardly ever holds it; just over, it is fast (framed at
    its clip's fit), though in frame widths it is well under the bound."""
    tl, z = _tuned_north_south(0.95)
    path = camera_path(tl, FPS, HD, "fixed")
    frames = _followed_frames(path, tl, (3, 0))
    assert frames and all(path[n].zoom == z for n in frames)
    total, clamped = _leash_clamps(path, tl, HD)[(3, 0)]
    assert clamped <= 0.05 * total

    tl, z_over = _tuned_north_south(1.05)
    assert z_over == z
    path = camera_path(tl, FPS, HD, "fixed")
    fit = cam._fit([lonlat_to_world(*p) for p in tl.clips[3].subs[0].leg.coords], HD, CLIP_FILL)[2]
    frames = _followed_frames(path, tl, (3, 0))
    assert frames and all(path[n].zoom == pytest.approx(fit) for n in frames)
    assert fit < z
    # By width alone it would have been followed at Z.
    samples = [tl.sample(n / FPS) for n in frames]
    xs = [lonlat_to_world(s.lon, s.lat)[0] * 512 * 2 ** z / HD[0] for s in samples]
    assert max(abs(b - a) for a, b in zip(xs, xs[1:])) * FPS < FIXED_MAX_PAN_PER_S * 0.6


def _walks_and_trains():
    """Four short walks (fit ≈ 14.6) and three long, slow trains (fit ≈ 6.1,
    fast above ≈ 9.7) at 30 s."""
    return build_timeline(_days(
        _walk(6.0, 45.0), _walk(6.1, 45.0), _walk(6.2, 45.0), _walk(6.3, 45.0),
        ("train", (6.3, 45.0), (18.0, 47.0), 6 * 3600),
        ("train", (18.0, 47.0), (6.5, 45.5), 6 * 3600),
        ("train", (6.5, 45.5), (18.0, 44.0), 6 * 3600)), 30)


def test_the_zoom_is_searched_from_the_top_down():
    """R2-2: "fixed" keeps the walks' zoom and flies the trains; strict
    drops to the highest zoom where no train is fast."""
    tl = _walks_and_trains()
    speeds = cam._pan_speeds(tl, HD)
    trains = [(ci, 0) for ci in range(4, 7)]
    assert all(5.5 < _sub_fit(tl.clips[ci].subs[0], HD) < 6.5 for ci, _ in trains)
    assert all(9 <= math.log2(FIXED_MAX_PAN_PER_S / speeds[k]) < 10.5 for k in trains)

    z_fixed, z_strict = fixed_zoom(tl, HD, "fixed"), fixed_zoom(tl, HD, "fixed_strict")
    assert z_fixed >= 10 and z_strict < z_fixed
    assert z_strict == max(z for z in range(15) if all(speeds[k] * 2 ** z <= FIXED_MAX_PAN_PER_S
                                                       for k in speeds))
    path = camera_path(tl, FPS, HD, "fixed")
    for key in trains:
        fit = cam._fit([lonlat_to_world(*p) for s in tl.clips[key[0]].subs
                        for p in s.leg.coords], HD, CLIP_FILL)[2]
        assert all(path[n].zoom == pytest.approx(fit) for n in _followed_frames(path, tl, key))


def _walks_flight_walks(flight=True):
    """Two walks near Paris, a ~1,000 km flight, two walks on the Adriatic."""
    legs = [_walk(2.35, 48.85), _walk(2.40, 48.86)]
    if flight:
        legs.append(("flight", (2.42, 48.87), (13.4, 44.0), 2 * 3600))
    legs += [_walk(13.4, 44.0), _walk(13.45, 44.02)]
    return build_timeline(_days(*legs), 90)


def test_fixed_flies_the_flight_and_keeps_the_walks_zoom():
    tl = _walks_flight_walks()
    z = fixed_zoom(tl, HD, "fixed")
    assert z == fixed_zoom(_walks_flight_walks(flight=False), HD, "fixed")
    path = camera_path(tl, FPS, HD, "fixed")
    walks = [0, 1, 3, 4]
    for ci in walks:
        frames = _followed_frames(path, tl, (ci, 0))
        assert frames and all(path[n].zoom == z for n in frames)
    fit = _sub_fit(tl.clips[2].subs[0], HD)
    frames = _followed_frames(path, tl, (2, 0))
    assert frames and all(path[n].zoom == pytest.approx(fit) for n in frames)
    assert fit < z - 4


def test_fixed_strict_follows_the_flight_at_the_one_zoom():
    tl = _walks_flight_walks()
    z = fixed_zoom(tl, HD, "fixed_strict")
    assert z < fixed_zoom(tl, HD, "fixed")
    path = camera_path(tl, FPS, HD, "fixed_strict")
    for n, shot in enumerate(path):
        if tl.sample(n / FPS).kind == "clip" and not shot.flying:
            assert shot.zoom == z, n
    frames = _followed_frames(path, tl, (2, 0))
    steps = _steps([path[n] for n in frames], HD)
    assert frames and max(m for m, _, _ in steps) * FPS <= FIXED_MAX_PAN_PER_S + 1e-9


def _walks_then_a_day_of_short_rides():
    """Four walks (Z = 14), then a day of ten joined rides, each under
    FOLLOW_MIN_S at 30 s, spanning 0.6° — seven frame widths at Z."""
    d = date(2026, 5, 1)
    specs = [(*_walk(6.0 + 0.1 * i, 45.0), d + timedelta(days=i)) for i in range(4)]
    specs += [("ride", (6.0 + 0.06 * k, 45.0 + 0.018 * k),
               (6.06 + 0.06 * k, 45.0 + 0.018 * (k + 1)), 1800, d + timedelta(days=4), 23)
              for k in range(10)]           # 23 points: sin(22) ≈ 0, so the rides join up
    return build_timeline(_legset(specs), 30)


@pytest.mark.parametrize("mode", FIXED_MODES)
def test_fixed_modes_follow_the_marker_on_legs_too_short_to_follow(mode):
    """F1-1: a clip of sub-legs not followed is not parked at its centre at
    Z (where its legs play off-screen): the marker stays in the frame, and
    the frames are at Z."""
    tl = _walks_then_a_day_of_short_rides()
    z = fixed_zoom(tl, HD, mode)
    clip = tl.clips[4]
    assert not any(is_followed(sub) for sub in clip.subs)
    wide = cam._fit([lonlat_to_world(*p) for sub in clip.subs for p in sub.leg.coords], HD, 1.0)
    assert wide[2] < z - 1                             # the clip is wider than the frame
    path = camera_path(tl, FPS, HD, mode)
    checked = 0
    for n, shot in enumerate(path):
        state = tl.sample(n / FPS)
        if state.kind != "clip" or state.clip_index != 4 or shot.flying:
            continue
        assert _marker_offset(shot, state, HD) < 0.5, n
        assert shot.zoom == z, n
        checked += 1
    assert checked > 0.8 * clip.duration_s * FPS


@pytest.mark.parametrize("mode", FIXED_MODES)
@pytest.mark.parametrize("total", [30, 90])
def test_a_year_long_trip_stays_within_the_tile_budget(mode, total):
    from src.video.basemap_bands import plan_bands
    tl = build_timeline(_scattered(), total)
    path = camera_path(tl, FPS, HD, mode)
    assert estimate_tiles(path, HD) <= MAX_TILES
    assert plan_bands(path, HD).max_band is None       # no band lowered to fit


# ── the tile estimate ────────────────────────────────────────────────────────

def test_the_tile_estimate_uses_the_basemap_constants():
    from src.video import basemap_bands as bb
    assert (cam.PIXEL_RATIO, cam.FADE, cam.GAP_FRAMES, cam.SHEET_MAX_FRAMES, cam.MARGIN_PX,
            cam.MAX_TILES) == (bb.PIXEL_RATIO, bb.FADE, bb.GAP_FRAMES, bb.SHEET_MAX_FRAMES,
                               bb.MARGIN_PX, bb.MAX_TILES)


@pytest.mark.parametrize("mode", CAMERA_MODES)
@pytest.mark.parametrize("trip", ["tour", "scattered", "pacific", "flight"])
@pytest.mark.parametrize("size", [HD, SMALL])
def test_the_tile_estimate_matches_the_basemap_plan(trip, size, mode):
    from src.video.basemap_bands import plan_bands
    if trip == "flight":
        tl = _walks_flight_walks()
    else:
        tl = build_timeline({**TRIPS, "pacific": _pacific_flight}[trip](), 90)
    path = camera_path(tl, FPS, size, mode)
    planned = plan_bands(path, size, max_tiles=10 ** 9).tiles
    assert estimate_tiles(path, size) == pytest.approx(planned, rel=0.1)


# ── Convention 4 ─────────────────────────────────────────────────────────────

def test_camera_imports_no_framework():
    code = ("import sys, src.video.camera\n"
            "bad = [m for m in ('fastapi', 'PIL', 'sqlalchemy', 'sqlmodel', 'models.db',"
            " 'requests') if m in sys.modules]\n"
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == ""

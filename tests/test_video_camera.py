"""Trip video follow camera (docs/TRIP_VIDEO_PLAN.md, U2)."""
from __future__ import annotations

import math
import subprocess
import sys
from datetime import date, timedelta

import pytest

from src.video import camera as cam
from src.video.camera import (
    LEASH,
    MAX_ZOOM,
    MIN_ZOOM,
    TRANSITION_S,
    camera,
    camera_path,
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

@pytest.mark.parametrize("trip", sorted(TRIPS))
def test_identical_output_twice(trip):
    """Convention 3: two timelines built alike give the same path, and a
    frame asked for out of order, after the cache is gone, is the same."""
    a = build_timeline(TRIPS[trip](), 30)
    b = build_timeline(TRIPS[trip](), 30)
    first = camera_path(a, FPS, HD)
    assert camera_path(b, FPS, HD) == first
    cam._path.cache_clear()
    n = frame_count(a, FPS)
    backwards = [camera(a, i, FPS, HD) for i in reversed(range(n))][::-1]
    assert backwards == [(s.lon, s.lat, s.zoom) for s in first]


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


@pytest.mark.parametrize("trip", sorted(TRIPS))
@pytest.mark.parametrize("total", [30, 90])
@pytest.mark.parametrize("size", [HD, SMALL])
def test_consecutive_frames_change_little(trip, total, size):
    """No frame jumps: at most a quarter of the frame and one zoom level per
    frame anywhere (fly-tos included), a tenth of the frame and 0.6 zoom
    levels while following."""
    tl = build_timeline(TRIPS[trip](), total)
    steps = _steps(camera_path(tl, FPS, size), size)
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


@pytest.mark.parametrize("size", [HD, SMALL])
def test_a_pacific_flight_is_framed_the_short_way(size):
    """Every frame's viewport spans well under the whole world, the overview
    holds the whole route, and the followed marker stays on screen."""
    tl = build_timeline(_pacific_flight(), 30)
    for n, shot in enumerate(camera_path(tl, FPS, size)):
        box = viewport_bounds(shot.lon, shot.lat, shot.zoom, size)
        assert box["east"] - box["west"] < 180, n
        state = tl.sample(n / FPS)
        if state.kind == "clip" and not shot.flying:
            assert _marker_offset(shot, state, size) < 0.5, n
        elif state.kind == "end":
            assert box["west"] < 139.78 and -118.41 + 360 < box["east"], n


# ── Convention 4 ─────────────────────────────────────────────────────────────

def test_camera_imports_no_framework():
    code = ("import sys, src.video.camera\n"
            "bad = [m for m in ('fastapi', 'PIL', 'sqlalchemy', 'sqlmodel', 'models.db',"
            " 'requests') if m in sys.modules]\n"
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == ""

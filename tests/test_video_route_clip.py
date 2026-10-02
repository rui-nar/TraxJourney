"""Drawing only the on-screen runs of each leg (docs/VIDEO_ROUTE_DRAWING_PLAN.md,
U1; #523).

``Overlay._draw_route`` converts and draws only the runs of each visible leg
that come within a margin of the frame. That must change no pixel (D2): with
clipping on and off (``overlay._CLIP``), ``_draw_route`` over a flat frame
gives byte-identical output.

- The renderer's synthetic trips (Paris to Lyon, Paris to New York, the
  Pacific flight across the antimeridian) in every camera mode: every pair
  at 320×180, every :data:`HD_STRIDE`-th pair at 1080p plus every 1080p pair
  where a leg is cut into several runs. Every 1080p pair takes about 2½ min;
  ``VIDEO_CLIP_ALL=1`` checks them all.
- D5's dense trip (:func:`dense_timeline`, which U2's joint bounds reuse) on
  a sample of its pairs, as its pie-slice joints make each pair costly.
- Hand-made frames: a leg that leaves and comes back, and the marker on a
  segment across the frame's edge, from either side.
- A few whole frames per camera mode through the renderer, as a smoke check.
"""
from __future__ import annotations

import functools
import math
import os
import random
from datetime import date

import pytest
from PIL import Image

from src.models.project import ConnectingSegment, Project, ProjectItem, SegmentEndpoint
from src.video import overlay as overlay_module
from src.video.camera import (
    CAMERA_MODES,
    TILE_SIZE,
    Shot,
    camera_path,
    lonlat_to_world,
    world_to_lonlat,
)
from src.video.legs import build_legs
from src.video.overlay import Overlay
from src.video.renderer import FrameRenderer
from src.video.timeline import build_timeline
from tests.test_video_memory import _activity, _track
from tests.test_video_renderer import fake_tile, timeline30, timeline30_ny

SMALL = (320, 180)
HD = (1920, 1080)
HD_STRIDE = 12
# At most this many pairs of the dense trip per camera mode and size.
DENSE_PAIRS = 40
_BASE = (90, 110, 90)


# ── fixtures ─────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def timeline_pacific():
    """test_video_renderer's Tokyo to Los Angeles flight, across ±180°."""
    seg = ConnectingSegment(id="f", segment_type="flight", date="2026-05-01",
                            start=SegmentEndpoint(35.55, 139.78),
                            end=SegmentEndpoint(33.94, -118.41))
    return build_timeline(build_legs(Project(name="Pacific", items=[
        ProjectItem(item_type="segment", segment=seg)])), 30.0)


def _zigzag(rng: random.Random, lat: float, lon: float, n: int):
    """*n* (lat, lon) points 300 m apart, each segment turning 30–120° from
    the last, left and right in turn: at the zooms the cameras follow it at
    in 1080p, every point is kept, so every joint is a sharp one."""
    pts = [(lat, lon)]
    heading = rng.uniform(0, 2 * math.pi)
    for i in range(n - 1):
        heading += (1 if i % 2 else -1) * math.radians(rng.uniform(30, 120))
        lat += 0.300 / 111.2 * math.cos(heading)
        lon += 0.300 / (111.2 * math.cos(math.radians(lat))) * math.sin(heading)
        pts.append((lat, lon))
    return pts


def dense_trip() -> Project:
    """D5's dense trip: a ride, a run and a hike of 3,000 points each with a
    drifting heading, each from where the last ended, then a 300-point
    zig-zag walk."""
    rng = random.Random(525)
    ride = _track(rng, 45.0, 6.0, 3000)
    run = _track(rng, *ride[-1], 3000)
    hike = _track(rng, *run[-1], 3000)
    walk = _zigzag(rng, *hike[-1], 300)
    activities = [_activity(1, "Ride", date(2026, 5, 1), ride),
                  _activity(2, "Run", date(2026, 5, 2), run),
                  _activity(3, "Hike", date(2026, 5, 3), hike),
                  _activity(4, "Walk", date(2026, 5, 4), walk)]
    return Project(name="Dense", activities=activities,
                   items=[ProjectItem(item_type="activity", activity_id=a.id) for a in activities])


@functools.lru_cache(maxsize=None)
def dense_timeline():
    """:func:`dense_trip` as a 10 s video: 300 frames at 30 fps."""
    return build_timeline(build_legs(dense_trip()), 10.0)


def _turns(pts, idx):
    """The turn at each interior kept point of a leg, in degrees."""
    out = []
    for p in range(1, len(idx) - 1):
        a, b, c = (idx[p + d] for d in (-1, 0, 1))
        ux, uy = pts[2 * b] - pts[2 * a], pts[2 * b + 1] - pts[2 * a + 1]
        vx, vy = pts[2 * c] - pts[2 * b], pts[2 * c + 1] - pts[2 * b + 1]
        out.append(abs(math.degrees(math.atan2(ux * vy - uy * vx, ux * vx + uy * vy))))
    return out


@pytest.mark.parametrize("mode", ["variable", "fixed"])
def test_the_dense_trips_zigzag_turns_30_to_120_degrees_where_followed(mode):
    """D5: wherever a follow camera frames the walk at 1080p, consecutive
    kept points of it turn by 30–120°."""
    tl = dense_timeline()
    route = _overlay(dense_timeline, HD).route
    walk = len(tl.legs) - 1
    shots = camera_path(tl, tl.fps, HD, mode)
    levels = {int(shot.zoom) for n, shot in enumerate(shots)
              if (s := tl.sample(n / tl.fps)).kind == "clip" and s.leg_index == walk}
    assert levels
    for level in levels:
        idx = route.kept(level)[walk]
        assert len(idx) == tl.legs[walk].n_points, level
        assert all(30 <= t <= 120 for t in _turns(route.world[walk], idx)), level


@functools.lru_cache(maxsize=None)
def _overlay(tl_fn, size) -> Overlay:
    """``_draw_route`` doesn't depend on the camera mode: one overlay per
    trip and size serves every mode's shots."""
    return Overlay(tl_fn(), size, "Trip")


def _pairs(tl, size, mode):
    """Every (shot, state) pair of *tl*'s video in *mode* at *size*."""
    shots = camera_path(tl, tl.fps, size, mode)
    return [(shot, tl.sample(n / tl.fps)) for n, shot in enumerate(shots)]


def _draw(ov: Overlay, shot: Shot, state, clip: bool) -> bytes:
    overlay_module._CLIP = clip
    try:
        frame = Image.new("RGB", ov.size, _BASE)
        ov._draw_route(frame, shot, state)
        return frame.tobytes()
    finally:
        overlay_module._CLIP = True


def _runs(ov: Overlay, shot: Shot, state):
    """Each visible leg's runs, clipping on."""
    return [runs for _, _, runs in ov._lines(shot, state)[3]]


def _clipped(ov: Overlay, shot: Shot, state) -> bool:
    """Whether clipping leaves out any segment of a visible leg."""
    return any(runs != [(0, len(idx) - 1)] for _, idx, runs in ov._lines(shot, state)[3])


def _several_runs(ov: Overlay, shot: Shot, state) -> bool:
    return any(len(runs) > 1 for runs in _runs(ov, shot, state))


def _assert_identical(ov: Overlay, pairs, label: str):
    """Clipping on and off draw the same bytes on every pair; returns how
    many pairs clipping left something out of and drew a route on."""
    blank = Image.new("RGB", ov.size, _BASE).tobytes()
    clipped = drawn = 0
    for n, (shot, state) in enumerate(pairs):
        on = _draw(ov, shot, state, True)
        assert on == _draw(ov, shot, state, False), f"{label}: pair {n} differs"
        clipped += _clipped(ov, shot, state)
        drawn += on != blank
    assert drawn, f"{label}: no pair draws a route"
    return clipped


# ── identity on the renderer's synthetic trips ───────────────────────────────

TRIPS = {"paris_lyon": timeline30, "paris_new_york": timeline30_ny,
         "pacific": timeline_pacific}


@pytest.mark.parametrize("trip", sorted(TRIPS))
def test_clipping_changes_no_pixel_of_the_renderers_trips_at_320x180(trip):
    tl_fn = TRIPS[trip]
    ov = _overlay(tl_fn, SMALL)
    clipped = sum(_assert_identical(ov, _pairs(tl_fn(), SMALL, mode), f"{trip} {mode}")
                  for mode in CAMERA_MODES)
    assert clipped, f"{trip}: clipping never left anything out"


@pytest.mark.parametrize("trip", sorted(TRIPS))
def test_clipping_changes_no_pixel_of_the_renderers_trips_at_1080p(trip):
    tl_fn = TRIPS[trip]
    ov = _overlay(tl_fn, HD)
    stride = 1 if os.environ.get("VIDEO_CLIP_ALL") else HD_STRIDE
    clipped = 0
    for mode in CAMERA_MODES:
        pairs = _pairs(tl_fn(), HD, mode)
        pairs = [p for n, p in enumerate(pairs) if n % stride == 0 or _several_runs(ov, *p)]
        clipped += _assert_identical(ov, pairs, f"{trip} {mode}")
    assert clipped, f"{trip}: clipping never left anything out"


# ── identity on the dense trip ───────────────────────────────────────────────

def _dense_sample(ov: Overlay, size, mode):
    """At most :data:`DENSE_PAIRS` pairs of the dense trip's video: the
    first at each integer zoom, the first few where a leg is cut into several
    runs or the marker's segment crosses the frame's edge, then frames spread
    over the video."""
    pairs = _pairs(dense_timeline(), size, mode)
    picked, zooms, several, marker = [], set(), 0, 0
    for n, (shot, state) in enumerate(pairs):
        z = int(shot.zoom)
        if z not in zooms:
            zooms.add(z)
            picked.append(n)
        elif several < 5 and _several_runs(ov, shot, state):
            several += 1
            picked.append(n)
        elif marker < 5 and _marker_crosses_edge(ov, shot, state):
            marker += 1
            picked.append(n)
    step = max(1, len(pairs) // 12)
    picked += [n for n in range(0, len(pairs), step) if n not in picked]
    return [pairs[n] for n in sorted(picked[:DENSE_PAIRS])]


def _marker_crosses_edge(ov: Overlay, shot: Shot, state) -> bool:
    """Whether the travelled line's last segment, from its last kept point
    to the marker, has one end in the frame and the other out of it."""
    (scale, ox, oy), _, _, faint, travelled = ov._lines(shot, state)
    w, h = ov.size
    for _, j, m, marker in travelled:
        if marker is None:
            continue
        pts, idx, _ = faint[j]
        ends = [(pts[2 * idx[m - 1]], pts[2 * idx[m - 1] + 1]), marker]
        inside = [0 <= x * scale + ox < w and 0 <= y * scale + oy < h for x, y in ends]
        return inside[0] != inside[1]
    return False


@pytest.mark.parametrize("size", [SMALL, HD], ids=["320x180", "1080p"])
@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_clipping_changes_no_pixel_of_the_dense_trip(mode, size):
    ov = _overlay(dense_timeline, size)
    pairs = _dense_sample(ov, size, mode)
    assert len(pairs) <= DENSE_PAIRS
    clipped = _assert_identical(ov, pairs, f"dense {mode}")
    if mode != "overview":
        assert clipped, f"{mode}: clipping never left anything out"


# ── hand-made frames ─────────────────────────────────────────────────────────

def _one_leg(points, type="Ride"):
    """A one-activity trip of (lat, lon) *points*, as a 10 s timeline."""
    project = Project(name="Leg", activities=[_activity(1, type, date(2026, 5, 1), points)],
                      items=[ProjectItem(item_type="activity", activity_id=1)])
    return build_timeline(build_legs(project), 10.0)


def _shot_at(cx: float, cy: float, scale: float) -> Shot:
    """The shot centred on world (cx, cy), *scale* frame px per world unit."""
    return Shot(*world_to_lonlat(cx, cy), math.log2(scale / TILE_SIZE), False)


# A hairpin near the equator: east along lat +0.01 from lon 10.00 to 10.05,
# then back west along lat -0.01.
_HAIRPIN = ([(0.01, 10.0 + 0.002 * i) for i in range(26)] +
            [(-0.01, 10.05 - 0.002 * i) for i in range(26)])


@functools.lru_cache(maxsize=None)
def _hairpin():
    return _one_leg(_HAIRPIN)


def _hairpin_shot(lon: float, half_lat: float) -> Shot:
    """A 320×180 shot centred on (*lon*, 0) whose frame is ±*half_lat*
    degrees high (a degree is about the same in x and y at the equator)."""
    cx, cy = lonlat_to_world(lon, 0.0)
    _, top = lonlat_to_world(lon, half_lat)
    return _shot_at(cx, cy, (SMALL[1] / 2) / (cy - top))


def test_a_leg_across_the_frame_twice_gives_two_runs():
    tl = _hairpin()
    ov = Overlay(tl, SMALL, "Hairpin")
    shot, state = _hairpin_shot(10.005, 0.02), tl.sample(tl.total_s)
    assert len(_runs(ov, shot, state)[0]) == 2
    on = _draw(ov, shot, state, True)
    assert on == _draw(ov, shot, state, False)
    assert on != Image.new("RGB", SMALL, _BASE).tobytes()


def test_a_leg_off_screen_inside_a_visible_bbox_gives_no_run():
    """The frame sits inside the hairpin, clear of both arms and its turn:
    the leg's bbox is visible, none of its segments is."""
    tl = _hairpin()
    ov = Overlay(tl, SMALL, "Hairpin")
    shot, state = _hairpin_shot(10.025, 0.004), tl.sample(tl.total_s)
    faint = ov._lines(shot, state)[3]
    assert len(faint) == 1                       # visible by its bbox
    assert faint[0][2] == []
    assert _draw(ov, shot, state, True) == _draw(ov, shot, state, False)


def test_a_leg_that_leaves_and_comes_back_is_drawn_identically():
    """The hairpin travelled part-way back, its turn off screen: the travelled
    line is cut in two runs, the second ending at the marker."""
    tl = _hairpin()
    ov = Overlay(tl, SMALL, "Hairpin")
    shot = _hairpin_shot(10.005, 0.02)
    state = next(s for s in (tl.sample(n / tl.fps) for n in range(round(tl.total_s * tl.fps)))
                 if s.kind == "clip" and s.lat < 0 and s.lon < 10.03)
    assert len(_runs(ov, shot, state)[0]) == 2
    assert _draw(ov, shot, state, True) == _draw(ov, shot, state, False)


# A straight ride east along the equator in six 0.02° steps: the marker is
# on a long segment.
_SPARSE = [(0.0, 10.0 + 0.02 * i) for i in range(7)]


@pytest.mark.parametrize("marker_at", [1.25, 0.25], ids=["marker-out", "marker-in"])
def test_the_marker_on_a_segment_across_the_frames_edge(marker_at):
    """The frame's right edge cuts the travelled line's last segment, from
    its last kept point to the marker: the marker 1/4 frame out with that
    point 3/4 in (the segment carries on the run before it), or the marker
    1/4 in with the point 1/4 out (the segment is a run of its own)."""
    tl = _one_leg(_SPARSE)
    ov = Overlay(tl, SMALL, "Sparse")
    clip = tl.clips[0]
    state = tl.sample(clip.start_s + 0.55 * (clip.end_s - clip.start_s))
    assert state.kind == "clip"
    probe = Shot(state.lon, state.lat, 10.0, False)
    _, _, _, faint, travelled = ov._lines(probe, state)
    (_, j, m, (mx, my)), = travelled
    pts, idx, _ = faint[j]
    px, py = pts[2 * idx[m - 1]], pts[2 * idx[m - 1] + 1]
    assert m >= 3 and mx > px
    # The point and the marker half a frame apart, the marker at marker_at
    # frame widths from the left edge.
    scale = SMALL[0] / 2 / (mx - px)
    cx = mx - (marker_at - 0.5) * SMALL[0] / scale
    shot = _shot_at(cx, my, scale)
    assert _marker_crosses_edge(ov, shot, state)
    assert _clipped(ov, shot, state)
    on = _draw(ov, shot, state, True)
    assert on == _draw(ov, shot, state, False)
    assert on != Image.new("RGB", SMALL, _BASE).tobytes()


# ── whole frames ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_whole_frames_are_identical_with_and_without_clipping(mode, monkeypatch):
    tl = dense_timeline()
    out = {}
    for clip in (True, False):
        monkeypatch.setattr(overlay_module, "_CLIP", clip)
        frames = FrameRenderer(tl, SMALL, "Dense", tile_fetcher=fake_tile, camera=mode)
        try:
            out[clip] = [frames.frame(n).tobytes() for n in range(0, len(frames), 60)]
        finally:
            frames.close()
    assert len(out[True]) >= 5
    assert out[True] == out[False]

"""The follow camera of a trip video (docs/TRIP_VIDEO_PLAN.md, "Camera (U2)").

:func:`camera` gives the map centre and zoom of any frame. The title and end
cards show the whole trip; a clip follows the marker at a zoom that fits the
clip's route, never below its sub-leg's mode floor; every hand-off (card to
clip, clip to clip, a jump between legs) is a 0.6 s eased fly-to that lands
on the next view as it starts.

The whole path is computed at once on the frame clock and cached per
(timeline, fps, size), so frame *n* is a pure function of those and *n*
(Convention 3) however the frames are asked for.

Zoom is Web Mercator zoom on the 512 px logical tile grid of
``src/poster/tile_stitcher.py``. That module's projection math is re-derived
here rather than imported: it loads Pillow and requests at import time, and
this module must not (Convention 4).

Longitudes may lie outside [-180, 180]: the legs' are unwrapped across the
trip (``legs.unwrap_lons``), and the projection is linear in longitude, so a
route over the ±180° meridian is framed and followed along its short way, with
world x below 0 or above 1. The basemap wraps them back into the world.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from src.video.timeline import FrameState, TimedClip, TimedLeg, Timeline

TILE_SIZE = 512          # tile_stitcher.DEFAULT_TILE_SIZE
MIN_ZOOM = 0.0
MAX_ZOOM = 16.0          # a short walk is shown at street level, not closer
MAX_LAT = 85.0511287798  # Web Mercator's latitude limit

# The trip fills this much of the frame on the title and end cards, a clip's
# route this much while it is followed.
OVERVIEW_FILL = 0.85
CLIP_FILL = 0.6

# The lowest zoom a clip may use while the marker travels on each mode, so a
# walk is never shown from a plane's altitude. Flights have none.
MODE_MIN_ZOOM: Dict[str, float] = {
    "hike": 12.0, "run": 12.0,
    "ride": 10.0, "other": 10.0,
    "train": 6.0, "bus": 6.0, "boat": 6.0,
    "flight": MIN_ZOOM,
}

TRANSITION_S = 0.6       # each fly-to, and each zoom ramp inside a clip
SPRING_OMEGA = 10.0      # rad/s: critically damped, settles in ~0.5 s
LEASH = 0.3              # the marker stays within this fraction of the frame
                         # of the centre, whatever the spring's lag
CUT = 0.5                # a jump of the aim of more than this fraction of
                         # the frame in one frame (a gap between two legs)
                         # is flown to, not followed
FOLLOW_MIN_S = 1.0       # a sub-leg shorter than this isn't followed
_RHO = math.sqrt(2.0)    # van Wijk & Nuij's zoom/pan trade-off (d3's default)

Size = Tuple[int, int]
View = Tuple[float, float, float]   # (x, y, zoom), x/y in world units [0, 1]


class Shot(NamedTuple):
    """The camera at one frame. *flying* is True inside the fly-to before a
    cut (a card or clip starting, or the marker jumping a gap between legs),
    where the camera is between two views rather than following."""
    lon: float
    lat: float
    zoom: float
    flying: bool


# ── Web Mercator ─────────────────────────────────────────────────────────────

def lonlat_to_world(lon: float, lat: float) -> Tuple[float, float]:
    """(lon, lat) degrees → Web Mercator (x, y) in [0, 1], (0, 0) at the NW
    corner: ``tile_stitcher.lonlat_to_pixel`` divided by the world size.
    A longitude past ±180 maps past the world's edge (x < 0 or x > 1)."""
    lat = math.radians(min(max(lat, -MAX_LAT), MAX_LAT))
    x = (lon + 180.0) / 360.0
    y = (1.0 - math.log(math.tan(lat) + 1.0 / math.cos(lat)) / math.pi) / 2.0
    return x, y


def world_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    """The inverse of :func:`lonlat_to_world`, unwrapped: x outside [0, 1]
    gives a longitude outside [-180, 180]."""
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))
    return lon, lat


def _world_px(zoom: float) -> float:
    """The world's width in pixels at *zoom*."""
    return TILE_SIZE * 2.0 ** zoom


def viewport_bounds(lon: float, lat: float, zoom: float, size: Size) -> Dict[str, float]:
    """The lon/lat box a *size* = (width, height) frame shows around
    (lon, lat) at *zoom*, as ``{"west", "south", "east", "north"}``; west
    is always below east, either may lie past ±180."""
    x, y = lonlat_to_world(lon, lat)
    hw, hh = size[0] / 2.0 / _world_px(zoom), size[1] / 2.0 / _world_px(zoom)
    west, north = world_to_lonlat(x - hw, y - hh)
    east, south = world_to_lonlat(x + hw, y + hh)
    return {"west": west, "south": south, "east": east, "north": north}


def _fit(points: Iterable[Tuple[float, float]], size: Size, fill: float) -> View:
    """Centre and zoom that fit the world-unit bbox of *points* into *fill*
    of the frame, within [MIN_ZOOM, MAX_ZOOM]."""
    xs, ys = zip(*points)
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    zoom = MAX_ZOOM
    if w > 0:
        zoom = min(zoom, math.log2(fill * size[0] / (TILE_SIZE * w)))
    if h > 0:
        zoom = min(zoom, math.log2(fill * size[1] / (TILE_SIZE * h)))
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0,
            max(MIN_ZOOM, zoom))


# ── motion primitives ────────────────────────────────────────────────────────

def _smoothstep(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3.0 - 2.0 * u)


def _spring(pos: float, vel: float, target: float, dt: float) -> Tuple[float, float]:
    """One exact step of a critically damped spring towards a *target* held
    for *dt* — exact, so the result doesn't depend on step size."""
    d = pos - target
    e = math.exp(-SPRING_OMEGA * dt)
    k = (vel + SPRING_OMEGA * d) * dt
    return target + (d + k) * e, (vel - SPRING_OMEGA * k) * e


def _fly(a: View, b: View, s: float, size: Size) -> View:
    """The view a fraction *s* of the way along van Wijk & Nuij's smooth
    zoom-and-pan path from *a* to *b*: it pulls out for long pans, so the
    picture moves at a steady rate instead of sliding a world past at
    street level."""
    w0 = size[0] / _world_px(a[2])
    w1 = size[0] / _world_px(b[2])
    dx, dy = b[0] - a[0], b[1] - a[1]
    d1 = math.hypot(dx, dy)
    if d1 < 1e-12:
        return a[0] + s * dx, a[1] + s * dy, a[2] + s * (b[2] - a[2])
    rho2 = _RHO * _RHO
    b0 = (w1 * w1 - w0 * w0 + rho2 * rho2 * d1 * d1) / (2 * w0 * rho2 * d1)
    b1 = (w1 * w1 - w0 * w0 - rho2 * rho2 * d1 * d1) / (2 * w1 * rho2 * d1)
    r0, r1 = -math.asinh(b0), -math.asinh(b1)   # log(√(b² + 1) − b), stably
    r = r0 + s * (r1 - r0)
    u = w0 / (rho2 * d1) * (math.cosh(r0) * math.tanh(r) - math.sinh(r0))
    w = w0 * math.cosh(r0) / math.cosh(r)
    return a[0] + u * dx, a[1] + u * dy, math.log2(size[0] / (TILE_SIZE * w))


# ── the path ─────────────────────────────────────────────────────────────────

def mode_min_zoom(mode: str) -> float:
    return MODE_MIN_ZOOM.get(mode, MODE_MIN_ZOOM["other"])


def frame_count(timeline: Timeline, fps: int) -> int:
    return int(round(timeline.total_s * fps))


def _zoom_plan(levels: Sequence[float], ramp: int) -> List[float]:
    """Smooth a clip's per-frame zoom *levels* without ever going below them.

    Each frame's level is a plateau with smoothstep shoulders *ramp* frames
    wide that fall to the clip's lowest level; the zoom is their upper
    envelope. So the camera zooms in before a sub-leg with a higher floor
    starts and out after it ends — always on the lower floor's side, where
    the lower zoom is allowed.
    """
    low = min(levels)
    out = []
    for n in range(len(levels)):
        best = levels[n]
        for m in range(max(0, n - ramp), min(len(levels), n + ramp + 1)):
            if levels[m] > best:
                z = levels[m] - (levels[m] - low) * _smoothstep(abs(n - m) / ramp)
                best = max(best, z)
        out.append(best)
    return out


def is_followed(sub: TimedLeg) -> bool:
    """Whether the camera follows the marker along *sub*: not when it is
    drawn at once, nor when it is too short to follow. The mode floors hold
    only while a sub-leg is followed."""
    return not sub.instant and sub.end_s - sub.start_s >= FOLLOW_MIN_S


def _clip_aims(clip: TimedClip, size: Size) -> List[Tuple[Optional[Tuple[float, float]], float]]:
    """Per sub-leg of *clip*: where the camera aims — None for the marker,
    else a fixed world point — and its zoom level.

    A followed sub-leg: the marker, at the clip's fit zoom raised to the
    mode's floor. A run of sub-legs not followed shows the whole clip at its
    fit zoom — when it lasts at least a transition; a shorter run holds the
    start of the next followed sub-leg (else the end of the previous one) at
    that one's level, so it neither pulses the zoom nor forces a fly-to."""
    cx, cy, fit = _fit([lonlat_to_world(lon, lat) for sub in clip.subs
                        for lon, lat in sub.leg.coords], size, CLIP_FILL)
    followed = [is_followed(sub) for sub in clip.subs]
    level = [min(MAX_ZOOM, max(fit, mode_min_zoom(sub.leg.mode))) for sub in clip.subs]
    aims: List[Tuple[Optional[Tuple[float, float]], float]] = []
    i = 0
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
        if clip.subs[j - 1].end_s - clip.subs[i].start_s >= TRANSITION_S or (
                after is None and before is None):
            aim = ((cx, cy), fit)
        elif after is not None:
            aim = (lonlat_to_world(*clip.subs[after].leg.coords[0]), level[after])
        else:
            aim = (lonlat_to_world(*clip.subs[before].leg.coords[-1]), level[before])
        aims.extend([aim] * (j - i))
        i = j
    return aims


def overview(timeline: Timeline, size: Size) -> View:
    """The title and end cards' view: the whole trip in
    :data:`OVERVIEW_FILL` of the frame."""
    return _fit((lonlat_to_world(lon, lat) for leg in timeline.legs for lon, lat in leg.coords),
                size, OVERVIEW_FILL)


def _off_frame(point: Tuple[float, float], view: View, size: Size) -> float:
    """How far *point* is from *view*'s centre, in frame widths or heights
    (whichever is more): beyond 0.5 it is off screen."""
    px = _world_px(view[2])
    return max(abs(point[0] - view[0]) * px / size[0], abs(point[1] - view[1]) * px / size[1])


def _leash(view: View, point: Tuple[float, float], reach: float, size: Size) -> View:
    """*view* moved the least needed to bring *point* within *reach* frame
    widths/heights of its centre."""
    hw, hh = reach * size[0] / _world_px(view[2]), reach * size[1] / _world_px(view[2])
    return (min(max(view[0], point[0] - hw), point[0] + hw),
            min(max(view[1], point[1] - hh), point[1] + hh), view[2])


def _segment_of(state: FrameState, n_clips: int) -> int:
    """-1 on the title card, the clip index in a clip, *n_clips* on the end card."""
    if state.kind == "title":
        return -1
    if state.kind == "end":
        return n_clips
    return state.clip_index


def camera_path(timeline: Timeline, fps: int, size: Size) -> Tuple[Shot, ...]:
    """Every frame's :class:`Shot`, frame 0 first."""
    return _path(timeline, int(fps), (int(size[0]), int(size[1])))


@lru_cache(maxsize=4)
def _path(timeline: Timeline, fps: int, size: Size) -> Tuple[Shot, ...]:
    n_frames = frame_count(timeline, fps)
    dt = 1.0 / fps
    ramp = max(1, int(round(TRANSITION_S * fps)))
    home = overview(timeline, size)
    aims = [_clip_aims(clip, size) for clip in timeline.clips]

    # Each frame's aim: the overview on the cards, else its sub-leg's aim.
    samples = [timeline.sample(n * dt) for n in range(n_frames)]
    segs = [_segment_of(s, len(timeline.clips)) for s in samples]
    target: List[View] = []
    for s, seg in zip(samples, segs):
        if 0 <= seg < len(timeline.clips):
            point, level = aims[seg][s.sub_index]
            x, y = point if point is not None else lonlat_to_world(s.lon, s.lat)
            target.append((x, y, level))
        else:
            target.append(home)
    zoom = [v[2] for v in target]
    start = 0
    for n in range(1, n_frames + 1):
        if n == n_frames or segs[n] != segs[start]:
            if 0 <= segs[start] < len(timeline.clips):
                zoom[start:n] = _zoom_plan(zoom[start:n], ramp)
            start = n

    # Cuts: a card or clip starts, or the aim jumps (a gap between legs).
    cuts = [n for n in range(1, n_frames) if segs[n] != segs[n - 1] or _off_frame(
        target[n - 1][:2], (*target[n][:2], min(zoom[n - 1], zoom[n])), size) > CUT]

    # Follow: a critically damped spring on the centre, reset on every cut.
    follow: List[View] = []
    x = y = vx = vy = 0.0
    cut_set = set(cuts)
    for n in range(n_frames):
        tx, ty, _ = target[n]
        if n == 0 or n in cut_set:
            x, y, vx, vy = tx, ty, 0.0, 0.0
        else:
            x, vx = _spring(x, vx, tx, dt)
            y, vy = _spring(y, vy, ty, dt)
            x, y, _ = _leash((x, y, zoom[n]), (tx, ty), LEASH, size)
        follow.append((x, y, zoom[n]))

    # Fly: the TRANSITION_S before each cut glides from the view being
    # followed to the cut's first view — known in advance, so the camera has
    # pulled out before a flight takes off and is in place before a walk
    # starts, rather than chasing a marker that is already moving.
    views = list(follow)
    flying = [False] * n_frames
    prev = 0
    for c in cuts:
        w0 = max(prev, c - ramp)
        for n in range(w0 + 1, c):
            views[n] = _fly(follow[n], follow[c], _smoothstep((n - w0) / (c - w0)), size)
            flying[n] = True
        prev = c
    return tuple(Shot(*world_to_lonlat(wx, wy), wz, f)
                 for (wx, wy, wz), f in zip(views, flying))


def camera(timeline: Timeline, frame_n: int, fps: int, size: Size) -> Tuple[float, float, float]:
    """``(lon, lat, zoom)`` of frame *frame_n* of *timeline* rendered at
    *fps* into a *size* = (width, height) frame.

    Raises ValueError for a frame outside ``0 … frame_count − 1``.
    """
    path = camera_path(timeline, fps, size)
    if not 0 <= frame_n < len(path):
        raise ValueError(f"frame {frame_n} is outside 0..{len(path) - 1}")
    shot = path[frame_n]
    return shot.lon, shot.lat, shot.zoom

"""The follow camera of a trip video (docs/TRIP_VIDEO_PLAN.md, "Camera (U2)").

:func:`camera` gives the map centre and zoom of any frame. The title and end
cards show the whole trip; a clip follows the marker at a zoom that fits the
clip's route, never below its sub-leg's mode floor; every hand-off (card to
clip, clip to clip, a jump between legs) is a 0.6 s eased fly-to that lands
on the next view as it starts.

That is the ``"variable"`` camera. Three more modes
(docs/VIDEO_CAMERA_QUALITY_PLAN.md, D1-D3): ``"overview"`` holds the title
card's view of the whole trip on every frame; ``"fixed"`` and
``"fixed_strict"`` follow the marker with the same machinery at one integer
zoom chosen by :func:`fixed_zoom` — ``"fixed"`` pulls out to the clip's fit
for a sub-leg too fast to follow at that zoom, ``"fixed_strict"`` picks a zoom
low enough that none is.

The whole path is computed at once on the frame clock and cached per
(timeline, fps, size, mode), so frame *n* is a pure function of those and *n*
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
import statistics
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

CAMERA_MODES = ("variable", "overview", "fixed", "fixed_strict")

# Fixed zoom: a followed sub-leg is *fast* at a zoom where its marker moves
# more than this many frame widths or heights (whichever is more) per second.
# The spring lags a steady pan by 2v/ω, so this is LEASH·SPRING_OMEGA/2: the
# speed at which the lag reaches the leash.
FIXED_MAX_PAN_PER_S = 1.5
# The speed is the marker's move over this long — the spring's lag time — so
# a wiggle in the line doesn't count as speed and a sustained pan does.
PAN_WINDOW_S = 2.0 / SPRING_OMEGA
# The search for the fixed zoom runs from FIXED_TOP_ZOOM down to the mode's
# floor (see :func:`fixed_floor`).
FIXED_TOP_ZOOM = 14
# Fixed zoom: a run of sub-legs too short to follow is still chased when its
# clip is wider than the frame, if the marker moves at most this many frame
# widths or heights per second (a tenth of the frame per frame at 30 fps,
# what the camera may move while following) and jumps a gap no more often
# than once a transition.
CHASE_MAX_PAN_PER_S = 3.0
FIXED_FLOOR: Dict[str, int] = {"fixed": 4, "fixed_strict": 2}

# basemap_bands' sheet planning, duplicated for :func:`estimate_tiles`: that
# module loads Pillow, this one must not (a test pins the two together).
PIXEL_RATIO = 2
FADE = 0.2
GAP_FRAMES = 15
SHEET_MAX_FRAMES = 12
MARGIN_PX = 2
MAX_TILES = 3000

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


Aim = Tuple[Optional[Tuple[float, float]], float, bool]


def _can_chase(steps: Sequence[float], zoom: float, fps: int) -> bool:
    """Whether the camera can follow a marker making these per-frame *steps*
    (in frame widths or heights at zoom 0, as :func:`_off_frame`) at *zoom*
    without jumping: every step within :data:`CHASE_MAX_PAN_PER_S`, or a gap
    over :data:`CUT`, flown across, at least a transition after the last."""
    ramp = max(1, int(round(TRANSITION_S * fps)))
    last = -ramp
    for n, step in enumerate(steps):
        step *= 2.0 ** zoom
        if step > CUT:
            if n - last < ramp:
                return False
            last = n
        elif step > CHASE_MAX_PAN_PER_S / fps:
            return False
    return True


class _Fixed(NamedTuple):
    """A clip's framing at fixed zoom *z*: which of its sub-legs are fast,
    and each sub-leg's marker steps into its frames (see :func:`_marker_steps`)
    on a *fps* clock."""
    z: int
    fast: Sequence[bool]
    steps: Sequence[Sequence[float]]
    fps: int


def _clip_aims(clip: TimedClip, size: Size, fixed: Optional[_Fixed] = None) -> List[Aim]:
    """Per sub-leg of *clip*: where the camera aims — None for the marker,
    else a fixed world point — its zoom level, and whether it is framed as a
    fast sub-leg.

    A followed sub-leg: the marker, at the clip's fit zoom raised to the
    mode's floor. A run of sub-legs not followed shows the whole clip at its
    fit zoom — when it lasts at least a transition; a shorter run holds the
    start of the next followed sub-leg (else the end of the previous one) at
    that one's level, so it neither pulses the zoom nor forces a fly-to.

    With *fixed*, every level is Z except a fast sub-leg's (and a short
    run's holding one), which is the clip's fit zoom without the mode floor.
    When the clip doesn't fit in :data:`OVERVIEW_FILL` of the frame at Z, a
    run not followed chases the marker at its level instead, rather than
    leave it off-screen — unless it moves too fast or jumps too often to
    chase (:func:`_can_chase`)."""
    points = [lonlat_to_world(lon, lat) for sub in clip.subs for lon, lat in sub.leg.coords]
    cx, cy, fit = _fit(points, size, CLIP_FILL)
    followed = [is_followed(sub) for sub in clip.subs]
    if fixed is None:
        level = [min(MAX_ZOOM, max(fit, mode_min_zoom(sub.leg.mode))) for sub in clip.subs]
        fast: Sequence[bool] = [False] * len(clip.subs)
        whole = fit
        chase = False
    else:
        fast = fixed.fast
        level = [fit if f else float(fixed.z) for f in fast]
        whole = float(fixed.z)
        chase = _fit(points, size, OVERVIEW_FILL)[2] < fixed.z
    aims: List[Aim] = []
    i = 0
    while i < len(clip.subs):
        if followed[i]:
            aims.append((None, level[i], fast[i]))
            i += 1
            continue
        j = i
        while j < len(clip.subs) and not followed[j]:
            j += 1
        after = next((k for k in range(j, len(clip.subs)) if followed[k]), None)
        before = next((k for k in range(i - 1, -1, -1) if followed[k]), None)
        if clip.subs[j - 1].end_s - clip.subs[i].start_s >= TRANSITION_S or (
                after is None and before is None):
            aim = ((cx, cy), whole, False)
        elif after is not None:
            aim = (lonlat_to_world(*clip.subs[after].leg.coords[0]), level[after], fast[after])
        else:
            aim = (lonlat_to_world(*clip.subs[before].leg.coords[-1]), level[before],
                   fast[before])
        if chase and _can_chase([step for k in range(i, j) for step in fixed.steps[k]],
                                aim[1], fixed.fps):
            aim = (None, aim[1], aim[2])
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


def camera_path(timeline: Timeline, fps: int, size: Size,
                mode: str = "variable") -> Tuple[Shot, ...]:
    """Every frame's :class:`Shot` in camera *mode* (one of
    :data:`CAMERA_MODES`), frame 0 first."""
    if mode not in CAMERA_MODES:
        raise ValueError(f"unknown camera mode {mode!r}")
    return _path(timeline, int(fps), (int(size[0]), int(size[1])), mode)


@lru_cache(maxsize=4)
def _path(timeline: Timeline, fps: int, size: Size, mode: str) -> Tuple[Shot, ...]:
    if mode == "overview":
        x, y, z = overview(timeline, size)
        return (Shot(*world_to_lonlat(x, y), z, False),) * frame_count(timeline, fps)
    if mode == "variable":
        return _follow(timeline, fps, size, [_clip_aims(clip, size) for clip in timeline.clips])
    return _fixed_path(timeline, fps, size, mode, fixed_zoom(timeline, size, mode))


@lru_cache(maxsize=4)
def _samples(timeline: Timeline, fps: int) -> Tuple[FrameState, ...]:
    dt = 1.0 / fps
    return tuple(timeline.sample(n * dt) for n in range(frame_count(timeline, fps)))


def _follow(timeline: Timeline, fps: int, size: Size,
            aims: Sequence[Sequence[Aim]]) -> Tuple[Shot, ...]:
    """The path through each clip's per-sub-leg *aims*. A clip is one
    segment, split where its aims switch between fast and not (fixed mode),
    so the camera flies to and from a fast sub-leg as it does between clips."""
    n_frames = frame_count(timeline, fps)
    dt = 1.0 / fps
    ramp = max(1, int(round(TRANSITION_S * fps)))
    home = overview(timeline, size)
    groups = []
    for clip_aims in aims:
        g, out = 0, []
        for k, aim in enumerate(clip_aims):
            g += k > 0 and aim[2] != clip_aims[k - 1][2]
            out.append(g)
        groups.append(out)

    # Each frame's aim: the overview on the cards, else its sub-leg's aim.
    samples = _samples(timeline, fps)
    segs: List[object] = []
    target: List[View] = []
    for s in samples:
        seg = _segment_of(s, len(timeline.clips))
        if 0 <= seg < len(timeline.clips):
            point, level, _ = aims[seg][s.sub_index]
            x, y = point if point is not None else lonlat_to_world(s.lon, s.lat)
            target.append((x, y, level))
            segs.append((seg, groups[seg][s.sub_index]))
        else:
            target.append(home)
            segs.append(seg)
    zoom = [v[2] for v in target]
    start = 0
    for n in range(1, n_frames + 1):
        if n == n_frames or segs[n] != segs[start]:
            if samples[start].kind == "clip":
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


# ── fixed zoom ───────────────────────────────────────────────────────────────

@lru_cache(maxsize=4)
def _pan_speeds(timeline: Timeline, size: Size) -> Dict[Tuple[int, int], float]:
    """Per followed sub-leg, keyed (clip index, sub index): the fastest its
    marker moves over :data:`PAN_WINDOW_S`, in frame widths or heights per
    second (whichever is more, as :func:`_off_frame`) at zoom 0 — times
    ``2 ** z`` at zoom z. Measured on the timeline's own frame clock, so it
    doesn't depend on the fps a path is asked for."""
    fps = timeline.fps
    samples = _samples(timeline, fps)
    m = max(1, int(round(PAN_WINDOW_S * fps)))
    span = m / fps
    speeds = {(clip.index, k): 0.0 for clip in timeline.clips
              for k, sub in enumerate(clip.subs) if is_followed(sub)}
    points = [lonlat_to_world(s.lon, s.lat) for s in samples]
    for n in range(len(samples) - m):
        a, b = samples[n], samples[n + m]
        key = (a.clip_index, a.sub_index)
        if key not in speeds or (b.clip_index, b.sub_index) != key:
            continue
        (ax, ay), (bx, by) = points[n], points[n + m]
        v = max(abs(bx - ax) * TILE_SIZE / size[0], abs(by - ay) * TILE_SIZE / size[1]) / span
        speeds[key] = max(speeds[key], v)
    return speeds


def _is_fast(speed: float, zoom: int) -> bool:
    return speed * 2.0 ** zoom > FIXED_MAX_PAN_PER_S


def _fixed_path(timeline: Timeline, fps: int, size: Size, mode: str, z: int) -> Tuple[Shot, ...]:
    """The path at fixed zoom *z*: in ``"fixed"`` the sub-legs fast at *z*
    are framed at their clip's fit, in ``"fixed_strict"`` none are."""
    speeds = _pan_speeds(timeline, size)
    steps = _marker_steps(timeline, fps, size)
    aims = [_clip_aims(clip, size, _Fixed(z, [
        mode == "fixed" and _is_fast(speeds.get((clip.index, k), 0.0), z)
        for k in range(len(clip.subs))], [steps.get((clip.index, k), ())
                                          for k in range(len(clip.subs))], fps))
            for clip in timeline.clips]
    return _follow(timeline, fps, size, aims)


@lru_cache(maxsize=4)
def _marker_steps(timeline: Timeline, fps: int, size: Size) -> Dict[Tuple[int, int], Tuple[float, ...]]:
    """Per sub-leg, keyed (clip index, sub index): how far the marker moves
    into each of its frames from the frame before, in frame widths or heights
    (whichever is more) at zoom 0 — times ``2 ** z`` at zoom z. A clip's
    first frame has none: the camera flies to it."""
    samples = _samples(timeline, fps)
    points = [lonlat_to_world(s.lon, s.lat) for s in samples]
    out: Dict[Tuple[int, int], List[float]] = {}
    for n in range(1, len(samples)):
        a, b = samples[n - 1], samples[n]
        if b.kind != "clip" or a.kind != "clip" or a.clip_index != b.clip_index:
            continue
        (ax, ay), (bx, by) = points[n - 1], points[n]
        step = max(abs(bx - ax) * TILE_SIZE / size[0], abs(by - ay) * TILE_SIZE / size[1])
        out.setdefault((b.clip_index, b.sub_index), []).append(step)
    return {key: tuple(v) for key, v in out.items()}


def fixed_zoom(timeline: Timeline, size: Size, mode: str) -> int:
    """The integer zoom ``Z`` of a ``"fixed"`` or ``"fixed_strict"`` path
    (docs/VIDEO_CAMERA_QUALITY_PLAN.md, D3): the highest z from
    :data:`FIXED_TOP_ZOOM` down to the mode's :func:`fixed_floor` that

    - ``"fixed"``: is at most the floor of the median ``CLIP_FILL`` fit zoom
      of the followed sub-legs not fast at z, and whose path — the fast ones
      at their clip's fit — needs at most :data:`MAX_TILES` tiles;
    - ``"fixed_strict"``: is at most the floor of the median fit zoom of all
      followed sub-legs, at which none is fast, and whose path needs at most
      :data:`MAX_TILES` tiles;

    else the floor. A trip with no followed sub-leg takes the median over
    its clips' fit zooms instead, the view it shows in variable mode."""
    if mode not in FIXED_FLOOR:
        raise ValueError(f"{mode!r} is not a fixed-zoom camera mode")
    return _fixed_zoom(timeline, (int(size[0]), int(size[1])), mode)


def fixed_floor(mode: str, size: Size) -> int:
    """The lowest fixed zoom of *mode* for a *size* frame: 4 for ``"fixed"``;
    for ``"fixed_strict"`` 2, raised so that no frame is wider than 180° of
    longitude (a *size*[0]-pixel frame at zoom z spans
    ``size[0] / (TILE_SIZE · 2**z)`` of the world): 3 at 1920 px."""
    if mode == "fixed_strict":
        return max(FIXED_FLOOR[mode], math.ceil(math.log2(size[0] / (TILE_SIZE / 2))))
    return FIXED_FLOOR[mode]


@lru_cache(maxsize=8)
def _fixed_zoom(timeline: Timeline, size: Size, mode: str) -> int:
    speeds = _pan_speeds(timeline, size)
    fits = {(ci, k): _fit([lonlat_to_world(lon, lat) for lon, lat
                           in timeline.clips[ci].subs[k].leg.coords], size, CLIP_FILL)[2]
            for ci, k in speeds}
    clip_fits = [_fit([lonlat_to_world(lon, lat) for sub in clip.subs
                       for lon, lat in sub.leg.coords], size, CLIP_FILL)[2]
                 for clip in timeline.clips]
    floor = fixed_floor(mode, size)
    for z in range(FIXED_TOP_ZOOM, floor - 1, -1):
        fast = {key for key, v in speeds.items() if _is_fast(v, z)}
        if mode == "fixed_strict":
            if fast:
                continue
            pool = list(fits.values())
        else:
            pool = [f for key, f in fits.items() if key not in fast]
            if fits and not pool:
                continue      # every followed sub-leg is fast at z
        if z > math.floor(statistics.median(pool or clip_fits)):
            continue
        if estimate_tiles(_fixed_path(timeline, timeline.fps, size, mode, z), size) > MAX_TILES:
            continue
        return z
    return floor


# ── tile estimate ────────────────────────────────────────────────────────────

def estimate_tiles(shots: Sequence[Shot], size: Size) -> int:
    """The map tiles ``basemap_bands.plan_bands`` fetches for *shots* before
    any cap: each frame drawn from band ``floor(zoom)`` (and the next band in
    the last :data:`FADE` of a level), each band's viewports merged into
    sheets while they stay within :data:`SHEET_MAX_FRAMES` frames' area and
    no :data:`GAP_FRAMES` apart, each sheet counted in tiles per world copy
    it overlaps."""
    max_area = SHEET_MAX_FRAMES * size[0] * size[1]
    sheets: List[List[int]] = []          # [band, x0, y0, x1, y1, last frame]
    open_: Dict[int, int] = {}
    for n, shot in enumerate(shots):
        x, y = lonlat_to_world(shot.lon, shot.lat)
        k = math.floor(shot.zoom)
        bands = {k, k + 1} if shot.zoom - k > 1.0 - FADE else {k}
        for band in sorted({max(0, b) for b in bands}):
            world = TILE_SIZE * PIXEL_RATIO * 2 ** band
            scale = PIXEL_RATIO * 2.0 ** (band - shot.zoom)
            hw, hh = size[0] / 2.0 * scale, size[1] / 2.0 * scale
            l, t, r, b = x * world - hw, y * world - hh, x * world + hw, y * world + hh
            c = (l + r) / 2.0
            rect = [max(math.floor(c - world / 2), math.floor(l) - MARGIN_PX),
                    max(0, math.floor(t) - MARGIN_PX),
                    min(math.ceil(c + world / 2), math.ceil(r) + MARGIN_PX),
                    min(world, math.ceil(b) + MARGIN_PX)]
            i = open_.get(band)
            if i is not None:
                s = sheets[i]
                u = [min(s[1], rect[0]), min(s[2], rect[1]), max(s[3], rect[2]), max(s[4], rect[3])]
                if n - s[5] <= GAP_FRAMES and (u[2] - u[0]) * (u[3] - u[1]) <= max_area:
                    s[1:6] = [*u, n]
                    continue
            open_[band] = len(sheets)
            sheets.append([band, *rect, n])
    return sum(_sheet_tiles(*s[:5]) for s in sheets)


def _sheet_tiles(band: int, x0: int, y0: int, x1: int, y1: int) -> int:
    """Tiles covering a sheet (in *band*'s sheet pixels), per world copy."""
    world = TILE_SIZE * PIXEL_RATIO * 2 ** band
    last = 2 ** band - 1

    def span(a: float, b: float) -> int:     # tile_stitcher.tile_range_for_bounds
        lo = max(0, min(math.floor(a / PIXEL_RATIO / TILE_SIZE), last))
        hi = max(0, min(math.floor((b / PIXEL_RATIO - 1e-9) / TILE_SIZE), last))
        return hi - lo + 1

    rows = span(y0, y1)
    total = 0
    for k in range(math.floor(x0 / world), math.ceil(x1 / world)):
        a, b = max(x0, k * world), min(x1, (k + 1) * world)
        if b > a:
            total += span(a - k * world, b - k * world) * rows
    return total


def camera(timeline: Timeline, frame_n: int, fps: int, size: Size,
           mode: str = "variable") -> Tuple[float, float, float]:
    """``(lon, lat, zoom)`` of frame *frame_n* of *timeline* rendered at
    *fps* into a *size* = (width, height) frame, in camera *mode*.

    Raises ValueError for a frame outside ``0 … frame_count − 1``.
    """
    path = camera_path(timeline, fps, size, mode)
    if not 0 <= frame_n < len(path):
        raise ValueError(f"frame {frame_n} is outside 0..{len(path) - 1}")
    shot = path[frame_n]
    return shot.lon, shot.lat, shot.zoom

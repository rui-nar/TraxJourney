"""The timeline of a trip video: title card, clips, end card, on one clock
(docs/TRIP_VIDEO_PLAN.md, "Pacing and timeline design").

:meth:`Timeline.sample` is a pure function of the timeline and *t*: the
renderer and the camera ask it where the marker is, what it is travelling on
and how far each mode has gone, for any frame, in any order.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.models.project import Project
from src.video.legs import Leg, LegSet, Skipped, build_legs
from src.video.pacing import (
    END_S,
    FIXED_S,
    MIN_CLIP_S,
    TITLE_S,
    Clip,
    PacingPolicy,
    SqrtBudgetPolicy,
    clip_budget_s,
    group_clips,
    max_clips,
)

DEFAULT_FPS = 30

# Each clip eases in and out over this long, or a fifth of the clip if that
# is shorter, so the marker never starts or stops with a jerk.
EASE_S = 0.3
EASE_FRACTION = 0.2

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class NothingToAnimate(ValueError):
    """The trip has no leg with a line to draw."""


@dataclass(frozen=True)
class TimedLeg:
    """A leg inside its clip. ``start_s``/``end_s`` are on the clip's eased
    clock (0 … clip duration). An *instant* leg is shorter than two frames
    and is drawn whole the moment it starts."""
    leg: Leg
    start_s: float
    end_s: float
    instant: bool


@dataclass(frozen=True)
class TimedClip:
    index: int
    clip: Clip
    start_s: float        # absolute
    end_s: float          # absolute
    subs: Tuple[TimedLeg, ...]

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True)
class FrameState:
    """What is on screen at one instant."""
    kind: str                         # "title" | "clip" | "end"
    t: float
    clip_index: Optional[int]         # None on the title and end cards
    sub_index: Optional[int]          # the leg's position inside its clip
    leg_index: Optional[int]          # the leg's position in Timeline.legs
    lon: float                        # unwrapped, may lie past ±180 (legs.unwrap_lons)
    lat: float
    heading: float                    # degrees clockwise from north, [0, 360)
    mode: str
    speed_kmh: float
    km_by_mode: Mapping[str, float]   # travelled so far, per mode
    date: Optional[date]
    day: Optional[int]                # 1-based day of the trip

    @property
    def date_label(self) -> str:
        """The date ticker: ``Day 34 · 12 May 2026``."""
        if self.date is None:
            return ""
        text = f"{self.date.day} {_MONTHS[self.date.month - 1]} {self.date.year}"
        return f"Day {self.day} · {text}" if self.day is not None else text


def ease_clock(u: float, duration: float) -> float:
    """Clip-local time *u* on the eased clock: smoothstep velocity ramps over
    the first and last :data:`EASE_S` (at most :data:`EASE_FRACTION` of the
    clip), constant speed between. Monotone, maps 0 → 0 and *duration* →
    *duration*."""
    if duration <= 0:
        return 0.0
    u = min(max(u, 0.0), duration)
    e = min(EASE_S, EASE_FRACTION * duration)
    v = duration / (duration - e)

    def ramp(x: float) -> float:  # ∫₀ˣ smoothstep, scaled to the ramp length
        tau = x / e
        return v * e * (tau ** 3 - tau ** 4 / 2)

    if u < e:
        return ramp(u)
    if u > duration - e:
        return duration - ramp(duration - u)
    return v * (e / 2 + (u - e))


def bearing(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Forward azimuth from point 1 to point 2 in degrees [0, 360)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlon = lon2 - lon1
    x = math.cos(lat2) * math.sin(dlon)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _point_at(leg: Leg, fraction: float) -> Tuple[float, float, float]:
    """(lon, lat, heading) at *fraction* of *leg*'s length."""
    cum = leg.cum_km
    target = fraction * cum[-1]
    i = bisect.bisect_left(cum, target)
    i = max(1, min(i, len(cum) - 1))
    span = cum[i] - cum[i - 1]
    f = 0.0 if span <= 0 else min(1.0, max(0.0, (target - cum[i - 1]) / span))
    (lon0, lat0), (lon1, lat1) = leg.point(i - 1), leg.point(i)
    return (lon0 + f * (lon1 - lon0), lat0 + f * (lat1 - lat0),
            bearing(lon0, lat0, lon1, lat1))


class Timeline:
    """Title card, clips and end card of one video, *total_s* long."""

    def __init__(self, legs: Sequence[Leg], clips: Sequence[Clip],
                 durations: Sequence[float], total_s: float, *,
                 fps: int = DEFAULT_FPS, skipped: Sequence[Skipped] = (),
                 start_date: Optional[date] = None) -> None:
        self.legs: Tuple[Leg, ...] = tuple(legs)
        self.skipped: Tuple[Skipped, ...] = tuple(skipped)
        self.total_s = float(total_s)
        self.fps = fps
        self.title_s = TITLE_S
        self.end_s = END_S
        self.start_date = start_date or next(
            (leg.date for leg in self.legs if leg.date is not None), None)

        timed: List[TimedClip] = []
        clips_end = TITLE_S + clip_budget_s(total_s)
        t = TITLE_S
        for i, (clip, d) in enumerate(zip(clips, durations)):
            end = clips_end if i == len(clips) - 1 else t + d
            timed.append(TimedClip(i, clip, t, end, self._time_subs(clip, end - t)))
            t = end
        self.clips: Tuple[TimedClip, ...] = tuple(timed)
        self._clip_starts = [c.start_s for c in self.clips]
        self._sub_ends = [[s.end_s for s in c.subs] for c in self.clips]

        # km_before[i]: km per mode travelled before leg i; the last entry is
        # the trip's totals.
        running: Dict[str, float] = {}
        self._km_before: List[Dict[str, float]] = []
        for leg in self.legs:
            self._km_before.append(dict(running))
            running[leg.mode] = running.get(leg.mode, 0.0) + leg.km
        self._km_before.append(dict(running))
        self._leg_pos = {id(leg): n for n, leg in enumerate(self.legs)}

    @property
    def km_totals(self) -> Dict[str, float]:
        return dict(self._km_before[-1])

    @property
    def clips_end_s(self) -> float:
        return self.total_s - self.end_s

    def _time_subs(self, clip: Clip, duration: float) -> Tuple[TimedLeg, ...]:
        """Share *duration* among *clip*'s legs by √(real duration), no floor."""
        shares = [math.sqrt(max(leg.real_s, 0.0)) for leg in clip.legs]
        total = sum(shares)
        if total <= 0:
            shares, total = [1.0] * len(shares), float(len(shares))
        frame = 1.0 / self.fps
        out: List[TimedLeg] = []
        t = 0.0
        for i, (leg, share) in enumerate(zip(clip.legs, shares)):
            end = duration if i == len(shares) - 1 else t + duration * share / total
            out.append(TimedLeg(leg, t, end, (end - t) < 2 * frame))
            t = end
        return tuple(out)

    def _day(self, d: Optional[date]) -> Optional[int]:
        if d is None or self.start_date is None:
            return None
        return (d - self.start_date).days + 1

    def _state(self, kind: str, t: float, clip_index: Optional[int],
               sub_index: Optional[int], leg: Leg, fraction: float) -> FrameState:
        n = self._leg_pos[id(leg)]
        lon, lat, heading = _point_at(leg, fraction)
        km = dict(self._km_before[n])
        km[leg.mode] = km.get(leg.mode, 0.0) + leg.km * fraction
        return FrameState(
            kind=kind, t=t, clip_index=clip_index, sub_index=sub_index,
            leg_index=None if kind != "clip" else n,
            lon=lon, lat=lat, heading=heading, mode=leg.mode,
            speed_kmh=leg.speed_kmh, km_by_mode=km,
            date=leg.date, day=self._day(leg.date))

    def sample(self, t: float) -> FrameState:
        """The state at *t* seconds. A clip owns ``[start, end)``: at a clip
        boundary the next clip has just begun. Before 0 is the title card,
        from ``total_s − end_s`` on the end card."""
        t = min(max(t, 0.0), self.total_s)
        if t < TITLE_S:
            return self._state("title", t, None, None, self.legs[0], 0.0)
        if t >= self.clips_end_s:
            return self._state("end", t, None, None, self.legs[-1], 1.0)
        ci = bisect.bisect_right(self._clip_starts, t) - 1
        clip = self.clips[ci]
        local = ease_clock(t - clip.start_s, clip.duration_s)
        si = min(bisect.bisect_right(self._sub_ends[ci], local), len(clip.subs) - 1)
        sub = clip.subs[si]
        if sub.instant:
            fraction = 1.0
        else:
            fraction = min(1.0, max(0.0, (local - sub.start_s) / (sub.end_s - sub.start_s)))
        return self._state("clip", t, ci, si, sub.leg, fraction)


def build_timeline(legset: LegSet, total_s: float, *,
                   policy: Optional[PacingPolicy] = None, fps: int = DEFAULT_FPS,
                   start_date: Optional[date] = None) -> Timeline:
    """The timeline of *legset* for a *total_s*-second video.

    Raises :class:`NothingToAnimate` when there is no leg, and ValueError when
    *total_s* leaves no room for a single clip.
    """
    if not legset.legs:
        raise NothingToAnimate("nothing to animate")
    n_max = max_clips(total_s)
    if n_max < 1:
        raise ValueError(f"a {total_s} s video has no room for a clip "
                         f"(needs more than {FIXED_S + MIN_CLIP_S} s)")
    clips = group_clips(legset.legs, n_max)
    durations = (policy or SqrtBudgetPolicy()).allocate(clips, clip_budget_s(total_s))
    return Timeline(legset.legs, clips, durations, total_s, fps=fps,
                    skipped=legset.skipped, start_date=start_date)


def timeline_for_project(project: Project, total_s: float, *,
                         geometry: Optional[Mapping[int, str]] = None,
                         policy: Optional[PacingPolicy] = None,
                         fps: int = DEFAULT_FPS) -> Timeline:
    """:func:`build_timeline` over *project*'s legs; day 1 is the trip's
    ``trip_start`` when it has one, else the first leg's date."""
    start: Optional[date] = None
    if project.trip_start:
        try:
            start = date.fromisoformat(project.trip_start)
        except ValueError:
            start = None
    return build_timeline(build_legs(project, geometry), total_s,
                          policy=policy, fps=fps, start_date=start)

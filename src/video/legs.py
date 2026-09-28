"""Legs: one per drawable trip item, in map order (docs/TRIP_VIDEO_PLAN.md).

A leg carries what pacing and sampling need and nothing else: the geometry
with its prefix distances, the mode, the real duration it stands for, the
distance and speed the video shows, and its date.

:func:`feature_coords` also lives here, rather than in ``api/geo.py`` whose
features it draws: this module must stay importable without FastAPI or a DB
(Convention 4), and the map and the video have to draw the same line.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, replace
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import polyline as polyline_lib

from src.models.activity import Activity
from src.models.great_circle import great_circle_points, haversine_km
from src.models.project import ConnectingSegment, Project
from src.utils.encryption_check import is_encrypted_envelope

Coords = List[List[float]]  # [[lon, lat], …]

SEGMENT_MODES = ("flight", "train", "bus", "boat")

# D9: segments have no times, so their real duration is length / a typical
# speed. Each is overridable with VIDEO_SPEED_KMH_<MODE>.
REAL_SPEED_KMH: Dict[str, float] = {
    "flight": 800.0, "train": 100.0, "bus": 60.0, "boat": 25.0,
}

# Only for an activity with neither moving nor elapsed time, which has to be
# given *some* duration: a typical pace for its bucket.
_ACTIVITY_SPEED_KMH: Dict[str, float] = {
    "ride": 18.0, "run": 10.0, "hike": 4.5, "other": 10.0,
}


def activity_mode(sport_type: Optional[str]) -> str:
    """The bucket of a Strava sport type — the same keys the client's
    ``activityTypeBucket`` uses for colours and styles."""
    t = (sport_type or "").lower()
    if t in ("ride", "virtualride", "ebikeride"):
        return "ride"
    if t in ("run", "virtualrun"):
        return "run"
    if t in ("hike", "walk"):
        return "hike"
    return "other"


def real_speed_kmh(mode: str) -> float:
    """*mode*'s D9 speed, from ``VIDEO_SPEED_KMH_<MODE>`` when that is a
    positive number."""
    default = REAL_SPEED_KMH[mode]
    raw = os.environ.get(f"VIDEO_SPEED_KMH_{mode.upper()}")
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def feature_coords(item, geometry_override: Optional[str] = None) -> Optional[Coords]:
    """The ``[[lon, lat], …]`` line drawn for *item*, or None when there is none.

    *item* is an :class:`Activity` or a :class:`ConnectingSegment`.

    Activity: its Google-encoded polyline — *geometry_override* when given,
    else ``summary_polyline`` — decoded; with no polyline, the straight line
    from its start to its end. None when the polyline is an E2EE envelope
    (the server can't read it) or decodes to fewer than two points.

    Segment: its resolved route for rail/ferry/bus, else a 50-point
    great-circle arc. *geometry_override* is ignored.
    """
    if isinstance(item, ConnectingSegment):
        if item.route_mode in ("rail", "ferry", "bus") and item.route_polyline:
            coords = json.loads(item.route_polyline)
        else:
            # great_circle_points returns [(lat, lon), ...]
            pts = great_circle_points(
                item.start.lat, item.start.lon,
                item.end.lat, item.end.lon,
                n_points=50,
            )
            coords = [[lon, lat] for lat, lon in pts]
        return coords if len(coords) >= 2 else None

    summary_polyline = (geometry_override if geometry_override is not None
                        else item.summary_polyline)
    if is_encrypted_envelope(summary_polyline):
        return None
    if summary_polyline:
        decoded = polyline_lib.decode(summary_polyline)
        coords = [[lon, lat] for lat, lon in decoded]
        return coords if len(coords) >= 2 else None
    if item.start_latlng and item.end_latlng:
        # No polyline (GPX import / private activity) — straight line fallback
        return [
            [item.start_latlng[1], item.start_latlng[0]],
            [item.end_latlng[1],   item.end_latlng[0]],
        ]
    return None


@dataclass(frozen=True)
class Leg:
    """One animated trip item."""
    index: int                      # position among the legs
    kind: str                       # "activity" | "segment"
    ref: object                     # activity id (int) or segment id (str)
    label: str
    mode: str                       # ride/run/hike/other or a SEGMENT_MODES entry
    coords: Tuple[Tuple[float, float], ...]   # ((lon, lat), …), ≥ 2 points
    cum_km: Tuple[float, ...]       # geometric prefix distances, same length
    km: float                       # distance shown on the counters
    real_s: float                   # the real duration this leg stands for
    speed_kmh: float                # the speed badge
    date: Optional[date]


@dataclass(frozen=True)
class Skipped:
    """A trip item that has no leg, and why: ``encrypted`` (geometry the
    server can't read and no consent geometry), ``no_geometry`` (fewer than
    two points) or ``missing`` (its activity isn't in the project)."""
    kind: str
    ref: object
    reason: str


@dataclass(frozen=True)
class LegSet:
    legs: Tuple[Leg, ...]
    skipped: Tuple[Skipped, ...]


def prefix_km(coords: Sequence[Sequence[float]]) -> Tuple[float, ...]:
    """Cumulative haversine km along ``(lon, lat)`` *coords*, starting at 0."""
    out = [0.0]
    for (lon0, lat0), (lon1, lat1) in zip(coords, coords[1:]):
        out.append(out[-1] + haversine_km(lat0, lon0, lat1, lon1))
    return tuple(out)


def _parse_date(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _activity_encrypted(activity: Activity) -> bool:
    return (is_encrypted_envelope(activity.summary_polyline)
            or activity.start_latlng_enc is not None
            or activity.end_latlng_enc is not None)


def _activity_leg(index: int, activity: Activity, coords: Coords) -> Leg:
    pts = tuple((float(lon), float(lat)) for lon, lat in coords)
    cum = prefix_km(pts)
    mode = activity_mode(activity.type)
    km = activity.distance / 1000.0 if activity.distance and activity.distance > 0 else cum[-1]
    if activity.moving_time and activity.moving_time > 0:
        real_s = float(activity.moving_time)
    elif activity.elapsed_time and activity.elapsed_time > 0:
        real_s = float(activity.elapsed_time)
    else:
        real_s = km / _ACTIVITY_SPEED_KMH[mode] * 3600.0
    speed = km / real_s * 3600.0 if real_s > 0 else 0.0
    return Leg(index=index, kind="activity", ref=activity.id,
               label=activity.name or mode, mode=mode, coords=pts, cum_km=cum,
               km=km, real_s=real_s, speed_kmh=speed,
               date=activity.start_date_local.date() if activity.start_date_local else None)


def _segment_leg(index: int, seg: ConnectingSegment, coords: Coords) -> Leg:
    pts = tuple((float(lon), float(lat)) for lon, lat in coords)
    cum = prefix_km(pts)
    mode = seg.segment_type if seg.segment_type in SEGMENT_MODES else "flight"
    speed = real_speed_kmh(mode)
    return Leg(index=index, kind="segment", ref=seg.id, label=seg.label or mode,
               mode=mode, coords=pts, cum_km=cum, km=cum[-1],
               real_s=cum[-1] / speed * 3600.0, speed_kmh=speed,
               date=_parse_date(seg.date))


def _fill_dates(legs: List[Leg]) -> List[Leg]:
    """Give an undated leg (a segment without a date) the previous leg's
    date, or the next dated one's when it comes first — so a flight between
    two days' rides sits on the day it left, not on no day at all."""
    out: List[Leg] = []
    last: Optional[date] = None
    for leg in legs:
        if leg.date is None and last is not None:
            leg = replace(leg, date=last)
        last = leg.date if leg.date is not None else last
        out.append(leg)
    nxt: Optional[date] = None
    for i in range(len(out) - 1, -1, -1):
        if out[i].date is None and nxt is not None:
            out[i] = replace(out[i], date=nxt)
        nxt = out[i].date if out[i].date is not None else nxt
    return out


def build_legs(project: Project,
               geometry: Optional[Mapping[int, str]] = None) -> LegSet:
    """*project*'s legs in item order.

    *geometry* maps activity id → Google-encoded polyline (the consent
    geometry of D2) and replaces that activity's own polyline.
    """
    geometry = geometry or {}
    legs: List[Leg] = []
    skipped: List[Skipped] = []
    for item in project.items:
        if item.item_type == "activity" and item.activity_id is not None:
            activity = project.activity_by_id(item.activity_id)
            if activity is None:
                skipped.append(Skipped("activity", item.activity_id, "missing"))
                continue
            override = geometry.get(activity.id)
            coords = feature_coords(activity, override)
            if coords is None:
                reason = ("encrypted" if override is None and _activity_encrypted(activity)
                          else "no_geometry")
                skipped.append(Skipped("activity", activity.id, reason))
                continue
            legs.append(_activity_leg(len(legs), activity, coords))
        elif item.item_type == "segment" and item.segment is not None:
            coords = feature_coords(item.segment)
            if coords is None:
                skipped.append(Skipped("segment", item.segment.id, "no_geometry"))
                continue
            legs.append(_segment_leg(len(legs), item.segment, coords))
    return LegSet(legs=tuple(_fill_dates(legs)), skipped=tuple(skipped))

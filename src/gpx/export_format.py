"""What a TraxJourney GPX export writes, shared by the export and the import.

The export writes one ``<trk>`` per activity and one per connecting segment
(#367). The importer turns each ``<trk>`` into one candidate, so a file of
separate tracks reads back as separate activities, where the old single track
of segments read back as one activity spanning the whole trip.

Three things here are part of the file format, and both sides use them:

* :data:`CONNECTION_TRACK_TYPE`, the ``<type>`` of a connecting segment's
  track, so an import can tell a train ride drawn as a great-circle arc from
  an activity;
* the extension carrying the activity's stored moving time and distance. A
  Strava activity's polyline is simplified, so a distance recomputed from it
  falls short, and a moving time cannot be recomputed from spread times at
  all. The import reads the stored values back instead;
* :func:`spread_times`, the per-point times written along an activity.

Pure: no HTTP, no database, no clock.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple

from src.models.great_circle import haversine_km
from src.models.value_bounds import DISTANCE_MAX_M, DURATION_MAX_S

#: ``<type>`` of a track drawn for a connecting segment (train, flight...).
CONNECTION_TRACK_TYPE = "traxjourney-connection"

#: Namespace of the activity extension, and the prefix the export declares.
EXTENSION_NAMESPACE = "https://traxjourney.com/xmlns/gpx/1"
EXTENSION_PREFIX = "traxj"

_MOVING_TIME_TAG = f"{{{EXTENSION_NAMESPACE}}}moving_time"
_DISTANCE_TAG = f"{{{EXTENSION_NAMESPACE}}}distance"


def spread_times(points: Sequence[Tuple[float, float]], start: datetime,
                 elapsed_seconds: int) -> List[datetime]:
    """One time per point, from *start* to *start* + *elapsed_seconds*.

    Spread by cumulative distance, so the speed is even along the track. A
    track that does not move is spread evenly by point instead. Times are
    whole seconds, as recording devices write them: two points less than a
    second apart can share a time, but times never go backwards, and the
    last point is exactly *start* + *elapsed_seconds*.
    """
    if not points:
        return []
    elapsed = max(int(elapsed_seconds or 0), 0)
    cumulative = [0.0]
    for (lat_a, lon_a), (lat_b, lon_b) in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + haversine_km(lat_a, lon_a, lat_b, lon_b))
    total = cumulative[-1]
    last = len(points) - 1
    times = []
    for index, travelled in enumerate(cumulative):
        if total > 0:
            share = travelled / total
        else:
            share = index / last if last else 0.0
        times.append(start + timedelta(seconds=round(elapsed * share)))
    return times


def activity_extensions(moving_time, distance) -> List[ET.Element]:
    """The ``<extensions>`` children carrying an activity's stored figures.

    A figure that is missing or not a finite number is left out, and the
    import then measures it from the track as it would for any other file.
    """
    elements = []
    moving = _bounded(moving_time, DURATION_MAX_S)
    if moving is not None:
        element = ET.Element(_MOVING_TIME_TAG)
        element.text = str(int(moving))
        elements.append(element)
    metres = _bounded(distance, DISTANCE_MAX_M)
    if metres is not None:
        element = ET.Element(_DISTANCE_TAG)
        element.text = repr(float(metres))
        elements.append(element)
    return elements


def read_activity_extensions(
        elements: Sequence[ET.Element]) -> Tuple[Optional[int], Optional[float]]:
    """``(moving_seconds, distance_m)`` carried by a track, each None if absent.

    The file is untrusted, and anyone can write this namespace: a value that
    is not a finite, non-negative number within the app's bounds counts as
    absent.
    """
    moving: Optional[int] = None
    distance: Optional[float] = None
    for element in elements or ():
        tag = getattr(element, "tag", None)
        if tag == _MOVING_TIME_TAG and moving is None:
            value = _bounded(_number(element.text), DURATION_MAX_S)
            moving = int(value) if value is not None else None
        elif tag == _DISTANCE_TAG and distance is None:
            value = _bounded(_number(element.text), DISTANCE_MAX_M)
            distance = float(value) if value is not None else None
    return moving, distance


def _number(text: Optional[str]) -> Optional[float]:
    try:
        return float((text or "").strip())
    except ValueError:
        return None


def _bounded(value, upper: float) -> Optional[float]:
    """*value* if it is a finite number in ``[0, upper]``, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if 0 <= value <= upper else None

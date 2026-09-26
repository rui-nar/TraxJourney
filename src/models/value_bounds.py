"""How large a stored figure can plausibly be (issue #462).

One set of bounds for every place a figure enters the app: the trip-file
import (src/project/traxj_schema.py), the request bodies that write trip
content, and the app's own writers (a GPX upload, an edited track, Strava).
A figure past them is either refused or, for an elevation, treated as no
reading. Whatever the app holds therefore exports to a file its own import
accepts, and the trip's totals, summing any number of such figures, stay far
from a float's range.

Each bound is physical plausibility with a large margin. Elevations: the
highest airliner cruises at 13 km and the deepest trench is 11 km down.
Distance: a single activity over 100,000 km is two and a half times round the
Earth. Time: 31 years. Climbing: 10,000 km, over a thousand Everests. Speed is
the one bound not taken from physics: a GPX track's average speed is its
distance over its whole seconds moving, so the distance bound over one second
is what the app itself can compute, and anything a real track reaches is far
below it.
"""
from __future__ import annotations

import math
from typing import Annotated, Any, Optional

from pydantic import Field

LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0
ELEVATION_MIN_M, ELEVATION_MAX_M = -20_000.0, 20_000.0
DISTANCE_MAX_M = 1e8
DURATION_MAX_S = 1e9
GAIN_MAX_M = 1e7
SPEED_MAX_M_S = DISTANCE_MAX_M
HEART_RATE_MAX = 1_000
COUNT_MAX = 1e9


def _bounded(lo: float, hi: float) -> Any:
    return Field(ge=lo, le=hi, allow_inf_nan=False)


Lat = Annotated[float, _bounded(LAT_MIN, LAT_MAX)]
Lon = Annotated[float, _bounded(LON_MIN, LON_MAX)]
Elevation = Annotated[float, _bounded(ELEVATION_MIN_M, ELEVATION_MAX_M)]
Distance = Annotated[float, _bounded(0, DISTANCE_MAX_M)]
Duration = Annotated[float, _bounded(0, DURATION_MAX_S)]
Gain = Annotated[float, _bounded(0, GAIN_MAX_M)]
Speed = Annotated[float, _bounded(0, SPEED_MAX_M_S)]
HeartRate = Annotated[float, _bounded(0, HEART_RATE_MAX)]
Count = Annotated[float, _bounded(0, COUNT_MAX)]


def finite_or_none(value: Any) -> Optional[float]:
    """*value* if it is a finite number (not a bool), else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def plausible_elevation(value: Any) -> Optional[float]:
    """*value* as an elevation reading, or None for none.

    NaN, ±Infinity and anything past ±20 km are not readings: gpxpy parses
    ``<ele>NaN</ele>`` and ``<ele>inf</ele>`` as floats, and a device or
    stream can carry a sentinel such as 65535. Stored, they break the trip
    (the client's JSON parser refuses the first two, the import all three),
    so they count as missing, as a point without ``<ele>`` always has.
    """
    value = finite_or_none(value)
    if value is None or not ELEVATION_MIN_M <= value <= ELEVATION_MAX_M:
        return None
    return value

"""The zone a GPX track was recorded in, and its times in that zone (issue #365).

A GPX file stamps its points in UTC and says nothing about where on the clock
face they fell. The import used to store that UTC instant as the local time and
label it ``"UTC"``, so a ride in Tokyo showed nine hours off, and a time typed
for it was read as UTC.

The zone is looked up offline, from the track's first point, with ``tzfpy``,
whose wheel carries its own boundary data. Every GPX time conversion goes
through this module: no other module calls ``tzfpy`` or builds a ``ZoneInfo``
for GPX.

Zone names are IANA names. A zone that is UTC is written ``"Etc/UTC"``, never
the bare ``"UTC"``: that label is kept for GPX rows imported before this
change, whose stored instant is unknown (plan Decision 5), and the GPX export
reads it as such.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tzfpy

from src.utils.logging import get_logger

_log = get_logger(__name__)

#: What a zone that is UTC, or that cannot be resolved, is stored as.
UTC_ZONE = "Etc/UTC"

#: Names ``tzfpy`` or the tz database may give for UTC itself. ``Etc/GMT`` is
#: what the lookup returns at sea on the prime meridian's band.
_UTC_NAMES = frozenset({
    "UTC", "Etc/UTC", "Etc/UCT", "UCT", "Etc/Universal", "Universal",
    "Etc/Zulu", "Zulu", "GMT", "Etc/GMT", "Etc/GMT+0", "Etc/GMT-0",
    "Etc/GMT0", "GMT0", "GMT+0", "GMT-0", "Etc/Greenwich", "Greenwich",
})


def zone_at(lat: Optional[float], lon: Optional[float]) -> str:
    """The IANA zone at a coordinate, for the activity's ``timezone`` column.

    At sea ``tzfpy`` answers a nautical ``Etc/GMT±N`` zone, which is kept.
    Returns :data:`UTC_ZONE` when the coordinate is missing, the lookup finds
    nothing or a name this platform's tz database does not know (logged as a
    warning), or the zone is UTC. Never returns the bare ``"UTC"``.
    """
    if lat is None or lon is None:
        return UTC_ZONE
    try:
        name = tzfpy.get_tz(float(lon), float(lat))
    except (TypeError, ValueError, OverflowError):
        return UTC_ZONE
    if not name or name in _UTC_NAMES:
        return UTC_ZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Neither the system tz database nor the tzdata package knows a name
        # tzfpy's newer data gives: the activity is stored in UTC, so say so.
        _log.warning("unknown timezone %r from tzfpy at the track's start "
                     "— storing %s", name, UTC_ZONE)
        return UTC_ZONE
    return name


def to_local(instant_utc: datetime, zone: str) -> datetime:
    """The naive wall clock in *zone* at *instant_utc*.

    A naive *instant_utc* is read as UTC, as the GPX import reads a stamp
    written without an offset. Raises ``OverflowError`` for an instant whose
    wall clock falls outside the calendar.
    """
    if instant_utc.tzinfo is None:
        instant_utc = instant_utc.replace(tzinfo=timezone.utc)
    return instant_utc.astimezone(ZoneInfo(zone)).replace(tzinfo=None)


def local_to_utc(naive_wall_clock: datetime, zone: str, *,
                 fold: int = 0) -> datetime:
    """The UTC instant at which *zone*'s clocks read *naive_wall_clock*.

    Daylight saving makes a wall clock name zero or two instants:

    - an ambiguous time, in the hour repeated when clocks go back, takes the
      first occurrence (``fold=0``), unless the caller asks for the second
      with ``fold=1``;
    - a time that does not exist, in the hour skipped when clocks go forward,
      moves forward by the gap: 01:30 on the morning Lisbon jumps from 01:00
      to 02:00 is read as 02:30 local, whatever *fold* says.

    Raises ``OverflowError`` for a wall clock whose instant falls outside the
    calendar.
    """
    tz = ZoneInfo(zone)
    instant = naive_wall_clock.replace(tzinfo=tz, fold=fold).astimezone(
        timezone.utc)
    if instant.astimezone(tz).replace(tzinfo=None) != naive_wall_clock:
        # Not a time the clocks ever show. fold=0 applies the offset in force
        # before the jump, which lands the same distance past it.
        instant = naive_wall_clock.replace(tzinfo=tz, fold=0).astimezone(
            timezone.utc)
    return instant

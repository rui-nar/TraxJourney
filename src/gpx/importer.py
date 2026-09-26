"""GPX import: parse, validate, and read what the file already tells us.

A GPX file carries far more than geometry, and the first version of this import
threw all of it away — it asked the user to type a date, a start time, an end
time and an activity type for a ride they did three weeks ago, while the file
in front of it held every one of those facts. This module reads them.

It also accepts a shape the first version refused. Issue #260 was written for
"a route drawn in Komoot or RideWithGPS", and those planners frequently export
a ``<rte>`` — a planned route — rather than a ``<trk>``. Looking only at
``gpx.tracks`` meant the motivating case came back as "GPX contains no tracks",
which is both a refusal and a lie about why.

Three vocabularies meet here and are deliberately kept apart:

* what the FILE says — ``<trk>``/``<rte>``, ``<name>``, ``<type>``, ``<time>``
* what the IMPORT decides — one chosen candidate, derived times, a mapped type
* what the USER confirms — all of the above, editable, in the preview

This module does the first two and is pure: no HTTP, no database, no clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

import gpxpy
import gpxpy.gpx

from src.models.great_circle import haversine_km
from src.models.track_edit import TrackPoint

MAX_IMPORT_POINTS = 50000

#: Largest upload accepted, checked BEFORE parsing. gpxpy builds an object
#: tree many times the size of the XML, so a point-count limit applied after
#: parsing is a limit that has already let the damage happen. Measured: a
#: 4.8 MB file of plain trackpoints peaks at 73 MB of heap, and a 13.2 MB
#: Garmin-style file carrying <extensions> at 132 MB — about ten to fifteen
#: times the bytes on the wire.
#:
#: 12 MB is chosen from the DENSEST encoding rather than the cheapest. Plain
#: trackpoints cost ~95 bytes each and Garmin's extensions ~261, so 12 MB is
#: roughly 130k plain points or 46k rich ones — the latter below the point cap
#: below, which is the point: a file that passes this guard should not then be
#: rejected after having already cost 200 MB to parse. An earlier 20 MB let a
#: rich 80k-point file through to exactly that.
MAX_IMPORT_BYTES = 12 * 1024 * 1024

#: Below this speed the track is not moving: a stop at a café, a wait at a
#: junction, a fix drifting while the phone sits on a table. 0.3 m/s is about a
#: quarter of walking pace.
MOVING_MIN_SPEED_MS = 0.3

#: Speed is measured as NET displacement across this many seconds of track,
#: not between one sample and the next. That distinction is the whole test: a
#: phone sitting still on a table still reports a position that wanders, and at
#: 1 Hz the wander between consecutive samples easily clears 0.3 m. Measured on
#: a twenty-minute stationary recording, a per-sample rule counted 91% of it as
#: moving under half a metre of jitter and 99% under one and a half. Net
#: displacement is what jitter cannot fake, because a random walk goes nowhere:
#: over a 30 s window the same stops measure 0-5%, while a genuine walk at
#: 1.4 m/s still measures 100% moving.
MOVING_WINDOW_S = 30.0

#: A gap longer than this is a pause in the RECORDING rather than a slow stretch
#: of it — the device was switched off, or lost its fix in a tunnel. Counting it
#: would hand a two-hour lunch to the ride's moving time.
MAX_SAMPLE_GAP_S = 300

#: GPX ``<type>`` is free text and every tool writes it differently. Mapped into
#: the types the app draws and colours; anything unrecognised stays None so the
#: user picks, rather than being handed a confident wrong answer.
_TYPE_ALIASES = {
    "run": "run", "running": "run", "jog": "run", "jogging": "run",
    "trail running": "run", "track running": "run",
    "treadmill running": "run", "virtualrun": "run",
    "ride": "ride", "cycling": "ride", "bike": "ride", "biking": "ride",
    "cycle": "ride", "mtb": "ride", "mountain biking": "ride",
    "road biking": "ride", "road cycling": "ride",
    "gravel cycling": "ride", "cyclocross": "ride",
    "e bike fitness": "ride", "e bike": "ride", "ebike": "ride",
    "ebikeride": "ride", "virtualride": "ride",
    "hike": "hike", "hiking": "hike", "trekking": "hike",
    "mountaineering": "hike",
    "walk": "walk", "walking": "walk", "stroll": "walk",
}


class GPXImportError(Exception):
    """Raised when a GPX file cannot be imported; carries human-readable reasons."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class GpxCandidate:
    """One importable thing in the file — a recorded track, or a planned route.

    ``index`` addresses it for the picker a multi-candidate file needs: the
    first version refused those outright, leaving the user to go and edit the
    file in another tool.
    """
    index: int
    name: Optional[str]
    activity_type: Optional[str]
    points: List[TrackPoint]
    times: List[Optional[datetime]]
    is_route: bool

    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def has_times(self) -> bool:
        return any(t is not None for t in self.times)

    @property
    def distance_m(self) -> float:
        return sum(
            haversine_km(a.lat, a.lng, b.lat, b.lng) * 1000.0
            for a, b in zip(self.points, self.points[1:])
        )

    @property
    def started_at(self) -> Optional[datetime]:
        return next((t for t in self.times if t is not None), None)

    @property
    def ended_at(self) -> Optional[datetime]:
        return next((t for t in reversed(self.times) if t is not None), None)

    @property
    def time_span(self) -> Optional[Tuple[datetime, datetime]]:
        """Earliest and latest stamp, or None if the track has no usable clock.

        Earliest and latest rather than first and last: devices do emit the
        occasional backwards step after a clock resync, and taking the ends
        blindly reports a span shorter than the ride, or none at all.

        Everything that needs the track's own times goes through this, so
        the preview and the import cannot disagree about whether a file has
        a usable clock — a disagreement the user meets as a form that
        prefills happily and then refuses to submit.
        """
        stamps = [t for t in self.times if t is not None]
        if len(stamps) < 2:
            return None
        first, last = min(stamps), max(stamps)
        return (first, last) if last > first else None

    @property
    def elapsed_seconds(self) -> Optional[int]:
        """Wall-clock span of the track in seconds."""
        span = self.time_span
        return int((span[1] - span[0]).total_seconds()) if span else None

    @property
    def moving_seconds(self) -> Optional[int]:
        """Elapsed time minus the standing still, when the file has a clock.

        Until now moving time was simply set equal to elapsed time, so every
        imported activity claimed it had never stopped, even when its own
        timestamps disagreed.

        Speed is taken over :data:`MOVING_WINDOW_S` of track rather than
        between neighbouring samples; see that constant for why a per-sample
        test counts a stop as movement. Gaps longer than
        :data:`MAX_SAMPLE_GAP_S` are dropped whole: they are the device
        switched off, not a slow stretch of riding.
        """
        if not self.has_times:
            return None

        stamped = [(p, t) for p, t in zip(self.points, self.times)
                   if t is not None]
        if len(stamped) < 2:
            return 0

        moving = 0.0
        low = high = 0
        half = MOVING_WINDOW_S / 2.0
        for index in range(len(stamped) - 1):
            here = stamped[index][1]
            seconds = (stamped[index + 1][1] - here).total_seconds()
            if seconds <= 0 or seconds > MAX_SAMPLE_GAP_S:
                continue
            if seconds >= MOVING_WINDOW_S:
                # This one interval is already longer than the window, so it
                # carries its own verdict: over that much time, real movement
                # dwarfs jitter. Judging it by a window drawn from its faster
                # neighbours would credit a two-minute rest with their speed.
                here_pt, next_pt = stamped[index][0], stamped[index + 1][0]
                step_m = haversine_km(here_pt.lat, here_pt.lng,
                                      next_pt.lat, next_pt.lng) * 1000.0
                if step_m / seconds >= MOVING_MIN_SPEED_MS:
                    moving += seconds
                continue
            # Widen a window centred on this sample, in TIME, so an
            # irregular recording rate does not change how much track it
            # spans.
            while (here - stamped[low][1]).total_seconds() > half:
                low += 1
            while (high + 1 < len(stamped)
                   and (stamped[high + 1][1] - here).total_seconds() <= half):
                high += 1
            window_s = (stamped[high][1] - stamped[low][1]).total_seconds()
            if window_s <= 0:
                continue
            net_m = haversine_km(
                stamped[low][0].lat, stamped[low][0].lng,
                stamped[high][0].lat, stamped[high][0].lng) * 1000.0
            if net_m / window_s >= MOVING_MIN_SPEED_MS:
                moving += seconds

        elapsed = self.elapsed_seconds
        if elapsed is not None:
            moving = min(moving, elapsed)   # never claim to move for longer
        return int(moving)


def guard_declared_size(size: int) -> None:
    """Reject an upload by its DECLARED size, before reading a byte of it.

    A multipart upload announces its length, so an oversized body can be
    refused without being pulled into memory at all. Reading first and
    measuring afterwards — which is what this used to do — grew the process by
    the whole file before deciding it was too big.

    A declaration is a claim, so :func:`guard_upload_size` still checks the
    bytes that actually arrived.

    Raises:
        GPXImportError: if *size* exceeds :data:`MAX_IMPORT_BYTES`.
    """
    if size > MAX_IMPORT_BYTES:
        raise GPXImportError([
            f"File is {size / 1_048_576:.1f} MB; the limit is "
            f"{MAX_IMPORT_BYTES // 1_048_576} MB."
        ])


def guard_upload_size(data: bytes) -> None:
    """Reject an oversized upload before it is parsed.

    Raises:
        GPXImportError: if the payload exceeds :data:`MAX_IMPORT_BYTES`.
    """
    guard_declared_size(len(data))


def parse_gpx_bytes(data: bytes) -> gpxpy.gpx.GPX:
    """Parse raw GPX file bytes into a gpxpy.gpx.GPX object.

    Raises:
        GPXImportError: if the data is not a valid GPX/XML document.
    """
    try:
        return gpxpy.parse(data)
    except gpxpy.gpx.GPXException:
        raise GPXImportError(["File is not a valid GPX document."])


def candidates(gpx: gpxpy.gpx.GPX) -> List[GpxCandidate]:
    """Everything in the file that could become an activity, in file order.

    Tracks are preferred: a file holding both a recorded track and the route it
    was planned from should import what actually happened. Routes are read only
    when there is no track — the Komoot/RideWithGPS case issue #260 was written
    for, and the one the first version refused.

    Segments within one track are concatenated, as before: a recording paused
    and resumed is one activity, and the gap between segments is exactly what
    :attr:`GpxCandidate.moving_seconds` declines to count.
    """
    found: List[GpxCandidate] = []
    for index, track in enumerate(gpx.tracks):
        points, times = _flatten([p for seg in track.segments for p in seg.points])
        found.append(GpxCandidate(
            index=index, name=_clean(track.name),
            activity_type=map_activity_type(track.type),
            points=points, times=times, is_route=False,
        ))
    # An element carrying no points is not a candidate. Some tools write an
    # empty <trk> as a placeholder alongside the real <rte>, and treating it
    # as a track suppressed the route entirely: the file was then refused for
    # having a track with no points, which is true and useless.
    found = [c for c in found if c.points]
    if found:
        return _renumbered(found)

    for index, route in enumerate(gpx.routes):
        points, times = _flatten(route.points)
        found.append(GpxCandidate(
            index=index, name=_clean(route.name),
            activity_type=map_activity_type(getattr(route, "type", None)),
            points=points, times=times, is_route=True,
        ))
    return _renumbered([c for c in found if c.points])


def _renumbered(found: List[GpxCandidate]) -> List[GpxCandidate]:
    """Re-index after dropping empties, so ``index`` addresses this list."""
    return [
        GpxCandidate(index=position, name=c.name,
                     activity_type=c.activity_type, points=c.points,
                     times=c.times, is_route=c.is_route)
        for position, c in enumerate(found)
    ]


def _flatten(raw_points: Sequence) -> Tuple[List[TrackPoint], List[Optional[datetime]]]:
    points = [TrackPoint(lat=p.latitude, lng=p.longitude, elev=p.elevation)
              for p in raw_points]
    times = [_as_utc(getattr(p, "time", None)) for p in raw_points]
    return points, times


def _as_utc(stamp: Optional[datetime]) -> Optional[datetime]:
    """Give a naive timestamp UTC, so one file cannot mix the two kinds.

    GPX times are UTC by specification, but a ``<time>`` written without a
    zone suffix parses naive, and a file mixing the two forms then raised
    ``can't subtract offset-naive and offset-aware datetimes`` from the
    middle of a duration calculation. Downstream that is a 500 rather than a
    refusal the user can act on.
    """
    if stamp is not None and stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


def _clean(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    return text or None


def map_activity_type(raw: Optional[str]) -> Optional[str]:
    """Map a GPX ``<type>`` onto one of the app's activity types, or None.

    None for anything unrecognised — including the bare numbers some Garmin
    exports write — so the user is asked rather than told something confidently
    wrong.
    """
    # Tools differ only in how they join words: Garmin Connect writes
    # "road_biking" and "trail_running", Strava "cycling" and "running".
    # Normalising the separators lets one table cover both.
    key = (raw or "").strip().lower().replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    return _TYPE_ALIASES.get(key)


def suggested_name(gpx: gpxpy.gpx.GPX, candidate: GpxCandidate,
                   filename: Optional[str] = None) -> Optional[str]:
    """The best name the file offers, in descending order of specificity.

    The track's own name, then the file's metadata name, then the filename's
    stem. A filename is the weakest of the three and often the worst thing to
    show a user — ``2024-08-12_073312`` is a name only a device would choose —
    but it beats nothing, and it is all the first version ever used.
    """
    metadata_name = _clean(getattr(gpx, "name", None))
    stem = None
    if filename:
        base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        stem = _clean(base.rsplit(".", 1)[0])
    return candidate.name or metadata_name or stem


def validate_candidate(candidate: GpxCandidate) -> List[str]:
    """Return rejection reasons for one candidate; empty means importable."""
    errors: List[str] = []
    points = candidate.points

    if len(points) < 2:
        errors.append(f"Track has fewer than 2 points ({len(points)}).")

    if any(p.lat is None or p.lng is None for p in points):
        errors.append("Track contains a point with missing coordinates.")
    else:
        out_of_range_lat = next(
            (p.lat for p in points if not -90 <= p.lat <= 90), None)
        if out_of_range_lat is not None:
            errors.append(
                f"Track contains an out-of-range latitude ({out_of_range_lat}).")
        out_of_range_lng = next(
            (p.lng for p in points if not -180 <= p.lng <= 180), None)
        if out_of_range_lng is not None:
            errors.append(
                f"Track contains an out-of-range longitude ({out_of_range_lng}).")

    if len(points) > MAX_IMPORT_POINTS:
        errors.append(
            f"Track has too many points ({len(points)}); the limit is "
            f"{MAX_IMPORT_POINTS}."
        )
    return errors


def validate_for_import(gpx: gpxpy.gpx.GPX,
                        track_index: Optional[int] = None) -> List[str]:
    """Return rejection reasons for the file; empty means importable.

    With no ``track_index`` a file holding more than one candidate is still
    refused — choosing for the user would be guessing which ride they meant —
    but the message now says a choice exists rather than that the file is
    unsupported, and :func:`candidates` is what the caller offers them.
    """
    found = candidates(gpx)
    if not found:
        if gpx.waypoints:
            return ["GPX contains only waypoints, and no route or track to import."]
        return ["GPX contains no route or track to import."]

    if track_index is None:
        if len(found) > 1:
            noun = "routes" if found[0].is_route else "tracks"
            return [f"GPX contains {len(found)} {noun}; choose which one to import."]
        track_index = 0

    if not 0 <= track_index < len(found):
        return [f"GPX has no track at position {track_index}."]

    return validate_candidate(found[track_index])


def gpx_track_to_points(gpx: gpxpy.gpx.GPX,
                        track_index: int = 0) -> List[TrackPoint]:
    """Flatten a validated candidate into TrackPoints, in order.

    Raises:
        GPXImportError: if there is no candidate at that position, rather
            than letting a negative index quietly select from the far end.
    """
    found = candidates(gpx)
    if not 0 <= track_index < len(found):
        raise GPXImportError([f"GPX has no track at position {track_index}."])
    return found[track_index].points

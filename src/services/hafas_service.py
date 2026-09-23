"""Train schedule + track geometry, resolved through MOTIS (Transitous).

Turns a train number and a service date into an ordered list of stops **and**
the real track polyline of that journey, using the public MOTIS instance at
https://api.transitous.org.

Why this replaced the four HAFAS provider backends
--------------------------------------------------
Every bespoke backend this module used to carry is dead or not worth a second
code path (verified against the live endpoints, issue #277):

* ``db``  — ``v6.db.transport.rest`` answers **503** on every API path. DB
  switched its HAFAS endpoint off permanently; the wrapper now proxies
  ``db-vendo-client``, which has a ~60 req/min quota and, per its own readme,
  "haphazard blocking".
* ``obb`` — ``oebb.macistry.com`` answers 503 "This service has been suspended
  by its owner."
* ``dsb`` — ``xmlopen.rejseplanen.dk`` answers HTTP **299** with an HTML page
  saying API 1.0 is closed. 299 is a 2xx, so ``raise_for_status()`` never
  fired and the old code called ``.json()`` on HTML.
* ``vr``  — ``rata.digitraffic.fi`` is healthy, but the same Finnish data
  (``fi-digitraffic``) is in the Transitous index, so it no longer earns its
  own wire format.

Both ``db-rest`` and ``db-vendo-client`` now point users at Transitous as the
migration path, and one MOTIS index covers DE/AT/DK/FI in a single API. The
``hafas_provider`` field is therefore vestigial: stored segments still carry it
and it is still persisted, but resolution ignores it.

Two defects disappear with the move:

1. **Match on a departure board, not a journey planner.** The old lookup asked
   ``/journeys`` for 15 results from a hardcoded 06:00+01:00 and passed
   ``lineName=<train>`` — a parameter ``hafas-rest-api``'s journeys route does
   not have, so it was silently ignored and the code just scanned 15 arbitrary
   journeys. ICE 75 leaves Hamburg Hbf at 14:29 local with 275 rail departures
   ahead of it on the board: it could never be found. ``/stoptimes`` is indexed
   by stop and time, so a train-number lookup is an exact scan of the service
   day instead of a 15-result guess.
2. **``/trip`` returns ``legGeometry``** — the whole journey's real track as an
   encoded polyline — so a matched train needs no Overpass query at all. That
   removes the bounding-box query cascade (3 mirrors x 45 s) from the train
   path entirely. ``overpass_service.get_rail_geometry`` remains the fallback
   for segments with no train number, or whose train could not be matched.

Raises HafasError on any failure — callers fall back to coordinate-only mode.
"""
from __future__ import annotations

import math

from src.models.simplify import simplify_lonlat
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polyline as polyline_lib
import requests

from src.brand import USER_AGENT
from src.utils.logging import get_logger
from src.utils.metrics import track_external

_log = get_logger(__name__)

_MOTIS_BASE = "https://api.transitous.org/api/v1"

# Transitous asks API users to identify themselves and keep volume modest
# (https://transitous.org/api/). Same format as overpass_service._HEADERS.
_HEADERS = {"User-Agent": USER_AGENT}

_TIMEOUT = 20  # seconds per request

# A resolve makes at most three MOTIS calls: one reverse-geocode, one or two
# departure-board pages, one trip. Retry only on the statuses that mean "try
# again later" — a single 503 used to end the whole resolve.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_S = 1.0  # 1 s then 2 s; bounded so the client's poll window survives it

# Rail modes accepted for the departure board. Regional services are included
# because users do enter regional train numbers; the extra volume costs nothing
# in requests (measured: one 500-entry page spans ~23 h at Hamburg Hbf).
_BOARD_MODES = (
    "HIGHSPEED_RAIL,LONG_DISTANCE,NIGHT_RAIL,REGIONAL_FAST_RAIL,REGIONAL_RAIL"
)

# Merge every stop within this radius of the anchor into one board. This is
# what makes a single reverse-geocode hit good enough as an anchor: MOTIS
# returns only five candidates and does not honour a count parameter, so a pin
# dropped on a town centre can come back as five bus stops with the station
# nowhere in the list (verified at Offenburg). Anchoring on the best-scoring
# stop whatever its modes, and sweeping 2 km around it, puts the station's rail
# departures on the board anyway — verified from an Offenburg bus stop and from
# a Hamburg U-Bahn platform, both of which then see ICE 75. The board is
# already filtered to rail modes, so the wider radius costs nothing.
_STOP_RADIUS_M = 2000

_BOARD_PAGE_SIZE = 500
_BOARD_MAX_PAGES = 4  # hard stop; one page already spans ~23 h at Hamburg Hbf

# How many board entries matching the query are worth resolving. A genuine
# train number is unique on a service day, so this only ever bites for a *line*
# number - "RE1" runs a dozen times a day and the short workings stop well
# short of where the long ones go. Each extra candidate costs one /trip
# request, and Transitous asks for modest volume, so the scan stops here rather
# than walking a whole day's departures.
_MAX_TRIP_CANDIDATES = 5

# How far a trip's nearest stop may be from the segment endpoint the user
# placed. Generous — pins get dropped on city centres, not platforms — but
# tight enough that a train not actually serving the endpoint is rejected
# instead of silently producing a plausible-looking wrong route.
_MAX_ENDPOINT_SNAP_KM = 30.0

# Fallback service-day window, used only when a feed gives no usable timezone.
# The real window comes from the anchor stop's own IANA zone, which MOTIS
# returns on the reverse-geocode hit we already make - see _service_window.
_DAY_START_SLACK = timedelta(hours=3)

# Track geometry is persisted and shipped to the client whole, so it is
# simplified before it leaves here. MOTIS shape points sit ~74 m apart, far
# finer than a map render or the track editor's draggable vertices need.
# Ramer-Douglas-Peucker at this tolerance stays within it everywhere by
# construction (measured: 2.07 m worst case on ICE 75, see _simplify) and takes
# Hamburg-Offenburg from 9,717 points / 242 KiB of JSON to 2,023 / 49 KiB.
_SIMPLIFY_TOLERANCE_M = 2.0

# Bound on the anchor cache. A miss is never cached - a transient reverse-geocode
# failure must not pin "no station here" for the worker's lifetime.
_STOP_CACHE_MAX = 256
_stop_cache: dict[tuple[float, float], tuple[str, str, str]] = {}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HafasError(Exception):
    pass


class _TripRejected(HafasError):
    """One candidate working does not fit the segment — try the next one.

    A HafasError like any other for callers, so the message a single candidate
    fails with is unchanged; the subclass only keeps the fallback loop from
    swallowing a transport failure, which must end the lookup outright.
    """


@dataclass
class TrainRoute:
    """A matched train service, trimmed to the caller's own two endpoints.

    ``polyline`` is the real track as ``[[lon, lat], …]`` — the same coordinate
    order ``overpass_service`` uses. It is empty when MOTIS returned a trip with
    no geometry, which tells the caller to fall back to Overpass.
    """
    stops: list[dict]            # [{name, lat, lon, uic}, …] start → end
    polyline: list[list[float]]  # [[lon, lat], …] start → end, or []


def get_train_route(
    train_number: str,
    date: str,          # ISO "YYYY-MM-DD"
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> TrainRoute:
    """Resolve *train_number* on *date* into its stops and real track geometry.

    Raises HafasError if the train can't be matched, doesn't serve the given
    endpoints, or the API fails.
    """
    name = re.sub(r"\s+", " ", (train_number or "").strip())
    if not name:
        raise HafasError("No train number given")
    if not _DATE_RE.match(date or ""):
        _log.warning("train lookup failed: no usable service date (train=%r date=%r)",
                     name, date)
        raise HafasError(f"No usable service date for train {name!r}")

    try:
        anchor = _nearest_stop(start_lat, start_lon)
        if anchor is None:
            _log.warning(
                "train lookup failed: no stop near the start point "
                "(train=%r start=%s,%s)", name, start_lat, start_lon)
            raise HafasError("Could not locate a station near the start point")

        # A line number matches many workings and they do not all run the same
        # way, so a rejected candidate is not the end of the lookup: keep going
        # down the board until one serves both endpoints. The scan is lazy, so
        # a number that matches once still costs exactly what it always did.
        tried = 0
        rejection: Optional[_TripRejected] = None
        for trip_id in _iter_trip_ids(anchor["id"], date, name, anchor["tz"]):
            tried += 1
            try:
                return _route_from_trip(
                    trip_id, name, start_lat, start_lon, end_lat, end_lon)
            except _TripRejected as exc:
                rejection = exc
                _log.info("train lookup: %r candidate %s rejected (%s)",
                          name, trip_id, exc)

        if tried == 0:
            _log.warning(
                "train lookup failed: %r not on the %s departure board at %s",
                name, date, anchor["name"])
            raise HafasError(
                f"Train {name!r} not found departing {anchor['name']} on {date}")
        if tried == 1:
            # Unambiguous number: the one candidate's own reason, as before.
            _log.warning("train lookup failed: %s", rejection)
            raise rejection
        _log.warning(
            "train lookup failed: none of the %d %r departures from %s on %s "
            "serve both endpoints (last: %s)",
            tried, name, anchor["name"], date, rejection)
        raise HafasError(
            f"Train {name!r} departs {anchor['name']} on {date}, but none of the "
            f"{tried} services checked runs between this segment's endpoints")

    except HafasError:
        raise
    except Exception as exc:
        _log.exception(
            "train route lookup failed unexpectedly (train=%r date=%s start=%s,%s end=%s,%s)",
            name, date, start_lat, start_lon, end_lat, end_lon)
        raise HafasError(f"Train route lookup failed: {exc}") from exc


def _route_from_trip(
    trip_id: str,
    name: str,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> TrainRoute:
    """One candidate trip, trimmed to the caller's endpoints.

    Raises _TripRejected if this particular working does not serve them, which
    is the caller's cue to try the next candidate rather than to give up. A
    transport failure propagates as a plain HafasError and ends the lookup.
    """
    stops, poly = _trip_stops_and_geometry(trip_id)
    if len(stops) < 2:
        raise _TripRejected("Trip returned fewer than 2 stops")

    trimmed = _trim_stops(stops, start_lat, start_lon, end_lat, end_lon)
    if len(trimmed) < 2:
        raise _TripRejected(
            f"Train {name!r} serves only one of this segment's endpoints")
    _check_serves_endpoints(trimmed, name, start_lat, start_lon, end_lat, end_lon)

    return TrainRoute(trimmed, _simplify(_trim_polyline(poly, trimmed)))


# ---------------------------------------------------------------------------
# MOTIS transport
# ---------------------------------------------------------------------------

def _get(path: str, params: dict) -> Any:
    """GET a MOTIS endpoint, retrying transient 5xx/429 with backoff.

    A single 503 used to end a resolve outright. Attempts are capped at three
    with 1 s/2 s backoff so the worst case stays well inside the client's poll
    window.
    """
    url = f"{_MOTIS_BASE}{path}"
    endpoint = f"motis{path}"
    last = ""
    for attempt in range(_MAX_ATTEMPTS):
        with track_external("hafas", endpoint) as call:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            if resp.status_code in _RETRY_STATUS:
                call.outcome = "retryable_error"
                last = f"HTTP {resp.status_code}"
            else:
                resp.raise_for_status()
                return resp.json()
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_BACKOFF_S * (2 ** attempt))
    raise HafasError(f"MOTIS {path} unavailable after {_MAX_ATTEMPTS} attempts: {last}")


def _nearest_stop(lat: float, lon: float) -> Optional[dict]:
    """Board anchor for (lat, lon): id, display name and IANA timezone, or None.

    Cached on coordinates rounded to 4 dp (~11 m) so segments sharing a station
    reverse-geocode once. Only *hits* are cached: caching a miss would pin a
    transient network failure as "no station here" for the worker's lifetime.
    """
    key = (round(lat, 4), round(lon, 4))
    found = _stop_cache.get(key)
    if found is None:
        found = _lookup_stop(*key)
        if found is None:
            return None
        if len(_stop_cache) >= _STOP_CACHE_MAX:
            _stop_cache.clear()
        _stop_cache[key] = found
    return {"id": found[0], "name": found[1], "tz": found[2]}


def _lookup_stop(lat: float, lon: float) -> Optional[tuple[str, str, str]]:
    results = _get("/reverse-geocode", {"place": f"{lat},{lon}", "type": "STOP"})
    if not isinstance(results, list):
        return None
    # Results come back best-first (MOTIS scores distance against importance).
    # Mode isn't a filter here — see _STOP_RADIUS_M.
    for r in results:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        if _crow_km(lat, lon, r["lat"], r["lon"]) > _MAX_ENDPOINT_SNAP_KM:
            continue
        # ``tz`` is the stop's IANA zone; it is what makes the service-day
        # window correct rather than a guess about which continent we are on.
        return str(r["id"]), str(r.get("name", "")), str(r.get("tz", ""))
    return None



def _iter_trip_ids(stop_id: str, date: str, train_name: str,
                   tz: str = "", limit: int = _MAX_TRIP_CANDIDATES) -> Iterator[str]:
    """Trips on *stop_id*'s board within *date*'s service day matching *train_name*.

    Yielded in board order, at most *limit* of them, so the caller can fall back
    to the next working when the first does not serve its endpoints. A train
    number matches once; a line number matches every working that runs.

    Lazy on purpose: the caller stops pulling as soon as a candidate resolves,
    so the common single-match lookup reads no more of the board than it did
    when this returned only the first hit.

    Pages forward with MOTIS's own ``pageCursor`` instead of brute-forcing a
    huge ``n``: one 500-entry page already spans ~23 h at Germany's busiest
    station, so this is normally a single request.

    *tz* is the anchor stop's IANA zone, which fixes the window to the local
    calendar day the user meant — see _service_window.
    """
    start, end = _service_window(date, tz)
    cursor: Optional[str] = None
    seen: set[str] = set()

    for _page in range(_BOARD_MAX_PAGES):
        params: dict[str, Any] = {
            "stopId": stop_id,
            "n": _BOARD_PAGE_SIZE,
            "radius": _STOP_RADIUS_M,
            "mode": _BOARD_MODES,
        }
        if cursor:
            params["pageCursor"] = cursor
        else:
            params["time"] = _iso_z(start)

        data = _get("/stoptimes", params)
        entries = data.get("stopTimes") or []
        if not entries:
            return

        for entry in entries:
            when = _entry_time(entry)
            if when is not None and when >= end:
                return  # past the service day — no more of today's workings
            if when is not None and when < start:
                continue
            if _entry_matches(entry, train_name):
                trip_id = entry.get("tripId")
                # The radius sweep puts one trip on the board once per stop it
                # calls at, so the same working can appear more than once.
                if trip_id and trip_id not in seen:
                    seen.add(trip_id)
                    yield trip_id
                    if len(seen) >= limit:
                        return

        last = _entry_time(entries[-1])
        if last is not None and last >= end:
            return
        cursor = data.get("nextPageCursor")
        if not cursor:
            return


def _trip_stops_and_geometry(trip_id: str) -> tuple[list[dict], list[list[float]]]:
    """Whole-trip stop list and decoded track for a MOTIS trip id."""
    data = _get("/trip", {"tripId": trip_id})
    stops: list[dict] = []
    poly: list[list[float]] = []

    for leg in data.get("legs") or []:
        places = [leg.get("from")] + list(leg.get("intermediateStops") or []) + [leg.get("to")]
        for place in places:
            stop = _stop_from_place(place)
            if stop is None:
                continue
            if stops and (stop["lat"], stop["lon"]) == (stops[-1]["lat"], stops[-1]["lon"]):
                continue  # a through-leg boundary repeats the shared stop
            stops.append(stop)

        part = _decode_leg_geometry(leg)
        if poly and part and part[0] == poly[-1]:
            part = part[1:]
        poly.extend(part)

    return stops, poly


def _stop_from_place(place: Optional[dict]) -> Optional[dict]:
    if not place or place.get("lat") is None or place.get("lon") is None:
        return None
    return {
        "name": place.get("name", ""),
        "lat": place["lat"],
        "lon": place["lon"],
        # MOTIS stop IDs are feed-scoped (``de-DELFI_de:02000:10950``), not UIC
        # codes; overpass_service only accepts numeric UIC refs, so leave it
        # empty and let its own enrichment run if it is ever needed.
        "uic": "",
    }


def _decode_leg_geometry(leg: dict) -> list[list[float]]:
    """Decode ``legGeometry`` into ``[[lon, lat], …]``.

    MOTIS encodes at **precision 7**, ten times the usual Google-polyline
    precision — decoding at the default 5 would place the line ~1000 km away.
    The response states its own precision, so honour that rather than assume.
    """
    geometry = leg.get("legGeometry") or {}
    encoded = geometry.get("points")
    if not encoded:
        return []
    precision = int(geometry.get("precision") or 5)
    return [[lon, lat] for lat, lon in polyline_lib.decode(encoded, precision=precision)]


# ---------------------------------------------------------------------------
# Matching and trimming
# ---------------------------------------------------------------------------

def _service_window(date: str, tz: str = "") -> tuple[datetime, datetime]:
    """UTC window covering *date* as a local calendar day at the anchor stop.

    The zone comes from the anchor's own ``tz`` field, so this is the day the
    user meant rather than a guess. A fixed UTC span was wrong in both
    directions. Too early: a window opening 3 h before 00:00 UTC put the
    *previous* local day's departures on the board first, and the scan takes
    the first forward match — a request for 2026-09-01 returned RE 11438's
    2026-08-31 trip. Too narrow: ``hafas_provider`` no longer gates the lookup
    and Transitous is worldwide, so at UTC-5 a window ending at 00:00 UTC cut
    off everything from 19:00 local and reported "not found" for trains that do
    run. "A train number repeats daily" rescues neither case — weekend,
    seasonal and night services may not run on the requested day at all, and
    silently resolving the wrong day is worse than failing.

    Falls back to the old fixed span only when a feed offers no usable zone.
    """
    day = datetime.strptime(date, "%Y-%m-%d")
    zone = _zone(tz)
    if zone is None:
        utc_day = day.replace(tzinfo=timezone.utc)
        return utc_day - _DAY_START_SLACK, utc_day + timedelta(days=1)
    start = day.replace(tzinfo=zone)
    end = (day + timedelta(days=1)).replace(tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _zone(tz: str) -> Optional[ZoneInfo]:
    """The IANA zone named by *tz*, or None if absent or unknown to the platform."""
    if not tz:
        return None
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        _log.warning("unknown timezone %r from MOTIS — using the fallback window", tz)
        return None



def _iso_z(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_time(entry: dict) -> Optional[datetime]:
    place = entry.get("place") or {}
    raw = place.get("departure") or place.get("arrival")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _entry_matches(entry: dict, train_name: str) -> bool:
    route = entry.get("routeShortName") or ""
    trip = entry.get("tripShortName") or ""
    # Most feeds set both to the full name ("ICE 75"); some split them
    # ("ECE" + "000393"), so the concatenation is a candidate too.
    return any(_name_matches(c, train_name)
               for c in (trip, route, f"{route} {trip}") if c.strip())


_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")
_BRACKETS_RE = re.compile(r"[()\[\]{}]")


def _name_variants(name: str) -> list[tuple[str, str]]:
    """(letters, digits) for each bracket-delimited part of a service name.

    German boards label a trip ``"RE8 (11438)"`` — the line and the trip number
    in one string. Normalising that in one pass concatenates the two numbers
    into ``811438``, which matches nothing a user could ever type, so ``RE8``
    silently found no train at all. Splitting on the brackets keeps ``RE8`` and
    ``11438`` separately addressable.
    """
    parts = [p for p in _BRACKETS_RE.split(name or "") if p.strip()]
    return [_split_name(p) for p in parts]


def _query_forms(query: str) -> list[str]:
    """The query as typed, plus it with a leading operator token dropped.

    The Operator dropdown is gone, so the train-number field is the only
    place left to name the operator - and #277's own title reads "DB ICE
    75" while the board carries only "ICE 75". A token is dropped only
    while what remains still has letters of its own: without that guard
    "RS 1" would degrade to the bare "1" and match every service numbered
    1, and a letterless query matches on digits alone.
    """
    forms = [query]
    parts = (query or "").split()
    for i in range(1, len(parts)):
        tail = " ".join(parts[i:])
        if not re.search(r"[A-Za-z]", tail):
            break
        forms.append(tail)
    return forms


def _name_matches(line_name: str, query: str) -> bool:
    """True if *line_name* names the service the user asked for.

    Compares the letter part and the number part separately, which is stricter
    than the substring test this replaced: "ICE 75" no longer matches "ICE 755"
    (a real hazard now that whole departure boards are scanned). A bare number
    still matches a named service, and leading zeros are ignored so "393"
    matches Rejseplanen's "000393".

    A *named* query always has its letters confirmed. Waiving that whenever the
    candidate happened to carry no letters made "TGV 11438" match a Deutsche
    Bahn RE8, because that board's ``tripShortName`` is the bare "011438"; the
    split-name feeds ("ECE" + "000393") are served by the concatenated
    candidate in _entry_matches, which does carry the letters.
    """
    for a_letters, a_digits in _name_variants(line_name):
        for form in _query_forms(query):
          for b_letters, b_digits in _name_variants(form):
            if not (a_letters or a_digits) or not (b_letters or b_digits):
                continue
            if a_digits.lstrip("0") != b_digits.lstrip("0"):
                continue
            if not b_letters or a_letters == b_letters:
                return True
    return False


def _split_name(name: str) -> tuple[str, str]:
    norm = _NON_ALNUM_RE.sub("", (name or "").upper())
    return re.sub(r"\d", "", norm), re.sub(r"\D", "", norm)



def _check_serves_endpoints(
    stops: list[dict],
    train_name: str,
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> None:
    """Reject a match whose stops are nowhere near the segment's endpoints.

    The board is searched from the start stop only, so nothing else guarantees
    the matched train also serves the destination. Without this a wrong match
    would silently yield a confident-looking but wrong line.
    """
    for stop, (lat, lon), which in (
        (stops[0], (lat1, lon1), "start"),
        (stops[-1], (lat2, lon2), "end"),
    ):
        gap = _crow_km(lat, lon, stop["lat"], stop["lon"])
        if gap > _MAX_ENDPOINT_SNAP_KM:
            # Only INFO: with several candidates on the board this rejection is
            # routine, and the caller logs the failure once nothing is left.
            _log.info(
                "train candidate rejected: %r does not serve the %s point "
                "(nearest stop %r is %.0f km away)", train_name, which, stop["name"], gap)
            raise _TripRejected(
                f"Train {train_name!r} does not stop near the {which} of this segment")


def _trim_stops(
    stops: list[dict],
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> list[dict]:
    def d2(s, lat, lon):
        return (s["lat"] - lat) ** 2 + (s["lon"] - lon) ** 2

    si = min(range(len(stops)), key=lambda i: d2(stops[i], lat1, lon1))
    ei = min(range(len(stops)), key=lambda i: d2(stops[i], lat2, lon2))

    if si <= ei:
        return stops[si : ei + 1]
    return list(reversed(stops[ei : si + 1]))


def _trim_polyline(poly: list[list[float]], stops: list[dict]) -> list[list[float]]:
    """Cut the whole-trip track down to the caller's own two endpoints.

    ``/trip`` returns the entire service — Hamburg→Basel for a Hamburg→Offenburg
    segment — so the geometry needs the same treatment ``_trim_stops`` gives the
    stop list, including the reversal when the segment runs against the trip's
    direction of travel.
    """
    if len(poly) < 2 or len(stops) < 2:
        return []
    i = _nearest_point(poly, stops[0])
    j = _nearest_point(poly, stops[-1])
    part = poly[i : j + 1] if i <= j else list(reversed(poly[j : i + 1]))
    return part if len(part) >= 2 else []


def _simplify(poly: list[list[float]],
              tolerance_m: float = _SIMPLIFY_TOLERANCE_M) -> list[list[float]]:
    """Trim MOTIS shape points to what anything downstream can actually use.

    MOTIS ships points ~74 m apart over the whole journey, which is far finer
    than anything needs and is not free: the polyline is persisted, returned
    by /geo on every load, and turned into one draggable marker per vertex by
    the track editor, which has no cap of its own. Hamburg->Offenburg goes
    from 9,717 points / 242 KiB of JSON to 2,023 / 49 KiB, with every dropped
    point provably within the tolerance of the line that replaces it.

    The algorithm itself lives in src/models/simplify.py, shared with the geo
    endpoints' zoom-level-of-detail (issue #295).
    """
    return simplify_lonlat(poly, tolerance_m)


def _nearest_point(poly: list[list[float]], stop: dict) -> int:
    lat, lon = stop["lat"], stop["lon"]
    return min(
        range(len(poly)),
        key=lambda k: (poly[k][1] - lat) ** 2 + (poly[k][0] - lon) ** 2,
    )


def _crow_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in km (equirectangular; same approx as overpass_service)."""
    dlat = (lat1 - lat2) * 111.0
    dlon = (lon1 - lon2) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)

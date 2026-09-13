"""
Overpass API service — extracts railway geometry from OpenStreetMap.

Two strategies (tried in order):
  3a  Route-relation strategy: query OSM train route relations containing
      both station nodes (matched by uic_ref tag).  The relation's member
      ways are routed start→end through a node graph (Dijkstra) rather than
      naively chained — train relations include double-track ways, sidings
      and station tracks that would otherwise produce a self-overlapping line.
  3b  Coordinate fallback: query railway ways inside the bounding box,
      build a node graph, and route with Dijkstra between the two nearest
      nodes to start/end coords.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Optional, Sequence
from urllib.parse import urlsplit

import requests

from src.brand import USER_AGENT
from src.jobs.upstream_cache import get as cache_get, put as cache_put
from src.jobs.upstream_slots import is_cooling, mark_cooling, slot
from src.services.rail_source import (
    LocalRailSource,
    RailSource,
    RailSourceOverload,
)
from src.utils.logging import get_logger

_log = get_logger(__name__)


@dataclass
class RailGeometry:
    """Result of a rail-geometry resolution.

    Carries *how* the polyline was obtained, not just the points, so callers can
    log it and surface degradation to the user. ``degraded`` is True when every
    strategy failed and we fell back to a straight endpoint-to-endpoint chord —
    i.e. the line is approximate, not real track. Ferry/bus resolution doesn't
    use this: those raise ``OverpassError`` on failure instead of degrading.
    """
    polyline: list[list[float]]
    strategy: str          # relation_uic | relation_endpoints | coordinate_dijkstra | straight
    degraded: bool

# ÖBB and some HAFAS providers return compound location IDs like
# "A=1@O=Linz Hbf@X=14280@Y=48290@U=81@L=8100013@…"
# Extract the numeric station code from the @L= field.
_HAFAS_L_RE = re.compile(r'@L=(\d+)@')

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Endpoints in preference order. Measured from the production VPS, from inside
# a container so it is the IPv4 path the app actually uses:
#
#   2026-09-06  overpass-api.de          200 in  8.4s for a full strategy-C
#                                        query (5.2 MB), both slots free
#               overpass.kumi.systems    no response at all within 50s
#               overpass.private.coffee  200 in 36.9s for a *trivial* query
#
#   2026-09-08  overpass-api.de          65.109.112.52 drops us, 162.55.144.139
#                                        sends RST. The block is still in force.
#               overpass.private.coffee  no answer to `out count;` within 90s
#               overpass.openstreetmap.fr  200 in 0.2s, timestamp_osm_base
#                                        current, 2 stations near Flensburg
#
# kumi was removed because it IS private.coffee: the OSM wiki records the former
# as the old name of the latter, so listing both was one operator counted twice
# and a retired hostname costing a full _TIMEOUT_HTTP to rediscover.
#
# private.coffee is removed now for the same reason in a different form: it has
# not answered a trivial query since the incident, so every attempt spends
# _TIMEOUT_HTTP to learn what the last one learned. Put it back if it recovers.
#
# openstreetmap.fr leads while overpass-api.de blocks this address. Order is by
# measured health, not by preference: with the canonical instance first, one
# resolve an hour pays a full timeout before reaching a host that works, and
# _COOLDOWN_UNREACHABLE_S is what keeps that to one. overpass-api.de stays in
# the list so it is used again if the block lifts.
#
# Sequential failover between public instances is sanctioned; running them in
# parallel to raise throughput is explicitly not.
_OVERPASS_ENDPOINTS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    _OVERPASS_URL,
]

# Instances that answer 200 quickly and hold only their own country. These are
# more dangerous than a dead host: an empty `elements` array is what a *correct*
# query over a region with no rail looks like, so all three strategies read it
# as "no route found" and degrade to a straight line, with nothing in the logs
# to say the data was never there. Verified 2026-09-08 from the VPS:
# overpass.osm.ch answers a Flensburg station query 200 in 0.1s with 0 elements,
# while overpass.openstreetmap.fr returns 2. Check coverage, not liveness,
# before adding any instance here.
_REGION_LIMITED_INSTANCES = ("overpass.osm.ch",)
# 429 is deliberately NOT in here — see _overpass. These are the codes that mean
# the host itself is in trouble, where another host is the faster path.
_OVERPASS_RETRYABLE = {502, 503, 504}
_RATE_LIMITED = 429
_TIMEOUT_QUERY = 30   # seconds for the Overpass QL timeout directive
# The server enqueues a request for up to 15s deciding whether to admit it,
# then runs it for up to the declared [timeout:] above. A 45s socket timeout
# sat exactly on that worst compliant case and aborted valid requests — which
# still burn the slot and its cooldown, for nothing.
_TIMEOUT_HTTP  = _TIMEOUT_QUERY + 15 + 15   # admission window + runtime + margin

# Backing off a host that has told us to stop. overpass-api.de's stated policy is
# "if you receive an HTTP error code such as 429 or 406, pause for 30 seconds
# before making a new request", and its operators have said that clients which
# repeatedly hit a 429 are banned faster — a high 429 *rate* being a trigger in
# its own right. 60s is that documented minimum with margin.
#
# We do NOT wait inside the request and retry. A resolve makes several queries,
# so pausing this one while the next hits the same host would honour the letter
# and miss the point; and every extra attempt adds to the 429 rate that gets
# apps banned. The host is marked instead, and this query moves on or degrades.
_COOLDOWN_RATE_LIMITED_S = 60
# A host that will not complete a TCP connection at all is either down or has
# blocked us. Reconnecting tells us nothing it has not already said, and an
# operator answering a report of this exact symptom could not rule out that
# continued attempts prolong a block. Back off by an order of magnitude.
_COOLDOWN_UNREACHABLE_S = 3600
# Statuses that mean "stop asking", per the policy above. 504 is included on the
# server's own documented meaning — not a generic gateway error but "the server
# is probably too busy to handle your request", i.e. resource admission refused.
_BACK_OFF_STATUSES = {429, 502, 503, 504}

# Our own pacing against the host's advertised per-IP concurrency, shared across
# processes (src.jobs.upstream_slots). This is the real Overpass politeness bound;
# QUEUE_MAX_CONCURRENCY cannot express it, because one resolve makes several
# queries and jobs on the other queues make none.
#
# ONE, and note that `Rate limit: N` in /api/status is NOT a concurrency limit.
# It is a count of slots, and a slot is held for the query's execution time plus
# a cooldown that grows with server load and in proportion to that execution
# time. Two sequential 30s queries can therefore leave zero slots free with
# nothing of ours in flight at all. Treating it as "two at once are allowed" is
# what had us sitting permanently on the boundary, 429'ing constantly — and a
# rejection is not free, it costs 10-14s because the dispatcher queues you
# before refusing. What we are really rationing is slot-seconds, so the useful
# lever is asking less and finishing fast, not counting sockets.
_OVERPASS_CONCURRENCY = 1
# Generous against how long a slot is actually held — a healthy query answers in
# a couple of seconds, so a wait this long means something is badly wrong and
# another endpoint is the better bet.
_SLOT_ACQUIRE_TIMEOUT_S = 15
_SLOT_LEASE_TTL_S = _TIMEOUT_HTTP + 15   # outlive the request the lease covers

# Every Overpass query is a pure function of the segment's coordinates, so a
# retry re-asks an identical question. Caching them is what stops a person
# tapping "retry", or the hourly degraded-route sweep, from spending our
# whole quota re-fetching answers we already have (src.jobs.upstream_cache).
_CACHE_NAMESPACE = "overpass"

# A real rail leg between two endpoints is at most a few times the straight-line
# distance through its stops. A resolved polyline far longer than that has
# self-overlapped or stitched the wrong ways (e.g. a relation routed via Helsinki
# for a Hanko→Salo trip: observed 291 km / 47 km crow = 6.2x). Such a result is
# rejected so a cleaner relation/strategy is used instead of shipping garbage.
_MAX_RAIL_DETOUR_RATIO = 4.0
_MIN_PLAUSIBLE_KM = 5.0   # floor so very short legs (noisy ratio) aren't rejected


class OverpassError(Exception):
    pass


# ---------------------------------------------------------------------------
# The rail data source  (issue #345, Phase 3)
# ---------------------------------------------------------------------------

# Strategy B's relation filter, and the one Overpass matches on: the same three
# route values _route_relation_segment lists, written as one regex.
_ROUTE_TAGS = '"route"~"^(train|railway|light_rail)$"'


class OverpassRailSource(RailSource):
    """The rail strategies' five questions, asked over the network as before.

    The queries below are byte-identical to the ones the strategies issued
    inline until this phase; they moved here rather than changed, so that
    ``LocalRailSource`` can answer the same questions from disk. Anything
    strategy-shaped — how many relation candidates to fetch, how a bounding box
    is buffered, how a relation's geometry is turned into a polyline — stayed
    with the strategy, because it is not a property of the source.

    ``near`` is ignored: Overpass has no regions to select between.
    """

    def nearest_station(
        self, lat: float, lon: float, radius_m: float = 5000
    ) -> Optional[dict]:
        query = f"""
[out:json][timeout:15];
(
  node["railway"~"^(station|halt)$"]["uic_ref"](around:{radius_m},{lat},{lon});
  way["railway"~"^(station|halt)$"]["uic_ref"](around:{radius_m},{lat},{lon});
  rel["railway"~"^(station|halt)$"]["uic_ref"](around:{radius_m},{lat},{lon});
);
out center body;
"""
        elements = _overpass(query).get("elements", [])
        if not elements:
            return None

        def _coords(e: dict) -> tuple[float, float]:
            if e.get("type") == "node":
                return e.get("lat", 0.0), e.get("lon", 0.0)
            c = e.get("center", {})
            return c.get("lat", 0.0), c.get("lon", 0.0)

        nearest = min(
            elements,
            key=lambda e: (_coords(e)[0] - lat) ** 2 + (_coords(e)[1] - lon) ** 2,
        )
        uic = nearest.get("tags", {}).get("uic_ref")
        if not uic:
            return None
        elat, elon = _coords(nearest)
        return {"lat": elat, "lon": elon, "uic": uic}

    def relations_for_uic_pair(
        self, uic1: str, uic2: str, near: Sequence[tuple[float, float]]
    ) -> list[dict]:
        query = f"""
[out:json][timeout:{_TIMEOUT_QUERY}];
node["uic_ref"="{uic1}"]->.a;
node["uic_ref"="{uic2}"]->.b;
(
  rel["route"="train"](bn.a)(bn.b);
  rel["route"="railway"](bn.a)(bn.b);
  rel["route"="light_rail"](bn.a)(bn.b);
)->.r;
.r out geom;
"""
        return _overpass(query).get("elements", [])

    def relations_near(
        self, lat: float, lon: float, radius_m: float = 25_000
    ) -> set[int]:
        query = f"""
[out:json][timeout:{_TIMEOUT_QUERY}];
rel[{_ROUTE_TAGS}](around:{radius_m},{lat},{lon});
out ids;
"""
        return {e["id"] for e in _overpass(query).get("elements", [])}

    def relation_geometry(
        self, rel_ids: Sequence[int], near: Sequence[tuple[float, float]]
    ) -> list[dict]:
        ids_str = ",".join(str(i) for i in rel_ids)
        query = f"""
[out:json][timeout:{_TIMEOUT_QUERY}];
rel(id:{ids_str});
._ out geom;
"""
        return _overpass(query).get("elements", [])

    def ways_in_bbox(
        self, min_lat: float, min_lon: float, max_lat: float, max_lon: float
    ) -> list[dict]:
        # No usage filter — OSM tagging conventions vary by country (Germany uses
        # usage=main/branch; France often uses usage=main_line or omits it entirely).
        # The railway type filter is restrictive enough to avoid excessive data.
        query = (
            f"[out:json][timeout:{_TIMEOUT_QUERY}];"
            f'way["railway"~"^(rail|narrow_gauge|light_rail)$"]'
            f'["service"!~"."]'
            f"({min_lat},{min_lon},{max_lat},{max_lon});"
            "out geom;"
        )
        return _overpass(query).get("elements", [])


# One instance, because it holds nothing: the pacing, caching and cooldowns all
# live in _overpass and are shared process-wide already.
_OVERPASS_SOURCE = OverpassRailSource()

# Which source the resolver reads rail from. Overpass unless a deployment says
# otherwise, so shipping the local source changes nothing until it is switched
# on deliberately — Phase 4 compares the two before that becomes the default.
#
# Runtime environment, not build args: the data directory is a mounted volume
# whose contents change on a refresh schedule that has nothing to do with
# releases (see .env.example).
_RAIL_SOURCE_ENV = "RAIL_SOURCE"
_RAIL_DATA_DIR_ENV = "RAIL_DATA_DIR"

# Built once per directory and held for a while: re-reading the manifest and
# reopening the region files on every resolve would make RailStoreCache's LRU
# pointless. Held for a *bounded* time rather than forever, for two reasons.
#
# The data directory is a mounted volume refreshed on its own schedule, so a
# worker that started before a refresh would otherwise serve the old coverage
# until someone restarted it — and a release is exactly what the refresh is not
# supposed to need. And a manifest read while it is being rewritten fails, which
# would otherwise pin that one worker to Overpass permanently, after a single
# log line: the traffic this whole issue exists to stop, restored quietly.
#
# Five minutes is far below any refresh interval (monthly is ample) and far
# above any resolve, so it costs one manifest read per five minutes per worker.
_LOCAL_SOURCE_TTL_S = 300.0
_local_source: Optional[tuple[str, Optional[LocalRailSource], float]] = None


def _local_rail_source() -> Optional[LocalRailSource]:
    """The local source when this deployment is configured for it, else None.

    None means "resolve against Overpass exactly as before" and covers every way
    the configuration can be incomplete: the flag unset, no directory given, or
    a directory whose manifest we refuse to read. The last one is logged loudly
    and retried on the next expiry (see ``_LOCAL_SOURCE_TTL_S``) — it is a
    deployment fault, and its symptom is a quiet return to the traffic volume
    this whole issue exists to stop.

    "We refuse to read it" is deliberately every exception rather than
    ``RailSourceError`` alone. A manifest is a file someone else wrote, so it
    can be wrong in shapes ``load_coverage`` never enumerated — an entry with no
    ``region`` key, a bbox holding a string, a top-level list — each of which
    raises a plain builtin. There is one safe answer to all of them, and the
    alternative is a traceback out of a rail resolve.
    """
    global _local_source
    if os.environ.get(_RAIL_SOURCE_ENV, "").strip().lower() != "local":
        return None
    directory = os.environ.get(_RAIL_DATA_DIR_ENV, "").strip()
    if not directory:
        _log.warning("%s=local but %s is unset — resolving rail via Overpass",
                     _RAIL_SOURCE_ENV, _RAIL_DATA_DIR_ENV)
        return None
    now = time.monotonic()
    if (_local_source is None or _local_source[0] != directory
            or now - _local_source[2] >= _LOCAL_SOURCE_TTL_S):
        try:
            _local_source = (directory, LocalRailSource(directory), now)
        except Exception as exc:  # noqa: BLE001 — see docstring
            _log.warning("local rail data at %s is unusable (%s) — resolving "
                         "rail via Overpass, retrying in %.0fs",
                         directory, exc, _LOCAL_SOURCE_TTL_S)
            _local_source = (directory, None, now)
    return _local_source[1]


def get_rail_geometry(stops: list[dict]) -> RailGeometry:
    """
    Resolve [[lon, lat], …] rail geometry from stops[0] to stops[-1].

    *stops* is a list of dicts with keys lat, lon, and optionally uic.

    Strategies tried in order:
      A  UIC-based route relations (most precise; requires UIC enrichment).
      B  Two-endpoint route-relation intersection — finds relations that pass
         through both endpoint areas without needing UIC codes.
      C  Coordinate Dijkstra on bounding-box railway ways (last resort).

    Never raises (beyond the <2-stop guard): when all strategies fail, Strategy C
    returns a straight endpoint chord. The return value records which strategy
    won and whether the result is that degraded straight line, and every outcome
    is logged — so a silent straight line in production is now observable.

    Where the elements come from is configuration (issue #345, Phase 3). With
    the local source configured, the whole chain is tried against it first and
    Overpass answers only when that produced no route — a store can hold a
    region legitimately and hold almost nothing (Cyprus: two rail ways), so a
    local miss must not become a straight line while a real answer exists.
    """
    t0 = time.monotonic()
    if len(stops) < 2:
        raise OverpassError("Need at least 2 stops")

    lat1, lon1 = stops[0]["lat"], stops[0]["lon"]
    lat2, lon2 = stops[-1]["lat"], stops[-1]["lon"]

    result = None
    local = _local_rail_source()
    if local is not None:
        try:
            result = _resolve_rail(stops, local)
            if result.degraded:
                _log.info("local rail source found no route — retrying via Overpass")
                result = None
        except RailSourceOverload as exc:
            # The box is too big to hold in memory. Overpass cannot help: it
            # would answer the same question with the same volume, and building
            # the graph from its answer is the allocation that was just refused.
            # So this is the one local failure that does not fall back.
            _log.warning("rail bounding box refused locally (%s) — straight-lining", exc)
            result = RailGeometry(_straight(lat1, lon1, lat2, lon2), "straight", True)
    if result is None:
        result = _resolve_rail(stops, _OVERPASS_SOURCE)

    log = _log.warning if result.degraded else _log.info
    log("rail geometry resolved: strategy=%s points=%d degraded=%s elapsed=%.1fs",
        result.strategy, len(result.polyline), result.degraded, time.monotonic() - t0)
    return result


def _resolve_rail(stops: list[dict], source: RailSource) -> RailGeometry:
    """The three strategies, in order, against one source.

    Unchanged from what ``get_rail_geometry`` did inline before Phase 3, other
    than reading its elements from *source*.
    """
    lat1, lon1 = stops[0]["lat"], stops[0]["lon"]
    lat2, lon2 = stops[-1]["lat"], stops[-1]["lon"]

    # Only enrich the first and last stop to avoid O(N) Overpass calls on long
    # routes (e.g. VR Helsinki→Rovaniemi has ~8 stops and returns uic="" for
    # all of them, which previously triggered a _find_station_near HTTP call per
    # stop plus N-1 pairwise route-relation queries = ~15 calls = nginx 504).
    enriched = list(stops)
    enriched[0]  = _enrich_uic(stops[0], source)
    enriched[-1] = _enrich_uic(stops[-1], source)

    result: Optional[RailGeometry] = None

    def _accept(poly: list[list[float]], strategy: str) -> Optional[RailGeometry]:
        """Accept a strategy's geometry only if it's a plausible-length rail path;
        otherwise reject (None) so resolution falls through instead of shipping a
        self-overlapping / wrong-relation line."""
        if not _rail_length_ok(poly, stops):
            _log.info("rail strategy %s rejected: %.0f km path is implausibly long "
                      "(self-overlap/wrong relation) — falling through",
                      strategy, _polyline_km(poly))
            return None
        return RailGeometry(poly, strategy, False)

    # Strategy A: try a direct start→end route-relation lookup using the two
    # endpoint UIC codes.  One Overpass query; covers most long-haul trains.
    if enriched[0].get("uic") and enriched[-1].get("uic"):
        try:
            result = _accept(
                _via_route_relations([enriched[0], enriched[-1]], source), "relation_uic")
        except Exception as exc:  # noqa: BLE001 — fall through to the next strategy
            _log.info("rail strategy A (uic relations) failed: %s", exc)

    # Strategy B: two-endpoint route-relation intersection (works without UIC)
    if result is None:
        try:
            result = _accept(
                _via_train_relations_endpoints(enriched, source), "relation_endpoints")
        except Exception as exc:  # noqa: BLE001 — fall through to the last resort
            _log.info("rail strategy B (endpoint relations) failed: %s", exc)

    # Strategy C: coordinate Dijkstra — last resort. May return a straight chord
    # (degraded). A self-overlapping C result is worse than an honest straight
    # line, so straight-line it (flagged degraded) rather than ship garbage.
    if result is None:
        poly = _via_coordinate_fallback(enriched, source)
        # A 2-point result is the straight endpoint chord (no real track found) —
        # detect it by length, not by comparing coords: _enrich_uic may have
        # snapped the endpoints to nearby stations, so the chord won't equal a
        # _straight() built from the original stop coords.
        degraded = len(poly) <= 2
        if not degraded and not _rail_length_ok(poly, stops):
            _log.info("rail strategy C rejected (%.0f km implausible) — straight-lining",
                      _polyline_km(poly))
            poly = _straight(lat1, lon1, lat2, lon2)
            degraded = True
        result = RailGeometry(
            poly, "straight" if degraded else "coordinate_dijkstra", degraded)

    return result


def _enrich_uic(stop: dict, source: Optional[RailSource] = None) -> dict:
    """Return stop with a valid numeric uic_ref, snapping lat/lon to the OSM station if needed."""
    raw = stop.get("uic", "")

    # Already a bare numeric UIC — nothing to do.
    if raw and raw.isdigit():
        return stop

    # HAFAS compound ID (e.g. ÖBB "A=1@O=...@L=8100013@...") — extract the code.
    if raw and "@" in raw:
        m = _HAFAS_L_RE.search(raw)
        if m:
            return {**stop, "uic": m.group(1)}

    # Missing or unrecognised format — look up the nearest OSM station and also
    # snap lat/lon to that station so the Dijkstra starts on the actual mainline.
    station = _find_station_near(stop["lat"], stop["lon"], source=source)
    if station:
        return {**stop, "uic": station["uic"], "lat": station["lat"], "lon": station["lon"]}
    return {**stop, "uic": ""}


def _find_station_near(
    lat: float, lon: float, radius_m: int = 5000,
    source: Optional[RailSource] = None,
) -> Optional[dict]:
    """
    Nearest OSM railway station with a uic_ref within radius_m metres.
    Queries nodes, ways, and relations so that stations mapped as polygons
    (common in some countries) are also found.  Returns {lat, lon, uic} or None.
    """
    try:
        return (source or _OVERPASS_SOURCE).nearest_station(lat, lon, radius_m)
    except OverpassError as exc:
        # Sub-step of _enrich_uic — the umbrella get_rail_geometry() already logs
        # a WARNING for the overall resolve once every strategy has been tried, so
        # this stays at INFO to avoid a second, duplicate WARNING for one failed
        # station lookup that resolution may still recover from.
        _log.info("station lookup near %s,%s failed: %s", lat, lon, exc)
        return None


def _find_uic_near(lat: float, lon: float, radius_m: int = 5000) -> Optional[str]:
    """Return the uic_ref of the nearest OSM railway station, or None."""
    result = _find_station_near(lat, lon, radius_m)
    return result["uic"] if result else None


# ---------------------------------------------------------------------------
# Strategy 3a — route relations
# ---------------------------------------------------------------------------

def _via_route_relations(
    stops: list[dict], source: Optional[RailSource] = None
) -> list[list[float]]:
    full: list[list[float]] = []
    for i in range(len(stops) - 1):
        seg = _route_relation_segment(stops[i], stops[i + 1], source)
        if seg is None:
            raise OverpassError("No route relation covers a stop pair")
        full = full + (seg[1:] if full else seg)
    if len(full) < 2:
        raise OverpassError("Route-relation strategy returned empty geometry")
    return full


def _route_relation_segment(
    s1: dict, s2: dict, source: Optional[RailSource] = None
) -> Optional[list[list[float]]]:
    """The best relation serving this stop pair, or None if none is close enough.

    Every candidate is scored, not just the first. Both sources answer
    ``relations_for_uic_pair`` from a *single* query, so the candidates are
    already in hand and scoring them costs CPU and no extra Overpass traffic —
    while taking ``elements[0]`` and giving up threw away 31 usable candidates
    on Paris Est → Strasbourg because the lowest-id one happened not to route
    (issue #359).
    """
    uic1 = _clean_uic(s1["uic"])
    uic2 = _clean_uic(s2["uic"])

    elements = (source or _OVERPASS_SOURCE).relations_for_uic_pair(
        uic1, uic2, [(s1["lat"], s1["lon"]), (s2["lat"], s2["lon"])])
    return _best_relation_geometry(
        elements[:_MAX_RELATION_CANDIDATES],
        s1["lat"], s1["lon"], s2["lat"], s2["lon"])


def _clean_uic(uic: str) -> str:
    return uic.lstrip("0") or uic


# How far a resolved polyline's ends may sit from the stops it claims to join.
#
# Strategies A and B both pick between relations, and "which relation is this
# leg on" is answered by where the resolved line starts and ends — a relation
# that serves other stations entirely still routes cleanly, it just does not go
# where the traveller went. Strategy B has always scored that; strategy A never
# checked it at all, and `_accept` does not either, because `_rail_length_ok`
# tests a detour *ratio* and a plausible-length line to the wrong station is
# plausible-length.
#
# 5 km, and in kilometres because that is a distance. The predecessor was
# `_MAX_SCORE = 0.05` in squared degrees, commented "≈ both endpoints within
# ~5 km" — but sqrt(0.05) is 0.2236°, which is 24.9 km of latitude. That is the
# guard that passed issue #359's line, whose start sat 13.9 km from Gare de
# l'Est and scored 0.0161 against it.
#
# Not tighter than 5 km: `_enrich_uic` snaps a stop to its OSM station node, so
# a good relation lands within a few hundred metres and the slack is for real
# gaps in OSM's relation membership rather than for noise. At 2 km, Paris
# Montparnasse → Bordeaux — whose relations are genuinely disconnected at the
# station throat, 3.5 km out — loses its route entirely and degrades to a
# straight line, because its bounding box is 15.2 deg² and strategy C refuses
# it (`_RAIL_BBOX_MAX_AREA`). A straight line across France is the worse answer.
_ENDPOINT_TOLERANCE_KM = 5.0

# How many relation candidates to score, per strategy. Both sources return the
# whole candidate list from one query, so this bounds CPU, not requests.
_MAX_RELATION_CANDIDATES = 10


def _endpoints_near(
    poly: list[list[float]], lat1: float, lon1: float, lat2: float, lon2: float
) -> bool:
    """Does *poly* actually run between these two stops?"""
    return (_crow_km(lat1, lon1, poly[0][1], poly[0][0]) <= _ENDPOINT_TOLERANCE_KM
            and _crow_km(lat2, lon2, poly[-1][1], poly[-1][0]) <= _ENDPOINT_TOLERANCE_KM)


def _best_relation_geometry(
    relations: list[dict], lat1: float, lon1: float, lat2: float, lon2: float
) -> Optional[list[list[float]]]:
    """The candidate whose resolved ends sit closest to the stops, or None.

    Shared by strategies A and B so one tolerance governs both: they differ in
    how they *find* candidates, never in how a candidate is judged.
    """
    best: Optional[list[list[float]]] = None
    best_score = math.inf
    for rel in relations:
        geom = _extract_relation_geometry(rel, lat1, lon1, lat2, lon2)
        if not geom or len(geom) < 2:
            continue
        if not _endpoints_near(geom, lat1, lon1, lat2, lon2):
            _log.debug("rail relation %s rejected: ends %.1f km / %.1f km from "
                       "the stops (tolerance %.1f km)", rel.get("id"),
                       _crow_km(lat1, lon1, geom[0][1], geom[0][0]),
                       _crow_km(lat2, lon2, geom[-1][1], geom[-1][0]),
                       _ENDPOINT_TOLERANCE_KM)
            continue
        score = _sq(geom[0], [lon1, lat1]) + _sq(geom[-1], [lon2, lat2])
        if score < best_score:
            best_score, best = score, geom
    return best


def _extract_relation_geometry(
    rel: dict,
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> Optional[list[list[float]]]:
    """Extract a single clean start→end polyline from a route relation.

    Route relations (train, ferry, bus) are not simple ordered polylines. Train
    relations bundle parallel double-track ways, passing loops, sidings and
    station tracks; ferry/bus relations bundle several legs that may not be
    physically connected. Greedily chaining their member ways walks into and out
    of every stub *and teleports across any gap between disconnected ways* —
    producing a self-overlapping or jumping line (observed: Helsinki→Rovaniemi
    rail at 3031 km vs ~970 km of real track; a far-north Norway ferry rendered
    with 80 km + 50 km mid-route straight jumps where chaining stitched
    disconnected coastal ferry ways together).

    Routing start→end through the relation's own member-way node graph (Dijkstra)
    instead yields a shortest on-graph path that visits each node at most once —
    no backtracking, no double-track duplication, and structurally no teleports
    (an edge exists only between vertices actually adjacent within a way).
    Returns None when there is no on-graph path at all, so the caller falls
    through to another relation or strategy rather than emitting a teleporting /
    self-overlapping line (observed on Hanko→Salo: a 116 km teleport mid-line,
    6.2x the real distance).

    Since #363 one exception to "no teleports" exists and it is bounded to
    ``_COMPONENT_BRIDGE_M``: see ``_bridge_to_named_stops``, which joins the
    component holding a stop *this relation names at this leg's endpoint* to
    whatever it comes within that many metres of. Nothing else may bridge, and
    the limit is 250 m against the 116 km this docstring is otherwise about.

    **What comes back is the best path this relation offers, not a promise that
    it runs between these two stops.** Since #359 the endpoints are snapped
    inside one connected component (see ``_snap_endpoints``), so a relation
    whose graph is in pieces returns its best piece instead of nothing — which
    is what recovers a route whose station throat is disconnected upstream, and
    equally what can return a short stub for a relation that holds no through
    line. Judging that is ``_best_relation_geometry``'s job, via
    ``_ENDPOINT_TOLERANCE_KM``, and every caller goes through it. Calling this
    directly and shipping the result skips the only check that a relation is
    the leg the traveller took.
    """
    ways = [
        m for m in rel.get("members", [])
        if m.get("type") == "way" and len(m.get("geometry", [])) >= 2
        and _is_route_path(m.get("role", ""))
    ]
    if ways:
        nodes, adj = _build_rail_graph(ways)
        if nodes:
            _bridge_to_named_stops(rel, nodes, adj, lat1, lon1, lat2, lon2)
            snapped = _snap_endpoints(nodes, adj, lat1, lon1, lat2, lon2)
            if snapped:
                path = _dijkstra(nodes, adj, *snapped)
                if path and len(path) >= 2:
                    return [nodes[n] for n in path]
    # No member way this relation contributes reaches any other — reject (None)
    # rather than chain into garbage.
    return None


# Member roles that are *not* the route's path. A deny-list, and it has to be:
# an allow-list of the empty role reads as "the path is the members nobody
# labelled", which is false — 2,299 of France's 681,815 way members carry
# `forward`, `backward` or `alternative` and every one of them is track the
# route runs on. Only these are things the route touches rather than follows.
_NON_PATH_ROLES = ("platform", "hail_and_ride")


def _is_route_path(role: str) -> bool:
    """Is this member way part of the route's path, per its OSM role?

    A `platform` member is a station's platform — mapped as a way or an area,
    not connected to track, and lying exactly where a leg begins or ends. Left
    in the graph it captures the endpoint snap and strands Dijkstra on a closed
    ring, so the relation is rejected as disconnected and a *worse* relation
    wins instead: issue #359, where Paris Est → Strasbourg discarded every
    correct relation this way and shipped the Strasbourg–CDG-airport TGV, whose
    Paris end is 14 km south of the station. One route relation in five carries
    a platform way member (684 of France's 3,365), so this is not a corner.

    `platform_entry_only` and `platform_exit_only` are prefixed, not separate
    values, which is why this matches on the prefix.
    """
    return not (role or "").startswith(_NON_PATH_ROLES)


def _snap_endpoints(
    nodes: dict[str, list[float]],
    adj: dict[str, list[str]],
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> Optional[tuple[str, str]]:
    """The two graph nodes to route between, chosen so a path can exist.

    ``_nearest_node`` twice is right whenever the graph is connected, and wrong
    in a specific way when it is not: it can put the start in one component and
    the end in another, and Dijkstra then reports "no path" for a relation that
    does contain the route. Relations *are* disconnected in practice — a member
    way missing from OSM's membership, a station throat mapped as a separate
    line — so the endpoints are picked per component instead, keeping the
    component whose own two nearest nodes are closest to the stops.

    On a connected graph this is exactly ``_nearest_node`` twice, because there
    is one component and its nearest nodes are the graph's. It costs one
    traversal of a graph Dijkstra is about to traverse anyway.

    This is deliberately *not* the fix for #359 — ``_is_route_path`` is. It
    reaches the same answer there by routing around the platform rather than by
    keeping it out, and reaching the right answer for the wrong reason is how a
    fix survives the bug that outlives it. It is here for the relations that
    are genuinely broken upstream, and it is why the caller's result must still
    pass the endpoint tolerance in ``_endpoints_near``: a component that reaches
    neither stop is a legitimate winner of this comparison.
    """
    best: Optional[tuple[str, str]] = None
    best_cost = math.inf
    for component in _components(nodes, adj):
        if len(component) < 2:
            continue
        start = min(component, key=lambda n: _sq(nodes[n], [lon1, lat1]))
        end = min(component, key=lambda n: _sq(nodes[n], [lon2, lat2]))
        cost = _sq(nodes[start], [lon1, lat1]) + _sq(nodes[end], [lon2, lat2])
        if cost < best_cost:
            best_cost, best = cost, (start, end)
    return best


# How close one of the relation's own node members must be to a leg endpoint
# before it is read as "this relation calls here". A route relation lists its
# stops as node members, and `_enrich_uic` has already snapped the leg's
# endpoint onto the OSM station node those stops sit on, so a match is metres:
# Paris Montparnasse is 69 m from the raw trip coordinate and 0 m from the
# enriched one. 1 km is slack for strategy B, which never enriches, and for a
# relation referencing a stop node on the platform rather than at the station
# centre. It is well inside `_ENDPOINT_TOLERANCE_KM` deliberately — an anchor
# must never be the reason a relation clears the check that says it is this leg.
_STOP_ANCHOR_KM = 1.0

# The longest gap one bridge may span, in metres.
#
# This is the *only* place `_extract_relation_geometry` may join two things that
# OSM does not join, and the number is what keeps it from being the Hanko→Salo
# teleport again (116 km mid-line, 6.2x the real distance) — three orders of
# magnitude, not a margin.
#
# Measured over the first 400 France route relations with more than 20 member
# ways: 162 have a graph in pieces, and their 868 non-main components sit this
# far from the rest of their own relation's graph:
#
#     ≤10 m  505    ≤100 m   43    ≤250 m    6    >1 km  50
#     ≤25 m  140    ≤150 m   20    ≤500 m   16
#     ≤50 m   44    ≤200 m   18    ≤1 km    22
#
# There is no cliff in that tail to read a limit off, so the limit is a
# judgement and these are its two sides. Paris Montparnasse → Bordeaux, the leg
# in issue #363, needs 100 m: the throat island reaches within 13 m of the
# station and stops 100 m short of the main line at the Petite Ceinture, in all
# three of its candidate relations. 250 m is the 95th percentile of the gaps
# above, so it covers that with headroom while leaving the kilometre tail — a
# straight line drawn a kilometre across a city, which is a visible lie — out.
#
# What makes 250 m safe is not the number alone but that only a component
# holding a stop the relation *names at this leg's endpoint* may be bridged at
# all. A blanket 250 m join would let a relation that does not serve this leg
# stitch itself into reach of `_ENDPOINT_TOLERANCE_KM`; anchoring on the stop
# sequence means a bridge is only ever built towards a station the relation
# itself says it calls at, which is not a guess about geometry.
_COMPONENT_BRIDGE_M = 250.0


def _relation_stop_near(
    rel: dict, lat: float, lon: float
) -> Optional[tuple[float, float]]:
    """Where this relation says it stops nearest (*lat*, *lon*), or None.

    Reads the relation's node members, which is where both sources put the stop
    sequence: Overpass's ``out geom`` returns ``{"type": "node", "ref", "role",
    "lat", "lon"}`` per node member (verified against overpass-api.de — all 16
    of relation 5928800's come back located), and since #363 ``RailStore.
    relation_geometry`` emits the same shape from ``relation_node``. So this
    needs no branch on where the relation came from, and a store that cannot
    place a node simply omits its coordinates, which reads here as "no stop".

    Every role counts, ``platform`` included. The deny-list in ``_is_route_path``
    is about which *ways* the train runs over; a node member is a point the
    relation calls at whatever it is labelled, and it is used here only to say
    where — never as a graph vertex.
    """
    best: Optional[tuple[float, float]] = None
    best_km = _STOP_ANCHOR_KM
    for member in rel.get("members", []):
        if member.get("type") != "node" or member.get("lat") is None:
            continue
        km = _crow_km(lat, lon, member["lat"], member["lon"])
        if km <= best_km:
            best_km, best = km, (member["lat"], member["lon"])
    return best


def _components(
    nodes: dict[str, list[float]], adj: dict[str, list[str]]
) -> list[list[str]]:
    """Connected components of the node graph. Iterative: a relation's graph
    runs to tens of thousands of nodes in a chain, which recursion cannot walk."""
    seen: set[str] = set()
    out: list[list[str]] = []
    for node in nodes:
        if node in seen:
            continue
        stack = [node]
        seen.add(node)
        component = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in (adj.get(current) or []):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        out.append(component)
    return out


def _bridge_to_named_stops(
    rel: dict,
    nodes: dict[str, list[float]],
    adj: dict[str, list[str]],
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> int:
    """Join the component holding a stop this relation names to what it touches.

    Adds edges to *adj* in place and returns how many. Each one spans at most
    ``_COMPONENT_BRIDGE_M``.

    The case it exists for is issue #363. Paris Montparnasse → Bordeaux has
    three candidate relations, all with ``missing_members = 0`` and no platform
    member, so neither of #359's fixes applies — and all three are in pieces
    anyway, because OSM's membership does not join the Montparnasse station
    throat to the main line. ``_snap_endpoints`` then correctly keeps the piece
    that reaches Bordeaux, and the leg is drawn starting 3.5 km late, from the
    Petite Ceinture. Overpass returns the same gap; there is no track to find.

    What there *is* is the relation saying, in its own node members, that it
    calls at Paris Montparnasse — and a 322-node component that reaches within
    13 m of it and stops 100 m short of the main line. So: find the stop this
    relation names at each end of the leg, find the component holding it, and
    join that component to every other one it comes within
    ``_COMPONENT_BRIDGE_M`` of. Dijkstra then routes over a graph where the
    throat is reachable, and the drawn line runs on real track apart from one
    100 m segment across the gap OSM left.

    **Every restriction here is load-bearing.** Only a component reaching a
    *named* stop may be bridged, so this can never become the general
    gap-chaining that produced the Hanko→Salo teleport — a relation that does
    not call at this leg's endpoints gets no bridge at all and is still refused
    by ``_ENDPOINT_TOLERANCE_KM``. Only one edge per other component is added,
    at that component's closest approach, so a bridge cannot accumulate into a
    path. And the metre limit is checked exactly, on the same equirectangular
    approximation ``_dijkstra`` weights its edges with, so what is drawn is what
    was measured.

    Doing nothing is the common case and costs one graph traversal: 238 of the
    400 France relations measured have a single component and return here.
    """
    comps = _components(nodes, adj)
    if len(comps) < 2:
        return 0
    owner = {node: i for i, comp in enumerate(comps) for node in comp}

    # Both ends of a leg usually anchor in the same component — the leg is on
    # the relation's main line and only one end's throat is broken — and the
    # two would then look for the same gaps and add the same edges twice.
    added: set[tuple[str, str]] = set()
    homes: set[int] = set()
    bridged = 0
    for lat, lon in ((lat1, lon1), (lat2, lon2)):
        stop = _relation_stop_near(rel, lat, lon)
        if stop is None:
            continue
        home = owner[_nearest_node(nodes, *stop)]
        if home in homes:
            continue
        homes.add(home)
        for a, b in _closest_approaches(nodes, comps, home):
            edge = (a, b) if a < b else (b, a)
            if edge in added:
                continue
            added.add(edge)
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
            bridged += 1
    if bridged:
        _log.debug("rail relation %s: bridged %d component gap(s) under %.0f m "
                   "to reach a stop it names", rel.get("id"), bridged,
                   _COMPONENT_BRIDGE_M)
    return bridged


def _closest_approaches(
    nodes: dict[str, list[float]], comps: list[list[str]], home: int
) -> list[tuple[str, str]]:
    """One (home node, other node) pair per component within the bridge limit.

    Brute force is O(|home| × |rest|) — 322 × 20,833 on the reported leg, and
    that is the *small* component of one candidate relation out of ten. So the
    other components' nodes go into a grid of roughly ``_COMPONENT_BRIDGE_M``
    cells and each home node looks only at its own cell and the eight around it.

    The grid is metric, projected once at the home component's mean latitude,
    because a degree of longitude is not a degree of latitude and cells keyed in
    raw degrees are too narrow east-west to guarantee a neighbour is adjacent.
    One reference latitude distorts distant components by a few percent, which
    the 3×3 window's full cell of slack absorbs; where it would not, the miss is
    a bridge not built, and a leg 3.5 km short is the failure this already had.
    Candidates the grid offers are then measured exactly, so the grid only ever
    prunes.
    """
    cell = _COMPONENT_BRIDGE_M / 111_000.0
    home_nodes = comps[home]
    ref_lat = sum(nodes[n][1] for n in home_nodes) / len(home_nodes)
    x_scale = max(math.cos(math.radians(ref_lat)), 0.01)

    def _key(node: str) -> tuple[int, int]:
        lon, lat = nodes[node]
        return int(lat / cell), int(lon * x_scale / cell)

    grid: dict[tuple[int, int], list[str]] = {}
    for node in home_nodes:
        grid.setdefault(_key(node), []).append(node)

    best: dict[int, tuple[float, str, str]] = {}
    for index, comp in enumerate(comps):
        if index == home:
            continue
        for other in comp:
            olat, olon = nodes[other][1], nodes[other][0]
            ci, cj = _key(other)
            for i in (ci - 1, ci, ci + 1):
                for j in (cj - 1, cj, cj + 1):
                    for node in grid.get((i, j), ()):
                        metres = _crow_km(
                            nodes[node][1], nodes[node][0], olat, olon) * 1000
                        if metres > _COMPONENT_BRIDGE_M:
                            continue
                        if index not in best or metres < best[index][0]:
                            best[index] = (metres, node, other)
    return [(node, other) for _, node, other in best.values()]


# ---------------------------------------------------------------------------
# Strategy 3b — two-endpoint route-relation intersection
# ---------------------------------------------------------------------------

def _via_train_relations_endpoints(
    stops: list[dict], source: Optional[RailSource] = None
) -> list[list[float]]:
    """
    Find OSM route=train/railway relations that pass through *both* endpoint
    areas (25 km radius each), then fetch and score their geometry.

    Works without UIC codes.  Avoids the large-bbox timeout that would occur
    if we queried a single bounding box for a long route (e.g. Helsinki–Oulu).
    Falls through to Strategy C if no common relation is found or the best
    match's endpoints are too far from the query points — the same
    ``_ENDPOINT_TOLERANCE_KM`` strategy A now applies, so a relation rejected
    here would have been rejected there.
    """
    lat1, lon1 = stops[0]["lat"], stops[0]["lon"]
    lat2, lon2 = stops[-1]["lat"], stops[-1]["lon"]
    src = source or _OVERPASS_SOURCE

    _RADIUS = 25_000  # metres

    # Step 1: IDs of train relations near the start point.
    start_ids = src.relations_near(lat1, lon1, _RADIUS)
    if not start_ids:
        raise OverpassError("No train route relations near start point")

    # Step 2: IDs near the end point.
    end_ids = src.relations_near(lat2, lon2, _RADIUS)
    if not end_ids:
        raise OverpassError("No train route relations near end point")

    # Step 3: Intersection — relations present near BOTH endpoints.
    common = start_ids & end_ids
    if not common:
        raise OverpassError("No train route relation serves both endpoints")

    # Fetch geometry for a bounded number of candidates (lowest IDs first to be
    # deterministic; we score them all and pick the best).
    relations = src.relation_geometry(
        sorted(common)[:_MAX_RELATION_CANDIDATES], [(lat1, lon1), (lat2, lon2)])
    if not relations:
        raise OverpassError("Could not fetch geometry for candidate relations")

    best = _best_relation_geometry(relations, lat1, lon1, lat2, lon2)
    if best is None:
        raise OverpassError(
            f"Train route relations found but none runs between the two stops "
            f"(within {_ENDPOINT_TOLERANCE_KM:.0f} km of each)"
        )
    return best


# ---------------------------------------------------------------------------
# Strategy 3c — coordinate fallback (bounding-box Dijkstra)
# ---------------------------------------------------------------------------

# Maximum *area* (square degrees) of the buffered bounding box for the single
# coordinate-fallback query. Beyond this Overpass cannot answer inside the
# 30 s QL / 45 s HTTP budget, so we straight-line instead of paying for a
# request that can only fail.
#
# This was 12.0 degrees of max *span*, chosen as a "pathological input" guard
# rather than from anything Overpass can serve, and it let through queries that
# could only time out: the Hamburg→Offenburg report (issue #277) sent a box that
# ran 43.4 s, returned 61 MB and still hit Overpass's own query timeout — ~135 s
# across three mirrors to land on the straight line it would have produced
# instantly.
#
# Span is the wrong axis. Cost tracks the area actually queried and the density
# of rail inside it, and a max-span cap tight enough for Germany throws away
# routes that sparse networks serve easily. Measured on overpass-api.de with
# this exact query (railway=rail|narrow_gauge|light_rail, service unset,
# out geom), each on its real buffered box:
#
#     Hamburg→Hannover      1.28 deg^2    8.4 s    6.2 MB   ok
#     Helsinki→Oulu         5.57 deg^2   10.4 s    3.8 MB   ok
#     Helsinki→Rovaniemi    8.80 deg^2    6.1 s    4.2 MB   ok
#     central Germany 3.0°   9.00 deg^2   32.3 s   37.0 MB   ok  <- dense ceiling
#     Hamburg→Offenburg    14.28 deg^2   43.4 s   61.0 MB   QUERY TIMED OUT
#     central Germany 4.0°  20.25 deg^2   51.6 s   71.0 MB   past the HTTP timeout
#
# 9.0 is therefore the densest network's measured ceiling. Sparse networks clear
# it with 5x margin, which is why Helsinki→Rovaniemi — the very route
# _via_coordinate_fallback exists for — still reaches the query, while
# Hamburg→Offenburg is rejected in ~0 s instead of after minutes. A degenerate
# long-thin box stays bounded because the 0.25° buffer on each side puts a floor
# of 0.5° under both dimensions.
#
# Issue #345 note: this cap outlives Overpass. The local store (src/rail/) reads
# a bbox's worth of ways into memory, ~268 bytes per vertex and again as much
# once _build_rail_graph runs, so an unbounded box is an OOM on a 1 GB worker
# rather than a timeout. RailStore.ways_in_bbox refuses one from its side; this
# is the same bound from the caller's. Do not retire it with the rest of the
# Overpass scaffolding in Phase 6.
_RAIL_BBOX_BUFFER = 0.25
_RAIL_BBOX_MAX_AREA = 9.0


def _via_coordinate_fallback(
    stops: list[dict], source: Optional[RailSource] = None
) -> list[list[float]]:
    """Single whole-route Overpass query, then sequential Dijkstra between
    consecutive stops on the shared railway graph.

    This previously issued one Overpass HTTP request *per consecutive stop
    pair*. A long route reaching this last-resort fallback (e.g. VR
    Helsinki–Rovaniemi returns ~20 stops, and Finnish ``route=train`` relations
    are too sparse for strategies A/B) therefore fired ~20 sequential requests,
    each with a 45 s timeout, and the resolve job appeared to hang for several
    minutes. One query bounds the network cost to a single round-trip; the
    per-pair Dijkstra stays cheap because consecutive stations are close, so
    each search only explores a small local frontier of the shared graph.
    """
    lat1, lon1 = stops[0]["lat"], stops[0]["lon"]
    lat2, lon2 = stops[-1]["lat"], stops[-1]["lon"]

    lats = [s["lat"] for s in stops]
    lons = [s["lon"] for s in stops]
    buf = _RAIL_BBOX_BUFFER
    bbox = (min(lats) - buf, min(lons) - buf, max(lats) + buf, max(lons) + buf)
    if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > _RAIL_BBOX_MAX_AREA:
        return _straight(lat1, lon1, lat2, lon2)
    try:
        elements = (source or _OVERPASS_SOURCE).ways_in_bbox(*bbox)
    except OverpassError:
        return _straight(lat1, lon1, lat2, lon2)

    nodes, adj = _build_rail_graph(elements)
    if not nodes:
        return _straight(lat1, lon1, lat2, lon2)

    # Route each consecutive stop pair on the shared graph. Anchoring on the
    # intermediate stops keeps the polyline on the correct line where several
    # railways share the corridor.
    full: list[list[float]] = []
    for i in range(len(stops) - 1):
        a = _nearest_node(nodes, stops[i]["lat"], stops[i]["lon"])
        b = _nearest_node(nodes, stops[i + 1]["lat"], stops[i + 1]["lon"])
        path = _dijkstra(nodes, adj, a, b)
        if path:
            seg = [nodes[n] for n in path]
        else:
            seg = [
                [stops[i]["lon"], stops[i]["lat"]],
                [stops[i + 1]["lon"], stops[i + 1]["lat"]],
            ]
        full = full + (seg[1:] if full else seg)

    return full if len(full) >= 2 else _straight(lat1, lon1, lat2, lon2)


def _build_rail_graph(
    ways: list[dict],
) -> tuple[dict[str, list[float]], dict[str, list[str]]]:
    """Build an undirected node graph from Overpass way geometry.

    Node IDs are rounded "lat,lon" strings so ways sharing a vertex connect.
    Returns (nodes: id→[lon, lat], adjacency: id→[neighbour ids]).
    """
    nodes: dict[str, list[float]] = {}
    adj:   dict[str, list[str]]   = {}
    for way in ways:
        prev: Optional[str] = None
        for pt in way.get("geometry", []):
            nid = f"{pt['lat']:.6f},{pt['lon']:.6f}"
            nodes[nid] = [pt["lon"], pt["lat"]]
            if prev is not None:
                adj.setdefault(prev, []).append(nid)
                adj.setdefault(nid,  []).append(prev)
            prev = nid
    return nodes, adj


def _nearest_node(nodes: dict[str, list[float]], lat: float, lon: float) -> str:
    return min(nodes, key=lambda n: (nodes[n][1] - lat) ** 2 + (nodes[n][0] - lon) ** 2)


def _dijkstra(
    nodes: dict[str, list[float]],
    adj: dict[str, list[str]],
    start: str,
    end: str,
) -> Optional[list[str]]:
    dist: dict[str, float] = {start: 0.0}
    prev: dict[str, Optional[str]] = {start: None}
    heap = [(0.0, start)]
    visited: set[str] = set()

    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        if u == end:
            path: list[str] = []
            cur: Optional[str] = end
            while cur is not None:
                path.append(cur)
                cur = prev.get(cur)
            return list(reversed(path))
        cu = nodes[u]
        for v in (adj.get(u) or []):
            cv = nodes[v]
            dlat = (cu[1] - cv[1]) * 111.0
            dlon = (cu[0] - cv[0]) * 111.0 * math.cos(math.radians((cu[1] + cv[1]) / 2))
            nd = d + math.sqrt(dlat * dlat + dlon * dlon)
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))

    return None


# ---------------------------------------------------------------------------
# Ferry / bus geometry  (shared Overpass route-relation strategy)
# ---------------------------------------------------------------------------

def get_ferry_geometry(lat1: float, lon1: float, lat2: float, lon2: float) -> list[list[float]]:
    """Return [[lon, lat], …] polyline following OSM ferry route geometry."""
    return _get_route_geometry("ferry", lat1, lon1, lat2, lon2)


def get_bus_geometry(lat1: float, lon1: float, lat2: float, lon2: float) -> list[list[float]]:
    """Return [[lon, lat], …] polyline following OSM bus route geometry."""
    return _get_route_geometry("bus", lat1, lon1, lat2, lon2)


def _get_route_geometry(
    route_tag: str,
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> list[list[float]]:
    """
    Three strategies tried in order:
      A  Route-relation strategy: query OSM route relations for *route_tag*
         (e.g. "ferry", "bus"), pick the best-fitting one, return trimmed geometry.
      B  Way route=* fallback: Dijkstra on ways tagged route=*route_tag*.
      C  ferry=yes way fallback (ferry only): Dijkstra on ways tagged ferry=yes.
         Many short island-hopper crossings use this tag instead of route=ferry.
    """
    t0 = time.monotonic()
    strategy = poly = None
    try:
        poly, strategy = _via_route_relation_type(route_tag, lat1, lon1, lat2, lon2), "relation"
    except OverpassError as exc:
        _log.info("%s strategy A (route relation) failed: %s", route_tag, exc)
    if poly is None:
        try:
            poly, strategy = _via_way_type_fallback(route_tag, lat1, lon1, lat2, lon2), "way_dijkstra"
        except OverpassError as exc:
            _log.info("%s strategy B (way dijkstra) failed: %s", route_tag, exc)
    if poly is None and route_tag == "ferry":
        try:
            poly, strategy = _via_ferry_yes_fallback(lat1, lon1, lat2, lon2), "ferry_yes_dijkstra"
        except OverpassError as exc:
            _log.info("ferry strategy C (ferry=yes dijkstra) failed: %s", exc)
    if poly is None:
        _log.warning("%s geometry unresolved: no route found, elapsed=%.1fs",
                     route_tag, time.monotonic() - t0)
        raise OverpassError(f"No {route_tag} route found between the two endpoints")
    _log.info("%s geometry resolved: strategy=%s points=%d elapsed=%.1fs",
              route_tag, strategy, len(poly), time.monotonic() - t0)
    return poly


def _via_route_relation_type(
    route_tag: str,
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> list[list[float]]:
    # Clamp buffer: enough headroom to capture terminal areas, but not so large
    # that mega-routes (Stockholm–Turku) flood the result and cause timeouts.
    raw_buf = max(abs(lat1 - lat2), abs(lon1 - lon2)) * 0.5 + 0.15
    buf = min(raw_buf, 0.4)
    bbox = (
        min(lat1, lat2) - buf, min(lon1, lon2) - buf,
        max(lat1, lat2) + buf, max(lon1, lon2) + buf,
    )
    query = f"""
[out:json][timeout:{_TIMEOUT_QUERY}];
rel["route"="{route_tag}"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
._;
out geom;
"""
    data = _overpass(query)
    relations = data.get("elements", [])

    # Maximum acceptable endpoint-proximity score (~0.002 ≈ both terminals
    # within ~1-2 km of the query points).  Routes whose trimmed endpoints
    # are further away are not actually connecting the requested terminals
    # and should be rejected so strategy B/C can try ferry=yes ways instead.
    # The Degerby–Svinö (Åland) ferry has no route=ferry OSM relation; without
    # this threshold strategy A picks a wrong nearby relation (score ≈ 0.0047).
    _MAX_SCORE = 0.002

    best: Optional[list[list[float]]] = None
    best_score = math.inf
    for rel in relations:
        geom = _extract_relation_geometry(rel, lat1, lon1, lat2, lon2)
        if geom and len(geom) >= 2:
            # Score by endpoint proximity: graph routing anchors geom[0] at the
            # relation node nearest (lon1,lat1) and geom[-1] at the node nearest
            # (lon2,lat2), so a low score means the route actually connects the
            # requested ports. Scoring by total path length (the previous
            # approach) caused long open-sea crossings to lose to short coastal
            # hops that happened to fall inside the same bounding box.
            score = _sq(geom[0], [lon1, lat1]) + _sq(geom[-1], [lon2, lat2])
            if score < best_score:
                best_score = score
                best = geom

    if best is None or best_score > _MAX_SCORE:
        raise OverpassError(f"No {route_tag} route relation found in bounding box")
    return best


def _via_way_type_fallback(
    route_tag: str,
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> list[list[float]]:
    buf = 0.25
    bbox = (
        min(lat1, lat2) - buf, min(lon1, lon2) - buf,
        max(lat1, lat2) + buf, max(lon1, lon2) + buf,
    )
    query = (
        f"[out:json][timeout:{_TIMEOUT_QUERY}];"
        f'way["route"="{route_tag}"]'
        f"({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});"
        "out geom;"
    )
    try:
        data = _overpass(query)
    except OverpassError:
        raise

    ways = data.get("elements", [])
    if not ways:
        raise OverpassError(f"No {route_tag} ways found in bounding box")

    nodes, adj = _build_rail_graph(ways)
    if not nodes:
        raise OverpassError(f"No {route_tag} nodes found in bounding box")

    start_node = _nearest_node(nodes, lat1, lon1)
    end_node   = _nearest_node(nodes, lat2, lon2)
    path = _dijkstra(nodes, adj, start_node, end_node)

    if path:
        return [[lon1, lat1]] + [nodes[n] for n in path] + [[lon2, lat2]]
    raise OverpassError(f"No {route_tag} path found between endpoints")


def _via_ferry_yes_fallback(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> list[list[float]]:
    """Strategy C: Dijkstra on OSM ways tagged ferry=yes.

    Many short island-hopper crossings (e.g. Finnish/Åland archipelago ferries)
    tag the navigable route as ferry=yes on a way rather than using a
    route=ferry relation or way.  This is the last resort before giving up.
    """
    buf = min(max(abs(lat1 - lat2), abs(lon1 - lon2)) * 0.5 + 0.15, 0.4)
    bbox = (
        min(lat1, lat2) - buf, min(lon1, lon2) - buf,
        max(lat1, lat2) + buf, max(lon1, lon2) + buf,
    )
    query = (
        f"[out:json][timeout:{_TIMEOUT_QUERY}];"
        f'way["ferry"="yes"]'
        f"({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});"
        "out geom;"
    )
    try:
        data = _overpass(query)
    except OverpassError:
        raise

    ways = data.get("elements", [])
    if not ways:
        raise OverpassError("No ferry=yes ways found in bounding box")

    nodes, adj = _build_rail_graph(ways)
    if not nodes:
        raise OverpassError("No ferry=yes nodes found in bounding box")

    start_node = _nearest_node(nodes, lat1, lon1)
    end_node   = _nearest_node(nodes, lat2, lon2)
    path = _dijkstra(nodes, adj, start_node, end_node)

    if path:
        return [[lon1, lat1]] + [nodes[n] for n in path] + [[lon2, lat2]]
    raise OverpassError("No ferry path found between endpoints via ferry=yes ways")


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------



def _straight(lat1: float, lon1: float, lat2: float, lon2: float) -> list[list[float]]:
    return [[lon1, lat1], [lon2, lat2]]


def _crow_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in km (equirectangular; same approx as _dijkstra)."""
    dlat = (lat1 - lat2) * 111.0
    dlon = (lon1 - lon2) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def _polyline_km(poly: list[list[float]]) -> float:
    """Total length in km of a [[lon, lat], …] polyline."""
    return sum(_crow_km(a[1], a[0], b[1], b[0]) for a, b in zip(poly, poly[1:]))


def _rail_length_ok(poly: list[list[float]], stops: list[dict]) -> bool:
    """True if *poly* is a plausible rail path for *stops* — i.e. not a
    self-overlapping / wrong-relation result far longer than the straight-line
    distance through the stops. See _MAX_RAIL_DETOUR_RATIO."""
    baseline = sum(
        _crow_km(stops[i]["lat"], stops[i]["lon"], stops[i + 1]["lat"], stops[i + 1]["lon"])
        for i in range(len(stops) - 1)
    )
    return _polyline_km(poly) <= max(_MIN_PLAUSIBLE_KM, baseline * _MAX_RAIL_DETOUR_RATIO)


def _sq(a: list[float], b: list[float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


# overpass-api.de requires a User-Agent that "uniquely identifies your app", and
# its operators ban stock, faked or rotating ones. The consequence of getting this
# wrong is worse than an IP ban: those expire on their own, while bans applied by
# user agent are manual and stay until someone asks for them to be lifted. The URL
# here previously pointed at a repository that does not exist, which is exactly the
# kind of unverifiable client that earns the manual kind.
#
# The version is read at runtime rather than hardcoded: "identifies application and
# version" means the real one, and a frozen string is a mild form of UA faking.
# src.brand.USER_AGENT carries both.
_HEADERS = {"User-Agent": USER_AGENT}


def _slot_name(url: str) -> str:
    """Rate-limit key for *url* — per host, since the quota is."""
    return "overpass:" + urlsplit(url).netloc


def _overpass(query: str) -> dict:
    """POST a query to Overpass, backing off a host rather than arguing with it.

    A cached answer short-circuits everything below — including the concurrency
    gate, since queueing to answer from memory would be absurd.

    Otherwise one attempt per endpoint, in order, skipping any host currently
    cooling. Every failure marks the host and moves on; nothing is retried in
    band. That is deliberate and it is policy, not taste: overpass-api.de asks
    for a 30 second pause after a 429, and its operators ban clients that
    repeatedly trigger one — the *rate* of 429s being a trigger by itself. An
    in-request retry loop maximises exactly that rate.

    An earlier version of this function waited ~5s and retried the same host up
    to three times, on the reasoning that a busy host is cheaper to wait out than
    a dead mirror is to discover. The first half was right and the second half
    was the documented ban trigger; this deployment's IPv4 address was blocked
    within minutes of shipping it. Cooling the host achieves what waiting was
    for — not stampeding to a worse endpoint — without the retries.

    Every attempt is recorded and surfaced on the final error, so a resolve that
    degrades can say which hosts refused it and why.
    """
    cached = cache_get(_CACHE_NAMESPACE, query)
    if cached is not None:
        _log.info("overpass cache hit (%d bytes)", len(cached))
        return json.loads(cached)

    attempts: list[str] = []
    for url in _OVERPASS_ENDPOINTS:
        host = _slot_name(url)
        if is_cooling(host):
            attempts.append(f"{url}: cooling down")
            _log.info("overpass %s: still cooling down, skipped", url)
            continue

        with slot(
            host, _OVERPASS_CONCURRENCY,
            timeout_s=_SLOT_ACQUIRE_TIMEOUT_S,
            lease_ttl_s=_SLOT_LEASE_TTL_S,
        ) as got_slot:
            if not got_slot:
                # Our own traffic is saturating this host. Another endpoint has
                # its own quota; queueing behind ourselves does not.
                attempts.append(
                    f"{url}: no free slot within {_SLOT_ACQUIRE_TIMEOUT_S:.0f}s")
                _log.info("overpass %s: no free slot, moving on", url)
                continue
            started = time.monotonic()
            try:
                resp = requests.post(
                    url, data={"data": query}, headers=_HEADERS,
                    timeout=_TIMEOUT_HTTP,
                )
            except Exception as exc:  # noqa: BLE001 — cannot even reach it
                elapsed = time.monotonic() - started
                mark_cooling(host, _COOLDOWN_UNREACHABLE_S)
                attempts.append(f"{url}: {type(exc).__name__} after {elapsed:.1f}s")
                _log.warning(
                    "overpass %s unreachable after %.1fs (%s) — backing off for %ds",
                    url, elapsed, exc, _COOLDOWN_UNREACHABLE_S)
                continue
            elapsed = time.monotonic() - started

        if resp.status_code in _BACK_OFF_STATUSES:
            mark_cooling(host, _COOLDOWN_RATE_LIMITED_S)
            attempts.append(f"{url}: HTTP {resp.status_code} after {elapsed:.1f}s")
            _log.warning(
                "overpass %s returned %d after %.1fs — backing off for %ds",
                url, resp.status_code, elapsed, _COOLDOWN_RATE_LIMITED_S)
            continue
        if not resp.ok:
            # A 4xx that is not a rate limit is about this query, not this host,
            # so another endpoint will reject it identically — but do not cool a
            # host over our own malformed request either.
            attempts.append(f"{url}: HTTP {resp.status_code} after {elapsed:.1f}s")
            _log.info("overpass %s returned %d after %.1fs",
                      url, resp.status_code, elapsed)
            continue
        try:
            data = resp.json()
        except ValueError:
            body = (resp.text or "")[:120].replace(chr(10), " ")
            mark_cooling(host, _COOLDOWN_RATE_LIMITED_S)
            attempts.append(f"{url}: unparseable body after {elapsed:.1f}s")
            _log.warning("overpass %s returned an unparseable body after %.1fs: %r",
                         url, elapsed, body)
            continue
        _log.info("overpass %s ok in %.1fs (%d bytes)",
                  url, elapsed, len(resp.content))
        # Cache the body rather than the parsed dict: it is what we already
        # hold, and re-serialising a multi-megabyte structure just to store
        # it would cost more than the parse it saves.
        cache_put(_CACHE_NAMESPACE, query, resp.content)
        return data

    raise OverpassError("Overpass query failed — " + "; ".join(attempts))

"""Reader for a per-region rail store (issue #345, Phase 2).

One SQLite file per region, holding exactly what the three Overpass lookups in
``src/services/overpass_service.py`` return today:

  * the nearest station carrying a ``uic_ref``      (``_find_station_near``)
  * route relations covering two points             (strategies A and B)
  * railway ways inside a bounding box              (strategy C)

Element shapes match Overpass's ``out geom`` output for those queries, so
``_build_rail_graph`` and ``_extract_relation_geometry`` consume the store's
results unchanged. Phase 3 does that wiring; this module only serves lookups.

Why SQLite with R-tree indices rather than a loaded graph: the resolver never
needs a whole country at once — strategy C already works on a bounding box, and
a bbox query returns only the ways inside it. So the 319 MB Germany graph the
spike measured never has to exist, and holding a region open costs a
connection's page cache (``_CACHE_KIB``) rather than the size of the region.

What that does *not* do is make a resolve free. A bbox result costs roughly
268 bytes per vertex and about as much again once ``_build_rail_graph`` runs
over it, so the memory is now proportional to the caller's box — which is why
``ways_in_bbox`` enforces ``_MAX_BBOX_VERTICES`` itself instead of trusting
every caller to pick a sane one. The file is also plain SQLite: when a route
looks wrong, the data behind it is one ``sqlite3`` session away.

Coordinates are stored as 1e-7 degree fixed-point int32 pairs — OSM's own
precision, so the round trip is lossless — packed into one blob per way.
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import threading
from array import array
from collections import OrderedDict
from typing import Iterable, Optional

# Bump when the schema changes shape. What the *builder* writes.
SCHEMA_VERSION = 3

# What the reader accepts. An explicit set, never a `>=` comparison: the point
# of refusing a version is that a file we only half understand returns wrong
# geometry rather than an error, and "anything at least this old" understands
# nothing about what changed. A version listed here has a branch below that
# says what it lacks.
#
# It is a *set* rather than the single current version because a schema bump is
# otherwise an outage. `_stores_for` treats a refused file as "region not
# covered", so a reader that accepts only its own version refuses every store on
# the box until the data is rebuilt — and a reader shipped after the data would
# refuse it just as flatly. Either order leaves every train resolve going to
# Overpass, on the address Overpass has already blocked once, which is the
# traffic issue #345 exists to stop. Accepting both lets the code ship first and
# the data land whenever it lands.
#
#   1  no `relation_way.role` (members read back with role ""), no
#      `relation_node`, so `relation_stops` is empty. Written before #359.
#   2  `way.rail`, a 0/1 track flag, where 3 has `way.cls`. Read as `cls` bit 0
#      and nothing else, which is exactly what the flag meant: a v1/v2 file is
#      a rail store, it answers a bit-0 query as it always did, and any other
#      bit with nothing.
#   3  current: `way.cls`, the class bitmask below, so one store format serves
#      the rail, ferry and bus layers (docs/LOCAL_TRANSPORT_DATA_PLAN.md,
#      Decision 9).
_SUPPORTED_SCHEMAS = (1, 2, 3)

# The layers a region can publish a store for, one file each — never one graph:
# bus ways are roads, and a joint graph would let a rail Dijkstra walk down a
# high street.
LAYERS = ("rail", "ferry", "bus")
DEFAULT_LAYER = "rail"

# `way.cls` bits. Each says which of the resolver's selections a way is in, so
# a store answers each strategy's own question, and a way in two selections is
# in both answers — what the two Overpass queries return.
#
#   bit 0  the layer's routable class: `railway` without `service` for rail
#          (what schema 2 called `rail = 1`), `route=ferry` / `route=bus` on
#          the way itself for ferry and bus. Strategy B asks for this one.
#   bit 1  `ferry=yes`. Strategy C (ferry) asks for this one.
#   bit 2  member of one of the layer's route relations. Strategy A reads those
#          through the relation, so no query asks for the bit; it is what gives
#          a bus layer — routes mapped as relations over ordinary roads — a
#          routable set at all.
CLS_ROUTE = 1
CLS_FERRY_YES = 2
CLS_MEMBER = 4

# The ways that decide whether a layer's store is refused as empty, and which
# its extent is measured over: the builder's side of Decision 9's table, which
# the CI filter applies to the same layers to decide `empty`.
ROUTABLE = {
    "rail": CLS_ROUTE,                                # unchanged from schema 2
    "ferry": CLS_ROUTE | CLS_FERRY_YES | CLS_MEMBER,  # B, C and A each answer
    "bus": CLS_ROUTE | CLS_MEMBER,                    # route=bus ways are rare
}

# Fixed-point scale for stored coordinates. 1e-7 degrees is OSM's own storage
# precision, so encode/decode loses nothing, and 180e7 still fits in an int32.
_COORD_SCALE = 1e7

# The blob format is 4-byte signed integers. `array` guarantees only that 'i' is
# at least 2 bytes, so pick the typecode that is exactly 4 rather than trust it —
# on a build where 'i' were 2 bytes, silently writing 16-bit coordinates would
# corrupt every geometry in the store.
_I32 = next((c for c in "il" if array(c).itemsize == 4), None)
if _I32 is None:  # pragma: no cover — no such platform is supported
    raise ImportError("no 4-byte integer typecode available for the store format")

_M_PER_DEG_LAT = 111_320.0

# Page cache per open connection. Two open regions then cost ~4 MB of cache
# regardless of how big those regions are — the ceiling the LRU test asserts.
_CACHE_KIB = 2048

# Radii tried in turn before giving up. Starting small keeps the common case (a
# stop sitting on the track) to one tiny R-tree hit; each step only runs when
# the previous one found nothing within itself. nearest_station searches in
# metres because it ranks in metres; nearest_node searches in degrees because it
# ranks in degrees — see there for why the two must agree.
_SEARCH_STEPS_M = (500.0, 2_000.0, 10_000.0)
_SEARCH_STEPS_DEG = tuple(r / _M_PER_DEG_LAT for r in _SEARCH_STEPS_M)

# Ceiling on one bbox query, in stored vertices. Measured on Germany, at ~268
# bytes per vertex for the returned elements and about as much again once
# _build_rail_graph turns them into a node graph:
#
#   Hamburg→Munich, about the longest leg the country offers   284,607  ~150 MB
#   the whole-Germany box                                    1,270,497  ~680 MB
#
# The ceiling has to pass the first and refuse the second, on a worker with
# 1 GB. 800k sits between them with margin either way (~430 MB, 2.8× the longest
# real leg). Refusing is the point: silently allocating 680 MB is an OOM that
# takes the worker with it, and _RAIL_BBOX_MAX_AREA in the resolver guards the
# same failure from the other end — it is a memory bound, not an Overpass
# artifact, and deleting it once Overpass is gone would remove a real guard.
_MAX_BBOX_VERTICES = 800_000


class RailStoreError(Exception):
    pass


def store_filename(region: str, layer: str = DEFAULT_LAYER) -> str:
    """File name for *region*'s *layer* store.

    ``("europe/germany")`` -> ``europe-germany.rail.sqlite``,
    ``("europe/germany", "bus")`` -> ``europe-germany.bus.sqlite``. Rail keeps
    the name it had before layers existed, so no installed store, sidecar or
    runbook command changes name.

    Shared with the builder so a store written for a region is found by it. An
    unknown layer raises rather than naming a file: the layer comes from a
    manifest, and this is where it becomes part of a path.
    """
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}, expected one of {', '.join(LAYERS)}")
    return region.strip("/").replace("/", "-") + f".{layer}.sqlite"


def encode_geometry(points: Iterable[tuple[float, float]]) -> bytes:
    """Pack (lat, lon) pairs into the stored blob format."""
    vals = array(_I32)
    for lat, lon in points:
        vals.append(int(round(lat * _COORD_SCALE)))
        vals.append(int(round(lon * _COORD_SCALE)))
    if sys.byteorder != "little":
        vals.byteswap()
    return vals.tobytes()


def decode_geometry(blob: bytes) -> list[dict]:
    """Unpack a stored blob into Overpass ``out geom`` vertices."""
    vals = array(_I32)
    vals.frombytes(blob)
    if sys.byteorder != "little":
        vals.byteswap()
    return [
        {"lat": vals[i] / _COORD_SCALE, "lon": vals[i + 1] / _COORD_SCALE}
        for i in range(0, len(vals), 2)
    ]


def clean_uic(uic: str) -> str:
    """Normalise *our* UIC code for matching, exactly as the resolver does.

    ``overpass_service._clean_uic`` strips leading zeros from the code we hold
    and then asks Overpass for that string; the OSM tag is matched verbatim. So
    this is applied to one side of the comparison only — see
    ``RailStore.relations_for_uic_pair`` — and does nothing else to the value,
    whitespace included, because ``_clean_uic`` does nothing else either.
    """
    uic = uic or ""
    return uic.lstrip("0") or uic


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular metres — same approximation the resolver's Dijkstra uses."""
    dlat = (lat1 - lat2) * _M_PER_DEG_LAT
    dlon = (lon1 - lon2) * _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def _box(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon) enclosing the radius circle."""
    dlat = radius_m / _M_PER_DEG_LAT
    # cos() floor keeps the box finite near the poles; rail there is a non-issue
    # but an infinite longitude span would break the R-tree query.
    dlon = radius_m / (_M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


class RailStore:
    """Read-only handle on one region's store file."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = str(path)
        if not os.path.exists(self.path):
            raise RailStoreError(f"no rail store at {self.path}")
        # check_same_thread=False plus a lock: resolve jobs run on worker
        # threads, and a store is shared between them by the LRU below. SQLite
        # itself is fine with that, the Python connection object is not.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(f"PRAGMA cache_size = -{_CACHE_KIB}")
        self._conn.execute("PRAGMA query_only = 1")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in _SUPPORTED_SCHEMAS:
            self._conn.close()
            raise RailStoreError(
                f"{self.path}: schema version {version}, this reader supports "
                f"{', '.join(str(v) for v in _SUPPORTED_SCHEMAS)}"
            )
        # Which columns the queries below may name. A file older than the
        # builder is read for what it holds, not refused — see _SUPPORTED_SCHEMAS.
        self.schema = version
        # A v1/v2 `rail` flag is 0 or 1, so `rail & mask` is `cls & mask` with
        # only bit 0 ever set — the reading _SUPPORTED_SCHEMAS promises.
        self._cls = "w.cls" if version >= 3 else "w.rail"
        self.meta = {
            k: v for k, v in self._conn.execute("SELECT key, value FROM meta")
        }

    # ------------------------------------------------------------------
    # Region identity
    # ------------------------------------------------------------------

    @property
    def region(self) -> str:
        return self.meta.get("region", "")

    @property
    def layer(self) -> str:
        """Which layer this store holds. Before schema 3 there was only rail."""
        return self.meta.get("layer", DEFAULT_LAYER)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lat, min_lon, max_lat, max_lon) the layer's routable set covers."""
        return (
            float(self.meta["min_lat"]),
            float(self.meta["min_lon"]),
            float(self.meta["max_lat"]),
            float(self.meta["max_lon"]),
        )

    # ------------------------------------------------------------------
    # Lookup 1 — nearest station with a uic_ref  (_find_station_near)
    # ------------------------------------------------------------------

    def nearest_station(self, lat: float, lon: float, radius_m: float = 5000) -> Optional[dict]:
        """Nearest station/halt with a ``uic_ref`` within *radius_m*, or None.

        Returns ``{"lat", "lon", "uic"}`` — what ``_enrich_uic`` consumes — and
        picks the same one ``_find_station_near`` picks, both halves of which
        matter: candidates come from a **metric** circle, because Overpass's
        ``around:`` is metres, and the winner among them is the smallest
        **squared-degree** distance, because that is what the resolver minimises.

        Ranking in metres instead moved 12.1% of jittered points onto a different
        station across Germany's 5,483 — and a different station is a different
        ``uic_ref``, so strategy A looks for a different relation, and
        ``_enrich_uic`` snaps the stop's coordinates to it, moving where Dijkstra
        starts. Same reasoning as ``nearest_node``: parity now, metric sanity in
        Phase 4.
        """
        min_lat, min_lon, max_lat, max_lon = _box(lat, lon, radius_m)
        rows = self._query(
            "SELECT s.lat, s.lon, s.uic FROM station_pos p JOIN station s ON s.id = p.id "
            "WHERE p.max_lon >= ? AND p.min_lon <= ? AND p.max_lat >= ? AND p.min_lat <= ?",
            (min_lon, max_lon, min_lat, max_lat),
        )
        best = None
        best_sq = math.inf
        for slat, slon, uic in rows:
            if _dist_m(lat, lon, slat, slon) > radius_m:
                continue                      # outside around:, as Overpass sees it
            sq = (slat - lat) ** 2 + (slon - lon) ** 2
            if sq < best_sq:
                best, best_sq = {"lat": slat, "lon": slon, "uic": uic}, sq
        return best

    # ------------------------------------------------------------------
    # Lookup 2 — route relations covering two points  (strategies A and B)
    # ------------------------------------------------------------------

    def relations_for_uic_pair(self, uic1: str, uic2: str) -> list[int]:
        """Relation ids whose member stations include both UIC codes.

        The local equivalent of strategy A's ``rel[route=train](bn.a)(bn.b)``.

        Normalisation is one-sided, exactly as Overpass's is: the query asks for
        ``node["uic_ref"="<our code, leading zeros stripped>"]`` and the OSM tag
        has to equal that string. Stripping the stored tag too would match
        relations Overpass does not — a behaviour change wearing a bug fix's
        clothes. If leading zeros in OSM data cost us a route, that belongs in
        the Phase 4 comparison, where it can be seen.
        """
        a, b = clean_uic(uic1), clean_uic(uic2)
        if not a or not b:
            return []
        rows = self._query(
            "SELECT ra.rel_id FROM relation_uic ra JOIN relation_uic rb "
            "ON ra.rel_id = rb.rel_id WHERE ra.uic = ? AND rb.uic = ? ORDER BY ra.rel_id",
            (a, b),
        )
        return [r[0] for r in rows]

    def relations_near(self, lat: float, lon: float, radius_m: float = 25_000) -> set[int]:
        """Ids of route relations with a member way inside *radius_m*.

        Strategy B intersects this for the two endpoints, so whatever is missed
        here is missed twice. The R-tree therefore indexes every way the extract
        holds, not only track: a relation whose members near the point are all
        platforms or service tracks is one Overpass's ``around:`` returns, and
        filtering to ``rail = 1`` here lost 12.6% of the candidate set at
        Luxembourg Gare — 83 relations found against 95 matched. (``rail = 1``
        is ``cls`` bit 0 since schema 3.)

        Overpass also matches on member *nodes*. A relation with a station
        inside the radius but no member way at all inside it does not occur in
        practice, so that case is left out rather than paid for.
        """
        min_lat, min_lon, max_lat, max_lon = _box(lat, lon, radius_m)
        rows = self._query(
            "SELECT DISTINCT rw.rel_id FROM way_bbox b JOIN relation_way rw ON rw.way_id = b.id "
            "WHERE b.max_lon >= ? AND b.min_lon <= ? AND b.max_lat >= ? AND b.min_lat <= ?",
            (min_lon, max_lon, min_lat, max_lat),
        )
        return {r[0] for r in rows}

    def relation_geometry(self, rel_ids: Iterable[int]) -> list[dict]:
        """Relations in Overpass ``out geom`` shape: member ways, then member nodes.

        ``_extract_relation_geometry`` reads ``members[].type``,
        ``members[].role``, ``members[].geometry`` and — since #363 — the
        ``lat``/``lon`` of the node members.

        The role is what says whether a member way is the route's *path* or
        something the route merely touches. Without it a station's
        ``railway=platform`` way goes into the routing graph, and because a
        platform is a closed ring unconnected to track it captures the endpoint
        snap and strands Dijkstra on it — issue #359, where every correct
        Paris Est ↔ Strasbourg relation was discarded that way. Overpass's
        ``out geom`` has always carried it; this store did not, so filtering
        had to wait for schema 2. A schema 1 file reads back ``""`` for every
        role, which is "path" — the pre-#359 behaviour, unchanged, for as long
        as that file is what the box holds.

        The node members are the relation's stops, and they are here rather
        than only in ``relation_stops`` because Overpass's ``out geom`` puts
        them here: ``{"type": "node", "ref", "role", "lat", "lon"}``, verified
        against overpass-api.de on relation 5928800, whose 16 node members all
        come back located. Emitting them in the same place means the resolver's
        stop anchor (#363) reads ``rel["members"]`` and works on both sources
        with no branch — a store-only accessor would have been a fix that only
        the local source got, which Phase 4 of docs/LOCAL_RAIL_DATA_PLAN.md
        exists to prevent.

        Two differences from Overpass, both deliberate. **Ways come first, then
        nodes**, because the store keeps the two member sequences in separate
        tables and cannot reconstruct how they interleaved; each sequence is in
        member order within itself, which is what order means for the stops.
        And a node the extract cannot place — Phase 1 keeps nodes with a
        ``uic_ref`` and station/halt nodes, so an ordinary stop node is named
        and not located — carries ``"held": False`` and *no* ``lat``/``lon``
        keys, where Overpass would have located it. So a consumer reads the
        coordinates with ``.get("lat") is not None`` and is right on both
        sources. ``uic`` is not repeated here: it is not in Overpass's member
        entries, and a key only one source carries is a key a consumer can come
        to depend on. ``relation_stops`` remains the way to ask for it.

        ``missing_members`` still counts **way** members only. It is what says
        how much of the relation's *path* was reconstructed, which is what
        ``_merge_relations`` breaks ties on, and most node members are
        legitimately unlocated (France: 11,957 of 18,361) — folding them in
        would report a complete relation as two thirds missing.

        A member way the extract does not hold — a platform or service track
        dropped by the way filter — is still listed, with an empty geometry and
        ``"held": False``. So "we could not reconstruct this member" is visible
        and countable (``missing_members``), and distinct from a relation that
        never had the member; the existing consumer skips such entries anyway,
        since it requires two points. What to do about a partially
        reconstructed relation is Phase 3's call, not the store's.
        """
        out = []
        for rel_id in rel_ids:
            row = self._query(
                "SELECT route, name FROM relation WHERE id = ?", (rel_id,)
            )
            if not row:
                continue
            route, name = row[0]
            members = []
            missing = 0
            role_col = "rw.role" if self.schema >= 2 else "''"
            for way_id, role, geom in self._query(
                f"SELECT rw.way_id, {role_col}, w.geom FROM relation_way rw "
                "LEFT JOIN way w ON w.id = rw.way_id WHERE rw.rel_id = ? ORDER BY rw.seq",
                (rel_id,),
            ):
                held = geom is not None
                missing += not held
                members.append({
                    "type": "way",
                    "ref": way_id,
                    "role": role,
                    "held": held,
                    "geometry": decode_geometry(geom) if held else [],
                })
            for stop in self.relation_stops(rel_id):
                member = {"type": "node", "ref": stop["ref"],
                          "role": stop["role"], "held": stop["lat"] is not None}
                if member["held"]:
                    member["lat"], member["lon"] = stop["lat"], stop["lon"]
                members.append(member)
            tags = {"route": route}
            if name:
                tags["name"] = name
            out.append({
                "type": "relation",
                "id": rel_id,
                "tags": tags,
                "members": members,
                "missing_members": missing,
            })
        return out

    def relation_stops(self, rel_id: int) -> list[dict]:
        """The relation's node members in member order: what it calls at.

        ``[{"ref", "role", "uic", "lat", "lon"}, …]``, ``uic`` empty and
        ``lat``/``lon`` None for a node the extract does not carry — Phase 1
        keeps nodes with a ``uic_ref`` and station/halt nodes, so an ordinary
        stop node is named here and not located.

        **Nothing in the resolver reads this yet, and that is deliberate.**
        ``relation_uic`` answers strategy A's question — "which relation serves
        both these codes" — as an unordered set, and it is what the pair query
        is indexed for. What it cannot answer is *in what order* a relation
        calls, which is what routing a leg through its intermediate stops needs
        instead of a shortest path between its endpoints. Recording it is
        nearly free at build time and costs a rebuild of every region to add
        later, so it was written during the #359 bump rather than bundled into
        whichever release first wants it.

        Empty on a schema 1 file, which has no such table.
        """
        if self.schema < 2:
            return []
        return [
            {"ref": ref, "role": role, "uic": uic, "lat": lat, "lon": lon}
            for ref, role, uic, lat, lon in self._query(
                "SELECT node_id, role, uic, lat, lon FROM relation_node "
                "WHERE rel_id = ? ORDER BY seq",
                (rel_id,),
            )
        ]

    # ------------------------------------------------------------------
    # Lookup 3 — railway ways in a bounding box  (strategy C)
    # ------------------------------------------------------------------

    def _in_box(self) -> str:
        """``FROM … WHERE`` for the ways of a class mask overlapping a box.

        The R-tree indexes every way so relations_near can see platforms, so
        each class query says which classes it wants for itself. Parameters:
        (mask, min_lon, max_lon, min_lat, max_lat). A way is selected when it
        has *any* bit of the mask, so a mask of several bits is the union of
        those selections.
        """
        return (
            f"FROM way_bbox b JOIN way w ON w.id = b.id WHERE ({self._cls} & ?) != 0 "
            "AND b.max_lon >= ? AND b.min_lon <= ? AND b.max_lat >= ? AND b.min_lat <= ?"
        )

    def ways_in_bbox(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        max_vertices: int = _MAX_BBOX_VERTICES,
        cls_mask: int = CLS_ROUTE,
    ) -> list[dict]:
        """Ways of *cls_mask* whose extent overlaps the box, in ``_build_rail_graph``'s shape.

        The default mask, bit 0, is the layer's routable class: track, on a
        rail store — and the only class a schema 1 or 2 file holds.

        Selection is by bounding box overlap, so a way that merely *spans* the
        box is included where Overpass would need a node inside it. That is a
        superset, and a superset is the safe direction: the graph gains a way
        the route may use, never loses one it needs.

        Raises ``RailStoreError`` when the box holds more than *max_vertices*
        (see ``_MAX_BBOX_VERTICES``). The size is counted from the blob lengths
        before anything is decoded, so an over-large box costs one index scan
        instead of the several hundred megabytes it would otherwise allocate. A
        caller that hits this wants a smaller box or an honest straight line,
        not a bigger worker.
        """
        params = (cls_mask, min_lon, max_lon, min_lat, max_lat)
        # 8 bytes per vertex on disk — two int32s. Counted, not estimated.
        vertices = self._query(
            f"SELECT COALESCE(SUM(LENGTH(w.geom)), 0) / 8 {self._in_box()}", params
        )[0][0]
        if vertices > max_vertices:
            raise RailStoreError(
                f"bbox ({min_lat}, {min_lon}, {max_lat}, {max_lon}) holds {vertices} "
                f"vertices, over the {max_vertices} ceiling"
            )
        rows = self._query(f"SELECT w.id, w.geom {self._in_box()}", params)
        return [
            {"type": "way", "id": way_id, "geometry": decode_geometry(geom)}
            for way_id, geom in rows
        ]

    def vertex_counts_in_bbox(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        cls_mask: int = CLS_ROUTE,
    ) -> dict[int, int]:
        """``{way id: vertex count}`` for exactly what ``ways_in_bbox`` returns.

        The per-way form of the total ``ways_in_bbox`` counts before it decodes,
        and it exists for one caller: ``LocalRailSource.ways_in_bbox`` answers a
        box from several overlapping regions at once, so it has to know the
        *deduplicated* size of the merged result before it decodes any of it.
        Ids are what make that possible — a border way is in both extracts, and
        summing the regions' own totals would charge it twice, which is the
        failure that degrades the effective ceiling towards ``ceiling / N``
        exactly where merging is the point.

        Same predicate, class mask included, and same parameters as
        ``ways_in_bbox``, so the ids and
        the counts describe that call's result and not an approximation of it.
        Counted from the blob lengths: 8 bytes per vertex, two int32s.
        """
        return dict(self._query(
            f"SELECT w.id, LENGTH(w.geom) / 8 {self._in_box()}",
            (cls_mask, min_lon, max_lon, min_lat, max_lat),
        ))

    # ------------------------------------------------------------------
    # Snapping — the index that replaces the linear _nearest_node scan
    # ------------------------------------------------------------------

    def nearest_node(self, lat: float, lon: float, max_radius_m: float = 25_000) -> Optional[dict]:
        """Nearest track vertex, as ``{"lat", "lon", "way"}``, or None.

        ``_nearest_node`` scans every node of the built graph — 1.43 s over
        Germany, twice per resolve. Here the R-tree bounds the scan to the ways
        near the point, and the winner is chosen by ``_nearest_node``'s own
        ordering: **squared degrees**, latitude and longitude weighted alike.

        That ordering is wrong as geometry — a degree of longitude is 0.65 of a
        degree of latitude at Luxembourg — and it is kept anyway, because this
        phase substitutes the source and changes nothing else. Ranking by metres
        instead picks a different vertex for 16.7% of points jittered by ±50 m,
        and 0.53% of ±200 m snaps land in a different connected component, where
        Dijkstra finds no path and the leg degrades to a straight line. Phase 4
        owns snapping and can change the metric against a stable baseline.

        What makes the search correct is the ``best_sq = h * h`` threshold below,
        not the shape of the box: a candidate is accepted only if its
        squared-degree distance is within h², the radius whose disc the box is
        guaranteed to contain, and otherwise the ladder widens. Drop that
        threshold — "the query already filtered these" — and 5.3% of points come
        back with the wrong vertex, because the box is a superset of the disc,
        not the disc. A metric circle of the equivalent radius is a valid
        superset too; the degree box is simply the tighter one (in degree space
        it is a square of side 2h against an ellipse 1.54× wider than tall), so
        it decodes fewer candidates for the same answer.

        *max_radius_m* is therefore a limit on **latitude-equivalent** distance:
        25 km means 0.2246°, which at Luxembourg's 49.6°N reaches 25 km
        north-south but only 16.2 km east-west (cos 49.6° = 0.65).

        Two behaviour changes remain, both deliberate: this returns None beyond
        that ceiling where ``_nearest_node`` always returns something, however
        absurd; and it sees only track the region holds, where ``_nearest_node``
        sees whatever graph it was handed.

        Coordinates come back at full precision, so a caller can form the
        ``"{lat:.6f},{lon:.6f}"`` id ``_build_rail_graph`` uses.
        """
        cap = max_radius_m / _M_PER_DEG_LAT
        for h in [d for d in _SEARCH_STEPS_DEG if d < cap] + [cap]:
            rows = self._query(f"SELECT w.id, w.geom {self._in_box()}",
                               (CLS_ROUTE, lon - h, lon + h, lat - h, lat + h))
            best = None
            best_sq = h * h
            for way_id, geom in rows:
                for pt in decode_geometry(geom):
                    sq = (pt["lat"] - lat) ** 2 + (pt["lon"] - lon) ** 2
                    if sq <= best_sq:
                        best, best_sq = {"lat": pt["lat"], "lon": pt["lon"], "way": way_id}, sq
            if best is not None:
                return best
        return None

    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "RailStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _query(self, sql: str, params: tuple) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()


class RailStoreCache:
    """LRU over open region stores.

    The bound is on *open files*, not on data: a store answers from disk, so an
    open region costs its connection's page cache and nothing else. Two is
    enough for the one case that needs more than one region at a time — a route
    crossing a border (Phase 3).
    """

    def __init__(self, directory: str | os.PathLike, max_open: int = 2,
                 layer: str = DEFAULT_LAYER) -> None:
        self.directory = str(directory)
        self.max_open = max_open
        # One cache per layer: a region name alone does not say which file.
        self.layer = layer
        self._open: "OrderedDict[str, RailStore]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, region: str) -> Optional[RailStore]:
        """Open store for *region*, or None when the region is not held.

        None is the "not covered" outcome Phase 0 requires callers to handle by
        falling back to Overpass — it is not an error.
        """
        with self._lock:
            store = self._open.get(region)
            if store is not None:
                self._open.move_to_end(region)
                return store
            path = os.path.join(self.directory, store_filename(region, self.layer))
            if not os.path.exists(path):
                return None
            store = RailStore(path)
            self._open[region] = store
            # Eviction drops the reference; it does not close the store. A
            # resolve holds the store it was handed across several queries, and
            # a third region arriving on another thread must not close the file
            # out from under it. The connection closes when the last holder
            # lets go, which is what bounds the open files in practice.
            while len(self._open) > self.max_open:
                self._open.popitem(last=False)
            return store

    def close_all(self) -> None:
        with self._lock:
            while self._open:
                _, store = self._open.popitem()
                store.close()

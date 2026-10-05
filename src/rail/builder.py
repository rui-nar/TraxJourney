"""Build a per-region rail store from a filtered rail-only ``.osm.pbf``.

The input is Phase 1's artifact — the extract already reduced to the selection
table in docs/LOCAL_RAIL_DATA_PLAN.md (railway ways without ``service``, route
relations, every node carrying a ``uic_ref``, and station/halt nodes, ways and
relations with one). This module makes no tag decisions of its own beyond
recognising those, so changing coverage stays a Phase 1 concern.

Run it from Phase 1's pipeline (see the plan — building in CI, not on the box,
is Phase 2's decision and this is the step that implements it)::

    python -m src.rail.builder germany-rail.osm.pbf europe-germany.rail.sqlite \\
        --region europe/germany --source-date 2026-09-05

``--layer ferry`` or ``--layer bus`` builds the same store format from that
layer's filtered file (docs/LOCAL_TRANSPORT_DATA_PLAN.md, Decision 7): only
which ways carry which ``cls`` bit, and which relations are kept, differ.

``osmium`` (pyosmium) is imported lazily: only the builder needs it, and the
builder does not run on the server.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from array import array
from datetime import datetime, timezone

from src.rail.store import (
    _I32,
    _COORD_SCALE,
    CLS_FERRY_YES,
    CLS_MEMBER,
    CLS_ROUTE,
    DEFAULT_LAYER,
    LAYERS,
    ROUTABLE,
    SCHEMA_VERSION,
    encode_geometry,
)

# The strategy-C way selection, repeated here only to tell track from a way that
# is in the file because something references it — a route relation's member, or
# a station polygon. Those keep their geometry but are not track the resolver
# may snap to or route over.
_RAIL_VALUES = {"rail", "narrow_gauge", "light_rail"}
_ROUTE_VALUES = {"train", "railway", "light_rail"}
_STATION_VALUES = {"station", "halt"}

# Which route relations each layer keeps: the `route=` values its strategy A
# asks Overpass for. Their member ways get `cls` bit 2.
_LAYER_ROUTES = {"rail": _ROUTE_VALUES, "ferry": {"ferry"}, "bus": {"bus"}}


def _route_class(layer: str, tags) -> bool:
    """Is a way in *layer*'s bit-0 selection — what strategy B asks for?"""
    if layer == "rail":
        return tags.get("railway") in _RAIL_VALUES and "service" not in tags
    return tags.get("route") == layer

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- One row per way, geometry packed as int32 lat/lon pairs (see store.py).
-- `cls` is the class bitmask (store.py, CLS_*): bit 0 the layer's routable
-- class — track, on a rail store — bit 1 `ferry=yes`, bit 2 a member of one of
-- the layer's route relations. A rail way without bit 0 is not track: a route
-- relation's platform or service member, or a station polygon. It keeps its
-- geometry because the relation's `out geom` includes it, but no bit-0 query
-- may return it.
CREATE TABLE way (
    id   INTEGER PRIMARY KEY,
    cls  INTEGER NOT NULL,
    geom BLOB NOT NULL
);

-- Indexes every way, not just track: `relations_near` has to see a relation
-- whose nearby members are all platforms, exactly as Overpass's around: does.
-- Class queries join `way` and filter on `cls & mask`.
CREATE VIRTUAL TABLE way_bbox USING rtree(id, min_lon, max_lon, min_lat, max_lat);

-- Stations mapped as nodes, ways or relations alike (Overpass's `out center`
-- returns a centre for all three, and polygon-mapped stations are common), so
-- `id` is ours and `osm_type`/`osm_id` say what it came from.
CREATE TABLE station (
    id       INTEGER PRIMARY KEY,
    osm_type TEXT NOT NULL,       -- node | way | relation
    osm_id   INTEGER NOT NULL,
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    uic      TEXT NOT NULL        -- verbatim tag; Overpass matches it verbatim
);
CREATE VIRTUAL TABLE station_pos USING rtree(id, min_lon, max_lon, min_lat, max_lat);

CREATE TABLE relation (
    id    INTEGER PRIMARY KEY,
    route TEXT NOT NULL,
    name  TEXT
);
-- Every way member of the relation, in member order, whether or not the
-- extract holds that way: the reader joins to `way` and reports the members it
-- could not reconstruct rather than silently shortening the relation.
--
-- `role` is the OSM member role, verbatim. It is what tells the route's path
-- from what the route merely touches: a `platform` member is a station's
-- platform way, which is not track, is not connected to track, and sits exactly
-- where a leg starts — so routing over it strands the search on a closed ring
-- (issue #359). Stored verbatim rather than reduced to a flag because the
-- consumer must be free to deny-list, and only a deny-list is safe: 2,299 of
-- France's 681,815 rows carry `forward`, `backward` or `alternative`, all of
-- which *are* the path.
CREATE TABLE relation_way (
    rel_id INTEGER NOT NULL,
    way_id INTEGER NOT NULL,
    seq    INTEGER NOT NULL,
    role   TEXT NOT NULL
);
CREATE INDEX relation_way_rel ON relation_way(rel_id);
CREATE INDEX relation_way_way ON relation_way(way_id);

-- The relation's node members, in member order: what it calls at, and in what
-- sequence. `relation_uic` below answers "does this relation serve both these
-- codes" and is the index for that question; it is a set, so it cannot answer
-- "in what order", which is what routing a leg through its intermediate stops
-- needs. Only nodes the extract carries have a location — Phase 1 keeps nodes
-- with a `uic_ref` and station/halt nodes — so an ordinary stop node is named
-- here with NULL lat/lon rather than dropped, because the *sequence* is the
-- point and a hole in it is not the same as a shorter route.
CREATE TABLE relation_node (
    rel_id  INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    role    TEXT NOT NULL,
    uic     TEXT NOT NULL,   -- '' when the node carries none
    lat     REAL,            -- NULL when the extract does not locate the node
    lon     REAL
);
CREATE INDEX relation_node_rel ON relation_node(rel_id);

-- Strategy A asks "which relation serves both these UIC codes". The uic is the
-- tag verbatim, because that is what Overpass compares against.
CREATE TABLE relation_uic (
    rel_id INTEGER NOT NULL,
    uic    TEXT NOT NULL
);
CREATE INDEX relation_uic_uic ON relation_uic(uic);
"""

_BATCH = 10_000

# Refuse an extract that has lost most of the node locations its ways refer to.
# A filter run without reference completion still produces a valid .pbf: the
# ways are there, their nodes are not. Building from one wrote a store labelled
# europe/germany holding three ways, 1,270,405 unlocatable nodes and a bbox
# covering Baden-Württemberg — which Phase 3 would then select for a Hamburg
# trip and get None from. A sound extract loses nothing, so anything above a
# rounding error is a broken upstream run, not a fact about the region.
_MAX_MISSING_NODE_RATIO = 0.01


class RailBuildError(Exception):
    pass


def build_store(
    pbf_path: str | os.PathLike,
    out_path: str | os.PathLike,
    region: str = "",
    source_date: str = "",
    layer: str = DEFAULT_LAYER,
) -> dict:
    """Build the store for one region's *layer*. Returns the stats written to ``meta``.

    Overwrites *out_path*: a store is a derived artifact, never edited in place.

    The layer decides three things and nothing else: which ways get ``cls``
    bit 0, which route relations are kept (and so whose members get bit 2), and
    the **routable set** — ``ROUTABLE[layer]`` — that the store is refused on
    when empty and whose extent becomes the store's bbox. For rail that set is
    bit 0, exactly the track schema 2 measured, so a rail store's extent and
    refusal are what they were.
    """
    import osmium  # noqa: PLC0415 — build-time only, see module docstring

    if layer not in LAYERS:
        raise RailBuildError(f"unknown layer {layer!r}, expected one of {', '.join(LAYERS)}")
    routable = ROUTABLE[layer]
    routes = _LAYER_ROUTES[layer]

    t0 = time.monotonic()
    out_path = str(out_path)
    if os.path.exists(out_path):
        os.remove(out_path)

    conn = sqlite3.connect(out_path)
    # No durability needed while building — a crashed build is thrown away and
    # rerun, and the fsync per transaction otherwise dominates the wall clock.
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.executescript(_SCHEMA)

    ways: list[tuple] = []
    boxes: list[tuple] = []
    stations: list[tuple] = []
    station_boxes: list[tuple] = []
    relations: list[tuple] = []
    rel_ways: list[tuple] = []
    rel_nodes: list[tuple] = []
    rel_uics: list[tuple] = []
    # Station relations resolve after the way pass: a multipolygon station's
    # centre is the centre of its members, and those are in the database by then.
    pending_rel_stations: list[tuple] = []
    # Which ways the file actually holds. Relation membership is recorded in
    # full even when a member is not held — a route relation names platforms and
    # service tracks that the way filter drops (Phase 1 measured 32% of
    # Denmark's route=train member ways falling outside it) — and a reader that
    # cannot tell "member we do not hold" from "not a member" has no way to
    # report a partially reconstructed relation.
    way_ids: set[int] = set()
    # Every node carrying a uic_ref, whatever else it is tagged: strategy A asks
    # Overpass for `node["uic_ref"=X]` with no railway filter, and route
    # relations reference the stop node rather than the station node.
    node_uic: dict[int, str] = {}
    # …and where they are, because a station mapped as a relation may have no
    # way members at all: `type=public_transport` stop_areas gather stop nodes,
    # and Overpass's rel[railway][uic_ref] + `out center` returns those.
    node_loc: dict[int, tuple[float, float]] = {}
    counts = {
        # `ways` holds bit 0 (track, on rail) and `member_ways` the rest, as
        # before layers; `routable_ways` is the layer's routable set, which for
        # rail is `ways` again and for bus is mostly relation members.
        "ways": 0, "member_ways": 0, "routable_ways": 0,
        "nodes": 0, "stations": 0, "relations": 0,
        "relation_ways": 0, "relation_ways_held": 0,
        # Node members of route relations, and how many of them the extract can
        # place. Most cannot be: Phase 1 keeps nodes carrying a uic_ref, so an
        # ordinary stop node is recorded in sequence with no location. The ratio
        # is the honest measure of how much of a relation's calling pattern this
        # region actually knows.
        "relation_nodes": 0, "relation_nodes_located": 0,
        # Nodes a way references that the extract does not locate. Dropping one
        # welds its neighbours together, which silently moves the geometry, so
        # the number is recorded rather than left to be guessed at — and, above
        # _MAX_MISSING_NODE_RATIO, refused outright.
        "ways_missing_nodes": 0, "missing_nodes": 0, "ways_dropped": 0,
        # Station relations we could not place, and ones placed from only part
        # of their members — where the centre is the centre of what we hold,
        # which is not what `out center` would have said.
        "stations_unlocatable": 0, "stations_partial": 0,
    }
    referenced_nodes = 0   # node slots across every way, located or not
    extent = [90.0, 180.0, -90.0, -180.0]  # min_lat, min_lon, max_lat, max_lon

    def flush() -> None:
        conn.executemany("INSERT INTO way VALUES (?, ?, ?)", ways)
        conn.executemany("INSERT INTO way_bbox VALUES (?, ?, ?, ?, ?)", boxes)
        ways.clear()
        boxes.clear()

    def add_station(osm_type: str, osm_id: int, lat: float, lon: float, uic: str) -> None:
        sid = len(stations) + 1
        stations.append((sid, osm_type, osm_id, lat, lon, uic))
        station_boxes.append((sid, lon, lon, lat, lat))
        counts["stations"] += 1

    # Nodes, then ways, then relations — PBF order, so the uic map is complete
    # by the time relations need it and one pass is enough.
    for obj in osmium.FileProcessor(str(pbf_path)).with_locations():
        tags = obj.tags
        uic = tags.get("uic_ref")
        is_station = bool(uic) and tags.get("railway") in _STATION_VALUES
        if obj.is_node():
            if not uic:
                continue
            node_uic[obj.id] = uic
            node_loc[obj.id] = (obj.location.lat, obj.location.lon)
            if is_station:
                add_station("node", obj.id, obj.location.lat, obj.location.lon, uic)
        elif obj.is_way():
            pts = [(n.lat, n.lon) for n in obj.nodes if n.location.valid()]
            missing = len(obj.nodes) - len(pts)
            referenced_nodes += len(obj.nodes)
            if missing:
                counts["ways_missing_nodes"] += 1
                counts["missing_nodes"] += missing
            if len(pts) < 2:
                counts["ways_dropped"] += 1
                continue
            # Bit 2 is not known yet — relations come after ways in a PBF — and
            # is added in one UPDATE once they have been read.
            cls = ((CLS_ROUTE if _route_class(layer, tags) else 0)
                   | (CLS_FERRY_YES if tags.get("ferry") == "yes" else 0))
            ways.append((obj.id, cls, encode_geometry(pts)))
            way_ids.add(obj.id)
            counts["nodes"] += len(pts)
            counts["ways" if cls & CLS_ROUTE else "member_ways"] += 1
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            box = (obj.id, min(lons), max(lons), min(lats), max(lats))
            boxes.append(box)
            if cls & routable:
                extent[0] = min(extent[0], box[3])
                extent[1] = min(extent[1], box[1])
                extent[2] = max(extent[2], box[4])
                extent[3] = max(extent[3], box[2])
            if is_station:
                # Overpass's `out center` is the centre of the element's
                # bounding box, so a polygon station lands where Overpass puts it.
                add_station("way", obj.id, (box[3] + box[4]) / 2, (box[1] + box[2]) / 2, uic)
            if len(ways) >= _BATCH:
                flush()
        else:
            if is_station:
                pending_rel_stations.append((
                    obj.id, uic,
                    [m.ref for m in obj.members if m.type == "w"],
                    [m.ref for m in obj.members if m.type == "n"],
                    sum(1 for m in obj.members if m.type == "r"),
                ))
            if tags.get("route") not in routes:
                continue
            relations.append((obj.id, tags["route"], tags.get("name")))
            counts["relations"] += 1
            seq = 0
            node_seq = 0
            seen_uic = set()
            for member in obj.members:
                if member.type == "w":
                    rel_ways.append((obj.id, member.ref, seq, member.role or ""))
                    counts["relation_ways_held"] += member.ref in way_ids
                    seq += 1
                elif member.type == "n":
                    member_uic = node_uic.get(member.ref)
                    loc = node_loc.get(member.ref)
                    # Every node member, in order, located or not — see the
                    # relation_node comment in _SCHEMA. `relation_uic` below is
                    # unchanged and still deduplicated: strategy A's pair query
                    # is indexed on it, and this table is not a replacement for
                    # it but the answer to a different question.
                    rel_nodes.append((
                        obj.id, member.ref, node_seq, member.role or "",
                        member_uic or "",
                        loc[0] if loc else None, loc[1] if loc else None,
                    ))
                    node_seq += 1
                    counts["relation_nodes_located"] += loc is not None
                    if member_uic and member_uic not in seen_uic:
                        seen_uic.add(member_uic)
                        rel_uics.append((obj.id, member_uic))

    flush()

    # Station relations: centre of the bounding box of the members we hold,
    # which is what `out center` reports for a relation whose members we hold in
    # full. A relation we cannot place at all is counted, not dropped quietly —
    # these are the polygon-mapped stations the store exists to recover, and a
    # silent zero here looks exactly like a country that has none.
    for rel_id, uic, way_ids_m, node_ids_m, sub_relations in pending_rel_stations:
        box, held, total = _members_extent(conn, way_ids_m, node_ids_m, node_loc)
        total += sub_relations
        if box is None:
            counts["stations_unlocatable"] += 1
            continue
        if held < total:
            counts["stations_partial"] += 1
        add_station("relation", rel_id, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, uic)

    conn.executemany("INSERT INTO station VALUES (?, ?, ?, ?, ?, ?)", stations)
    conn.executemany("INSERT INTO station_pos VALUES (?, ?, ?, ?, ?)", station_boxes)
    conn.executemany("INSERT INTO relation VALUES (?, ?, ?)", relations)
    conn.executemany("INSERT INTO relation_way VALUES (?, ?, ?, ?)", rel_ways)
    counts["relation_ways"] = len(rel_ways)
    conn.executemany("INSERT INTO relation_node VALUES (?, ?, ?, ?, ?, ?, ?)", rel_nodes)
    counts["relation_nodes"] = len(rel_nodes)
    conn.executemany("INSERT INTO relation_uic VALUES (?, ?)", rel_uics)

    # Bit 2: every held way a kept route relation names, whatever its role.
    conn.execute("UPDATE way SET cls = cls | ? WHERE id IN "
                 "(SELECT way_id FROM relation_way)", (CLS_MEMBER,))
    # The way pass measured the extent of what it could classify on its own;
    # a layer whose routable set includes relation members — bus, ferry — also
    # reaches as far as the members that carry no other routable bit. Rail's
    # set is bit 0 alone, so this adds nothing to a rail store's extent.
    if routable & CLS_MEMBER:
        for (geom,) in conn.execute(
                "SELECT geom FROM way WHERE (cls & ?) != 0 AND (cls & ?) = 0",
                (CLS_MEMBER, routable & ~CLS_MEMBER)):
            _grow_extent(extent, geom)
    counts["routable_ways"] = conn.execute(
        "SELECT COUNT(*) FROM way WHERE (cls & ?) != 0", (routable,)).fetchone()[0]

    def refuse(why: str) -> None:
        conn.close()
        os.remove(out_path)
        raise RailBuildError(f"{pbf_path}: {why}")

    # The region's extent comes from its routable set, and Phase 3 picks regions
    # by extent. A store built from a damaged extract does not fail on the way
    # in; it answers None for most of the country it claims, which is
    # indistinguishable from "no route exists". Both checks exist to make that
    # loud. Refusing on the routable set rather than on bit 0 alone is what
    # lets a ferry layer of only `ferry=yes` ways, or a bus layer of only
    # relation members, be a store — Decision 9's table.
    if not counts["routable_ways"]:
        refuse("no railway ways — not a rail extract" if layer == "rail" else
               f"no {layer} ways in the layer's routable set — not a {layer} extract")
    missing_ratio = counts["missing_nodes"] / referenced_nodes if referenced_nodes else 0.0
    if missing_ratio > _MAX_MISSING_NODE_RATIO:
        refuse(
            f"{counts['missing_nodes']} of {referenced_nodes} way nodes have no "
            f"location ({missing_ratio:.1%}) — the extract was filtered without "
            f"reference completion, so its geometry is gone"
        )

    stats = dict(counts)
    stats["build_seconds"] = round(time.monotonic() - t0, 2)
    meta = {
        "schema": str(SCHEMA_VERSION),
        "region": region,
        "layer": layer,
        "source_file": os.path.basename(str(pbf_path)),
        "source_date": source_date,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "min_lat": repr(extent[0]),
        "min_lon": repr(extent[1]),
        "max_lat": repr(extent[2]),
        "max_lon": repr(extent[3]),
        **{k: str(v) for k, v in stats.items()},
    }
    conn.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.execute("VACUUM")   # the store ships over the network; give it no slack
    conn.close()

    stats["bytes"] = os.path.getsize(out_path)
    return stats


def _grow_extent(extent: list[float], blob: bytes) -> None:
    """Widen *extent* (min_lat, min_lon, max_lat, max_lon) to a stored geometry.

    From the blob rather than the R-tree, whose float32 boxes are rounded
    outward: the extent the way pass measures is exact, and this half of it
    has to be too.
    """
    vals = array(_I32)
    vals.frombytes(blob)
    if sys.byteorder != "little":
        vals.byteswap()
    lats, lons = vals[0::2], vals[1::2]
    extent[0] = min(extent[0], min(lats) / _COORD_SCALE)
    extent[1] = min(extent[1], min(lons) / _COORD_SCALE)
    extent[2] = max(extent[2], max(lats) / _COORD_SCALE)
    extent[3] = max(extent[3], max(lons) / _COORD_SCALE)


def _members_extent(
    conn: sqlite3.Connection,
    way_ids: list[int],
    node_ids: list[int],
    node_loc: dict[int, tuple[float, float]],
) -> tuple[tuple | None, int, int]:
    """((min_lat, min_lon, max_lat, max_lon) or None, members held, members named).

    Both kinds of member place a station relation: a multipolygon's rings, and a
    stop_area's stop nodes. Only the nodes carrying a ``uic_ref`` have known
    locations — those are the ones the extract keeps — so a stop_area of plain
    nodes reports nothing held, and says so rather than vanishing.
    """
    boxes = []
    if way_ids:
        boxes = conn.execute(
            "SELECT min_lat, min_lon, max_lat, max_lon FROM way_bbox WHERE id IN "
            f"({','.join('?' * len(way_ids))})",
            way_ids,
        ).fetchall()
    points = [node_loc[n] for n in node_ids if n in node_loc]
    held = len(boxes) + len(points)
    total = len(way_ids) + len(node_ids)
    if not held:
        return None, 0, total
    lats = [b[0] for b in boxes] + [b[2] for b in boxes] + [p[0] for p in points]
    lons = [b[1] for b in boxes] + [b[3] for b in boxes] + [p[1] for p in points]
    return (min(lats), min(lons), max(lats), max(lons)), held, total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a rail store from a filtered .osm.pbf")
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--region", default="", help='e.g. "europe/germany"')
    ap.add_argument("--source-date", default="", help="date of the source extract")
    ap.add_argument("--layer", default=DEFAULT_LAYER, choices=LAYERS,
                    help="which layer the filtered file holds (default: rail)")
    args = ap.parse_args(argv)
    stats = build_store(args.pbf, args.out, args.region, args.source_date, args.layer)
    print(" ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Ferry and bus geometry read from local region stores (#345, U9).

The move changes where the strategies' elements come from and nothing else, so
two things are tested here: that each strategy, given a store, answers what it
answers given Overpass's response over the same data; and that every way the
local attempt can come up empty — no coverage, a broken file, nothing on the
leg — ends at Overpass rather than at a missing route.

Stores are real, built by ``src.rail.builder`` from synthetic extracts written
in ``tmp_path``. Only the network is mocked.
"""
import json
import logging
import os
import socket
import sqlite3
from unittest.mock import Mock, patch

import osmium
import pytest
from osmium.osm import mutable

from api.segments import _compute_segment_geometry
from src.models.project import ConnectingSegment, SegmentEndpoint
from src.rail.builder import build_store
from src.rail.store import CLS_FERRY_YES, CLS_ROUTE, RailStore, store_filename
from src.services import overpass_service as ov
from src.services.rail_source import MANIFEST_NAME
from src.services.route_source import LocalRouteSource

# A crossing from west to east port, ~25 km, and the middle of it, which is
# where the two regions of the split-relation tests meet.
WEST = (55.00, 11.00)
MID = (55.00, 11.20)
EAST = (55.00, 11.40)

_RESOLVES = "traxjourney_route_resolves_total"


# ---------------------------------------------------------------------------
# Building the data a test needs
# ---------------------------------------------------------------------------

def write_layer(path, ways, relations=(), stops=None):
    """A minimal ferry or bus extract.

    *ways* maps way id to ``(points, tags)`` with points as [(lat, lon), …];
    *relations* is ``[(rel_id, members, tags)]`` with members ``("w", id)`` or
    ``("n", id)``; *stops* maps node id to (lat, lon) — a relation's stops, as
    Phase 1 now writes them for these layers. A member id the file does not
    hold is allowed: it is how a relation crossing a border is expressed.
    """
    writer = osmium.SimpleWriter(str(path))
    for node_id, (lat, lon) in sorted((stops or {}).items()):
        writer.add_node(mutable.Node(id=node_id, location=(lon, lat)))
    node_id = 1000
    way_nodes = {}
    for way_id, (points, _) in ways.items():
        refs = []
        for lat, lon in points:
            writer.add_node(mutable.Node(id=node_id, location=(lon, lat)))
            refs.append(node_id)
            node_id += 1
        way_nodes[way_id] = refs
    for way_id, (_, tags) in ways.items():
        writer.add_way(mutable.Way(id=way_id, nodes=way_nodes[way_id], tags=tags))
    for rel_id, members, tags in relations:
        writer.add_relation(mutable.Relation(
            id=rel_id, members=[(kind, ref, "") for kind, ref in members], tags=tags))
    writer.close()
    return path


def build_region(directory, region, layer, pbf):
    """Build *pbf* as *region*'s *layer* store, returning its manifest entry."""
    path = os.path.join(directory, store_filename(region, layer))
    build_store(pbf, path, region=region, source_date="2026-10-02", layer=layer)
    return entry(region, layer, path)


def entry(region, layer, store_path=None, bbox=None):
    """A schema 3 manifest entry; the bbox is the store's own unless given."""
    if bbox is None:
        with RailStore(store_path) as store:
            bbox = store.bbox
    min_lat, min_lon, max_lat, max_lon = bbox
    return {"region": region, "layer": layer, "status": "ok",
            "file": f"x-{layer}.osm.pbf", "source": "https://example.invalid",
            "source_date": "2026-10-02", "sha256": "0" * 64, "bytes": 1,
            "ways": 1, "relations": 1, "stations": 0,
            "bbox": [min_lon, min_lat, max_lon, max_lat]}


def write_manifest(directory, entries):
    with open(os.path.join(directory, MANIFEST_NAME), "w", encoding="utf-8") as handle:
        json.dump({"schema": 3, "generated_at": "2026-10-02T04:00:00Z",
                   "regions": entries}, handle)


def ferry_relation(rel_id=500, way_ids=(50, 51)):
    """A route=ferry relation over *way_ids*, calling at both ports."""
    return (rel_id, [("n", 1), *[("w", w) for w in way_ids], ("n", 2)],
            {"route": "ferry", "name": "West - East"})


PORT_STOPS = {1: WEST, 2: EAST}
# The crossing in two halves, untagged: they are the relation's ways, so they
# are in the ferry layer as members (bit 2) and answer only strategy A.
WEST_HALF = {50: ([WEST, (55.01, 11.10), MID], {})}
EAST_HALF = {51: ([MID, (54.99, 11.30), EAST], {})}


def overpass_relation(rel_id, ways, stops):
    """What Overpass's ``out geom`` returns for a relation over *ways*."""
    return {"type": "relation", "id": rel_id, "tags": {"route": "ferry"},
            "members": [
                *[{"type": "way", "ref": way_id, "role": "",
                   "geometry": [{"lat": lat, "lon": lon} for lat, lon in points]}
                  for way_id, (points, _) in ways.items()],
                *[{"type": "node", "ref": node_id, "role": "",
                   "lat": lat, "lon": lon} for node_id, (lat, lon) in stops.items()],
            ]}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _forget_configured_sources():
    """The configured sources are cached per directory; each test gets its own."""
    ov._local_source = ov._local_route = None
    yield
    ov._local_source = ov._local_route = None


@pytest.fixture
def configure(monkeypatch):
    def _configure(directory):
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", str(directory))
    return _configure


@pytest.fixture
def no_network(monkeypatch):
    """Overpass mocked out, and any socket refused, so a local answer is provably local."""
    transport = Mock(name="_overpass", side_effect=AssertionError("network used"))
    monkeypatch.setattr(ov, "_overpass", transport)

    def _no_sockets(*args, **kwargs):
        raise AssertionError("a socket was opened")
    monkeypatch.setattr(socket.socket, "connect", _no_sockets)
    return transport


@pytest.fixture
def whole_crossing(tmp_path):
    """One region holding the whole relation."""
    pbf = write_layer(tmp_path / "whole.osm.pbf", {**WEST_HALF, **EAST_HALF},
                      [ferry_relation()], PORT_STOPS)
    write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/whole", "ferry", pbf)])
    return tmp_path


@pytest.fixture
def split_crossing(tmp_path):
    """The relation in both regions' extracts, each holding only its own half."""
    west = write_layer(tmp_path / "west.osm.pbf", WEST_HALF, [ferry_relation()],
                       {1: WEST})
    east = write_layer(tmp_path / "east.osm.pbf", EAST_HALF, [ferry_relation()],
                       {2: EAST})
    write_manifest(str(tmp_path), [
        build_region(str(tmp_path), "test/west", "ferry", west),
        build_region(str(tmp_path), "test/east", "ferry", east),
    ])
    return tmp_path


def _segment(kind, a, b):
    return ConnectingSegment(id="seg-1", segment_type=kind, label="A -> B",
                             start=SegmentEndpoint(*a), end=SegmentEndpoint(*b))


def _reaches(poly, a, b, km=0.5):
    return (ov._crow_km(a[0], a[1], poly[0][1], poly[0][0]) <= km
            and ov._crow_km(b[0], b[1], poly[-1][1], poly[-1][0]) <= km)


# ---------------------------------------------------------------------------
# Local hits
# ---------------------------------------------------------------------------

class TestLocalHit:
    def test_a_ferry_relation_resolves_locally_with_no_network(
            self, whole_crossing, configure, no_network):
        configure(whole_crossing)
        result = ov.get_ferry_geometry(*WEST, *EAST)
        assert (result.strategy, result.source, result.degraded) == (
            "relation", "local", False)
        assert _reaches(result.polyline, WEST, EAST)
        assert no_network.call_count == 0

    def test_strategy_a_answers_what_it_answers_from_overpass(
            self, whole_crossing, monkeypatch):
        """Parity: the same relation, read from the store and from an ``out
        geom`` response, routes to the same polyline."""
        local = ov._via_route_relation_type(
            "ferry", *WEST, *EAST, LocalRouteSource(str(whole_crossing)))
        answer = {"elements": [overpass_relation(500, {**WEST_HALF, **EAST_HALF},
                                                 PORT_STOPS)]}
        monkeypatch.setattr(ov, "_overpass", Mock(return_value=answer))
        assert local == ov._via_route_relation_type("ferry", *WEST, *EAST)

    def test_a_relation_split_across_two_regions_resolves_after_the_merge(
            self, split_crossing, configure, no_network):
        """Each region alone holds half the crossing, which strategy A refuses —
        its far end is 13 km from the port. Merged, the relation is whole."""
        source = LocalRouteSource(str(split_crossing))
        box = (54.9, 10.9, 55.1, 11.5)
        [relation] = source.relations_in_bbox("ferry", box)
        assert [m["ref"] for m in relation["members"] if m.get("held")] == [50, 51, 1, 2]
        assert relation["missing_members"] == 0

        configure(split_crossing)
        result = ov.get_ferry_geometry(*WEST, *EAST)
        assert (result.strategy, result.source) == ("relation", "local")
        assert _reaches(result.polyline, WEST, EAST)
        assert no_network.call_count == 0

    def test_one_half_alone_is_a_miss(self, tmp_path, configure):
        """The control for the merge test: one region's half is not an answer,
        so the leg goes to Overpass."""
        west = write_layer(tmp_path / "west.osm.pbf", WEST_HALF, [ferry_relation()],
                           {1: WEST})
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/west", "ferry", west)])
        configure(tmp_path)
        answer = {"elements": [overpass_relation(500, {**WEST_HALF, **EAST_HALF},
                                                 PORT_STOPS)]}
        with patch.object(ov, "_overpass", return_value=answer) as transport:
            result = ov.get_ferry_geometry(*WEST, *EAST)
        assert result.source == "overpass"
        assert transport.call_count == 1

    def test_a_leg_mapped_only_with_ferry_yes_resolves_through_strategy_c(
            self, tmp_path, configure, no_network):
        pbf = write_layer(tmp_path / "hopper.osm.pbf", {
            60: ([WEST, (55.01, 11.10), MID, EAST], {"ferry": "yes"})})
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/isles", "ferry", pbf)])
        configure(tmp_path)
        result = ov.get_ferry_geometry(*WEST, *EAST)
        assert (result.strategy, result.source) == ("ferry_yes_dijkstra", "local")
        assert len(result.polyline) == 6   # the leg's two ends around the way's four
        assert no_network.call_count == 0

    def test_each_strategy_asks_for_its_own_class(self, tmp_path):
        """Decision 9: B asks for bit 0, C for bit 1, so a way tagged both is in
        both answers and a ferry=yes-only way is in C's alone."""
        pbf = write_layer(tmp_path / "classes.osm.pbf", {
            70: ([WEST, MID], {"route": "ferry"}),
            71: ([MID, EAST], {"route": "ferry", "ferry": "yes"}),
            72: ([WEST, EAST], {"ferry": "yes"}),
        })
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/c", "ferry", pbf)])
        source = LocalRouteSource(str(tmp_path))
        box = (54.9, 10.9, 55.1, 11.5)
        assert [w["id"] for w in source.ways_in_bbox("ferry", CLS_ROUTE, box)] == [70, 71]
        assert [w["id"] for w in source.ways_in_bbox("ferry", CLS_FERRY_YES, box)] == [71, 72]

    def test_a_bus_relation_bridges_towards_its_stops_as_it_does_on_overpass(
            self, tmp_path, configure, no_network, monkeypatch):
        """F5's point: a broken relation is bridged only towards a stop it can
        place, so the store must place its stops as Overpass does. The two
        halves miss each other by ~110 m; the stop near each end lets the
        bridge be built, and the route reaches both."""
        gap = (55.001, 11.20)
        ways = {80: ([WEST, (55.0, 11.10), MID], {}),
                81: ([gap, (55.0, 11.30), EAST], {})}
        rel = (800, [("n", 1), ("w", 80), ("w", 81), ("n", 2)], {"route": "bus"})
        pbf = write_layer(tmp_path / "bus.osm.pbf", ways, [rel], PORT_STOPS)
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/bus", "bus", pbf)])
        configure(tmp_path)

        result = ov.get_bus_geometry(*WEST, *EAST)
        assert (result.strategy, result.source) == ("relation", "local")
        assert _reaches(result.polyline, WEST, EAST, km=0.01)

        answer = overpass_relation(800, ways, PORT_STOPS)
        answer["tags"]["route"] = "bus"
        monkeypatch.setattr(ov, "_overpass", Mock(return_value={"elements": [answer]}))
        assert result.polyline == ov._via_route_relation_type("bus", *WEST, *EAST)


# ---------------------------------------------------------------------------
# Way selection is by geometry, as Overpass's way(bbox) is (F6)
# ---------------------------------------------------------------------------

# Strategy B's box for WEST -> EAST: the two ports, buffered by 0.25 degrees.
B_BOX = (54.75, 10.75, 55.25, 11.65)
# A crossing far to the north and east whose extent covers B_BOX entirely, and
# not one vertex or segment of which comes near it: the Ancona - Greece lines
# over the Bay of Naples, in miniature.
AROUND = [(56.0, 10.0), (56.0, 12.0), (54.0, 12.0)]


class TestWaySelection:
    @pytest.mark.parametrize("mode", ["ferry", "bus"])
    def test_a_way_is_returned_only_when_its_geometry_meets_the_box(self, tmp_path, mode):
        pbf = write_layer(tmp_path / f"{mode}.osm.pbf", {
            # extent overlaps the box, geometry does not
            90: (AROUND, {"route": mode}),
            # a vertex inside
            91: ([(55.0, 11.2), (56.0, 11.2)], {"route": mode}),
            # crosses the box with both ends, and every vertex, outside
            92: ([(54.0, 11.2), (56.0, 11.2)], {"route": mode}),
            # crosses one corner only, ends outside on two different sides
            93: ([(55.05, 10.6), (55.35, 10.90)], {"route": mode}),
            # passes the same corner outside it, its extent overlapping the box
            94: ([(55.20, 10.6), (55.40, 10.80)], {"route": mode}),
        })
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/g", mode, pbf)])
        source = LocalRouteSource(str(tmp_path))
        assert sorted(w["id"] for w in source.ways_in_bbox(mode, CLS_ROUTE, B_BOX)) == [
            91, 92, 93]

    def test_ferry_yes_ways_are_selected_the_same_way(self, tmp_path):
        """Strategy C's question goes through the same filter as B's."""
        pbf = write_layer(tmp_path / "c.osm.pbf", {
            95: (AROUND, {"ferry": "yes"}),
            96: ([(54.0, 11.2), (56.0, 11.2)], {"ferry": "yes"}),
        })
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/c", "ferry", pbf)])
        source = LocalRouteSource(str(tmp_path))
        assert [w["id"] for w in source.ways_in_bbox("ferry", CLS_FERRY_YES, B_BOX)] == [96]

    def test_a_crossing_whose_extent_alone_covers_the_leg_is_a_local_miss(
            self, tmp_path, configure, monkeypatch):
        """Napoli -> Capri with only Croatia's data: the store's extent match
        hands B a far-off crossing, B snaps both ends to it and invents a
        route. Overpass would return no ways, so the local answer is a miss and
        production asks Overpass, once."""
        pbf = write_layer(tmp_path / "far.osm.pbf", {
            90: (AROUND, {"route": "ferry", "ferry": "yes"})})
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/far", "ferry", pbf)])
        configure(tmp_path)
        transport = _overpass_answer()
        monkeypatch.setattr(ov, "_overpass", transport)

        result = ov.get_ferry_geometry(*WEST, *EAST)
        assert (result.strategy, result.source) == ("relation", "overpass")
        assert transport.call_count == 1


# ---------------------------------------------------------------------------
# Local misses — every one of them ends at Overpass
# ---------------------------------------------------------------------------

def _overpass_answer():
    """Overpass holding the crossing: strategy A answers on the first query."""
    return Mock(name="_overpass", return_value={"elements": [
        overpass_relation(500, {**WEST_HALF, **EAST_HALF}, PORT_STOPS)]})


class TestLocalMiss:
    def test_a_leg_in_neither_region_falls_back_to_overpass_exactly_once(
            self, whole_crossing, configure, monkeypatch):
        """Coverage elsewhere, the leg here: the local chain finds nothing and
        the Overpass chain runs once — one query, strategy A answering it."""
        far = {1: (57.60, 18.30), 2: (57.60, 18.70)}
        ways = {90: ([far[1], (57.61, 18.50), far[2]], {})}
        transport = Mock(return_value={"elements": [overpass_relation(900, ways, far)]})
        monkeypatch.setattr(ov, "_overpass", transport)
        configure(whole_crossing)

        result = ov.get_ferry_geometry(*far[1], *far[2])
        assert (result.strategy, result.source) == ("relation", "overpass")
        assert transport.call_count == 1

    def test_no_route_anywhere_still_raises(self, whole_crossing, configure, monkeypatch):
        """A local miss defers to Overpass; it does not invent a straight line."""
        monkeypatch.setattr(ov, "_overpass", Mock(return_value={"elements": []}))
        configure(whole_crossing)
        with pytest.raises(ov.OverpassError):
            ov.get_ferry_geometry(57.60, 18.30, 57.60, 18.70)

    @pytest.mark.parametrize("state", ["garbage", "zero_byte", "wrong_schema"])
    def test_a_corrupt_store_falls_back_and_never_raises(
            self, tmp_path, configure, monkeypatch, caplog, state):
        path = tmp_path / store_filename("test/whole", "ferry")
        if state == "garbage":
            path.write_bytes(b"\x1f\x8b\x08\x00 not a sqlite file at all")
        elif state == "zero_byte":
            path.write_bytes(b"")
        else:
            conn = sqlite3.connect(str(path))
            conn.execute("PRAGMA user_version = 99")
            conn.close()
        write_manifest(str(tmp_path), [entry("test/whole", "ferry",
                                             bbox=(54.9, 10.9, 55.1, 11.5))])
        configure(tmp_path)
        transport = _overpass_answer()
        monkeypatch.setattr(ov, "_overpass", transport)

        with caplog.at_level(logging.WARNING, logger="src.services.rail_source"):
            result = ov.get_ferry_geometry(*WEST, *EAST)
        assert result.source == "overpass"
        assert transport.call_count == 1
        assert "ferry region test/whole" in caplog.text
        assert "cannot be opened" in caplog.text

    def test_a_store_that_goes_bad_mid_query_falls_back(
            self, whole_crossing, configure, monkeypatch):
        def malformed(self, *args, **kwargs):
            raise sqlite3.DatabaseError("database disk image is malformed")
        monkeypatch.setattr(RailStore, "_query", malformed)
        monkeypatch.setattr(RailStore, "relation_geometry", malformed)
        configure(whole_crossing)
        transport = _overpass_answer()
        monkeypatch.setattr(ov, "_overpass", transport)

        assert ov.get_ferry_geometry(*WEST, *EAST).source == "overpass"
        assert transport.call_count == 1

    @pytest.mark.parametrize("question,error", [
        ("relations_in_bbox", KeyError("region")),
        ("ways_in_bbox", ValueError("bad blob")),
    ])
    def test_any_local_failure_falls_back_to_overpass_once(
            self, whole_crossing, configure, monkeypatch, metric, caplog,
            question, error):
        """Not only what ``_ask`` and the store cache absorb: whatever the local
        attempt raises is a local miss. Let out, it would reach RQ's retry and
        meet the same file again — every ferry resolve failing while it sits
        there. The far leg has no local relation, so strategy A finds nothing
        and B asks ``ways_in_bbox``: each question fails in its own case."""
        far = {1: (57.60, 18.30), 2: (57.60, 18.70)}
        ways = {90: ([far[1], (57.61, 18.50), far[2]], {})}
        transport = Mock(return_value={"elements": [overpass_relation(900, ways, far)]})
        monkeypatch.setattr(ov, "_overpass", transport)

        def broken(*args, **kwargs):
            raise error
        monkeypatch.setattr(LocalRouteSource, question, broken)
        configure(whole_crossing)
        labels = dict(mode="boat", source="overpass", degraded="false")
        before = metric(_RESOLVES, **labels)

        with caplog.at_level(logging.WARNING, logger="src.services.overpass_service"):
            polyline, _, degraded, _ = _compute_segment_geometry(
                _segment("boat", far[1], far[2]), {})
        assert transport.call_count == 1
        assert metric(_RESOLVES, **labels) == before + 1
        assert degraded is False
        assert _reaches(polyline, far[1], far[2])
        assert "local ferry source failed" in caplog.text
        assert type(error).__name__ in caplog.text   # the traceback is logged

    def test_an_unusable_manifest_stays_on_overpass(
            self, tmp_path, configure, monkeypatch, caplog):
        (tmp_path / MANIFEST_NAME).write_text("[1, 2, 3]", encoding="utf-8")
        configure(tmp_path)
        transport = _overpass_answer()
        monkeypatch.setattr(ov, "_overpass", transport)
        with caplog.at_level(logging.WARNING, logger="src.services.overpass_service"):
            assert ov.get_ferry_geometry(*WEST, *EAST).source == "overpass"
        assert "unusable" in caplog.text

    def test_a_ferry_entry_never_opens_a_bus_store(self, whole_crossing, configure,
                                                  monkeypatch):
        """Coverage is per layer: the ferry store here is no answer to a bus leg."""
        configure(whole_crossing)
        transport = Mock(return_value={"elements": []})
        monkeypatch.setattr(ov, "_overpass", transport)
        with pytest.raises(ov.OverpassError):
            ov.get_bus_geometry(*WEST, *EAST)
        assert LocalRouteSource(str(whole_crossing)).relations_in_bbox(
            "bus", (54.9, 10.9, 55.1, 11.5)) == []


class TestVertexCeiling:
    def test_a_refused_box_straight_lines_without_asking_overpass(
            self, tmp_path, configure, monkeypatch):
        """The ceiling bounds this worker's memory; Overpass answering the same
        box rebuilds the allocation it refused. So, as for rail, no fallback."""
        pbf = write_layer(tmp_path / "hopper.osm.pbf", {
            60: ([WEST, (55.01, 11.10), MID, EAST], {"ferry": "yes"})})
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/isles", "ferry", pbf)])
        configure(tmp_path)
        monkeypatch.setattr("src.services.rail_source._MAX_BBOX_VERTICES", 2)
        transport = Mock(name="_overpass")
        monkeypatch.setattr(ov, "_overpass", transport)

        result = ov.get_ferry_geometry(*WEST, *EAST)
        assert (result.strategy, result.degraded, result.source) == (
            "straight", True, "local")
        assert result.polyline == [[WEST[1], WEST[0]], [EAST[1], EAST[0]]]
        assert transport.call_count == 0


# ---------------------------------------------------------------------------
# The resolve counter, end to end
# ---------------------------------------------------------------------------

class TestResolveCounter:
    def test_a_local_ferry_resolve_counts_local(
            self, whole_crossing, configure, no_network, metric):
        configure(whole_crossing)
        labels = dict(mode="boat", source="local", degraded="false")
        before = metric(_RESOLVES, **labels)
        polyline, _, degraded, strategy = _compute_segment_geometry(
            _segment("boat", WEST, EAST), {})
        assert metric(_RESOLVES, **labels) == before + 1
        assert (degraded, strategy) == (False, "ferry")
        assert _reaches(polyline, WEST, EAST)

    def test_a_ferry_resolve_that_falls_back_counts_overpass(
            self, whole_crossing, configure, monkeypatch, metric):
        far = {1: (57.60, 18.30), 2: (57.60, 18.70)}
        ways = {90: ([far[1], (57.61, 18.50), far[2]], {})}
        monkeypatch.setattr(ov, "_overpass", Mock(
            return_value={"elements": [overpass_relation(900, ways, far)]}))
        configure(whole_crossing)
        labels = dict(mode="boat", source="overpass", degraded="false")
        before = metric(_RESOLVES, **labels)
        _compute_segment_geometry(_segment("boat", far[1], far[2]), {})
        assert metric(_RESOLVES, **labels) == before + 1

    def test_a_local_bus_resolve_counts_local(
            self, tmp_path, configure, no_network, metric):
        ways = {80: ([WEST, MID, EAST], {})}
        rel = (800, [("n", 1), ("w", 80), ("n", 2)], {"route": "bus"})
        pbf = write_layer(tmp_path / "bus.osm.pbf", ways, [rel], PORT_STOPS)
        write_manifest(str(tmp_path), [build_region(str(tmp_path), "test/bus", "bus", pbf)])
        configure(tmp_path)
        labels = dict(mode="bus", source="local", degraded="false")
        before = metric(_RESOLVES, **labels)
        _compute_segment_geometry(_segment("bus", WEST, EAST), {})
        assert metric(_RESOLVES, **labels) == before + 1

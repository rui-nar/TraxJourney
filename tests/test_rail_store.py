"""Local rail store — build, read, index correctness and resource bounds (#345).

The fixture is a real rail-only extract of Luxembourg, cut with the selection
table in docs/LOCAL_RAIL_DATA_PLAN.md; see ``tests/fixtures/rail/README.md``. A
synthetic extract covers the shapes a single country's mappers may simply not
have used — polygon stations, a relation whose only nearby member is a platform
— which no real extract can be relied on to contain.
"""
import os
import random
import shutil
import sqlite3
import time
import tracemalloc

import pytest

from src.rail.builder import RailBuildError, build_store
from src.rail.store import (
    RailStore,
    RailStoreCache,
    RailStoreError,
    _dist_m,
    decode_geometry,
    encode_geometry,
    store_filename,
)
from src.services.overpass_service import (
    _build_rail_graph,
    _extract_relation_geometry,
    _nearest_node,
)

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "rail", "luxembourg-rail.osm.pbf")
REGION = "europe/luxembourg"


@pytest.fixture(scope="module")
def store_path(tmp_path_factory):
    out = tmp_path_factory.mktemp("railstore") / store_filename(REGION)
    build_store(FIXTURE, out, region=REGION, source_date="2026-09-05")
    return str(out)


@pytest.fixture(scope="module")
def store(store_path):
    with RailStore(store_path) as s:
        yield s


@pytest.fixture(scope="module")
def region_graph(store):
    """The whole fixture region as ``_build_rail_graph`` sees it.

    This is the vertex set ``_nearest_node`` would scan today, so it is the
    oracle for what the index must return.
    """
    return _build_rail_graph(store.ways_in_bbox(*store.bbox))


def _write_synthetic(path):
    """An extract built to contain what Luxembourg happens not to.

    Nodes first, then ways, then relations, in id order — PBF order, which is
    what the builder's single pass assumes.
    """
    import osmium
    from osmium.osm import mutable

    w = osmium.SimpleWriter(str(path))
    # A station node whose tag carries a leading zero, and a stop node that is
    # not a station but does carry a uic_ref (strategy A matches those).
    w.add_node(mutable.Node(id=1, location=(6.10, 49.60),
                            tags={"railway": "station", "uic_ref": "8200100"}))
    w.add_node(mutable.Node(id=2, location=(6.20, 49.70),
                            tags={"railway": "stop", "uic_ref": "8200200"}))
    w.add_node(mutable.Node(id=3, location=(6.10, 49.60)))
    w.add_node(mutable.Node(id=4, location=(6.20, 49.70)))
    # Corners of a station polygon, and of a multipolygon station's ring.
    for nid, (lon, lat) in enumerate(
            [(6.30, 49.80), (6.32, 49.80), (6.32, 49.82), (6.30, 49.82)], start=5):
        w.add_node(mutable.Node(id=nid, location=(lon, lat)))
    for nid, (lon, lat) in enumerate(
            [(6.40, 49.90), (6.42, 49.90), (6.42, 49.92), (6.40, 49.92)], start=9):
        w.add_node(mutable.Node(id=nid, location=(lon, lat)))
    w.add_node(mutable.Node(id=13, location=(6.50, 50.00)))
    w.add_node(mutable.Node(id=14, location=(6.51, 50.00)))
    # A stop whose OSM tag carries a leading zero. Overpass cannot match it
    # either, because it strips only our side of the comparison.
    w.add_node(mutable.Node(id=15, location=(6.21, 49.71),
                            tags={"railway": "stop", "uic_ref": "08200500"}))

    w.add_way(mutable.Way(id=10, nodes=[3, 4], tags={"railway": "rail"}))
    w.add_way(mutable.Way(id=11, nodes=[3, 4],
                          tags={"railway": "rail", "service": "yard"}))
    w.add_way(mutable.Way(id=12, nodes=[5, 6, 7, 8, 5],
                          tags={"railway": "station", "uic_ref": "8200300"}))
    w.add_way(mutable.Way(id=13, nodes=[9, 10, 11, 12, 9]))          # station ring
    w.add_way(mutable.Way(id=14, nodes=[13, 14], tags={"railway": "platform"}))

    # Member way 99 is deliberately absent: a route relation names platforms and
    # service tracks the way filter drops, and members outside the country's
    # extent entirely (the Luxembourg fixture holds 1,925 of 10,669 referenced
    # member ways, most of the rest being across a border).
    w.add_relation(mutable.Relation(
        id=100,
        members=[("w", 10, ""), ("w", 99, ""),
                 ("n", 1, "stop"), ("n", 2, "stop"), ("n", 15, "stop")],
        tags={"route": "train", "name": "Test line"}))
    # A station mapped as a multipolygon, which _find_station_near matches and
    # `out center` answers with the centre of its bounding box.
    w.add_relation(mutable.Relation(
        id=101, members=[("w", 13, "outer")],
        tags={"railway": "halt", "uic_ref": "8200400", "type": "multipolygon"}))
    # A route relation whose only member near the platform is that platform.
    w.add_relation(mutable.Relation(
        id=102, members=[("w", 14, "")], tags={"route": "train", "name": "Platform line"}))
    w.close()


@pytest.fixture(scope="module")
def synthetic_store(tmp_path_factory):
    pbf = tmp_path_factory.mktemp("synth") / "synth-rail.osm.pbf"
    _write_synthetic(pbf)
    out = tmp_path_factory.mktemp("synthstore") / store_filename("europe/synth")
    build_store(pbf, out, region="europe/synth")
    with RailStore(out) as s:
        yield s


# ---------------------------------------------------------------------------
# Round trip: extract -> store -> the shapes the resolver already consumes
# ---------------------------------------------------------------------------

def test_store_holds_the_three_kinds_of_data_the_resolver_asks_for(store):
    assert int(store.meta["ways"]) == 1167          # track
    assert int(store.meta["member_ways"]) == 989    # geometry a relation needs
    assert int(store.meta["nodes"]) == 23_986
    assert int(store.meta["stations"]) == 66
    assert int(store.meta["relations"]) == 109
    assert store.region == REGION
    assert store.meta["source_date"] == "2026-09-05"
    min_lat, min_lon, max_lat, max_lon = store.bbox
    assert 49 < min_lat < max_lat < 51 and 5 < min_lon < max_lon < 7


def test_bbox_ways_build_the_same_graph_as_overpass_elements(store, region_graph):
    nodes, adj = region_graph
    ways = store.ways_in_bbox(*store.bbox)
    assert len(ways) == 1167
    for way in ways:
        assert len(way["geometry"]) >= 2
        assert set(way["geometry"][0]) == {"lat", "lon"}

    # The point of the shape contract: _build_rail_graph is unchanged by Phase 2.
    assert len(nodes) > 10_000
    # Every adjacency entry names a node that exists — a malformed geometry list
    # would surface here rather than deep inside a Dijkstra run.
    assert all(n in nodes for neighbours in adj.values() for n in neighbours)


def test_bbox_selection_is_bounded_by_the_box(store):
    min_lat, min_lon, max_lat, max_lon = store.bbox
    mid_lat = (min_lat + max_lat) / 2
    half = store.ways_in_bbox(min_lat, min_lon, mid_lat, max_lon)
    whole = store.ways_in_bbox(min_lat, min_lon, max_lat, max_lon)
    assert 0 < len(half) < len(whole)
    assert {w["id"] for w in half} <= {w["id"] for w in whole}
    for way in half:
        assert any(min_lat <= p["lat"] <= mid_lat for p in way["geometry"])


def test_geometry_round_trips_at_osm_precision():
    pts = [(49.6001234, 6.1339999), (-33.8688, 151.2093), (0.0, 0.0)]
    out = decode_geometry(encode_geometry(pts))
    for (lat, lon), got in zip(pts, out):
        assert got["lat"] == pytest.approx(lat, abs=1e-7)
        assert got["lon"] == pytest.approx(lon, abs=1e-7)


def test_relation_geometry_feeds_extract_relation_geometry(store):
    conn = sqlite3.connect(store.path)
    rel_id = conn.execute(
        "SELECT rel_id FROM relation_way GROUP BY rel_id "
        "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    conn.close()

    rels = store.relation_geometry([rel_id])
    assert len(rels) == 1
    rel = rels[0]
    assert rel["tags"]["route"] in {"train", "railway", "light_rail"}
    # Way members and node members share the list, as they do in Overpass's own
    # `out geom`; only the ways carry geometry and only they are counted missing.
    ways = [m for m in rel["members"] if m["type"] == "way"]
    held = [m for m in ways if m["held"]]
    assert len(held) >= 2
    assert all(len(m["geometry"]) >= 2 for m in held)
    assert all(m["geometry"] == [] for m in ways if not m["held"])
    assert rel["missing_members"] == len(ways) - len(held)

    # Not merely "does not raise": the existing strategy-A/B code path must get
    # a real polyline out of the store's relation, endpoints included.
    first, last = held[0]["geometry"][0], held[-1]["geometry"][-1]
    geom = _extract_relation_geometry(
        rel, first["lat"], first["lon"], last["lat"], last["lon"])
    assert geom is not None and len(geom) >= 2
    assert all(len(pt) == 2 for pt in geom)      # [lon, lat] pairs

    assert store.relation_geometry([-1]) == []


def test_ways_carried_only_for_a_relation_stay_out_of_rail_results(store):
    """A way present because a relation references it has geometry but is not
    track: it must never be snapped to or returned by a bbox query — while
    still being visible to the relation lookups that need it."""
    conn = sqlite3.connect(store.path)
    member_only = conn.execute("SELECT COUNT(*) FROM way WHERE cls & 1 = 0").fetchone()[0]
    indexed = conn.execute("SELECT COUNT(*) FROM way_bbox").fetchone()[0]
    rail = conn.execute("SELECT COUNT(*) FROM way WHERE cls & 1 = 1").fetchone()[0]
    conn.close()
    assert member_only > 0
    assert indexed == rail + member_only        # the R-tree holds every way
    assert len(store.ways_in_bbox(*store.bbox)) == rail


# ---------------------------------------------------------------------------
# Spatial index correctness — against the function it replaces
# ---------------------------------------------------------------------------

def test_nearest_node_returns_the_same_vertex_as_the_resolvers_scan(store, region_graph):
    """Parity, not improvement: identical vertex to ``_nearest_node`` over the
    same vertex set, including its squared-degree ordering."""
    nodes, _ = region_graph
    min_lat, min_lon, max_lat, max_lon = store.bbox
    rng = random.Random(20260906)
    checked = 0
    for _ in range(60):
        lat = rng.uniform(min_lat, max_lat)
        lon = rng.uniform(min_lon, max_lon)
        want = nodes[_nearest_node(nodes, lat, lon)]      # [lon, lat]
        got = store.nearest_node(lat, lon)
        if got is None:
            # Allowed only past the search ceiling, which _nearest_node has not.
            # The ceiling is in degrees, because the ordering is (see the store).
            assert (want[1] - lat) ** 2 + (want[0] - lon) ** 2 > (25_000 / 111_320) ** 2
            continue
        assert (got["lat"], got["lon"]) == pytest.approx((want[1], want[0]), abs=1e-7)
        checked += 1
    assert checked >= 45


def test_nearest_node_on_a_vertex_returns_that_vertex(store, region_graph):
    nodes, _ = region_graph
    coords = list(nodes.values())
    for lon, lat in coords[:: max(1, len(coords) // 20)]:
        got = store.nearest_node(lat, lon)
        assert got["lat"] == pytest.approx(lat, abs=1e-7)
        assert got["lon"] == pytest.approx(lon, abs=1e-7)


def test_nearest_node_gives_up_rather_than_snapping_across_a_continent(store):
    assert store.nearest_node(48.85, 2.35) is None            # Paris, 250 km away
    assert store.nearest_node(48.85, 2.35, max_radius_m=400_000) is not None


def test_nearest_station_picks_what_find_station_near_picks(store):
    """Parity with ``_find_station_near``, which is two rules, not one: the
    candidates are those inside a metric ``around:`` circle, and the winner
    among them is the smallest squared-degree distance.

    Ranking in metres instead moved 12.1% of jittered points onto a different
    station across Germany's 5,483 — and a different station is a different
    uic_ref, so strategy A hunts a different relation and _enrich_uic moves the
    stop's coordinates to it.
    """
    conn = sqlite3.connect(store.path)
    stations = conn.execute("SELECT lat, lon, uic FROM station").fetchall()
    conn.close()

    def oracle(lat, lon, radius_m):
        near = [s for s in stations if _dist_m(lat, lon, s[0], s[1]) <= radius_m]
        if not near:
            return None
        # _find_station_near: min over squared degrees, lat and lon alike.
        return min(near, key=lambda s: (s[0] - lat) ** 2 + (s[1] - lon) ** 2)

    rng = random.Random(7)
    checked = 0
    for _ in range(200):
        base = rng.choice(stations)
        lat = base[0] + rng.uniform(-0.05, 0.05)
        lon = base[1] + rng.uniform(-0.05, 0.05)
        want = oracle(lat, lon, 5000)
        got = store.nearest_station(lat, lon)
        if want is None:
            assert got is None
            continue
        assert got == {"lat": want[0], "lon": want[1], "uic": want[2]}
        checked += 1
    assert checked >= 100

    far_lat, far_lon = 48.85, 2.35
    assert store.nearest_station(far_lat, far_lon) is None
    assert store.nearest_station(far_lat, far_lon, radius_m=400_000) is not None


def test_relations_near_finds_a_relation_from_its_own_track(store):
    conn = sqlite3.connect(store.path)
    rel_id, geom = conn.execute(
        "SELECT rw.rel_id, w.geom FROM relation_way rw JOIN way w ON w.id = rw.way_id "
        "WHERE w.cls & 1 LIMIT 1").fetchone()
    conn.close()
    pt = decode_geometry(geom)[0]
    assert rel_id in store.relations_near(pt["lat"], pt["lon"], radius_m=1000)
    # Strategy B's intersection: a point 200 km away shares no relation with it.
    assert not store.relations_near(pt["lat"], pt["lon"], 1000) & store.relations_near(
        48.85, 2.35, 1000)


def test_relations_for_uic_pair_finds_real_relations_in_the_fixture(store):
    """Strategy A on real data — which needs the #349 contract's *any* node
    with a uic_ref, since route relations reference stop nodes, not stations."""
    conn = sqlite3.connect(store.path)
    pairs = conn.execute(
        "SELECT a.uic, b.uic, a.rel_id FROM relation_uic a JOIN relation_uic b "
        "ON a.rel_id = b.rel_id AND a.uic < b.uic").fetchall()
    conn.close()
    assert len(pairs) >= 8
    for uic1, uic2, rel_id in pairs:
        assert rel_id in store.relations_for_uic_pair(uic1, uic2)
        assert rel_id in store.relations_for_uic_pair(uic2, uic1)


# ---------------------------------------------------------------------------
# What one country's mappers happen not to have done — on a built extract
# ---------------------------------------------------------------------------

def test_stations_mapped_as_ways_and_relations_are_found_too(synthetic_store):
    """``_find_station_near`` queries node, way *and* relation. A polygon
    station that the store dropped would leave _enrich_uic with nothing, so
    strategy A could not fire and strategy C would start from the raw HAFAS
    coordinate instead of the platform."""
    store = synthetic_store
    assert int(store.meta["stations"]) == 3

    conn = sqlite3.connect(store.path)
    kinds = dict(conn.execute("SELECT osm_type, COUNT(*) FROM station GROUP BY 1"))
    conn.close()
    assert kinds == {"node": 1, "way": 1, "relation": 1}

    # Centres are the centre of the element's bounding box, as `out center` is.
    polygon = store.nearest_station(49.81, 6.31, radius_m=500)
    assert polygon == {"lat": pytest.approx(49.81), "lon": pytest.approx(6.31),
                       "uic": "8200300"}
    multipolygon = store.nearest_station(49.91, 6.41, radius_m=500)
    assert multipolygon["uic"] == "8200400"

    # A stop_position node carries a uic_ref but is not a station.
    assert store.nearest_station(49.70, 6.20, radius_m=100) is None


def test_relations_near_sees_a_relation_whose_only_member_is_a_platform(synthetic_store):
    """Overpass's ``around:`` matches any member. Indexing track alone lost
    12.6% of the strategy-B candidate set at Luxembourg Gare, and strategy B
    intersects two such sets."""
    assert synthetic_store.relations_near(50.00, 6.505, radius_m=500) == {102}
    # …while the platform is still not track.
    assert synthetic_store.nearest_node(50.00, 6.505, max_radius_m=1000) is None


def test_relations_for_uic_pair_normalises_exactly_as_overpass_does(synthetic_store):
    """Overpass strips leading zeros from *our* code and then matches the OSM
    tag verbatim. Stripping the stored tag as well would find relations Overpass
    does not — parity, not politeness."""
    store = synthetic_store
    assert store.relations_for_uic_pair("8200100", "8200200") == [100]
    # Leading zeros on *our* code are stripped, as Overpass strips them.
    assert store.relations_for_uic_pair("008200100", "0008200200") == [100]
    # Leading zeros in the *OSM tag* are not: node 15 is tagged "08200500", and
    # neither spelling of our code equals that string, so Overpass finds nothing
    # and neither do we. Widening this here would invent a match the comparison
    # period could never see.
    assert store.relations_for_uic_pair("8200100", "8200500") == []
    assert store.relations_for_uic_pair("8200100", "08200500") == []
    assert store.relations_for_uic_pair("8200100", "9999999") == []
    assert store.relations_for_uic_pair("", "8200200") == []


def test_builder_applies_the_strategy_c_way_selection(synthetic_store):
    """A ``service`` way is track, but not track a route may use — the same
    exclusion the Overpass bbox query makes. Nor is a station polygon."""
    store = synthetic_store
    assert int(store.meta["ways"]) == 1          # way 10 only
    assert int(store.meta["member_ways"]) == 4   # service, station, ring, platform
    assert [w["id"] for w in store.ways_in_bbox(49.5, 6.0, 50.1, 6.6)] == [10]


def test_a_member_way_the_extract_does_not_hold_is_reported_not_hidden(synthetic_store):
    rel = synthetic_store.relation_geometry([100])[0]
    held, unheld = [m for m in rel["members"] if m["type"] == "way"]
    assert held["ref"] == 10 and held["held"] is True and len(held["geometry"]) == 2
    assert unheld["ref"] == 99 and unheld["held"] is False and unheld["geometry"] == []
    assert rel["missing_members"] == 1
    # The relation stays usable: the existing consumer needs two points per
    # member and so ignores the gap.
    assert _extract_relation_geometry(rel, 49.60, 6.10, 49.70, 6.20)


def test_builder_refuses_an_extract_with_no_track(tmp_path):
    """A store's bbox is its track's extent, and Phase 3 selects regions by
    bbox. A trackless extract has no extent to report, so it is an error rather
    than a store that claims to cover a degenerate box."""
    import osmium
    from osmium.osm import mutable

    pbf = tmp_path / "empty-rail.osm.pbf"
    w = osmium.SimpleWriter(str(pbf))
    w.add_node(mutable.Node(id=1, location=(6.1, 49.6),
                            tags={"railway": "station", "uic_ref": "8200100"}))
    w.close()
    out = tmp_path / store_filename("europe/empty")
    with pytest.raises(RailBuildError):
        build_store(pbf, out)
    assert not os.path.exists(out)


def test_builder_counts_nodes_it_could_not_locate(tmp_path):
    """Dropping an unlocatable node welds its neighbours together, which moves
    the line. The store cannot avoid that, but it does not hide it either."""
    import osmium
    from osmium.osm import mutable

    pbf = tmp_path / "gappy-rail.osm.pbf"
    w = osmium.SimpleWriter(str(pbf))
    for nid in range(1, 201):
        w.add_node(mutable.Node(id=nid, location=(6.10 + nid / 1000, 49.60)))
    # One unlocatable node among 201 references — under the ratio that means a
    # broken extract, so it is counted and built rather than refused.
    w.add_way(mutable.Way(id=10, nodes=[*range(1, 201), 999],
                          tags={"railway": "rail"}))
    w.close()

    out = tmp_path / store_filename("europe/gappy")
    stats = build_store(pbf, out)
    assert stats["ways_missing_nodes"] == 1
    assert stats["missing_nodes"] == 1
    assert stats["ways_dropped"] == 0
    with RailStore(out) as store:
        assert len(store.ways_in_bbox(49.5, 6.0, 49.8, 6.3)[0]["geometry"]) == 200


def test_builder_refuses_an_extract_whose_nodes_were_filtered_away(tmp_path):
    """A filter run without reference completion yields a valid .pbf whose ways
    have no locations. Building from one wrote a store labelled europe/germany
    holding three ways and a bbox over Baden-Württemberg — which Phase 3 would
    select for a Hamburg trip and get None from. Fail on the way in instead."""
    import osmium
    from osmium.osm import mutable

    pbf = tmp_path / "nodeless-rail.osm.pbf"
    w = osmium.SimpleWriter(str(pbf))
    # Three ways whose nodes are locatable, then a hundred whose nodes are not:
    # geometry mostly gone, but not entirely, which the zero-ways guard misses.
    for nid in range(1, 7):
        w.add_node(mutable.Node(id=nid, location=(6.10 + nid / 100, 49.60)))
    for wid, nodes in enumerate([[1, 2], [3, 4], [5, 6]], start=10):
        w.add_way(mutable.Way(id=wid, nodes=nodes, tags={"railway": "rail"}))
    for wid in range(100, 200):
        w.add_way(mutable.Way(id=wid, nodes=[900 + wid, 901 + wid],
                              tags={"railway": "rail"}))
    w.close()

    out = tmp_path / store_filename("europe/nodeless")
    with pytest.raises(RailBuildError, match="reference completion"):
        build_store(pbf, out, region="europe/nodeless")
    assert not os.path.exists(out)


def test_station_relations_the_extract_cannot_place_are_counted(tmp_path):
    """The stations B2 exists to recover are exactly the ones a silent drop
    would hide: a zero here is indistinguishable from a country that maps no
    station as a relation."""
    import osmium
    from osmium.osm import mutable

    pbf = tmp_path / "relstations-rail.osm.pbf"
    w = osmium.SimpleWriter(str(pbf))
    w.add_node(mutable.Node(id=1, location=(6.10, 49.60)))
    w.add_node(mutable.Node(id=2, location=(6.20, 49.70)))
    # Stop nodes of a stop_area: kept by the #349 contract because of the uic_ref.
    w.add_node(mutable.Node(id=3, location=(6.30, 49.80),
                            tags={"railway": "stop", "uic_ref": "8200600"}))
    w.add_node(mutable.Node(id=4, location=(6.32, 49.82),
                            tags={"railway": "stop", "uic_ref": "8200601"}))
    w.add_way(mutable.Way(id=10, nodes=[1, 2], tags={"railway": "rail"}))
    # Located from node members alone — no way members at all.
    w.add_relation(mutable.Relation(
        id=200, members=[("n", 3, "stop"), ("n", 4, "stop")],
        tags={"type": "public_transport", "railway": "station",
              "uic_ref": "8200600"}))
    # Nothing locatable: a member way the extract does not hold…
    w.add_relation(mutable.Relation(
        id=201, members=[("w", 999, "outer")],
        tags={"railway": "station", "uic_ref": "8200700"}))
    # …and a member that is another relation.
    w.add_relation(mutable.Relation(
        id=202, members=[("r", 200, "")],
        tags={"railway": "halt", "uic_ref": "8200800"}))
    w.close()

    out = tmp_path / store_filename("europe/relstations")
    stats = build_store(pbf, out, region="europe/relstations")
    assert stats["stations"] == 1
    assert stats["stations_unlocatable"] == 2

    with RailStore(out) as store:
        # Centre of the stop nodes' bounding box, as `out center` reports.
        found = store.nearest_station(49.81, 6.31, radius_m=2000)
        assert found["uic"] == "8200600"
        assert found["lat"] == pytest.approx(49.81)
        assert found["lon"] == pytest.approx(6.31)


def test_a_station_relation_placed_from_part_of_its_members_says_so(tmp_path):
    """Where only some members are held the centre is the centre of what we
    hold, which is not what `out center` would have said."""
    import osmium
    from osmium.osm import mutable

    pbf = tmp_path / "partial-rail.osm.pbf"
    w = osmium.SimpleWriter(str(pbf))
    w.add_node(mutable.Node(id=1, location=(6.10, 49.60)))
    w.add_node(mutable.Node(id=2, location=(6.20, 49.70)))
    w.add_node(mutable.Node(id=3, location=(6.30, 49.80),
                            tags={"railway": "stop", "uic_ref": "8200900"}))
    w.add_way(mutable.Way(id=10, nodes=[1, 2], tags={"railway": "rail"}))
    w.add_relation(mutable.Relation(
        id=300, members=[("n", 3, "stop"), ("w", 999, "outer")],
        tags={"railway": "station", "uic_ref": "8200900"}))
    w.close()

    stats = build_store(pbf, tmp_path / store_filename("europe/partial"))
    assert stats["stations"] == 1
    assert stats["stations_partial"] == 1
    assert stats["stations_unlocatable"] == 0


# ---------------------------------------------------------------------------
# Resource bounds
# ---------------------------------------------------------------------------

def test_a_bbox_too_large_to_answer_raises_instead_of_allocating(store):
    """The whole-Germany box is 1,270,497 vertices, ~680 MB once built into a
    graph, on a 1 GB worker. Refusing is the only outcome that leaves the worker
    alive, and it has to be cheap — counted from blob lengths, not decoded."""
    min_lat, min_lon, max_lat, max_lon = store.bbox
    with pytest.raises(RailStoreError, match="ceiling"):
        store.ways_in_bbox(min_lat, min_lon, max_lat, max_lon, max_vertices=100)
    # Cheap: refusing a box costs an index scan, not the geometry it declined.
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    with pytest.raises(RailStoreError):
        store.ways_in_bbox(min_lat, min_lon, max_lat, max_lon, max_vertices=100)
    grew = tracemalloc.get_traced_memory()[1] - before
    tracemalloc.stop()
    assert grew < 1_000_000, f"refusing the box allocated {grew / 1e6:.1f} MB"
    # And the same box under the ceiling still answers.
    assert store.ways_in_bbox(min_lat, min_lon, max_lat, max_lon)


def test_vertex_counts_describe_the_bbox_result_without_decoding_it(store):
    """The count pass ``LocalRailSource.ways_in_bbox`` sizes the merge with.

    It has to describe *that call's* result exactly — same ways, same vertex
    counts — or the merged bound is computed over a different set than the one
    that gets allocated. And it has to be cheap, because the whole point is to
    learn the size before paying for the geometry.
    """
    min_lat, min_lon, max_lat, max_lon = store.bbox
    box = (min_lat, min_lon, (min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
    ways = store.ways_in_bbox(*box)
    assert ways, "the half-region box has to hold something to compare"

    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    counts = store.vertex_counts_in_bbox(*box)
    grew = tracemalloc.get_traced_memory()[1] - before
    tracemalloc.stop()

    assert counts == {w["id"]: len(w["geometry"]) for w in ways}
    decoded = sum(counts.values()) * 268
    assert grew < decoded / 10, (
        f"counting allocated {grew / 1e6:.1f} MB for a result that would "
        f"decode to about {decoded / 1e6:.1f} MB")


def test_a_bbox_result_costs_a_bounded_number_of_bytes_per_vertex(store):
    """The ceiling in ``_MAX_BBOX_VERTICES`` is only as good as this ratio, so
    it is measured rather than assumed: ~268 bytes per vertex today, and the
    guard is sized on that."""
    min_lat, min_lon, max_lat, max_lon = store.bbox
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    ways = store.ways_in_bbox(min_lat, min_lon, max_lat, max_lon)
    peak = tracemalloc.get_traced_memory()[1] - before
    tracemalloc.stop()
    vertices = sum(len(w["geometry"]) for w in ways)
    assert vertices > 10_000
    assert peak / vertices < 500, f"{peak / vertices:.0f} bytes per vertex"


def test_lru_evicts_without_closing_a_store_someone_is_using(tmp_path, store_path):
    regions = ["europe/a", "europe/b", "europe/c"]
    for region in regions:
        shutil.copy(store_path, tmp_path / store_filename(region))

    cache = RailStoreCache(tmp_path, max_open=2)
    first = cache.get("europe/a")
    for region in regions:
        s = cache.get(region)
        min_lat, min_lon, max_lat, max_lon = s.bbox
        s.nearest_node((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)

    assert len(cache._open) == 2
    # The evicted region is gone from the cache, and asking for it again opens a
    # new store — but the handle a caller already holds keeps working, so a
    # resolve in flight when a third region arrives does not fail.
    assert "europe/a" not in cache._open
    assert cache.get("europe/a") is not first
    assert first.nearest_node(49.6, 6.1) is not None

    assert cache.get("europe/nowhere") is None      # not covered, not an error
    cache.close_all()
    assert cache._open == {}


def test_cold_open_and_lookups_are_fast(store_path):
    """Budget check, generously set: this is the per-resolve cost Phase 3 pays.

    The spike's naive graph build cost 8.41 s for Germany and 1.43 s per snap
    before answering anything. Opening a store touches no geometry at all, so
    anything approaching a second here means something is scanning what the
    index should have narrowed.
    """
    t0 = time.monotonic()
    store = RailStore(store_path)
    min_lat, min_lon, max_lat, max_lon = store.bbox
    load = time.monotonic() - t0

    mid_lat, mid_lon = (min_lat + max_lat) / 2, (min_lon + max_lon) / 2
    t0 = time.monotonic()
    for _ in range(10):
        store.nearest_node(mid_lat, mid_lon)
        store.nearest_station(mid_lat, mid_lon, radius_m=50_000)
    lookups = time.monotonic() - t0
    store.close()

    assert load < 1.0, f"cold open took {load:.2f}s"
    assert lookups < 2.0, f"20 point lookups took {lookups:.2f}s"


def test_reader_refuses_a_store_it_does_not_understand(tmp_path, store_path):
    path = tmp_path / store_filename("europe/wrong-schema")
    shutil.copy(store_path, path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RailStoreError):
        RailStore(path)
    with pytest.raises(RailStoreError):
        RailStore(tmp_path / "does-not-exist.rail.sqlite")


def test_store_is_read_only(store):
    with pytest.raises(sqlite3.OperationalError):
        store._conn.execute("DELETE FROM way")

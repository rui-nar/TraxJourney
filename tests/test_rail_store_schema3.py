"""Store schema 3: `way.cls`, layers, and reading a schema 1 or 2 file (#345, U8).

Schema 3 replaces the one `way.rail` flag with a class bitmask
(docs/LOCAL_TRANSPORT_DATA_PLAN.md, Decision 9) so the same store format can
hold the ferry and bus layers and answer each resolver strategy's own
selection: bit 0 the layer's routable class (strategy B), bit 1 `ferry=yes`
(strategy C), bit 2 a member of one of the layer's route relations.

Three things are under test, in the order they matter:

* **rail answers do not move.** v1, v2 and v3 rail stores answer the same rail
  queries, and a rail store's extent is still its track's;
* each layer's store holds its own selection, and is refused or measured over
  its **routable set**, not over bit 0 alone — or the bus layer, mapped as
  relations over ordinary roads, would be refused everywhere;
* the names: rail stores keep theirs.
"""
import os
import sqlite3

import osmium
import pytest
from osmium.osm import mutable

from src.rail.builder import RailBuildError, build_store
from src.rail.store import (
    CLS_FERRY_YES,
    CLS_MEMBER,
    CLS_ROUTE,
    ROUTABLE,
    SCHEMA_VERSION,
    _SUPPORTED_SCHEMAS,
    RailStore,
    RailStoreCache,
    store_filename,
)
from tests.test_rail_store_schema2 import _downgrade_to_schema_1, _downgrade_to_schema_2

LUXEMBOURG = os.path.join(
    os.path.dirname(__file__), "fixtures", "rail", "luxembourg-rail.osm.pbf")


def write_extract(path, nodes, ways=(), relations=()):
    """A filtered extract: *nodes* {id: (lat, lon)}, *ways* [(id, [node ids], tags)],
    *relations* [(id, [(type, ref, role)], tags)]. Written in PBF order."""
    writer = osmium.SimpleWriter(str(path))
    for node_id, (lat, lon) in sorted(nodes.items()):
        writer.add_node(mutable.Node(id=node_id, location=(lon, lat)))
    for way_id, refs, tags in ways:
        writer.add_way(mutable.Way(id=way_id, nodes=refs, tags=tags))
    for rel_id, members, tags in relations:
        writer.add_relation(mutable.Relation(id=rel_id, members=members, tags=tags))
    writer.close()
    return path


# Ferry: one way in both B's and C's selection, one in C's alone, one in B's
# alone, and an untagged way present only as the relation's member.
FERRY_NODES = {
    1: (54.50, 11.20), 2: (54.60, 11.30),     # way 10: route=ferry + ferry=yes
    3: (55.00, 12.00), 4: (55.10, 12.10),     # way 11: ferry=yes only
    5: (56.00, 10.00), 6: (56.10, 10.10),     # way 12: route=ferry only
    7: (57.00, 9.00), 8: (57.10, 9.10),       # way 13: relation member only
}


def write_ferry_extract(path):
    return write_extract(path, FERRY_NODES, ways=[
        (10, [1, 2], {"route": "ferry", "ferry": "yes"}),
        (11, [3, 4], {"ferry": "yes"}),
        (12, [5, 6], {"route": "ferry"}),
        (13, [7, 8], {}),
    ], relations=[
        (100, [("w", 10, ""), ("w", 13, "")], {"route": "ferry", "name": "Crossing"}),
    ])


def write_ferry_yes_only_extract(path):
    """A region whose ferries are all mapped as `ferry=yes` ways — common on
    island hoppers — with no route=ferry way or relation at all."""
    return write_extract(path, {1: (43.50, 16.40), 2: (43.40, 16.50),
                                3: (43.30, 16.60), 4: (43.20, 16.70)}, ways=[
        (20, [1, 2], {"ferry": "yes"}),
        (21, [3, 4], {"ferry": "yes"}),
    ])


def write_bus_members_only_extract(path):
    """Bus routes as OSM maps them: a route=bus relation over ordinary roads, and
    no `route=bus` way anywhere. Way 32 is in the file and in no relation."""
    return write_extract(path, {1: (55.60, 12.50), 2: (55.70, 12.60),
                                3: (55.80, 12.70), 4: (56.40, 13.40),
                                5: (56.50, 13.50)}, ways=[
        (30, [1, 2], {"highway": "primary"}),
        (31, [2, 3], {"highway": "secondary"}),
        (32, [4, 5], {"highway": "residential"}),
    ], relations=[
        (300, [("w", 30, ""), ("w", 31, ""), ("w", 99, "")],
         {"route": "bus", "name": "Line 1"}),
    ])


def _build(tmp_path, writer, layer, region="europe/test"):
    pbf = writer(tmp_path / f"{layer}.osm.pbf")
    out = tmp_path / store_filename(region, layer)
    stats = build_store(pbf, out, region=region, layer=layer)
    return out, stats


def _ids(ways):
    return sorted(w["id"] for w in ways)


WORLD = (-90.0, -180.0, 90.0, 180.0)


# ---------------------------------------------------------------------------
# The class bits
# ---------------------------------------------------------------------------

def test_each_strategy_gets_exactly_its_own_selection(tmp_path):
    """R1-1: a way tagged both is in both answers, a `ferry=yes`-only way in C's
    alone — exactly what `way["route"="ferry"]` and `way["ferry"="yes"]` return."""
    out, _ = _build(tmp_path, write_ferry_extract, "ferry")
    with RailStore(out) as store:
        assert store.layer == "ferry"
        assert _ids(store.ways_in_bbox(*WORLD, cls_mask=CLS_ROUTE)) == [10, 12]
        assert _ids(store.ways_in_bbox(*WORLD, cls_mask=CLS_FERRY_YES)) == [10, 11]
        assert _ids(store.ways_in_bbox(*WORLD, cls_mask=CLS_MEMBER)) == [10, 13]
        # The default is bit 0, as every rail caller expects.
        assert _ids(store.ways_in_bbox(*WORLD)) == [10, 12]
        # A mask of several bits is the union, which is what the routable set is.
        assert _ids(store.ways_in_bbox(*WORLD, cls_mask=ROUTABLE["ferry"])) == [
            10, 11, 12, 13]
        # The count pass agrees with the decode pass for every mask.
        for mask in (CLS_ROUTE, CLS_FERRY_YES, CLS_MEMBER, ROUTABLE["ferry"]):
            assert store.vertex_counts_in_bbox(*WORLD, cls_mask=mask) == {
                w["id"]: len(w["geometry"])
                for w in store.ways_in_bbox(*WORLD, cls_mask=mask)}


def test_the_bits_are_stored_as_the_decision_defines_them(tmp_path):
    out, _ = _build(tmp_path, write_ferry_extract, "ferry")
    conn = sqlite3.connect(out)
    cls = dict(conn.execute("SELECT id, cls FROM way"))
    conn.close()
    assert cls == {
        10: CLS_ROUTE | CLS_FERRY_YES | CLS_MEMBER,
        11: CLS_FERRY_YES,
        12: CLS_ROUTE,
        13: CLS_MEMBER,
    }


def test_the_relation_is_kept_with_its_members(tmp_path):
    out, _ = _build(tmp_path, write_ferry_extract, "ferry")
    with RailStore(out) as store:
        rel = store.relation_geometry([100])[0]
        assert rel["tags"] == {"route": "ferry", "name": "Crossing"}
        assert [m["ref"] for m in rel["members"]] == [10, 13]
        assert rel["missing_members"] == 0


# ---------------------------------------------------------------------------
# The routable set — what a store is refused on and measured over
# ---------------------------------------------------------------------------

def test_a_ferry_store_of_only_ferry_yes_ways_is_built_with_an_extent(tmp_path):
    """Bit 0 alone would refuse it, and every island hopper with it."""
    out, stats = _build(tmp_path, write_ferry_yes_only_extract, "ferry")
    assert stats["ways"] == 0                 # no route=ferry way
    assert stats["routable_ways"] == 2
    with RailStore(out) as store:
        assert store.bbox == pytest.approx((43.20, 16.40, 43.50, 16.70))


def test_a_bus_store_of_only_relation_members_is_built_with_an_extent(tmp_path):
    """Bus routes are relations over roads; `route=bus` ways are rare. The
    extent is the members', and a road in the file that no route uses does not
    widen it."""
    out, stats = _build(tmp_path, write_bus_members_only_extract, "bus")
    assert stats["ways"] == 0
    assert stats["routable_ways"] == 2        # 30 and 31, not 32
    with RailStore(out) as store:
        assert store.layer == "bus"
        assert store.bbox == pytest.approx((55.60, 12.50, 55.80, 12.70))
        assert _ids(store.ways_in_bbox(*WORLD, cls_mask=ROUTABLE["bus"])) == [30, 31]
        assert store.relation_geometry([300])[0]["missing_members"] == 1


def test_a_layer_with_nothing_routable_is_refused(tmp_path):
    def roads_only(path):
        return write_extract(path, {1: (55.6, 12.5), 2: (55.7, 12.6)},
                             ways=[(30, [1, 2], {"highway": "primary"})])

    with pytest.raises(RailBuildError, match="routable set"):
        _build(tmp_path, roads_only, "bus")
    assert not (tmp_path / store_filename("europe/test", "bus")).exists()


def test_a_bus_relation_does_not_count_for_the_ferry_layer(tmp_path):
    """Each layer keeps its own route relations, so bit 2 means *this* layer's."""
    with pytest.raises(RailBuildError):
        _build(tmp_path, write_bus_members_only_extract, "ferry")


def test_a_rail_store_is_measured_over_its_track_alone(tmp_path):
    """Rail's routable set is bit 0, as before layers: a platform member far
    from the track gets bit 2 but neither widens the extent nor becomes track."""
    def track_and_platform(path):
        return write_extract(path, {1: (49.60, 6.10), 2: (49.70, 6.20),
                                    3: (50.50, 7.00), 4: (50.51, 7.01)}, ways=[
            (10, [1, 2], {"railway": "rail"}),
            (11, [3, 4], {"railway": "platform"}),
        ], relations=[
            (100, [("w", 11, "platform"), ("w", 10, "")], {"route": "train"}),
        ])

    out, stats = _build(tmp_path, track_and_platform, "rail")
    assert stats["ways"] == stats["routable_ways"] == 1
    with RailStore(out) as store:
        assert store.bbox == pytest.approx((49.60, 6.10, 49.70, 6.20))
        assert _ids(store.ways_in_bbox(*WORLD)) == [10]
        assert _ids(store.ways_in_bbox(*WORLD, cls_mask=CLS_MEMBER)) == [10, 11]
        assert store.nearest_node(50.505, 7.005, max_radius_m=1000) is None


def test_an_unknown_layer_is_refused_before_anything_is_written(tmp_path):
    pbf = write_ferry_extract(tmp_path / "x.osm.pbf")
    with pytest.raises(RailBuildError, match="unknown layer"):
        build_store(pbf, tmp_path / "x.sqlite", layer="tram")
    assert not (tmp_path / "x.sqlite").exists()


def test_the_builder_cli_takes_a_layer(tmp_path):
    from src.rail.builder import main

    pbf = write_ferry_yes_only_extract(tmp_path / "f.osm.pbf")
    out = tmp_path / "f.sqlite"
    assert main([str(pbf), str(out), "--region", "europe/croatia", "--layer", "ferry"]) == 0
    with RailStore(out) as store:
        assert store.layer == "ferry"
    with pytest.raises(SystemExit):
        main([str(pbf), str(out), "--layer", "tram"])


# ---------------------------------------------------------------------------
# Rail answers do not move — v1, v2 and v3
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def three_versions(tmp_path_factory):
    """The Luxembourg fixture as a v3 store, and the same data as v2 and v1."""
    d = tmp_path_factory.mktemp("versions")
    v3 = d / "v3.rail.sqlite"
    build_store(LUXEMBOURG, v3, region="europe/luxembourg", source_date="2026-09-05")
    v2 = _downgrade_to_schema_2(str(v3), str(d / "v2.rail.sqlite"))
    v1 = _downgrade_to_schema_1(str(v3), str(d / "v1.rail.sqlite"))
    stores = {3: RailStore(v3), 2: RailStore(v2), 1: RailStore(v1)}
    yield stores
    for store in stores.values():
        store.close()


def test_the_downgraded_files_are_what_the_old_builders_wrote(three_versions):
    for version, store in three_versions.items():
        assert store.schema == version
        conn = sqlite3.connect(store.path)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(way)")]
        conn.close()
        assert columns == (["id", "cls", "geom"] if version == 3 else ["id", "rail", "geom"])
        assert store.layer == "rail"


def test_v1_v2_and_v3_rail_stores_answer_the_same_rail_queries(three_versions):
    v3 = three_versions[3]
    min_lat, min_lon, max_lat, max_lon = v3.bbox
    mid_lat, mid_lon = (min_lat + max_lat) / 2, (min_lon + max_lon) / 2
    boxes = [v3.bbox, (min_lat, min_lon, mid_lat, mid_lon),
             (mid_lat, mid_lon, max_lat, max_lon), (49.58, 6.10, 49.62, 6.16)]
    points = [(49.6, 6.13), (mid_lat, mid_lon), (49.8, 5.95), (50.1, 6.1)]

    def answers(store):
        return {
            "bbox": store.bbox,
            "ways": [store.ways_in_bbox(*box) for box in boxes],
            "counts": [store.vertex_counts_in_bbox(*box) for box in boxes],
            "nearest_node": [store.nearest_node(*p) for p in points],
            "nearest_station": [store.nearest_station(*p, radius_m=10_000) for p in points],
            "relations_near": [store.relations_near(*p) for p in points],
            "uic_pair": store.relations_for_uic_pair("8200100", "8200710"),
        }

    want = answers(v3)
    assert want["ways"][0] and want["uic_pair"]
    assert answers(three_versions[2]) == want
    assert answers(three_versions[1]) == want


def test_a_v1_or_v2_store_has_no_other_class(three_versions):
    """The old flag is bit 0 and nothing else: a ferry question gets nothing."""
    for version in (1, 2):
        store = three_versions[version]
        assert store.ways_in_bbox(*WORLD, cls_mask=CLS_FERRY_YES) == []
        assert store.ways_in_bbox(*WORLD, cls_mask=CLS_MEMBER) == []
        assert store.vertex_counts_in_bbox(*WORLD, cls_mask=CLS_FERRY_YES) == {}


def test_the_rail_store_holds_what_schema_2_held(three_versions):
    """Same track count, member count and extent as the v2 builder wrote — the
    meta keys every report since Phase 2 has quoted."""
    meta = three_versions[3].meta
    assert int(meta["ways"]) == 1167
    assert int(meta["member_ways"]) == 989
    assert int(meta["routable_ways"]) == 1167
    assert meta["layer"] == "rail"


def test_the_supported_set_spans_every_schema_a_box_can_hold():
    assert SCHEMA_VERSION == 3
    assert _SUPPORTED_SCHEMAS == (1, 2, 3)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_rail_store_names_are_unchanged_and_layers_sit_beside_them():
    assert store_filename("europe/germany") == "europe-germany.rail.sqlite"
    assert store_filename("europe/germany", "rail") == "europe-germany.rail.sqlite"
    assert store_filename("europe/denmark", "ferry") == "europe-denmark.ferry.sqlite"
    assert store_filename("europe/denmark", "bus") == "europe-denmark.bus.sqlite"
    with pytest.raises(ValueError, match="layer"):
        store_filename("europe/denmark", "../tram")


def test_a_layer_cache_opens_its_own_layers_files(tmp_path):
    out, _ = _build(tmp_path, write_ferry_extract, "ferry", region="europe/denmark")
    assert not (tmp_path / store_filename("europe/denmark")).exists()

    assert RailStoreCache(tmp_path).get("europe/denmark") is None
    ferry = RailStoreCache(tmp_path, layer="ferry").get("europe/denmark")
    assert ferry is not None and ferry.path == str(out) and ferry.layer == "ferry"
    ferry.close()

"""Rail geometry read from local region stores (#345, Phase 3).

The phase substitutes the data source and changes nothing else, so the central
test here is parity: each strategy, run against a store built from the fixture
extract, returns the *same polyline* it returns against an Overpass response
carrying the same data. ``FixtureOverpass`` is that response — it answers the
resolver's real queries out of the fixture's tables with Overpass's own
semantics (a metric ``around:`` circle, a way selected by having a node inside
the bounding box), deliberately not by calling the reader under test.

Stores are real, built by ``src.rail.builder`` from real and synthetic extracts.
Only the network is mocked, because the network is the only thing this phase is
allowed to stop using.
"""
import json
import logging
import math
import os
import re
import sqlite3
from unittest.mock import Mock, patch

import pytest

import src.rail.store as rail_store
from src.rail.builder import build_store
from src.rail.store import RailStore, decode_geometry, store_filename
from src.services import overpass_service as ov
from src.services.rail_source import (
    LocalRailSource,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    MANIFEST_SCHEMAS,
    RailSourceError,
    RailSourceOverload,
    load_coverage,
)

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "rail", "luxembourg-rail.osm.pbf")
REGION = "europe/luxembourg"

# Real Luxembourg stations from the fixture. The first pair shares four route
# relations, which is what strategy A looks for; the third is far enough away
# for strategies B and C to have something to route.
LUX_GARE = {"lat": 49.5999681, "lon": 6.1342493, "uic": "8200100"}
HOLLERICH = {"lat": 49.5956799, "lon": 6.1201184, "uic": "8200710"}
KLEINBETTINGEN = {"lat": 49.6385071, "lon": 5.9823816, "uic": "8200518"}

_M_PER_DEG_LAT = 111_320.0


def _dist_m(lat1, lon1, lat2, lon2):
    dlat = (lat1 - lat2) * _M_PER_DEG_LAT
    dlon = (lon1 - lon2) * _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


# ---------------------------------------------------------------------------
# Building the data a test needs
# ---------------------------------------------------------------------------

def write_manifest(directory, entries, schema=MANIFEST_SCHEMA):
    with open(os.path.join(directory, MANIFEST_NAME), "w", encoding="utf-8") as handle:
        json.dump({"schema": schema, "generated_at": "2026-09-06T18:00:00Z",
                   "regions": entries}, handle)


def ok_entry(region, bbox):
    """A published region. *bbox* is the store's (min_lat, min_lon, max_lat, max_lon).

    The manifest records it the other way round — Phase 1 writes
    ``[min_lon, min_lat, max_lon, max_lat]`` — and the transposition is a thing
    that can silently be got wrong, so the tests state it here once.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    return {"region": region, "status": "ok", "file": "x-rail.osm.pbf",
            "source": "https://example.invalid", "source_date": "2026-09-05",
            "sha256": "0" * 64, "bytes": 1, "ways": 1, "relations": 1, "stations": 1,
            "bbox": [min_lon, min_lat, max_lon, max_lat]}


def build_region(directory, region, pbf):
    """Build *pbf* into *directory* as *region*, returning its store bbox."""
    path = os.path.join(directory, store_filename(region))
    build_store(pbf, path, region=region, source_date="2026-09-05")
    conn = sqlite3.connect(path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()
    return tuple(float(meta[k]) for k in ("min_lat", "min_lon", "max_lat", "max_lon"))


def write_extract(path, ways, stations=(), relations=()):
    """A minimal rail extract.

    *ways* maps way id to [(lat, lon), …]; *stations* is [(lat, lon, uic)];
    *relations* is [(rel_id, [member way ids])] — member ids the file does not
    hold are allowed, and are how a cross-border relation is expressed.
    """
    import osmium
    from osmium.osm import mutable

    writer = osmium.SimpleWriter(str(path))
    node_id = 1
    way_nodes = {}
    for way_id, points in ways.items():
        refs = []
        for lat, lon in points:
            writer.add_node(mutable.Node(id=node_id, location=(lon, lat)))
            refs.append(node_id)
            node_id += 1
        way_nodes[way_id] = refs
    for lat, lon, uic in stations:
        writer.add_node(mutable.Node(
            id=node_id, location=(lon, lat),
            tags={"railway": "station", "uic_ref": uic}))
        node_id += 1
    for way_id, refs in way_nodes.items():
        writer.add_way(mutable.Way(id=way_id, nodes=refs, tags={"railway": "rail"}))
    for rel_id, members in relations:
        writer.add_relation(mutable.Relation(
            id=rel_id, members=[("w", m, "") for m in members],
            tags={"route": "train", "name": f"Relation {rel_id}"}))
    writer.close()


# ---------------------------------------------------------------------------
# The oracle: what Overpass would answer, given this extract's data
# ---------------------------------------------------------------------------

class FixtureOverpass:
    """A stand-in for ``_overpass`` that answers out of a store's tables.

    It reads the tables with its own SQL and its own geometry maths rather than
    through ``RailStore``, so a bug in the reader or in the region merging shows
    up as a difference rather than cancelling out on both sides. Overpass's
    semantics, not the store's: ``around:`` is a circle in metres, and a
    bounding box selects a way that has a *node* inside it, where the store
    accepts any way whose extent overlaps.
    """

    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.queries = []

    def __call__(self, query, purpose):
        self.queries.append(query)
        if '"railway"~"^(station|halt)$"' in query:
            return {"elements": self._stations(*self._around(query))}
        if 'node["uic_ref"="' in query:
            uic1, uic2 = re.findall(r'node\["uic_ref"="([^"]*)"\]', query)
            return {"elements": self._relations(self._pair_ids(uic1, uic2))}
        if "out ids" in query:
            return {"elements": [{"type": "relation", "id": rel_id}
                                 for rel_id in self._near_ids(*self._around(query))]}
        if "rel(id:" in query:
            ids = [int(i) for i in
                   re.search(r"rel\(id:([\d,]+)\)", query).group(1).split(",")]
            return {"elements": self._relations(ids)}
        if 'way["railway"' in query:
            box = [float(v) for v in
                   re.search(r"\(([-\d.,]+)\);", query).group(1).split(",")]
            return {"elements": self._ways(*box)}
        raise AssertionError(f"unrecognised query: {query}")

    @staticmethod
    def _around(query):
        radius, lat, lon = re.search(
            r"around:([\d.]+),([-\d.]+),([-\d.]+)", query).groups()
        return float(radius), float(lat), float(lon)

    def _stations(self, radius, lat, lon):
        out = []
        for osm_type, slat, slon, uic in self.conn.execute(
                "SELECT osm_type, lat, lon, uic FROM station"):
            if _dist_m(lat, lon, slat, slon) > radius:
                continue
            element = {"type": osm_type, "tags": {"uic_ref": uic}}
            if osm_type == "node":
                element.update(lat=slat, lon=slon)
            else:
                element["center"] = {"lat": slat, "lon": slon}
            out.append(element)
        return out

    def _pair_ids(self, uic1, uic2):
        return [row[0] for row in self.conn.execute(
            "SELECT a.rel_id FROM relation_uic a JOIN relation_uic b "
            "ON a.rel_id = b.rel_id WHERE a.uic = ? AND b.uic = ? ORDER BY a.rel_id",
            (uic1, uic2))]

    def _near_ids(self, radius, lat, lon):
        found = set()
        for rel_id, geom in self.conn.execute(
                "SELECT rw.rel_id, w.geom FROM relation_way rw "
                "JOIN way w ON w.id = rw.way_id"):
            if rel_id in found:
                continue
            if any(_dist_m(lat, lon, p["lat"], p["lon"]) <= radius
                   for p in decode_geometry(geom)):
                found.add(rel_id)
        return sorted(found)

    def _relations(self, rel_ids):
        out = []
        for rel_id in rel_ids:
            row = self.conn.execute(
                "SELECT route, name FROM relation WHERE id = ?", (rel_id,)).fetchone()
            if row is None:
                continue
            members = [
                {"type": "way", "ref": way_id,
                 "geometry": decode_geometry(geom) if geom is not None else []}
                for way_id, geom in self.conn.execute(
                    "SELECT rw.way_id, w.geom FROM relation_way rw LEFT JOIN way w "
                    "ON w.id = rw.way_id WHERE rw.rel_id = ? ORDER BY rw.seq", (rel_id,))
            ]
            # Node members, as `out geom` returns them: ref, role and a location
            # for every one of them. Overpass locates them all; the store can
            # only locate the nodes its extract kept, and that difference is
            # exactly what the parity tests exist to expose rather than hide.
            members += [
                {"type": "node", "ref": node_id, "role": role,
                 "lat": lat, "lon": lon}
                for node_id, role, lat, lon in self.conn.execute(
                    "SELECT node_id, role, lat, lon FROM relation_node "
                    "WHERE rel_id = ? ORDER BY seq", (rel_id,))
                if lat is not None
            ]
            tags = {"route": row[0]}
            if row[1]:
                tags["name"] = row[1]
            out.append({"type": "relation", "id": rel_id, "tags": tags,
                        "members": members})
        return out

    def _ways(self, min_lat, min_lon, max_lat, max_lon):
        out = []
        for way_id, geom in self.conn.execute(
                "SELECT id, geom FROM way WHERE cls & 1 ORDER BY id"):
            points = decode_geometry(geom)
            if any(min_lat <= p["lat"] <= max_lat and min_lon <= p["lon"] <= max_lon
                   for p in points):
                out.append({"type": "way", "id": way_id, "geometry": points})
        return out


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _forget_configured_source():
    """The configured source is cached per directory; each test gets its own."""
    ov._local_source = None
    yield
    ov._local_source = None


@pytest.fixture(scope="module")
def lux_dir(tmp_path_factory):
    """A data directory holding the Luxembourg fixture as a built region."""
    directory = str(tmp_path_factory.mktemp("raildata"))
    bbox = build_region(directory, REGION, FIXTURE)
    write_manifest(directory, [ok_entry(REGION, bbox)])
    return directory


@pytest.fixture(scope="module")
def local(lux_dir):
    return LocalRailSource(lux_dir)


@pytest.fixture
def oracle(lux_dir):
    return FixtureOverpass(os.path.join(lux_dir, store_filename(REGION)))


# ---------------------------------------------------------------------------
# Parity — the point of the phase
# ---------------------------------------------------------------------------

class TestSameGeometryAsOverpass:
    """Same data, same polyline, whichever source it came through."""

    def test_station_lookup(self, local, oracle):
        with patch.object(ov, "_overpass", side_effect=oracle):
            over = ov._find_station_near(49.601, 6.130)
        assert over is not None
        assert ov._find_station_near(49.601, 6.130, source=local) == over

    def test_strategy_a_uic_relations(self, local, oracle):
        stops = [LUX_GARE, HOLLERICH]
        with patch.object(ov, "_overpass", side_effect=oracle):
            over = ov._via_route_relations(stops)
        assert len(over) > 10          # a real path, not a two-point stub
        assert ov._via_route_relations(stops, local) == over

    def test_strategy_b_endpoint_relations(self, local, oracle):
        stops = [LUX_GARE, KLEINBETTINGEN]
        with patch.object(ov, "_overpass", side_effect=oracle):
            over = ov._via_train_relations_endpoints(stops)
        assert len(over) > 10
        assert ov._via_train_relations_endpoints(stops, local) == over

    def test_strategy_c_coordinate_dijkstra(self, local, oracle):
        stops = [LUX_GARE, KLEINBETTINGEN]
        with patch.object(ov, "_overpass", side_effect=oracle):
            over = ov._via_coordinate_fallback(stops)
        assert len(over) > 10
        assert ov._via_coordinate_fallback(stops, local) == over

    def test_whole_resolve(self, lux_dir, oracle, monkeypatch):
        """End to end, through get_rail_geometry's own strategy selection."""
        stops = [dict(LUX_GARE), dict(KLEINBETTINGEN)]
        with patch.object(ov, "_overpass", side_effect=oracle):
            over = ov.get_rail_geometry(stops)

        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", lux_dir)
        transport = Mock(name="_overpass")
        with patch.object(ov, "_overpass", transport):
            locally = ov.get_rail_geometry(stops)

        assert not over.degraded
        assert (locally.polyline, locally.strategy, locally.degraded) == (
            over.polyline, over.strategy, over.degraded)
        assert transport.call_count == 0, "a local hit must not touch the network"

    def test_a_local_hit_opens_no_socket(self, lux_dir, monkeypatch):
        """The same claim, one level below the transport mock.

        `_overpass` is ours to mock, so mocking it proves only that we did not
        call the function we know about. Refusing `socket.socket` outright is the
        claim the issue actually rests on: with the local source configured, a
        resolve inside a European region makes no network call by any route.
        """
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", lux_dir)
        stops = [dict(LUX_GARE), dict(KLEINBETTINGEN)]

        def _no_sockets(*args, **kwargs):
            raise AssertionError("the local source opened a socket")

        with patch("socket.socket", _no_sockets):
            result = ov.get_rail_geometry(stops)
        assert not result.degraded


# ---------------------------------------------------------------------------
# Overlapping regions — the data picks the region, not the geometry
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def two_regions(tmp_path_factory):
    """Two regions whose boxes both cover the same points.

    Both hold a line spanning the same longitudes, so neither box can be
    preferred over the other by geometry. What differs is the data: each has its
    own station, and way 100 is in both files — as a real border way is, because
    Geofabrik's extracts overlap.
    """
    directory = str(tmp_path_factory.mktemp("tworegions"))
    shared = {100: [(49.60, 5.90), (49.60, 6.30)]}
    west = tmp_path_factory.mktemp("west") / "west-rail.osm.pbf"
    east = tmp_path_factory.mktemp("east") / "east-rail.osm.pbf"
    # The two stations are 2.9 km apart — inside each other's 5 km search
    # radius, so both regions really do answer and the nearer one has to win on
    # distance rather than on which region happened to be read first.
    write_extract(west, {**shared, 101: [(49.61, 6.00), (49.61, 6.02)]},
                  stations=[(49.600, 6.010, "1000")])
    write_extract(east, {**shared, 102: [(49.61, 6.09), (49.61, 6.11)]},
                  stations=[(49.600, 6.050, "2000")])
    entries = [ok_entry("test/west", build_region(directory, "test/west", west)),
               ok_entry("test/east", build_region(directory, "test/east", east))]
    write_manifest(directory, entries)
    return directory


class TestOverlappingRegions:
    def test_the_store_cache_holds_every_candidate_region(self, tmp_path_factory):
        """A resolve asks the same regions five times; they open once.

        Luxembourg City falls inside four configured regions' boxes, and a
        resolve puts several questions to each of them — two relation lookups,
        a relation-geometry fetch, a bbox query. A cache bound below the
        candidate count evicts on every open, so it caches nothing at all in
        exactly the case this source calls common.
        """
        directory = str(tmp_path_factory.mktemp("threeregions"))
        entries = []
        for i, region in enumerate(["test/a", "test/b", "test/c"]):
            pbf = tmp_path_factory.mktemp(f"src{i}") / "r-rail.osm.pbf"
            write_extract(pbf, {200 + i: [(49.60, 6.00), (49.60, 6.30)]})
            entries.append(ok_entry(region, build_region(directory, region, pbf)))
        write_manifest(directory, entries)
        source = LocalRailSource(directory)

        box = (49.60, 6.10, 49.60, 6.10)
        assert len(source.regions_for(box)) == 3
        # References are kept, so a store that was evicted and reopened is a
        # different object rather than a recycled address.
        first = list(source._stores_for(box))
        second = list(source._stores_for(box))
        assert [a is b for a, b in zip(first, second)] == [True, True, True]

    def test_both_regions_are_candidates(self, two_regions):
        source = LocalRailSource(two_regions)
        assert source.regions_for((49.60, 6.02, 49.60, 6.02)) == [
            "test/east", "test/west"]

    def test_nearest_station_comes_from_whichever_region_holds_it(self, two_regions):
        source = LocalRailSource(two_regions)
        # Same pair of candidate regions both times, and both regions answer
        # both times; only which answer is nearer differs.
        assert source.nearest_station(49.600, 6.020)["uic"] == "1000"
        assert source.nearest_station(49.600, 6.045)["uic"] == "2000"

    def test_bbox_query_merges_both_regions_and_deduplicates_by_way_id(
            self, two_regions):
        source = LocalRailSource(two_regions)
        ways = source.ways_in_bbox(49.5, 5.8, 49.7, 6.4)
        assert [w["id"] for w in ways] == [100, 101, 102], (
            "a cross-border box must see both regions' track, and the way both "
            "extracts hold exactly once")
        # The shared way keeps its geometry rather than being merged into a stub.
        assert ways[0]["geometry"] == [{"lat": 49.60, "lon": 5.90},
                                       {"lat": 49.60, "lon": 6.30}]

    def test_a_relation_is_reassembled_from_both_sides_of_the_border(
            self, tmp_path_factory):
        """The gap Phase 1 cannot close from one file: a relation's member ways
        live in whichever country they run through. Overpass returns all of
        them, so the local source has to put the halves back together."""
        directory = str(tmp_path_factory.mktemp("crossborder"))
        west = tmp_path_factory.mktemp("cbw") / "west-rail.osm.pbf"
        east = tmp_path_factory.mktemp("cbe") / "east-rail.osm.pbf"
        # 500 crosses the border and is in both files. 501 and 502 run on one
        # side each, so neither region's answer alone is the whole answer.
        write_extract(west, {10: [(49.60, 6.00), (49.60, 6.05)]},
                      relations=[(500, [10, 11]), (502, [10])])
        write_extract(east, {11: [(49.60, 6.05), (49.60, 6.10)]},
                      relations=[(500, [10, 11]), (501, [11])])
        entries = [ok_entry("test/west", build_region(directory, "test/west", west)),
                   ok_entry("test/east", build_region(directory, "test/east", east))]
        write_manifest(directory, entries)
        source = LocalRailSource(directory)

        # Strategy B intersects the two endpoints' sets, so a set from one
        # region only would drop a relation at exactly the border.
        assert source.relations_near(49.60, 6.05) == {500, 501, 502}

        relations = source.relation_geometry([500], [(49.60, 6.00), (49.60, 6.10)])
        assert len(relations) == 1
        assert relations[0]["missing_members"] == 0
        assert [m["ref"] for m in relations[0]["members"]] == [10, 11]
        assert all(len(m["geometry"]) == 2 for m in relations[0]["members"])

    def test_the_far_endpoint_is_what_brings_its_region_into_scope(
            self, tmp_path_factory):
        """Every endpoint widens the region scope, not just the first.

        The regions here are 1.1 degrees apart — a real cross-border segment,
        and further than one endpoint's 25 km relation scope reaches. Scoping on
        the first endpoint alone would ask only the region it sits in, and that
        region holds half of the relation: the answer comes back looking
        complete, one member short of the route. That is the cross-border
        failure in its quietest form, so it is asserted from both directions.
        """
        directory = str(tmp_path_factory.mktemp("farapart"))
        west = tmp_path_factory.mktemp("faw") / "west-rail.osm.pbf"
        east = tmp_path_factory.mktemp("fae") / "east-rail.osm.pbf"
        # Relation 500 runs from one region to the other; each file holds only
        # the member way that runs through it, and records the other as absent.
        write_extract(west, {10: [(49.60, 6.00), (49.60, 6.05)]},
                      relations=[(500, [10, 11])])
        write_extract(east, {11: [(49.60, 7.10), (49.60, 7.15)]},
                      relations=[(500, [10, 11])])
        entries = [ok_entry("test/west", build_region(directory, "test/west", west)),
                   ok_entry("test/east", build_region(directory, "test/east", east))]
        write_manifest(directory, entries)
        source = LocalRailSource(directory)

        # Each endpoint alone reaches only its own region — so each alone
        # returns the relation with the far half missing.
        for endpoint, held in [((49.60, 6.00), 10), ((49.60, 7.15), 11)]:
            one_ended = source.relation_geometry([500], [endpoint])
            assert [m["ref"] for m in one_ended[0]["members"]] == [10, 11]
            assert one_ended[0]["missing_members"] == 1
            assert [m["ref"] for m in one_ended[0]["members"] if m["held"]] == [held]

        # Both endpoints reach both regions, and the relation is whole.
        both = source.relation_geometry([500], [(49.60, 6.00), (49.60, 7.15)])
        assert len(both) == 1
        assert both[0]["missing_members"] == 0
        assert [m["ref"] for m in both[0]["members"]] == [10, 11]
        assert all(len(m["geometry"]) == 2 for m in both[0]["members"])


# ---------------------------------------------------------------------------
# Coverage — every way of not covering a point ends in the same place
# ---------------------------------------------------------------------------

class TestCoverage:
    def test_no_data_directory(self, tmp_path):
        source = LocalRailSource(tmp_path / "nothing-here")
        assert source.coverage == []
        assert source.nearest_station(49.60, 6.13) is None

    def test_no_manifest(self, tmp_path):
        assert load_coverage(str(tmp_path)) == []

    def test_unknown_schema_is_refused_rather_than_read(self, tmp_path):
        write_manifest(str(tmp_path), [ok_entry(REGION, (49.0, 5.0, 51.0, 7.0))],
                       schema=max(MANIFEST_SCHEMAS) + 1)
        with pytest.raises(RailSourceError, match="schema"):
            LocalRailSource(str(tmp_path))

    def test_unparseable_manifest_is_refused(self, tmp_path):
        (tmp_path / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(RailSourceError):
            LocalRailSource(str(tmp_path))

    def test_empty_region_is_accounted_for_but_not_covered(self, tmp_path):
        """`empty` means "we know there is no rail here" — an answer, not a gap,
        and still not something to route on."""
        write_manifest(str(tmp_path), [
            {"region": "europe/andorra", "status": "empty",
             "source": "https://example.invalid", "source_date": "2026-09-05"},
            ok_entry(REGION, (49.0, 5.0, 51.0, 7.0)),
        ])
        assert [r for r, _ in load_coverage(str(tmp_path))] == [REGION]

    def test_a_non_ok_entry_is_dropped_even_when_it_carries_a_bbox(self, tmp_path):
        """The status filter, not the bbox guard, is what drops it.

        Today's builder writes no bbox on a non-`ok` entry, so the bbox guard
        happens to catch them and the status check is never exercised — deleting
        it changes no test. A future entry that is both non-`ok` and boxed (a
        `stale` or `failed` status, say) would then claim coverage backed by no
        file, which is coverage on paper reading as coverage in fact.
        """
        boxed_empty = ok_entry("europe/andorra", (42.4, 1.4, 42.7, 1.8))
        boxed_empty["status"] = "empty"
        write_manifest(str(tmp_path), [
            boxed_empty,
            ok_entry(REGION, (49.0, 5.0, 51.0, 7.0)),
        ])
        assert [r for r, _ in load_coverage(str(tmp_path))] == [REGION]

    def test_region_whose_store_file_is_absent_is_skipped(self, tmp_path, caplog):
        """…loudly. The manifest claims coverage the directory does not hold —
        a partial download, a release asset that failed to attach — and the
        query is answered from whatever else overlaps, which may be nothing.
        The warning is the only signal that the local source quietly stopped
        covering a country."""
        write_manifest(str(tmp_path), [ok_entry(REGION, (49.0, 5.0, 51.0, 7.0))])
        source = LocalRailSource(str(tmp_path))
        assert source.regions_for((49.6, 6.1, 49.6, 6.1)) == [REGION]
        with caplog.at_level(logging.WARNING, logger="src.services.rail_source"):
            assert source.nearest_station(49.60, 6.13) is None
            assert source.ways_in_bbox(49.5, 6.0, 49.7, 6.2) == []
        assert REGION in caplog.text
        assert "in the manifest but not in" in caplog.text

    def test_coordinate_outside_every_region(self, local):
        # Helsinki: covered by no European region this directory holds.
        assert local.regions_for((60.17, 24.94, 60.17, 24.94)) == []
        assert local.nearest_station(60.172097, 24.941249) is None
        assert local.relations_near(60.172097, 24.941249) == set()
        assert local.ways_in_bbox(60.0, 24.8, 60.3, 25.1) == []


# ---------------------------------------------------------------------------
# Layers — a manifest with ferry and bus entries leaves rail exactly as it was
# ---------------------------------------------------------------------------

def _layer_entry(region, bbox, layer):
    entry = ok_entry(region, bbox)
    entry["layer"] = layer
    entry["file"] = f"x-{layer}.osm.pbf"
    return entry


@pytest.fixture
def layered_dir(lux_dir, tmp_path):
    """The Luxembourg rail store beside ferry and bus stores, under a schema 3
    manifest — the directory a box holds once ferry and bus are published.

    The bus entry is for a region with **no rail store** and a box over the
    same stops, and its own bus store is on disk: if a bus entry were read as
    rail coverage, the rail source would ask for Belgium's rail and log its
    absence; if it opened files by region alone, it would open a bus store.
    """
    from tests.test_rail_store_schema3 import (
        write_bus_members_only_extract,
        write_ferry_extract,
    )

    directory = str(tmp_path / "layered")
    os.makedirs(directory)
    rail = os.path.join(lux_dir, store_filename(REGION))
    with open(rail, "rb") as src, open(
            os.path.join(directory, store_filename(REGION)), "wb") as dst:
        dst.write(src.read())
    with RailStore(rail) as store:
        rail_bbox = store.bbox
    build_store(write_ferry_extract(tmp_path / "f.osm.pbf"),
                os.path.join(directory, store_filename(REGION, "ferry")),
                region=REGION, layer="ferry")
    build_store(write_bus_members_only_extract(tmp_path / "b.osm.pbf"),
                os.path.join(directory, store_filename("europe/belgium", "bus")),
                region="europe/belgium", layer="bus")
    wide = (48.0, 4.0, 52.0, 8.0)
    write_manifest(directory, [
        _layer_entry("europe/belgium", wide, "bus"),
        _layer_entry(REGION, wide, "ferry"),
        _layer_entry(REGION, rail_bbox, "rail"),
    ], schema=3)
    return directory


class TestLayers:
    def test_a_schema_2_manifest_is_rail(self, lux_dir):
        """No `layer` anywhere, which is every manifest on the box today."""
        assert [r for r, _ in load_coverage(lux_dir)] == [REGION]
        assert load_coverage(lux_dir, "ferry") == []

    def test_a_schema_3_manifest_leaves_rail_coverage_unchanged(
            self, lux_dir, layered_dir):
        assert load_coverage(layered_dir) == load_coverage(lux_dir)
        assert load_coverage(layered_dir, "rail") == load_coverage(lux_dir)
        # Each layer's coverage is its own entries, for U9's sources to read.
        assert [r for r, _ in load_coverage(layered_dir, "ferry")] == [REGION]
        assert [r for r, _ in load_coverage(layered_dir, "bus")] == ["europe/belgium"]

    def test_a_ferry_or_bus_entry_never_opens_as_rail(self, layered_dir, caplog):
        source = LocalRailSource(layered_dir)
        everywhere = (48.0, 4.0, 52.0, 8.0)
        assert source.regions_for(everywhere) == [REGION]
        with caplog.at_level(logging.WARNING, logger="src.services.rail_source"):
            stores = list(source._stores_for(everywhere))
        assert [s.path for s in stores] == [
            os.path.join(layered_dir, store_filename(REGION))]
        assert all(s.layer == "rail" for s in stores)
        assert "europe/belgium" not in caplog.text

    def test_a_schema_3_directory_routes_rail_exactly_as_before(
            self, lux_dir, layered_dir, monkeypatch):
        """Both ship orders end here: code first, then a schema 3 release."""
        stops = [dict(LUX_GARE), dict(KLEINBETTINGEN)]
        monkeypatch.setenv("RAIL_SOURCE", "local")
        transport = Mock(name="_overpass")
        results = []
        for directory in (lux_dir, layered_dir):
            ov._local_source = None
            monkeypatch.setenv("RAIL_DATA_DIR", directory)
            with patch.object(ov, "_overpass", transport):
                results.append(ov.get_rail_geometry(stops))
        assert not results[0].degraded
        assert (results[1].polyline, results[1].strategy) == (
            results[0].polyline, results[0].strategy)
        assert transport.call_count == 0

    def test_an_unknown_layer_is_skipped_with_a_warning(self, lux_dir, tmp_path, caplog):
        rail = load_coverage(lux_dir)
        write_manifest(str(tmp_path), [
            ok_entry(REGION, rail[0][1]),
            _layer_entry("europe/belgium", (48.0, 4.0, 52.0, 8.0), "tram"),
        ], schema=3)
        with caplog.at_level(logging.WARNING, logger="src.services.rail_source"):
            assert load_coverage(str(tmp_path)) == rail
        assert "unknown layer 'tram'" in caplog.text

    def test_the_installed_manifest_schema_is_one_the_box_reads(self):
        """The age check reads the installed manifest against MANIFEST_SCHEMA;
        the fetch step writes it at that schema whatever the release was."""
        assert MANIFEST_SCHEMA == 2
        assert MANIFEST_SCHEMAS == (2, 3)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestConfiguration:
    def test_default_is_overpass_only(self, lux_dir, monkeypatch, oracle):
        """Unset config must leave the resolver exactly as it was: no store is
        opened even with a data directory sitting right there."""
        monkeypatch.delenv("RAIL_SOURCE", raising=False)
        monkeypatch.setenv("RAIL_DATA_DIR", lux_dir)
        assert ov._local_rail_source() is None

        stops = [dict(LUX_GARE), dict(KLEINBETTINGEN)]
        with patch.object(ov, "_overpass", side_effect=oracle):
            result = ov.get_rail_geometry(stops)
        assert not result.degraded
        assert oracle.queries, "the default path must still ask Overpass"

    def test_local_without_a_directory_stays_on_overpass(self, monkeypatch, caplog):
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.delenv("RAIL_DATA_DIR", raising=False)
        with caplog.at_level(logging.WARNING, logger="src.services.overpass_service"):
            assert ov._local_rail_source() is None
        # A deployment that meant to switch over and did not gets one line
        # saying so; without it the symptom is only the Overpass bill.
        assert "RAIL_DATA_DIR is unset" in caplog.text

    def test_unusable_manifest_stays_on_overpass(self, tmp_path, monkeypatch, caplog):
        (tmp_path / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="src.services.overpass_service"):
            assert ov._local_rail_source() is None
        assert "is unusable" in caplog.text

    def test_the_coverage_window_is_short_enough_to_be_a_refresh_window(self):
        """The two refresh tests below monkeypatch the TTL to 0, so they prove
        the re-read mechanism exists and say nothing about the window being
        short enough to matter: set it to 1e12 and every one of them stays
        green while a worker caches its coverage for the rest of its life.

        Bound rather than pinned, because the exact number is a judgement call
        and 300 s is one point in a usable range: long enough that the manifest
        read is negligible per resolve, short enough that a data refresh and a
        manifest read that lost a race with one are picked up without a
        restart, which is what .env.example promises.
        """
        assert 0 < ov._LOCAL_SOURCE_TTL_S <= 900

    def test_local_is_selected_when_configured(self, lux_dir, monkeypatch):
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", lux_dir)
        source = ov._local_rail_source()
        assert isinstance(source, LocalRailSource)
        # Cached: reading the manifest and reopening the region files per resolve
        # would make the store cache pointless.
        assert ov._local_rail_source() is source

    def test_a_failed_coverage_load_is_retried_rather_than_cached_forever(
            self, tmp_path, monkeypatch):
        """A worker that reads the manifest mid-rewrite must not be pinned to
        Overpass for the rest of its life.

        The data directory is a mounted volume rewritten on a refresh schedule,
        so reading a half-written manifest is a real, transient event. Caching
        that failure permanently returns exactly one worker to full Overpass
        traffic — the outcome this issue exists to prevent — after one log line
        and with no way back short of a restart.
        """
        (tmp_path / MANIFEST_NAME).write_text('{"schema": 2, "regi',
                                              encoding="utf-8")
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", str(tmp_path))
        assert ov._local_rail_source() is None

        # The refresh finishes writing.
        write_manifest(str(tmp_path), [ok_entry(REGION, (49.0, 5.0, 51.0, 7.0))])
        assert ov._local_rail_source() is None, "still inside the cache window"

        monkeypatch.setattr(ov, "_LOCAL_SOURCE_TTL_S", 0.0)
        assert isinstance(ov._local_rail_source(), LocalRailSource)

    def test_a_data_refresh_is_picked_up_without_a_restart(
            self, tmp_path, monkeypatch):
        """.env.example promises the data needs no release to update. A worker
        that caches its coverage forever needs a *restart* instead, which is the
        same promise broken more quietly."""
        write_manifest(str(tmp_path), [])
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", str(tmp_path))
        assert ov._local_rail_source().coverage == []

        write_manifest(str(tmp_path), [ok_entry(REGION, (49.0, 5.0, 51.0, 7.0))])
        assert ov._local_rail_source().coverage == [], "still inside the window"

        monkeypatch.setattr(ov, "_LOCAL_SOURCE_TTL_S", 0.0)
        assert [r for r, _ in ov._local_rail_source().coverage] == [REGION]


# ---------------------------------------------------------------------------
# Falling back
# ---------------------------------------------------------------------------

class TestOverpassFallback:
    def test_a_local_miss_resolves_via_overpass(self, tmp_path, monkeypatch):
        """A region can be held and hold almost nothing (Cyprus: two rail ways),
        so "the store said nothing" must not become a straight line."""
        write_manifest(str(tmp_path), [])
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", str(tmp_path))

        relation = {"type": "relation", "id": 7, "members": [{"type": "way", "geometry": [
            {"lat": 49.5999681, "lon": 6.1342493},
            {"lat": 49.62, "lon": 6.05},
            {"lat": 49.6385071, "lon": 5.9823816}]}]}

        def _answer(query, purpose):
            if "uic_ref" in query and "railway" in query:
                return {"elements": []}
            if "out ids" in query:
                return {"elements": [{"id": 7}]}
            return {"elements": [relation]}

        stops = [{"lat": LUX_GARE["lat"], "lon": LUX_GARE["lon"]},
                 {"lat": KLEINBETTINGEN["lat"], "lon": KLEINBETTINGEN["lon"]}]
        with patch.object(ov, "_overpass", side_effect=_answer):
            result = ov.get_rail_geometry(stops)

        assert result.strategy == "relation_endpoints"
        assert result.degraded is False

    def test_a_refused_bounding_box_does_not_become_an_overpass_query(
            self, lux_dir, monkeypatch):
        """The vertex ceiling bounds *our* memory. Overpass answering the same
        question rebuilds the allocation that was just refused, so a ceiling hit
        straight-lines instead of falling back."""
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", lux_dir)
        monkeypatch.setattr("src.services.rail_source._MAX_BBOX_VERTICES", 10)

        # Neither stop is near a station, so strategy A has no UIC codes, and
        # the second has no route relation within 25 km, so strategy B finds
        # nothing: the resolve reaches strategy C and its bounding box.
        stops = [{"lat": 49.50, "lon": 6.20}, {"lat": 50.17, "lon": 6.50}]
        transport = Mock(name="_overpass")
        with patch.object(ov, "_overpass", transport):
            result = ov.get_rail_geometry(stops)

        assert result.strategy == "straight"
        assert result.degraded is True
        assert result.polyline == [[6.20, 49.50], [6.50, 50.17]]
        assert transport.call_count == 0

    def test_the_ceiling_applies_to_the_merged_result(self, two_regions, monkeypatch):
        """Regions are merged, so the budget is shared: four regions must not
        together allocate what one of them would be refused."""
        source = LocalRailSource(two_regions)
        # Each region alone holds 4 vertices in this box; together they hold 6.
        monkeypatch.setattr("src.services.rail_source._MAX_BBOX_VERTICES", 5)
        with pytest.raises(RailSourceOverload):
            source.ways_in_bbox(49.5, 5.8, 49.7, 6.4)

    def test_the_ceiling_counts_a_shared_way_once(self, two_regions, monkeypatch):
        """…and counts what is *kept*, not what each region offered.

        Overlapping regions hold the same border ways, so summing their raw
        totals double-counts exactly where D-A's merging is the point. The
        merged result here is 6 vertices and the raw totals sum to 8; at a
        ceiling of 6 the query fits and must be answered. Charging the raw
        totals degrades the effective ceiling towards _MAX_BBOX_VERTICES / N,
        and an overload straight-lines with no fallback — so the symptom is a
        silent straight line on a cross-border route.
        """
        source = LocalRailSource(two_regions)
        monkeypatch.setattr("src.services.rail_source._MAX_BBOX_VERTICES", 6)
        ways = source.ways_in_bbox(49.5, 5.8, 49.7, 6.4)
        assert [w["id"] for w in ways] == [100, 101, 102]

    def test_a_shared_way_of_two_lengths_cannot_breach_the_ceiling(
            self, tmp_path_factory, monkeypatch):
        """The count pass and the decode pass must mean the *same* copy.

        A way id in two extracts is normally byte-identical, because both came
        from one build. It stops being identical as soon as the regions are
        refreshed on different dates and an OSM edit adds or removes a node from
        a border way — or when builder.py drops a node with an invalid location
        in one extract and not the other.

        Then the two passes disagree about which copy they describe: the decode
        keeps the first region's, so a count that charges the last region's
        authorises a decode it never measured. Here the long copy is 4 vertices
        and the stub is 2, so charging the stub passes a ceiling of 5 and then
        returns 6.
        """
        directory = str(tmp_path_factory.mktemp("twolengths"))
        long_copy = {100: [(49.60, 5.90), (49.60, 6.00),
                           (49.60, 6.10), (49.60, 6.30)]}
        stub_copy = {100: [(49.60, 5.90), (49.60, 6.30)]}

        first = tmp_path_factory.mktemp("first") / "first-rail.osm.pbf"
        last = tmp_path_factory.mktemp("last") / "last-rail.osm.pbf"
        write_extract(first, long_copy, stations=[(49.600, 6.010, "1000")])
        write_extract(last, {**stub_copy, 102: [(49.61, 6.09), (49.61, 6.11)]},
                      stations=[(49.600, 6.050, "2000")])
        # Read in name order, so the long copy is the one the decode keeps.
        entries = [
            ok_entry("test/aaa", build_region(directory, "test/aaa", first)),
            ok_entry("test/zzz", build_region(directory, "test/zzz", last)),
        ]
        write_manifest(directory, entries)

        source = LocalRailSource(directory)
        monkeypatch.setattr("src.services.rail_source._MAX_BBOX_VERTICES", 5)
        with pytest.raises(RailSourceOverload):
            source.ways_in_bbox(49.5, 5.8, 49.7, 6.4)

    def test_a_refused_merge_decodes_no_geometry_at_all(
            self, two_regions, monkeypatch):
        """The ceiling bounds memory, so it has to fire before the allocation.

        Checking the merged total *after* decoding each region is correct in
        what it returns and wrong in what it costs: the regions already merged
        and the region in hand coexist, so two regions near the ceiling
        transiently hold twice it — 392 MB measured against the 196 MB the
        answer itself costs (issue #352), on a worker sized for one ceiling.
        Counting first makes the peak the answer alone, and "no blob was
        decoded" is what says the count came first.
        """
        decoded = []
        real = rail_store.decode_geometry
        monkeypatch.setattr(rail_store, "decode_geometry",
                            lambda blob: decoded.append(blob) or real(blob))

        source = LocalRailSource(two_regions)
        monkeypatch.setattr("src.services.rail_source._MAX_BBOX_VERTICES", 5)
        with pytest.raises(RailSourceOverload):
            source.ways_in_bbox(49.5, 5.8, 49.7, 6.4)
        assert decoded == [], (
            f"{len(decoded)} geometries were decoded before the refusal")


# ---------------------------------------------------------------------------
# Broken local data — every shape of it defers to Overpass
# ---------------------------------------------------------------------------

def _corrupt_store(path, state):
    """Write a store file that exists and cannot be read, in *state*."""
    if state == "garbage":
        # A partial download: the bytes that arrived are not a database.
        path.write_bytes(b"\x1f\x8b\x08\x00 not a sqlite file at all")
    elif state == "zero_byte":
        # `touch`, or a download that got the file created and nothing else.
        path.write_bytes(b"")
    elif state == "wrong_schema":
        # A store built by a future (or past) builder.
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA user_version = 99")
        conn.close()
    else:  # pragma: no cover - test wiring
        raise AssertionError(state)


class TestUnreadableStoreFile:
    """A file the manifest names, that is present and cannot be opened.

    ``store.py`` already answers "the manifest names a file the directory does
    not hold" with None. A file that is *there* and broken raises instead, and
    every raise on this path escapes into the resolve job's retry — where the
    file is still broken, so the segment never resolves at all. A local problem
    must defer to Overpass, whatever shape it takes.
    """

    @pytest.fixture(params=["garbage", "zero_byte", "wrong_schema"])
    def broken_dir(self, request, tmp_path):
        _corrupt_store(tmp_path / store_filename(REGION), request.param)
        write_manifest(str(tmp_path), [ok_entry(REGION, (49.0, 5.0, 51.0, 7.0))])
        return str(tmp_path)

    def test_every_query_reads_as_not_covered(self, broken_dir, caplog):
        source = LocalRailSource(broken_dir)
        # The region is still coverage on paper — the manifest says so.
        assert source.regions_for((49.6, 6.1, 49.6, 6.1)) == [REGION]
        # …and every question about it answers "nothing here", not an exception.
        with caplog.at_level(logging.WARNING, logger="src.services.rail_source"):
            assert source.nearest_station(49.5999681, 6.1342493) is None
        # Silently falling back to Overpass is the traffic this issue exists to
        # stop, so the fallback has to say why it happened.
        assert "cannot be opened" in caplog.text
        assert source.relations_near(49.5999681, 6.1342493) == set()
        assert source.relations_for_uic_pair(
            "8200100", "8200710", [(49.60, 6.13), (49.64, 5.98)]) == []
        assert source.relation_geometry([1], [(49.60, 6.13), (49.64, 5.98)]) == []
        assert source.ways_in_bbox(49.5, 6.0, 49.7, 6.2) == []

    def test_the_resolve_falls_back_to_overpass_instead_of_failing(
            self, broken_dir, monkeypatch, oracle):
        """The whole point: a broken file costs Overpass traffic, not the route.

        Without this, the exception escapes ``get_rail_geometry`` into
        ``_resolve_route_job``'s retry, and the file is still broken on retry —
        so every train resolve in the deployment fails while it exists.
        """
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", broken_dir)
        stops = [dict(LUX_GARE), dict(KLEINBETTINGEN)]
        with patch.object(ov, "_overpass", side_effect=oracle):
            result = ov.get_rail_geometry(stops)
        assert not result.degraded
        assert oracle.queries, "a broken store must send the resolve to Overpass"


class TestStoreThatGoesBadAfterOpening:
    """A file that opens cleanly and fails on a query.

    ``TestUnreadableStoreFile`` covers the file that cannot be opened at all.
    SQLite reads pages lazily, so a store whose header and ``meta`` are intact
    can still meet a corrupt page on the first query that touches it, and that
    raises from an already-open connection — past the guard around ``open``,
    out of ``get_rail_geometry``, into ``_resolve_route_job``'s retry, where the
    page is still corrupt. Same outcome as any other local fault: Overpass.
    """

    QUERIES = ("nearest_station", "relations_near", "relations_for_uic_pair",
               "relation_geometry", "ways_in_bbox", "vertex_counts_in_bbox")

    @pytest.fixture
    def bad_after_open(self, lux_dir, monkeypatch):
        def malformed(self, *args, **kwargs):
            raise sqlite3.DatabaseError("database disk image is malformed")

        for name in self.QUERIES:
            monkeypatch.setattr(RailStore, name, malformed)
        return lux_dir

    def test_every_query_reads_as_not_covered(self, bad_after_open, caplog):
        source = LocalRailSource(bad_after_open)
        assert source.regions_for((49.6, 6.1, 49.6, 6.1)) == [REGION]
        with caplog.at_level(logging.WARNING, logger="src.services.rail_source"):
            assert source.nearest_station(49.5999681, 6.1342493) is None
            assert source.relations_near(49.5999681, 6.1342493) == set()
            assert source.relations_for_uic_pair(
                "8200100", "8200710", [(49.60, 6.13), (49.64, 5.98)]) == []
            assert source.relation_geometry([1], [(49.60, 6.13), (49.64, 5.98)]) == []
            assert source.ways_in_bbox(49.5, 6.0, 49.7, 6.2) == []
        assert "failed mid-query" in caplog.text

    def test_the_resolve_falls_back_to_overpass_instead_of_failing(
            self, bad_after_open, monkeypatch, oracle):
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", bad_after_open)
        stops = [dict(LUX_GARE), dict(KLEINBETTINGEN)]
        with patch.object(ov, "_overpass", side_effect=oracle):
            result = ov.get_rail_geometry(stops)
        assert not result.degraded
        assert oracle.queries, "a store gone bad must send the resolve to Overpass"

    def test_a_bug_in_our_own_merging_is_not_swallowed(self, lux_dir, monkeypatch):
        """The guard is ``sqlite3.DatabaseError`` and nothing wider.

        A blanket ``except`` around the query calls would answer a defect in
        this module's merging or scoping with "region not covered" — the one
        answer that looks exactly like success, and the reason the guard names
        the file's own exception type rather than catching everything.
        """
        monkeypatch.setattr(RailStore, "ways_in_bbox",
                            Mock(side_effect=TypeError("merge is broken")))
        with pytest.raises(TypeError, match="merge is broken"):
            LocalRailSource(lux_dir).ways_in_bbox(49.5, 6.0, 49.7, 6.2)

    @pytest.mark.parametrize("ours", [
        sqlite3.ProgrammingError("Incorrect number of bindings supplied."),
        sqlite3.NotSupportedError("no such feature"),
    ], ids=["wrong bindings", "unsupported"])
    def test_our_own_sql_mistakes_are_not_mistaken_for_a_bad_file(
            self, lux_dir, monkeypatch, ours):
        """`sqlite3.DatabaseError` is not as narrow as it reads.

        ``ProgrammingError`` and ``NotSupportedError`` are subclasses of it, and
        both mean *we* built the query wrong — the wrong number of bindings, a
        feature this SQLite does not have. Catching them would answer a defect
        in this module with "region not covered", which is exactly the
        looks-like-success failure the guard exists to avoid; the TypeError case
        above cannot show it, because TypeError was never in the family.
        """
        monkeypatch.setattr(RailStore, "ways_in_bbox", Mock(side_effect=ours))
        with pytest.raises(type(ours)):
            LocalRailSource(lux_dir).ways_in_bbox(49.5, 6.0, 49.7, 6.2)


class TestMalformedManifest:
    """Every way the manifest can be wrong ends on Overpass, not in a traceback.

    ``_local_rail_source`` is the only caller, and "we cannot read the local
    data" has exactly one safe answer regardless of which builtin the reading
    happened to raise.
    """

    SHAPES = {
        "invalid_json": "{not json",
        "wrong_schema": '{"schema": 99, "regions": []}',
        "entry_without_region":
            '{"schema": 2, "regions": [{"status": "ok", "bbox": [5, 49, 7, 51]}]}',
        "non_numeric_bbox":
            '{"schema": 2, "regions": [{"region": "a", "status": "ok",'
            ' "bbox": ["west", 49, 7, 51]}]}',
        "regions_not_dicts": '{"schema": 2, "regions": ["europe/luxembourg"]}',
        "top_level_list": '[{"region": "europe/luxembourg"}]',
        "top_level_number": '42',
    }

    @pytest.fixture(params=sorted(SHAPES))
    def bad_dir(self, request, tmp_path):
        (tmp_path / MANIFEST_NAME).write_text(
            self.SHAPES[request.param], encoding="utf-8")
        return str(tmp_path)

    def test_stays_on_overpass(self, bad_dir, monkeypatch):
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", bad_dir)
        assert ov._local_rail_source() is None

    def test_the_resolve_still_produces_a_route(self, bad_dir, monkeypatch, oracle):
        monkeypatch.setenv("RAIL_SOURCE", "local")
        monkeypatch.setenv("RAIL_DATA_DIR", bad_dir)
        stops = [dict(LUX_GARE), dict(KLEINBETTINGEN)]
        with patch.object(ov, "_overpass", side_effect=oracle):
            result = ov.get_rail_geometry(stops)
        assert not result.degraded
        assert oracle.queries

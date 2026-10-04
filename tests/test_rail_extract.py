"""The rail extract's tag selection and manifest (issue #345, phase 1).

The filter decides what route resolution can see. Widen it and unconnected
sidings enter the graph, which the spike showed *breaks* routes that work
today; narrow it and strategies stop finding things Overpass finds. Neither
failure is visible in any other test — the artifact is built monthly in CI, and
by the time a wrong selection shows up it is a wrong polyline on a user's trip.
The contract itself was wrong once (#349) and nothing caught it, which is what
these assertions exist for.

So the selection is pinned against a checked-in extract with known contents:
two 900 m boxes in Mannheim — one over the ARENA/Maimarkt halt, one over
Neuostheim — cut from Germany's Geofabrik extract with ``osmium extract`` and
joined with ``osmium merge`` (OpenStreetMap data, ODbL). Two boxes rather than
one because the dense kilometre between them costs 600 KB and decides nothing;
this way the fixture is 420 KB and still holds one of everything the filter has
to rule on:

- 12 ``railway=rail`` and 27 ``narrow_gauge`` ways to keep, against 152
  service-tagged ones to drop and 49 trams, 12 platforms and a signal box
  besides;
- one ``railway=light_rail`` way and one ``route=light_rail`` relation, which
  are **synthetic** (ids 9000000000001-9000000000004, a 200 m line inside the
  box): Mannheim maps its Stadtbahn as tram and narrow_gauge, so the box held
  no ``light_rail`` at all and the only thing asserting that value was a
  hand-written table restating the constant it was meant to guard. Dropping
  ``light_rail`` from either constant passed 83 tests. It now fails on the
  filter's own output. The way is in no relation and the relation's member is a
  real ``railway=rail`` way, so neither is kept by the other's row — drop
  either value and its element leaves the file;
- 11 nodes carrying ``uic_ref`` that are *not* tagged as stations — tram stops,
  a bus stop, ``public_transport=stop_position`` — which is the row #349
  corrected and the reason strategy A can find a relation at all;
- a station mapped as a way (ARENA/Maimarkt) and one mapped as a relation
  (Neuostheim), neither of which the old node-only contract could see;
- ``route=train`` and ``route=railway`` relations to keep, against trams,
  buses, cycle routes, a pipeline and a waterway to drop;
- relations whose members include four sidings and three platforms, so the
  member-way closure — geometry Overpass's ``out geom`` returns and this
  extract must too — is exercised rather than assumed.

The expected counts below are therefore not magic numbers: changing the
selection changes them, which is the point. The counts under "the fixture is
worth testing against" are the other half — a widened filter can make an
assertion pass vacuously, and those keep each row of the contract represented.

``rail_mannheim_filtered.osm.pbf`` beside it is this box put through the
filter: the selection's expected output, checked in so that a change to what is
selected has to be shown in a diff rather than only in a count.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import osmium
import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "rail_mannheim.osm.pbf"
# The same box put through the filter: the expected output of the selection.
PUBLISHED = ROOT / "tests" / "fixtures" / "rail_mannheim_filtered.osm.pbf"

_spec = importlib.util.spec_from_file_location(
    "build_rail_extract", ROOT / "scripts" / "build_rail_extract.py"
)
rail = importlib.util.module_from_spec(_spec)
# Registered before execution because the module defines a dataclass, and
# dataclasses resolve their annotations through sys.modules.
sys.modules[_spec.name] = rail
_spec.loader.exec_module(rail)

# What the fixture holds, counted from the raw box (see the docstring).
EXPECTED_WAYS = 40
EXPECTED_RELATIONS = 60
EXPECTED_STATIONS = 4
EXPECTED_UIC_NODES = 13
# Ways held only because a kept relation references them: four sidings and
# three platforms, none of them track to route over.
EXPECTED_MEMBER_WAYS = 7
# Members of those relations that this box does not contain — the fixture is
# two 900 m cuts out of a national network, so most of it is elsewhere. On a
# country extract this number is the cross-border residue instead.
#
# Two counts of the same fact and they differ by 2.6x here: distinct way ids
# nothing holds, and membership slots pointing at one. The build log reports
# both by name because the residue phase 3 has to size is the first number and
# the percentage it is tempting to quote is the second.
EXPECTED_MEMBER_WAYS_MISSING = 25177
EXPECTED_MEMBER_SLOTS_MISSING = 64562
EXPECTED_MEMBER_SLOTS = 64632


@pytest.fixture(scope="module")
def filtered(tmp_path_factory):
    """The fixture box put through the exact selection."""
    out = tmp_path_factory.mktemp("rail") / "mannheim-rail.osm.pbf"
    selection = rail.select(FIXTURE, out)
    return out, selection


@pytest.fixture(scope="module")
def contents(filtered):
    """(nodes, ways, relations) of the filtered file, as plain dicts."""
    path, _ = filtered
    nodes, ways, relations = {}, {}, {}
    for obj in osmium.FileProcessor(str(path)):
        if obj.is_node():
            nodes[obj.id] = (dict(obj.tags), obj.location.lon, obj.location.lat)
        elif obj.is_way():
            ways[obj.id] = (dict(obj.tags), [n.ref for n in obj.nodes])
        else:
            relations[obj.id] = (dict(obj.tags),
                                 [(m.type, m.ref) for m in obj.members])
    return nodes, ways, relations


# ---------------------------------------------------------------------------
# The predicates, stated on their own
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tags,kept", [
    ({"railway": "rail"}, True),
    ({"railway": "narrow_gauge"}, True),
    ({"railway": "light_rail"}, True),
    # `service` at any value is a siding, a yard track or a spur.
    ({"railway": "rail", "service": "siding"}, False),
    ({"railway": "rail", "service": "yard"}, False),
    ({"railway": "rail", "service": "crossover"}, False),
    ({"railway": "tram"}, False),
    ({"railway": "disused"}, False),
    ({"railway": "platform"}, False),
    ({"highway": "residential"}, False),
])
def test_rail_way_predicate(tags, kept):
    """Mirrors way["railway"~"^(rail|narrow_gauge|light_rail)$"]["service"!~"."]."""
    assert rail.is_rail_way(tags) is kept


@pytest.mark.parametrize("tags,kept", [
    ({"route": "train"}, True),
    # Strategy A lists all three separately; strategy B matches the same three
    # as one regex. Dropping either of these two was the #349 contract error.
    ({"route": "railway"}, True),
    ({"route": "light_rail"}, True),
    ({"route": "bus"}, False),
    ({"route": "ferry"}, False),
    ({"route": "tram"}, False),
    ({"type": "multipolygon"}, False),
])
def test_route_relation_predicate(tags, kept):
    assert rail.is_route_relation(tags) is kept


@pytest.mark.parametrize("tags,kept", [
    # _route_relation_segment matches node["uic_ref"=X] with *no* railway
    # filter, and relations reference the stop node, which is routinely
    # untagged as a station (#349).
    ({"uic_ref": "8000284"}, True),
    ({"railway": "stop", "uic_ref": "8000284"}, True),
    ({"public_transport": "stop_position", "uic_ref": "8000284"}, True),
    ({"railway": "station", "uic_ref": "8000284"}, True),
    ({"railway": "border", "uic_ref": "8000079"}, True),
    ({"railway": "station"}, False),
    ({"uic_ref": ""}, False),
    ({"railway": "rail"}, False),
])
def test_uic_node_predicate(tags, kept):
    assert rail.is_uic_node(tags) is kept


@pytest.mark.parametrize("tags,kept", [
    ({"railway": "station", "uic_ref": "8000284"}, True),
    ({"railway": "halt", "uic_ref": "8000766"}, True),
    # A station with no UIC code cannot answer the lookup it exists for.
    ({"railway": "station"}, False),
    ({"railway": "station", "uic_ref": ""}, False),
    ({"railway": "stop", "uic_ref": "1"}, False),
    ({"public_transport": "station", "uic_ref": "1"}, False),
])
def test_station_predicate(tags, kept):
    """Mirrors node|way|rel ["railway"~"^(station|halt)$"]["uic_ref"]."""
    assert rail.is_station(tags) is kept


# ---------------------------------------------------------------------------
# The fixture is worth testing against
#
# Each row of the contract has to be represented in the fixture, or the
# assertions below it pass for the wrong reason. A filter that is too wide is
# caught by pinned counts; a fixture that is too thin is caught here.
# ---------------------------------------------------------------------------

def test_fixture_holds_uic_nodes_that_are_not_stations(contents):
    """The row #349 corrected: strategy A finds relations through these."""
    nodes, _, _ = contents
    bare = [tags for tags, _, _ in nodes.values()
            if rail.is_uic_node(tags) and not rail.is_station(tags)]
    assert bare, "fixture cannot detect a regression on the node row"


def test_fixture_holds_a_station_mapped_as_a_way(contents):
    """The other row #349 corrected: _find_station_near queries ways too."""
    _, ways, _ = contents
    assert [w for w, (tags, _) in ways.items() if rail.is_station(tags)]


def test_fixture_holds_more_than_one_route_type(contents):
    """route=railway and route=light_rail are as much strategy A's as train."""
    _, _, relations = contents
    assert len({tags["route"] for tags, _ in relations.values()
                if rail.is_route_relation(tags)}) > 1


def test_fixture_holds_service_ways_to_exclude():
    """The one row that excludes rather than includes."""
    excluded = [obj for obj in osmium.FileProcessor(str(FIXTURE), osmium.osm.WAY)
                if obj.tags.get("railway") in rail.RAIL_WAY_TYPES
                and "service" in obj.tags]
    assert excluded


# ---------------------------------------------------------------------------
# The selection, against the fixture
# ---------------------------------------------------------------------------

def test_counts_are_pinned(filtered):
    """A change in what the filter selects has to fail here, loudly."""
    _, selection = filtered
    assert (selection.ways, selection.relations, selection.stations,
            selection.uic_nodes, selection.member_ways,
            selection.member_ways_missing) == (
        EXPECTED_WAYS, EXPECTED_RELATIONS, EXPECTED_STATIONS, EXPECTED_UIC_NODES,
        EXPECTED_MEMBER_WAYS, EXPECTED_MEMBER_WAYS_MISSING
    )


def test_the_two_residual_counts_are_reported_apart(filtered):
    """Distinct member ways missing, and membership slots missing, are
    different numbers — 25,177 against 64,562 on this box, and on Luxembourg
    82 % against 16-19 %.

    Phase 3 sizes the cross-border case from these. Reporting one under a name
    that could mean either is how a 2.6x error gets quoted with confidence, so
    both are on the Selection and both are in the build log.
    """
    _, selection = filtered
    assert selection.member_slots_missing == EXPECTED_MEMBER_SLOTS_MISSING
    assert selection.member_slots == EXPECTED_MEMBER_SLOTS
    assert selection.member_ways_missing != selection.member_slots_missing


def _elements(path: Path) -> dict:
    """Every element in *path* by (kind, id): its tags and what it points at."""
    out = {}
    for obj in osmium.FileProcessor(str(path)):
        if obj.is_node():
            out[("n", obj.id)] = (dict(obj.tags),
                                  (obj.location.x, obj.location.y))
        elif obj.is_way():
            out[("w", obj.id)] = (dict(obj.tags), [n.ref for n in obj.nodes])
        else:
            out[("r", obj.id)] = (dict(obj.tags),
                                  [(m.type, m.ref, m.role) for m in obj.members])
    return out


def test_the_published_filtered_fixture_is_what_this_filter_produces():
    """`rail_mannheim_filtered.osm.pbf` is the selection's expected output,
    checked in so a change to what is selected shows up as a diff.

    Phase 2's first fixture was cut by hand with the pre-#349 filter, which
    left its relation_uic table empty and strategy A untestable against real
    data — drift nobody could see. Checking in the filter's own output is what
    stops that happening twice: change the selection without regenerating this
    file and the test says so.

    Compared element by element rather than byte for byte. The bytes are stable
    for one pyosmium and not across versions — four bytes at offsets 68-75 hold
    the zlib-compressed `generator: libosmium/x.y.z` string, so 4.0.2, 4.1.0 and
    4.3.1 each write a different file for identical contents, and
    requirements.txt permits all three. A byte assertion there fails with
    "regenerate the fixture", which is the wrong diagnosis and pins the file to
    whoever last regenerated it.

        python -c "import ...; select(FIXTURE, PUBLISHED)"
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        regenerated = Path(tmp) / "check.osm.pbf"
        rail.select(FIXTURE, regenerated)
        assert _elements(regenerated) == _elements(PUBLISHED), (
            "tests/fixtures/rail_mannheim_filtered.osm.pbf is stale — "
            "regenerate it from tests/fixtures/rail_mannheim.osm.pbf"
        )


def test_counts_describe_the_file_that_was_written(contents, filtered):
    """The manifest's numbers must be of the artifact, not of some earlier pass."""
    nodes, ways, relations = contents
    _, selection = filtered
    assert sum(1 for tags, _ in ways.values() if rail.is_rail_way(tags)) \
        == selection.ways
    assert sum(1 for tags, _ in relations.values() if rail.is_route_relation(tags)) \
        == selection.relations
    assert sum(1 for tags, _, _ in nodes.values() if rail.is_uic_node(tags)) \
        == selection.uic_nodes
    stations = (
        sum(1 for tags, _, _ in nodes.values() if rail.is_station(tags))
        + sum(1 for tags, _ in ways.values() if rail.is_station(tags))
        + sum(1 for tags, _ in relations.values() if rail.is_station(tags))
    )
    assert stations == selection.stations


def test_no_service_way_is_track(contents):
    """Sidings beat the through line when _nearest_node snaps (spike finding),
    so none may be routable — but a relation that references one still needs
    its geometry. So: present only as members, never as rail.
    """
    _, ways, relations = contents
    members = {ref for _, member_list in relations.values()
               for kind, ref in member_list if kind == "w"}
    for way, (tags, _) in ways.items():
        if "service" not in tags:
            continue
        assert not rail.is_rail_way(tags), f"way {way} is service and rail"
        assert way in members, f"service way {way} is in the file for no reason"


def test_every_way_is_rail_a_station_or_a_relation_member(contents):
    """Nothing else has a reason to be in the file."""
    _, ways, relations = contents
    members = {ref for _, member_list in relations.values()
               for kind, ref in member_list if kind == "w"}
    for way, (tags, _) in ways.items():
        assert rail.is_rail_way(tags) or rail.is_station(tags) or way in members


def test_relation_members_are_kept_whatever_their_own_tags(contents):
    """Overpass answers a relation query with `out geom`, which returns every
    member's geometry. Keeping only the members that pass the way row hands
    phase 3 a shorter relation than Overpass gives — and silently, since
    _extract_relation_geometry returns None on a disconnected member graph, so
    strategies A and B fall through looking exactly like "no route found".

    The fixture holds four `service=siding` tracks and three platforms that are
    in the file for precisely this reason and no other.
    """
    _, ways, relations = contents
    members = {ref for _, member_list in relations.values()
               for kind, ref in member_list if kind == "w"}
    held = [tags for way, (tags, _) in ways.items()
            if way in members and not rail.is_rail_way(tags)]
    assert [t for t in held if t.get("service")], "no service-tagged member kept"
    assert [t for t in held if t.get("railway") == "platform"], "no platform kept"


def test_member_only_ways_are_not_rail(contents):
    """Phase 2 flags these `rail=0` using this same predicate, which is what
    keeps them out of its bbox index and out of strategy C's snapping. A
    platform the resolver can route over is worse than a missing one.
    """
    _, ways, relations = contents
    members = {ref for _, member_list in relations.values()
               for kind, ref in member_list if kind == "w"}
    member_only = [tags for way, (tags, _) in ways.items()
                   if way in members and not rail.is_rail_way(tags)
                   and not rail.is_station(tags)]
    assert member_only, "the closure is not represented in the fixture"
    assert not [t for t in member_only if rail.is_rail_way(t)]


def test_narrow_gauge_is_kept(contents):
    """The way query is a three-value regex, not railway=rail.

    Mannheim's OEG line is narrow_gauge, and a filter that quietly became
    railway=rail would take most of the fixture's kept ways with it.
    """
    _, ways, _ = contents
    assert any(tags.get("railway") == "narrow_gauge" for tags, _ in ways.values())


def test_light_rail_survives_as_a_way_and_as_a_route(contents):
    """Both rows of the contract that name ``light_rail``, asserted on the
    filter's output rather than on a table that restates the constants.

    The predicate tests above are a hand-written copy of ``RAIL_WAY_TYPES`` and
    ``ROUTE_TYPES``, so the natural edit — narrow the constant, update the table
    — used to pass everything. `light_rail` is the value that matters most for
    that: it is one of the three in `_via_coordinate_fallback`'s regex and one
    of the three in `_ROUTE_TAGS`, and dropping it silently is exactly the
    class of error #349 was.
    """
    _, ways, relations = contents
    assert [w for w, (tags, _) in ways.items()
            if tags.get("railway") == "light_rail"]
    assert [r for r, (tags, _) in relations.items()
            if tags.get("route") == "light_rail"]


def test_trams_do_not_survive(contents):
    """The sharpest neighbour: 49 tram ways run through this box and the
    regex excludes every one of them."""
    _, ways, _ = contents
    assert not [tags for tags, _ in ways.values() if tags.get("railway") == "tram"]


def test_only_route_and_station_relations_survive(contents):
    _, _, relations = contents
    for tags, _ in relations.values():
        assert rail.is_route_relation(tags) or rail.is_station(tags)


def test_bus_relations_do_not_survive(contents):
    """The fixture is full of them; a filter keyed on `route` alone would keep
    them and quietly triple the file."""
    _, _, relations = contents
    assert not [tags for tags, _ in relations.values() if tags.get("route") == "bus"]


def test_every_station_carries_a_uic_ref(contents):
    nodes, ways, relations = contents
    tagged = [tags for tags, _, _ in nodes.values()] \
        + [tags for tags, _ in ways.values()] \
        + [tags for tags, _ in relations.values()]
    stations = [tags for tags in tagged
                if tags.get("railway") in rail.STATION_RAILWAY_TYPES]
    assert stations, "the fixture must contain a station to be worth checking"
    assert all(tags.get("uic_ref") for tags in stations)


def test_the_maimarkt_halt_is_among_the_stations(contents):
    """A named element, so a selection that keeps the right *number* by luck fails."""
    nodes, ways, _ = contents
    uic = {tags["uic_ref"] for tags, _, _ in nodes.values() if tags.get("uic_ref")}
    uic |= {tags["uic_ref"] for tags, _ in ways.values() if tags.get("uic_ref")}
    assert "8003841" in uic


def test_kept_ways_keep_all_their_nodes(contents):
    """Geometry is the whole point: a way missing a node cannot be routed on,
    and a station polygon missing one has no centre.

    This is what the multi-pass selection buys — dropping the nodes of the ways
    the prefilter over-selected without dropping the nodes of the ways kept.
    """
    nodes, ways, _ = contents
    for way, (_, refs) in ways.items():
        missing = [ref for ref in refs if ref not in nodes]
        assert not missing, f"way {way} lost {len(missing)} nodes"


def test_station_relations_keep_their_member_ways(contents):
    """`out center` on a relation needs the geometry underneath it."""
    nodes, ways, relations = contents
    for rel, (tags, members) in relations.items():
        if not rail.is_station(tags):
            continue
        for kind, ref in members:
            if kind == "w":
                assert ref in ways, f"station relation {rel} lost way {ref}"
            elif kind == "n":
                assert ref in nodes, f"station relation {rel} lost node {ref}"


def test_untagged_nodes_are_only_there_to_carry_geometry(contents):
    """No node survives that no kept way references and that has no UIC code."""
    nodes, ways, _ = contents
    referenced = {ref for _, refs in ways.values() for ref in refs}
    for node, (tags, _, _) in nodes.items():
        assert node in referenced or rail.is_uic_node(tags)


def test_bbox_is_the_extent_of_the_rail_ways(filtered, contents):
    """Phase 3 picks a region by this box, so it must not claim empty space —
    and it must mean the same thing as phase 2's, which is over rail ways only.
    """
    nodes, ways, _ = contents
    _, selection = filtered
    rail_nodes = {ref for tags, refs in ways.values() if rail.is_rail_way(tags)
                  for ref in refs}
    lons = [lon for node, (_, lon, _) in nodes.items() if node in rail_nodes]
    lats = [lat for node, (_, _, lat) in nodes.items() if node in rail_nodes]
    min_lon, min_lat, max_lon, max_lat = selection.bbox
    assert (min_lon, min_lat) == pytest.approx((min(lons), min(lats)), abs=1e-5)
    assert (max_lon, max_lat) == pytest.approx((max(lons), max(lats)), abs=1e-5)


def test_bbox_ignores_nodes_that_are_not_on_track(tmp_path):
    """The two definitions coincide on the fixture and would not on a country:
    a bare ``uic_ref`` node is kept wherever it is, and a bus stop 3 degrees
    away would otherwise stretch the box over land the extract holds no rail
    for — which phase 3 then selects this region for and finds nothing in.
    """
    source = tmp_path / "src.osm.pbf"
    writer = osmium.SimpleWriter(str(source))
    writer.add_node(osmium.osm.mutable.Node(id=1, location=(8.0, 49.0)))
    writer.add_node(osmium.osm.mutable.Node(id=2, location=(8.1, 49.1)))
    # Far from the track, and in the file because strategy A looks it up.
    writer.add_node(osmium.osm.mutable.Node(
        id=3, location=(11.0, 52.0), tags={"uic_ref": "8000284"}))
    writer.add_way(osmium.osm.mutable.Way(
        id=10, nodes=[1, 2], tags={"railway": "rail"}))
    writer.close()

    selection = rail.select(source, tmp_path / "out.osm.pbf")

    assert selection.bbox == [8.0, 49.0, 8.1, 49.1]


def test_bbox_is_not_the_starting_sentinel_when_no_rail_node_is_located(tmp_path):
    """#350: a rail way whose nodes are absent from the file used to publish
    ``[180.0, 180.0, -180.0, -180.0]`` as the region's extent — an inverted
    box, emitted because the gate was on ``ways`` rather than on nodes seen."""
    source = tmp_path / "src.osm.pbf"
    writer = osmium.SimpleWriter(str(source))
    writer.add_way(osmium.osm.mutable.Way(
        id=10, nodes=[1, 2], tags={"railway": "rail"}))
    writer.close()

    selection = rail.select(source, tmp_path / "out.osm.pbf")

    assert selection.ways == 1
    assert selection.bbox == []


def test_bbox_agrees_with_the_store_phase_2_builds_from_it(filtered, tmp_path):
    """The same box, computed independently by both phases.

    `docs/LOCAL_RAIL_DATA_PLAN.md` promises phase 3 one region extent; phase 1
    writes it into the manifest and phase 2 writes it into the store's meta from
    the same file. Two definitions of "where this region reaches" is a bug
    waiting for the first country where they differ.
    """
    from src.rail.builder import build_store
    from src.rail.store import RailStore

    path, selection = filtered
    store_path = tmp_path / "region.sqlite"
    build_store(path, store_path, region="europe/germany")

    with RailStore(store_path) as store:
        min_lat, min_lon, max_lat, max_lon = store.bbox
    assert selection.bbox == pytest.approx(
        [min_lon, min_lat, max_lon, max_lat], abs=1e-5)


def test_an_extract_with_no_rail_ways_is_empty_not_an_error(tmp_path):
    """The third outcome (plan, phase 0 / the phase 1-2 contract).

    Liechtenstein has 10 relations, 2 stations and 823 uic nodes and not one
    railway way — its only line is tagged `railway=construction`. Under the old
    guard (`not stations and not ways`) it published a 0.06 MB artifact that
    `src/rail/builder.py` then refused with "no railway ways", and Andorra,
    Malta and the Azores failed the job outright. Three red matrix jobs by
    design every month is how a real failure stops being visible.
    """
    source = tmp_path / "src.osm.pbf"
    writer = osmium.SimpleWriter(str(source))
    writer.add_node(osmium.osm.mutable.Node(
        id=1, location=(9.5, 47.1),
        tags={"railway": "station", "uic_ref": "8509000"}))
    writer.close()

    selection = rail.select(source, tmp_path / "out.osm.pbf")

    assert selection.ways == 0
    assert selection.stations == 1
    # Nothing to cover, so nothing claimed.
    assert selection.bbox == []


def test_metadata_is_dropped(filtered):
    """Version/timestamp/user are ~15 % of the file and nothing reads them."""
    path, _ = filtered
    for obj in osmium.FileProcessor(str(path)):
        assert obj.version == 0
        break


# ---------------------------------------------------------------------------
# The manifest — the phase 1 / phase 2 contract
# ---------------------------------------------------------------------------

CONTRACT_KEYS = {
    "region", "status", "file", "source", "source_date", "sha256", "bytes",
    "ways", "relations", "stations", "bbox",
}
# An `empty` region has no file, so no checksum, size or extent either.
EMPTY_CONTRACT_KEYS = {"region", "status", "source", "source_date"}


@pytest.fixture(scope="module")
def entry(filtered):
    path, selection = filtered
    return rail.manifest_entry("europe/germany", path, selection, "2026-09-05")


def test_entry_has_exactly_the_contract_keys(entry):
    """Phase 2 reads this. Extra keys are a contract change, not a detail —
    including the uic_nodes count, which stays in the build log."""
    assert set(entry) == CONTRACT_KEYS


def test_entry_describes_the_file_on_disk(entry, filtered):
    path, _ = filtered
    assert entry["file"] == path.name
    assert entry["bytes"] == path.stat().st_size
    assert entry["sha256"] == rail.sha256_file(path)
    assert entry["source"] == \
        "https://download.geofabrik.de/europe/germany-latest.osm.pbf"
    assert entry["source_date"] == "2026-09-05"


def test_source_date_comes_from_the_extract_not_the_clock(tmp_path):
    """The artifact is versioned by the data's date, so a rebuild of unchanged
    data is recognisably the same data."""
    stamped = tmp_path / "stamped.osm.pbf"
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", "2026-09-05T21:20:02Z")
    osmium.SimpleWriter(str(stamped), header=header).close()

    assert rail.source_date(stamped) == "2026-09-05"


def test_an_undated_source_is_an_error(tmp_path):
    """A stale extract is the failure that looks like success (plan, phase 5),
    so an extract whose date we cannot read must not build at all.

    The fixture is undated because ``osmium extract`` does not carry the
    replication timestamp through; Geofabrik's own downloads all have one.
    """
    with pytest.raises(RuntimeError, match="replication timestamp"):
        rail.source_date(FIXTURE)


def test_merge_orders_regions_and_stamps_the_schema(entry):
    other = {**entry, "region": "europe/austria"}
    manifest = rail.merge_manifest([entry, other], generated_at="2026-09-06T18:00:00Z")
    assert manifest["schema"] == rail.MANIFEST_SCHEMA == 2
    assert manifest["generated_at"] == "2026-09-06T18:00:00Z"
    assert [r["region"] for r in manifest["regions"]] == [
        "europe/austria", "europe/germany"
    ]


def test_generated_at_is_utc_and_iso():
    manifest = rail.merge_manifest([])
    assert manifest["generated_at"].endswith("Z")
    assert len(manifest["generated_at"]) == 20


def test_verify_accepts_an_untouched_build(entry, filtered):
    path, _ = filtered
    rail.verify_manifest(rail.merge_manifest([entry]), path.parent)


def test_verify_rejects_a_truncated_file(entry, tmp_path):
    """The artifact crosses a job boundary between build and publish."""
    (tmp_path / entry["file"]).write_bytes(b"not a pbf")
    with pytest.raises(ValueError, match="bytes"):
        rail.verify_manifest(rail.merge_manifest([entry]), tmp_path)


def test_verify_rejects_a_corrupted_file(entry, tmp_path):
    (tmp_path / entry["file"]).write_bytes(b"\0" * entry["bytes"])
    with pytest.raises(ValueError, match="checksum"):
        rail.verify_manifest(rail.merge_manifest([entry]), tmp_path)


def test_verify_rejects_a_missing_file(entry, tmp_path):
    with pytest.raises(ValueError, match="missing"):
        rail.verify_manifest(rail.merge_manifest([entry]), tmp_path)


def test_verify_rejects_an_unknown_schema(entry, filtered):
    """Schema 1 had no `status`, so its entries cannot say "this region holds
    no rail" — reading one as if it could is the mistake the number prevents."""
    path, _ = filtered
    manifest = {**rail.merge_manifest([entry]), "schema": 1}
    with pytest.raises(ValueError, match="schema"):
        rail.verify_manifest(manifest, path.parent)


def test_collect_writes_and_verifies_the_published_manifest(filtered, entry, tmp_path):
    """What the publish job runs: entry files in, verified manifest.json out."""
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    (tmp_path / f"germany{rail.ENTRY_SUFFIX}").write_text(json.dumps(entry))

    manifest = rail.collect_manifest(tmp_path)

    written = json.loads((tmp_path / rail.MANIFEST_NAME).read_text())
    assert written == manifest
    assert [r["region"] for r in written["regions"]] == ["europe/germany"]


def test_collect_refuses_to_publish_nothing(tmp_path):
    """An empty directory would otherwise publish an empty manifest, which
    phase 3 reads as "Europe is not covered"."""
    with pytest.raises(RuntimeError, match="no .* files"):
        rail.collect_manifest(tmp_path)


# ---------------------------------------------------------------------------
# The three outcomes, and what the publish job does with them
# ---------------------------------------------------------------------------

def _entry_file(directory: Path, slug: str, entry: dict) -> None:
    (directory / f"{slug}{rail.ENTRY_SUFFIX}").write_text(json.dumps(entry))


def test_an_empty_region_is_recorded_without_a_file(entry, tmp_path):
    """It is in the manifest so phase 3 can tell "no rail here" from "we never
    built it", and so the completeness check counts it as accounted for."""
    empty = rail.empty_entry("europe/andorra", "2026-09-05")
    assert set(empty) == EMPTY_CONTRACT_KEYS
    assert empty["status"] == rail.STATUS_EMPTY

    manifest = rail.merge_manifest([empty])
    # No file to check, and no exception for the absence of one.
    rail.verify_manifest(manifest, tmp_path)
    assert rail.missing_regions(manifest, ["europe/andorra"]) == []


def test_an_unknown_status_is_refused(entry, filtered):
    """A newer producer's outcome must not be read as an artifact we can trust."""
    path, _ = filtered
    manifest = rail.merge_manifest([{**entry, "status": "partial"}])
    with pytest.raises(ValueError, match="unknown status"):
        rail.verify_manifest(manifest, path.parent)


def test_a_subset_rebuild_keeps_the_regions_it_did_not_touch(entry, filtered, tmp_path):
    """The documented recovery path is a dispatch with `regions: europe/denmark`.

    That rebuilds one region; the other 48 are still assets of the release being
    patched. A manifest holding only Denmark disowns them, and phase 3 reads a
    missing entry as "not covered" and falls back to Overpass — the service that
    banned us.
    """
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    _entry_file(tmp_path, "germany", entry)
    base = rail.merge_manifest([
        {**entry, "region": "europe/denmark", "file": "denmark-rail.osm.pbf"},
        {**entry, "region": "europe/germany", "source_date": "2026-01-01"},
        rail.empty_entry("europe/andorra", "2026-01-01"),
    ])

    manifest = rail.collect_manifest(tmp_path, base=base)

    regions = {r["region"]: r for r in manifest["regions"]}
    assert set(regions) == {"europe/denmark", "europe/germany", "europe/andorra"}
    # The rebuilt one is this run's, the untouched ones are carried verbatim —
    # including the one whose .pbf is not even in this directory.
    assert regions["europe/germany"]["source_date"] == entry["source_date"]
    assert regions["europe/denmark"]["file"] == "denmark-rail.osm.pbf"


def test_a_run_that_lost_a_region_is_not_a_publishable_manifest(entry, filtered, tmp_path):
    """Transient Geofabrik failures must not quietly become the current release."""
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    _entry_file(tmp_path, "germany", entry)

    manifest = rail.collect_manifest(tmp_path)

    assert rail.missing_regions(
        manifest, ["europe/germany", "europe/france", "europe/austria"]
    ) == ["europe/austria", "europe/france"]


def test_the_manifest_command_refuses_an_incomplete_run(entry, filtered, tmp_path, capsys):
    """What the publish job actually runs: a non-zero exit, naming the regions."""
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    _entry_file(tmp_path, "germany", entry)
    argv = ["build_rail_extract.py", "manifest", "--out-dir", str(tmp_path),
            "--expect", json.dumps(["europe/germany", "europe/france"])]

    assert rail.main(argv) == 1
    assert "europe/france" in capsys.readouterr().out

    # …and publishes anyway when a human says so.
    assert rail.main(argv + ["--force"]) == 0


def test_a_requested_region_that_failed_is_not_republished_from_the_base(
        entry, filtered, tmp_path, capsys):
    """#350: dispatch `europe/denmark europe/germany`, Germany's build fails.

    `--expect` is the subset, and the base still holds last month's Germany,
    so the region counts as covered and the release went out claiming a
    rebuild that never happened. A region asked for must be one rebuilt.
    """
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    _entry_file(tmp_path, "denmark", {**entry, "region": "europe/denmark"})
    base = tmp_path / "released.json"
    base.write_text(json.dumps(rail.merge_manifest([
        {**entry, "region": "europe/germany", "source_date": "2026-08-02"},
        {**entry, "region": "europe/france", "source_date": "2026-08-02"},
    ])))
    argv = ["build_rail_extract.py", "manifest", "--out-dir", str(tmp_path),
            "--base", str(base),
            "--expect", json.dumps(["europe/denmark", "europe/germany"])]

    assert rail.main(argv) == 1
    out = capsys.readouterr().out
    assert "europe/germany" in out
    # France was not asked for: carrying it is the merge doing its job.
    assert "europe/france" not in out

    assert rail.main(argv + ["--force"]) == 0


def test_the_manifest_command_merges_the_released_manifest(entry, filtered, tmp_path):
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    _entry_file(tmp_path, "germany", entry)
    base = tmp_path / "released.json"
    base.write_text(json.dumps(rail.merge_manifest(
        [{**entry, "region": "europe/denmark", "file": "denmark-rail.osm.pbf"}]
    )))

    assert rail.main([
        "build_rail_extract.py", "manifest", "--out-dir", str(tmp_path),
        "--base", str(base),
        "--expect", json.dumps(["europe/germany"]),
    ]) == 0

    written = json.loads((tmp_path / rail.MANIFEST_NAME).read_text())
    assert [r["region"] for r in written["regions"]] == [
        "europe/denmark", "europe/germany"
    ]


def test_a_base_manifest_of_another_schema_is_refused(entry, filtered, tmp_path):
    """Carried entries are never re-verified — ``collect_manifest`` re-checksums
    only what this run built — so merging a base of an unknown shape writes out
    entries nothing has validated. Schema 1 had no ``status``, and merging one
    in produces a file whose own verifier raises ``KeyError`` rather than the
    ``ValueError`` it is written to raise. Refuse the base instead: this is the
    path a future schema 3 walks, and it must not fail open.
    """
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    _entry_file(tmp_path, "germany", entry)
    base = tmp_path / "released.json"
    old = rail.merge_manifest(
        [{**entry, "region": "europe/denmark", "file": "denmark-rail.osm.pbf"}]
    )
    old["schema"] = rail.MANIFEST_SCHEMA - 1
    base.write_text(json.dumps(old))

    with pytest.raises(SystemExit) as excinfo:
        rail.main([
            "build_rail_extract.py", "manifest", "--out-dir", str(tmp_path),
            "--base", str(base),
            "--expect", json.dumps(["europe/germany"]),
        ])

    assert str(rail.MANIFEST_SCHEMA - 1) in str(excinfo.value)
    assert not (tmp_path / rail.MANIFEST_NAME).exists()


def test_a_missing_base_manifest_is_not_an_error(entry, filtered, tmp_path):
    """The first run has no release to merge into."""
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    _entry_file(tmp_path, "germany", entry)

    assert rail.main([
        "build_rail_extract.py", "manifest", "--out-dir", str(tmp_path),
        "--base", str(tmp_path / "nothing-here.json"),
        "--expect", json.dumps(["europe/germany"]),
    ]) == 0


# ---------------------------------------------------------------------------
# One region, end to end — the orchestration the workflow runs
# ---------------------------------------------------------------------------

def _fake_download(fixture: Path):
    """Stand in for the network: copy *fixture* where the real download would."""
    def download(url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(fixture.read_bytes())
        return dest
    return download


def _fake_prefilter(source: Path, dest: Path) -> Path:
    """The osmium CLI's job, without the osmium CLI: over-select everything.

    `select` does the exact pass and is what these tests are about; the CLI is
    an optimisation over the raw extract and is not installed here.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    return dest


def test_build_publishes_the_extract_and_deletes_the_raw_source(monkeypatch, tmp_path):
    """The invariant the whole pipeline rests on: the raw extract exists only on
    the runner, and only until it has been filtered. Europe raw is 34.9 GB and
    the VPS has 40 GB, so a source left behind is not untidiness.
    """
    monkeypatch.setattr(rail, "download", _fake_download(FIXTURE))
    monkeypatch.setattr(rail, "prefilter", _fake_prefilter)
    monkeypatch.setattr(rail, "source_date", lambda pbf: "2026-09-05")
    out_dir, work_dir = tmp_path / "out", tmp_path / "work"

    entry = rail.build("europe/germany", out_dir, work_dir)

    assert entry["status"] == rail.STATUS_OK
    assert list(work_dir.iterdir()) == []
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "germany-rail.entry.json", "germany-rail.osm.pbf",
    ]
    assert entry["bytes"] < FIXTURE.stat().st_size


@pytest.mark.parametrize("tags", [
    # Andorra, Malta, the Azores: no railway at all. The old guard failed the
    # matrix job — three red jobs by design, every month.
    {"uic_ref": "1"},
    # Liechtenstein: 2 stations and 823 uic nodes, and its only line is tagged
    # `railway=construction`, so no rail ways. The old guard published a 0.06 MB
    # artifact, and src/rail/builder.py then refused it with "no railway ways".
    {"railway": "station", "uic_ref": "8509000"},
])
def test_build_of_a_region_with_no_rail_publishes_nothing_and_succeeds(
    monkeypatch, tmp_path, tags
):
    """Both shapes of "this region has no rail" end the same way: the job is
    green, the region is accounted for in the manifest, and there is no
    artifact for phase 2 to reject."""
    source = tmp_path / "no-rail.osm.pbf"
    writer = osmium.SimpleWriter(str(source))
    writer.add_node(osmium.osm.mutable.Node(id=1, location=(1.5, 42.5), tags=tags))
    writer.close()
    monkeypatch.setattr(rail, "download", _fake_download(source))
    monkeypatch.setattr(rail, "prefilter", _fake_prefilter)
    monkeypatch.setattr(rail, "source_date", lambda pbf: "2026-09-05")
    out_dir, work_dir = tmp_path / "out", tmp_path / "work"

    entry = rail.build("europe/andorra", out_dir, work_dir)

    assert entry["status"] == rail.STATUS_EMPTY
    assert set(entry) == EMPTY_CONTRACT_KEYS
    assert [p.name for p in out_dir.iterdir()] == ["andorra-rail.entry.json"]


# ---------------------------------------------------------------------------
# The download — the one step nothing downstream can check
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, body: bytes = b"", text: str = "", status: int = 200,
                 url: str | None = None):
        self.body, self.text, self.status = body, text, status
        # What requests reports after following redirects; the transport fills
        # in the requested URL when a test does not stage a redirect.
        self.url = url

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def iter_content(self, chunk_size=None):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _transport(pairs):
    """A fake requests.get over {url: [response, response, ...]} — one per call."""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        queue = pairs.get(url, [_Response(status=404)])
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        response.url = response.url or url
        return response
    return get, calls


PAYLOAD = b"a raw extract, in miniature"
PAYLOAD_MD5 = "c2b9f0d5b1b1c6e6c3e7f9e0e5d3f4a1"


def test_the_download_is_checked_against_geofabriks_md5(tmp_path):
    import hashlib
    digest = hashlib.md5(PAYLOAD).hexdigest()
    url = "https://download.geofabrik.de/europe/denmark-latest.osm.pbf"
    get, calls = _transport({
        url: [_Response(body=PAYLOAD)],
        f"{url}.md5": [_Response(text=f"{digest}  denmark-latest.osm.pbf")],
    })
    dest = tmp_path / "denmark.osm.pbf"

    rail.download(url, dest, get=get, sleep=lambda s: None)

    assert dest.read_bytes() == PAYLOAD
    assert f"{url}.md5" in calls


def test_the_md5_comes_from_the_mirror_that_served_the_file(tmp_path):
    """The October 2026 scheduled run lost Germany this way: Geofabrik 307s its
    largest extracts to a mirror and has no ``-latest`` .md5 of its own for
    them, so asking the original host 404s four times and the region fails."""
    import hashlib
    digest = hashlib.md5(PAYLOAD).hexdigest()
    url = "https://download.geofabrik.de/europe/germany-latest.osm.pbf"
    mirror = ("https://ftp5.gwdg.de/pub/misc/openstreetmap/"
              "download.geofabrik.de/germany-latest.osm.pbf")
    get, calls = _transport({
        url: [_Response(body=PAYLOAD, url=mirror)],
        f"{mirror}.md5": [_Response(text=f"{digest}  germany-latest.osm.pbf")],
    })
    dest = tmp_path / "germany.osm.pbf"

    rail.download(url, dest, get=get, attempts=1, sleep=lambda s: None)

    assert dest.read_bytes() == PAYLOAD
    assert f"{url}.md5" not in calls


def test_a_mirror_checksum_still_refuses_a_bad_file(tmp_path):
    """Following the redirect must not turn the check into a formality."""
    url = "https://download.geofabrik.de/europe/germany-latest.osm.pbf"
    mirror = "https://mirror.example/germany-latest.osm.pbf"
    get, _ = _transport({
        url: [_Response(body=PAYLOAD[:10], url=mirror)],
        f"{mirror}.md5": [_Response(text=f"{PAYLOAD_MD5}  germany-latest.osm.pbf")],
    })

    with pytest.raises(RuntimeError, match="md5 mismatch"):
        rail.download(url, tmp_path / "germany.osm.pbf", get=get,
                      attempts=1, sleep=lambda s: None)


def test_a_truncated_download_is_refused(tmp_path):
    """Everything downstream is checksummed twice and the input was checked
    once by nobody. A half-file filters cleanly into half a country."""
    url = "https://download.geofabrik.de/europe/denmark-latest.osm.pbf"
    get, _ = _transport({
        url: [_Response(body=PAYLOAD[:10])],
        f"{url}.md5": [_Response(text=f"{PAYLOAD_MD5}  denmark-latest.osm.pbf")],
    })

    with pytest.raises(RuntimeError, match="md5 mismatch"):
        rail.download(url, tmp_path / "denmark.osm.pbf", get=get,
                      attempts=2, sleep=lambda s: None)


def test_a_transient_failure_is_retried(tmp_path):
    """49 monthly jobs, six at a time, against a mirror running on donated
    bandwidth: one failure is a retry, not a missing region in the release."""
    import hashlib
    digest = hashlib.md5(PAYLOAD).hexdigest()
    url = "https://download.geofabrik.de/europe/denmark-latest.osm.pbf"
    get, calls = _transport({
        url: [_Response(status=503), _Response(body=PAYLOAD)],
        f"{url}.md5": [_Response(text=f"{digest}  denmark-latest.osm.pbf")],
    })
    slept = []

    dest = rail.download(url, tmp_path / "denmark.osm.pbf", get=get,
                         sleep=slept.append)

    assert dest.read_bytes() == PAYLOAD
    assert slept, "a retry with no backoff is a retry into the same failure"


def test_the_download_gives_up_eventually(tmp_path):
    url = "https://download.geofabrik.de/europe/denmark-latest.osm.pbf"
    get, calls = _transport({
        url: [_Response(status=503)],
        f"{url}.md5": [_Response(text=f"{PAYLOAD_MD5}  denmark-latest.osm.pbf")],
    })

    with pytest.raises(RuntimeError, match="503"):
        rail.download(url, tmp_path / "denmark.osm.pbf", get=get,
                      attempts=3, sleep=lambda s: None)
    assert calls.count(url) == 3

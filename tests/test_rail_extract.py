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

The ferry and bus layers (docs/LOCAL_TRANSPORT_DATA_PLAN.md, U7) are filtered
from the same box. Its bus data is real — 11 ``route=bus`` relations, 128 of
whose member roads lie inside it, and 21 of whose stop nodes do — and Mannheim
has no ferry, so the rest is **synthetic** again, ids
9000000000101-9000000000121 north of the light-rail line, each element one row
of the ferry and bus selections:

- 9000000000111, a ``route=ferry`` way (ferry strategy B);
- 9000000000112, a ``ferry=yes`` road with no route (ferry strategy C);
- 9000000000121, a ``route=ferry`` relation (strategy A) whose only way member,
  9000000000113, carries no tags at all — kept for the relation alone. Its
  node members are its stops: 9000000000109, a ``public_transport=
  stop_position`` beside the way, and 9000000000199, which the box does not
  hold — a stop across a border (F5);
- 9000000000114, a ``route=bus`` way (bus strategy B), the rare mapping.

None of them is rail, so the rail selection — and the published filtered file
— did not change when they were added.
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


def _select_rail(source: Path, dest: Path):
    """The rail layer alone, for the tests that are about nothing else."""
    return rail.select(source, {"rail": dest})["rail"]


@pytest.fixture(scope="module")
def layered(tmp_path_factory):
    """The fixture box put through every layer's selection in one call:
    {layer: (path, selection)}."""
    out = tmp_path_factory.mktemp("layers")
    paths = {layer: out / rail.extract_name("mannheim", layer) for layer in rail.LAYERS}
    selections = rail.select(FIXTURE, paths)
    return {layer: (paths[layer], selections[layer]) for layer in rail.LAYERS}


@pytest.fixture(scope="module")
def filtered(layered):
    """The rail layer: the fixture box put through the exact rail selection."""
    return layered["rail"]


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

        python -c "import ...; select(FIXTURE, {'rail': PUBLISHED})"
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        regenerated = Path(tmp) / "check.osm.pbf"
        _select_rail(FIXTURE, regenerated)
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

    selection = _select_rail(source, tmp_path / "out.osm.pbf")

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

    selection = _select_rail(source, tmp_path / "out.osm.pbf")

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

    selection = _select_rail(source, tmp_path / "out.osm.pbf")

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
# The ferry and bus layers (docs/LOCAL_TRANSPORT_DATA_PLAN.md, U7)
# ---------------------------------------------------------------------------

FERRY_WAY = 9000000000111       # route=ferry
FERRY_YES_WAY = 9000000000112   # ferry=yes, no route
FERRY_MEMBER = 9000000000113    # untagged, in FERRY_RELATION
BUS_WAY = 9000000000114         # route=bus
FERRY_RELATION = 9000000000121  # route=ferry
FERRY_STOP = 9000000000109      # FERRY_RELATION's stop, beside FERRY_MEMBER
FERRY_STOP_ABSENT = 9000000000199  # FERRY_RELATION's stop the box does not hold


def _layer_contents(layered, layer):
    path, _ = layered[layer]
    return _elements(path)


def _ids(elements, kind):
    return {ref for k, ref in elements if k == kind}


@pytest.mark.parametrize("tags,ferry,ferry_yes,bus", [
    ({"route": "ferry"}, True, False, False),
    ({"ferry": "yes"}, False, True, False),
    ({"ferry": "yes", "highway": "unclassified"}, False, True, False),
    ({"route": "ferry", "ferry": "yes"}, True, True, False),
    ({"route": "bus"}, False, False, True),
    # Overpass compares the values exactly.
    ({"ferry": "no"}, False, False, False),
    ({"route": "trolleybus"}, False, False, False),
    ({"route": "train"}, False, False, False),
    ({"highway": "bus_stop"}, False, False, False),
])
def test_ferry_and_bus_predicates(tags, ferry, ferry_yes, bus):
    """Mirrors rel|way["route"="ferry"], way["ferry"="yes"] and
    rel|way["route"="bus"] — _get_route_geometry's three strategies."""
    assert rail.is_ferry_route(tags) is ferry
    assert rail.is_ferry_yes(tags) is ferry_yes
    assert rail.is_bus_route(tags) is bus


def test_the_layer_table_is_the_stores():
    """Decision 9's routable sets, and the layer names, are one table used by
    this script and src/rail/builder.py alike (R2-3): if they drift, CI
    publishes `ok` a file the builder refuses, or a bbox the store disagrees
    with. The two phases share no code, so the agreement is pinned here."""
    from src.rail import store

    assert rail.LAYERS == store.LAYERS
    assert (rail.CLS_ROUTE, rail.CLS_FERRY_YES, rail.CLS_MEMBER) == (
        store.CLS_ROUTE, store.CLS_FERRY_YES, store.CLS_MEMBER)
    assert rail.ROUTABLE == dict(store.ROUTABLE)


def test_each_layer_holds_exactly_its_selection(layered):
    """Every way in a layer is there for that layer's reason, and only for it."""
    for layer in rail.LAYERS:
        elements = _layer_contents(layered, layer)
        members = {ref for (kind, _), (_, refs) in elements.items() if kind == "r"
                   for mkind, ref, _ in refs if mkind == "w"}
        for (kind, ref), (tags, _) in elements.items():
            if kind == "w":
                assert rail.keeps_way(layer, tags, ref in members), (layer, ref, tags)
            elif kind == "r":
                assert rail.keeps_relation(layer, tags), (layer, ref, tags)


def test_a_bus_routes_road_is_in_bus_and_not_in_rail(layered):
    """Bus routes run over ordinary roads, which are kept only because a bus
    relation names them — and must never reach the rail graph, where a
    Dijkstra would happily take a high street."""
    bus = _layer_contents(layered, "bus")
    rail_ways = _ids(_layer_contents(layered, "rail"), "w")
    # The bus file no longer carries `highway`, so the roads are named from
    # the raw box.
    raw = _elements(FIXTURE)
    roads = [ref for ref in _ids(bus, "w")
             if raw[("w", ref)][0].get("highway") in ("primary", "trunk", "secondary")]
    assert roads, "the fixture's bus relations hold no road inside the box"
    assert not set(roads) & rail_ways
    assert BUS_WAY in _ids(bus, "w")
    assert {tags.get("route") for (kind, _), (tags, _) in bus.items() if kind == "r"} \
        == {"bus"}


def test_the_ferry_layer_holds_all_three_strategies(layered):
    ferry = _layer_contents(layered, "ferry")
    assert {FERRY_WAY, FERRY_YES_WAY, FERRY_MEMBER} == _ids(ferry, "w")
    assert {FERRY_RELATION} == _ids(ferry, "r")
    for layer in ("rail", "bus"):
        assert FERRY_YES_WAY not in _ids(_layer_contents(layered, layer), "w")


def test_the_layer_counts_are_pinned(layered):
    """The same purpose as the rail pin above: a selection change fails loudly.
    `ways` is the routable set — for bus, the 128 member roads and the one
    route=bus way."""
    got = {layer: (sel.ways, sel.relations, sel.member_ways, sel.stations,
                   sel.uic_nodes)
           for layer, (_, sel) in layered.items()}
    assert got == {
        "rail": (EXPECTED_WAYS, EXPECTED_RELATIONS, EXPECTED_MEMBER_WAYS,
                 EXPECTED_STATIONS, EXPECTED_UIC_NODES),
        "ferry": (3, 1, 1, 0, 0),
        "bus": (129, 11, 128, 0, 0),
    }


def _stops(elements) -> set[int]:
    """The node members of every relation in *elements*."""
    return {ref for (kind, _), (_, refs) in elements.items() if kind == "r"
            for mkind, ref, _ in refs if mkind == "n"}


def test_layer_files_keep_their_way_nodes_and_stops_and_nothing_else(layered):
    """Ferry and bus have no node row: no UIC lookup, no station. Every node is
    a kept way's or a kept relation's stop, every kept way has all of its, and
    every stop the source holds is there."""
    raw_nodes = _ids(_elements(FIXTURE), "n")
    for layer in ("ferry", "bus"):
        elements = _layer_contents(layered, layer)
        referenced = {ref for (kind, _), (_, refs) in elements.items() if kind == "w"
                      for ref in refs}
        stops = _stops(elements) & raw_nodes
        assert stops - referenced, f"{layer}: no stop off the ways to test with"
        assert _ids(elements, "n") == referenced | stops, layer


def test_ferry_and_bus_stops_are_written_where_the_source_has_them(layered):
    """What strategy A's bridge reads off Overpass's `out geom`: each stop's
    position. A stop the source does not hold cannot be written, and is not
    invented (F5)."""
    raw = _elements(FIXTURE)
    ferry = _layer_contents(layered, "ferry")
    assert ferry[("n", FERRY_STOP)][1] == raw[("n", FERRY_STOP)][1]
    # Its tags are stripped like any node's: nothing reads them.
    assert ferry[("n", FERRY_STOP)][0] == {}
    assert ("n", FERRY_STOP_ABSENT) not in ferry
    assert ("n", FERRY_STOP_ABSENT) not in raw
    bus = _layer_contents(layered, "bus")
    bus_stops = _stops(bus) & _ids(raw, "n")
    assert len(bus_stops) == 21
    for ref in bus_stops:
        assert bus[("n", ref)][1] == raw[("n", ref)][1], ref
    # The stops do not reach the rail file by this route — rail's are #570, and
    # its published fixture pins it.
    assert FERRY_STOP not in _ids(_layer_contents(layered, "rail"), "n")


def test_the_stop_counts_are_pinned(layered):
    """Distinct stop nodes written; a broken stop closure reads 0 here."""
    assert {layer: sel.stop_nodes for layer, (_, sel) in layered.items()} == {
        "rail": 0, "ferry": 1, "bus": 21}


def test_ferry_and_bus_stores_locate_every_stop_the_file_holds(layered, tmp_path):
    """The builder half of F5: every relation node member the layer file holds
    is located in the store, at the file's position, with or without a
    uic_ref — and comes back from `relation_geometry`, the path strategy A
    reads, as Overpass's `out geom` would return it."""
    from src.rail.builder import build_store
    from src.rail.store import RailStore

    for layer in ("ferry", "bus"):
        path, _ = layered[layer]
        elements = _elements(path)
        held = _ids(elements, "n")
        store_path = tmp_path / f"{layer}.sqlite"
        stats = build_store(path, store_path, region="europe/germany", layer=layer)
        slots = [(rel, ref) for (kind, rel), (_, refs) in elements.items()
                 if kind == "r" for mkind, ref, _ in refs if mkind == "n"]
        assert stats["relation_nodes"] == len(slots), layer
        assert stats["relation_nodes_located"] == sum(ref in held for _, ref in slots), layer
        with RailStore(store_path) as store:
            for rel in {rel for rel, _ in slots}:
                (geometry,) = store.relation_geometry([rel])
                for member in geometry["members"]:
                    if member["type"] != "node":
                        continue
                    if member["ref"] in held:
                        x, y = elements[("n", member["ref"])][1]
                        assert (member["lon"], member["lat"]) == pytest.approx(
                            (x / 1e7, y / 1e7), abs=1e-7), (layer, rel, member)
                    else:
                        assert not member["held"] and "lat" not in member
    with RailStore(tmp_path / "ferry.sqlite") as store:
        stops = store.relation_stops(FERRY_RELATION)
    assert [(s["ref"], s["role"], s["lat"] is not None) for s in stops] == [
        (FERRY_STOP, "stop", True), (FERRY_STOP_ABSENT, "stop", False)]


def test_bbox_per_layer_is_the_extent_of_its_routable_set(layered):
    """Over bits 0|1|2 for ferry and 0|2 for bus, as for rail over bit 0."""
    for layer in ("ferry", "bus"):
        elements = _layer_contents(layered, layer)
        _, selection = layered[layer]
        members = {ref for (kind, _), (_, refs) in elements.items() if kind == "r"
                   for mkind, ref, _ in refs if mkind == "w"}
        routable = {node for (kind, ref), (tags, refs) in elements.items()
                    if kind == "w"
                    and rail.way_class(layer, tags, ref in members) & rail.ROUTABLE[layer]
                    for node in refs}
        xs = [elements[("n", n)][1] for n in routable]
        lons = [x / 1e7 for x, _ in xs]
        lats = [y / 1e7 for _, y in xs]
        assert selection.bbox == pytest.approx(
            [min(lons), min(lats), max(lons), max(lats)], abs=1e-5), layer


def test_bbox_per_layer_agrees_with_the_store(layered, tmp_path):
    """U8's builder measures each store's extent over the same routable set,
    and refuses none of the three files CI calls `ok`."""
    from src.rail.builder import build_store
    from src.rail.store import RailStore

    for layer, (path, selection) in layered.items():
        store_path = tmp_path / f"{layer}.sqlite"
        stats = build_store(path, store_path, region="europe/germany", layer=layer)
        assert stats["routable_ways"] == selection.ways, layer
        with RailStore(store_path) as store:
            min_lat, min_lon, max_lat, max_lon = store.bbox
        assert selection.bbox == pytest.approx(
            [min_lon, min_lat, max_lon, max_lat], abs=1e-5), layer


def _write_source(path: Path, ways=(), relations=()):
    """A tiny extract: two nodes per way, laid out from (8.0, 49.0)."""
    writer = osmium.SimpleWriter(str(path))
    for i, _ in enumerate(ways):
        writer.add_node(osmium.osm.mutable.Node(id=2 * i + 1, location=(8.0 + i, 49.0)))
        writer.add_node(osmium.osm.mutable.Node(id=2 * i + 2, location=(8.1 + i, 49.1)))
    for i, (way_id, tags) in enumerate(ways):
        writer.add_way(osmium.osm.mutable.Way(id=way_id, nodes=[2 * i + 1, 2 * i + 2],
                                              tags=tags))
    for rel_id, members, tags in relations:
        writer.add_relation(osmium.osm.mutable.Relation(
            id=rel_id, members=[("w", m, "") for m in members], tags=tags))
    writer.close()


def _select_all(source: Path, out: Path):
    return rail.select(source, {layer: out / f"x-{layer}.osm.pbf" for layer in rail.LAYERS})


def test_a_layer_with_nothing_routable_is_empty(tmp_path):
    """A rail-only region: ferry and bus are `empty`, with nothing to cover."""
    source = tmp_path / "src.osm.pbf"
    _write_source(source, ways=[(10, {"railway": "rail"})])

    selections = _select_all(source, tmp_path)

    assert selections["rail"].ways == 1
    for layer in ("ferry", "bus"):
        assert selections[layer].ways == 0
        assert selections[layer].bbox == []


def test_a_ferry_layer_of_only_ferry_yes_ways_is_not_empty(tmp_path):
    """The Åland island hoppers: no route=ferry anywhere, and strategy C is
    the only one that finds them. Bit 0 alone would call this region empty."""
    source = tmp_path / "src.osm.pbf"
    _write_source(source, ways=[(10, {"ferry": "yes", "highway": "unclassified"})])

    selection = _select_all(source, tmp_path)["ferry"]

    assert selection.ways == 1
    assert selection.bbox == [8.0, 49.0, 8.1, 49.1]


def test_a_bus_layer_of_only_relation_members_is_not_empty(tmp_path):
    """How bus is mapped nearly everywhere: relations over untagged-for-bus
    roads, no route=bus way at all (R2-3)."""
    from src.rail.builder import build_store

    source = tmp_path / "src.osm.pbf"
    _write_source(source, ways=[(10, {"highway": "primary"})],
                  relations=[(20, [10], {"type": "route", "route": "bus"})])

    selection = _select_all(source, tmp_path)["bus"]

    assert selection.ways == 1
    assert selection.bbox == [8.0, 49.0, 8.1, 49.1]
    # …and the builder agrees: a store, not a refusal.
    stats = build_store(tmp_path / "x-bus.osm.pbf", tmp_path / "bus.sqlite", layer="bus")
    assert stats["routable_ways"] == 1


def test_a_road_no_bus_route_names_is_in_no_layer(tmp_path):
    source = tmp_path / "src.osm.pbf"
    _write_source(source, ways=[(10, {"highway": "primary"})])

    assert all(s.ways == 0 for s in _select_all(source, tmp_path).values())


# ---------------------------------------------------------------------------
# Ferry and bus carry only the tags the builder reads (U7, owner decision)
# ---------------------------------------------------------------------------

def test_ferry_and_bus_carry_only_the_tags_the_builder_reads(layered):
    """A bus layer is roads, and a road's `highway`, `surface` and `name` are
    most of what it weighs. Nothing reads them, so they are not published."""
    for layer in ("ferry", "bus"):
        for (kind, ref), (tags, _) in _layer_contents(layered, layer).items():
            assert set(tags) <= rail.LAYER_TAGS[kind], (layer, kind, ref, tags)


def test_stripping_drops_what_nothing_reads_and_keeps_what_it_does(layered):
    raw = _elements(FIXTURE)
    bus = _layer_contents(layered, "bus")
    relation = next(ref for kind, ref in bus if kind == "r"
                    and {"ref", "operator", "network", "name"} <= set(raw[("r", ref)][0]))
    road = next(ref for kind, ref in bus if kind == "w"
                and {"highway", "surface"} <= set(raw[("w", ref)][0]))
    rel_tags, road_tags = bus[("r", relation)][0], bus[("w", road)][0]
    assert not {"ref", "operator", "network", "type"} & set(rel_tags)
    assert rel_tags == {"route": "bus", "name": raw[("r", relation)][0]["name"]}
    assert not {"highway", "surface", "name"} & set(road_tags)
    ferry = _layer_contents(layered, "ferry")
    assert ferry[("w", FERRY_WAY)][0]["route"] == "ferry"
    assert ferry[("w", FERRY_YES_WAY)][0] == {"ferry": "yes"}
    assert ferry[("r", FERRY_RELATION)][0]["route"] == "ferry"
    # Selected and untagged is still written: the member is what the relation
    # is drawn along.
    assert ferry[("w", FERRY_MEMBER)][0] == {}
    # Members, roles and node lists are untouched.
    for layer in ("ferry", "bus"):
        for key, (_, refs) in _layer_contents(layered, layer).items():
            if key[0] != "n":
                assert refs == raw[key][1], (layer, key)


def test_the_rail_layer_keeps_every_tag(layered):
    """Rail is not stripped: its published fixture pins the file as it is."""
    raw = _elements(FIXTURE)
    for key, (tags, _) in _layer_contents(layered, "rail").items():
        assert tags == raw[key][0], key


def _tables(path: Path) -> dict:
    """Every row of every table in a store, but the two meta values that are
    the build's own clock."""
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        names = [n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        tables = {n: sorted(conn.execute(f'SELECT * FROM "{n}"').fetchall(), key=repr)
                  for n in names}
    finally:
        conn.close()
    tables["meta"] = [row for row in tables["meta"]
                      if row[0] not in ("built_at", "build_seconds")]
    return tables


def test_a_stripped_layer_builds_the_same_store(layered, tmp_path, monkeypatch):
    """The point of the keep-list: the store is what matters, and stripping
    must not change a row of it. Built with the builder's own command, from
    each stripped file and from the same selection written with every tag."""
    import subprocess

    monkeypatch.setattr(rail, "_strip", lambda obj, keep: obj)
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    rail.select(FIXTURE, {layer: full_dir / layered[layer][0].name
                          for layer in ("ferry", "bus")})
    for layer in ("ferry", "bus"):
        stripped = layered[layer][0]
        full = full_dir / stripped.name
        assert full.stat().st_size > stripped.stat().st_size, layer
        stores = []
        for pbf, name in ((stripped, "stripped"), (full, "full")):
            store = tmp_path / f"{layer}-{name}.sqlite"
            subprocess.run(
                [sys.executable, "-m", "src.rail.builder", str(pbf), str(store),
                 "--region", "europe/germany", "--layer", layer],
                cwd=ROOT, check=True, capture_output=True)
            stores.append(_tables(store))
        assert stores[0] == stores[1], layer


def test_the_keep_list_covers_every_tag_the_builder_reads():
    """If the builder starts reading a tag the keep-list drops, ferry and bus
    stores lose it silently — the equality test above only sees what the
    fixture happens to exercise. `service` is read for rail alone."""
    import re

    source = (ROOT / "src" / "rail" / "builder.py").read_text(encoding="utf-8")
    read = set(re.findall(r'tags(?:\.get\(|\[)"(\w+)"', source))
    read |= set(re.findall(r'"(\w+)" (?:not )?in tags\b', source))
    assert read, "the pattern no longer finds the builder's tag reads"
    kept = set().union(*rail.LAYER_TAGS.values())
    assert read - kept == {"service"}


def _matches(expression: str, kind: str, tags) -> bool:
    """Does one `osmium tags-filter` expression (``t/key`` or ``t/key=v1,v2``)
    match an object of *kind* with *tags*?"""
    types, _, rest = expression.partition("/")
    key, _, values = rest.partition("=")
    if kind not in types or key not in tags:
        return False
    return not values or tags[key] in values.split(",")


def test_the_prefilter_lets_every_layer_through(layered):
    """`select` can only narrow what the osmium CLI's pass keeps, so a layer
    the prefilter forgets is empty in every region — silently, since an empty
    layer is a normal outcome. Checked against what each layer actually kept:
    everything held for its own tags must match an expression. What is held by
    reference — a relation's member ways, a way's nodes — tags-filter keeps
    without being asked."""
    expressions = rail.prefilter_expressions()
    for layer in rail.LAYERS:
        for (kind, ref), (tags, _) in _layer_contents(layered, layer).items():
            own = {
                "n": lambda: rail.is_uic_node(tags),
                "w": lambda: rail.keeps_way(layer, tags, member=False),
                "r": lambda: rail.keeps_relation(layer, tags),
            }[kind]()
            if own:
                assert any(_matches(e, kind, tags) for e in expressions), \
                    (layer, kind, ref, tags)


# ---------------------------------------------------------------------------
# The manifest — the phase 1 / phase 2 contract
# ---------------------------------------------------------------------------

CONTRACT_KEYS = {
    "region", "layer", "status", "file", "source", "source_date", "sha256",
    "bytes", "ways", "relations", "stations", "bbox",
}
# An `empty` layer has no file, so no checksum, size or extent either.
EMPTY_CONTRACT_KEYS = {"region", "layer", "status", "source", "source_date"}


@pytest.fixture(scope="module")
def entry(filtered):
    path, selection = filtered
    return rail.manifest_entry("europe/germany", "rail", path, selection, "2026-09-05")


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
    assert manifest["schema"] == rail.MANIFEST_SCHEMA == 3
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
    (tmp_path / rail.entry_name("germany", "rail")).write_text(json.dumps(entry))

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
    (directory / rail.entry_name(slug, entry["layer"])).write_text(json.dumps(entry))


def test_an_empty_region_is_recorded_without_a_file(entry, tmp_path):
    """It is in the manifest so phase 3 can tell "no rail here" from "we never
    built it", and so the completeness check counts it as accounted for."""
    empty = rail.empty_entry("europe/andorra", "rail", "2026-09-05")
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
        rail.empty_entry("europe/andorra", "rail", "2026-01-01"),
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
    path a future schema 4 walks, and it must not fail open.
    """
    path, _ = filtered
    (tmp_path / path.name).write_bytes(path.read_bytes())
    _entry_file(tmp_path, "germany", entry)
    base = tmp_path / "released.json"
    old = rail.merge_manifest(
        [{**entry, "region": "europe/denmark", "file": "denmark-rail.osm.pbf"}]
    )
    old["schema"] = 1
    base.write_text(json.dumps(old))

    with pytest.raises(SystemExit) as excinfo:
        rail.main([
            "build_rail_extract.py", "manifest", "--out-dir", str(tmp_path),
            "--base", str(base),
            "--expect", json.dumps(["europe/germany"]),
        ])

    assert "schema 1" in str(excinfo.value)
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
# Layers in the manifest: rail is completeness, ferry and bus are not
# ---------------------------------------------------------------------------

@pytest.fixture
def layer_dir(layered, tmp_path):
    """A publish directory holding this run's Germany, all three layers."""
    out = tmp_path / "dist"
    out.mkdir()
    for layer, (path, selection) in layered.items():
        target = out / rail.extract_name("germany", layer)
        target.write_bytes(path.read_bytes())
        _entry_file(out, "germany", rail.manifest_entry(
            "europe/germany", layer, target, selection, "2026-09-05"))
    return out


def _manifest_argv(out: Path, *extra: str) -> list[str]:
    return ["build_rail_extract.py", "manifest", "--out-dir", str(out),
            "--expect", json.dumps(["europe/germany"]), *extra]


def test_every_layer_is_published_and_ordered_rail_last(layer_dir):
    """Rail sorts last within a region — the installed manifest's order too —
    so a reader from before layers, keying by region and keeping the last,
    lands on rail."""
    manifest = rail.collect_manifest(layer_dir)
    assert [(e["region"], e["layer"]) for e in manifest["regions"]] == [
        ("europe/germany", "bus"), ("europe/germany", "ferry"),
        ("europe/germany", "rail")]


def test_a_missing_bus_layer_warns_and_publishes(layer_dir, capsys):
    """Step 4: a broken bus layer — dropped by the size guard — must not cost
    the region its rail. That region's bus keeps going to Overpass."""
    (layer_dir / "germany-bus.osm.pbf").unlink()
    (layer_dir / rail.entry_name("germany", "bus")).unlink()

    assert rail.main(_manifest_argv(layer_dir)) == 0

    out = capsys.readouterr().out
    assert "::warning::europe/germany bus: no entry" in out
    assert "::error::" not in out
    written = json.loads((layer_dir / rail.MANIFEST_NAME).read_text())
    assert {e["layer"] for e in written["regions"]} == {"rail", "ferry"}


def test_an_empty_layer_is_an_answer_not_a_warning(layer_dir, capsys):
    (layer_dir / "germany-ferry.osm.pbf").unlink()
    _entry_file(layer_dir, "germany",
                rail.empty_entry("europe/germany", "ferry", "2026-09-05"))

    assert rail.main(_manifest_argv(layer_dir)) == 0
    assert "::warning::" not in capsys.readouterr().out


def test_a_missing_rail_layer_refuses(layer_dir, capsys):
    """Rail is what completeness means: ferry and bus entries do not make a
    region covered."""
    (layer_dir / "germany-rail.osm.pbf").unlink()
    (layer_dir / rail.entry_name("germany", "rail")).unlink()

    assert rail.main(_manifest_argv(layer_dir)) == 1
    assert "europe/germany" in capsys.readouterr().out


def test_a_schema_2_base_merges_as_rail(layer_dir):
    """R1-10: the first subset recovery after this ships patches a schema 2
    release. Its entries are rail entries without the word; refusing it would
    leave no recovery until the next full run."""
    base = layer_dir / "released.json"
    denmark = {"region": "europe/denmark", "status": "ok",
               "file": "denmark-rail.osm.pbf", "source": "x", "source_date": "2026-09-02",
               "sha256": "0" * 64, "bytes": 1, "ways": 1, "relations": 0,
               "stations": 0, "bbox": [0, 0, 1, 1]}
    base.write_text(json.dumps({
        "schema": 2, "generated_at": "2026-09-02T00:00:00Z",
        "regions": [denmark,
                    {"region": "europe/andorra", "status": "empty",
                     "source": "x", "source_date": "2026-09-02"},
                    # Rebuilt by this run: replaced, not merged.
                    {**denmark, "region": "europe/germany",
                     "file": "germany-rail.osm.pbf"}],
    }))

    assert rail.main(_manifest_argv(layer_dir, "--base", str(base))) == 0

    written = json.loads((layer_dir / rail.MANIFEST_NAME).read_text())
    assert written["schema"] == 3
    entries = {(e["region"], e["layer"]): e for e in written["regions"]}
    assert set(entries) == {
        ("europe/andorra", "rail"), ("europe/denmark", "rail"),
        ("europe/germany", "bus"), ("europe/germany", "ferry"),
        ("europe/germany", "rail")}
    assert entries[("europe/denmark", "rail")] == {**denmark, "layer": "rail"}
    assert entries[("europe/germany", "rail")]["source_date"] == "2026-09-05"


def test_a_schema_3_base_carries_every_layer(layer_dir):
    base = layer_dir / "released.json"
    carried = [rail.empty_entry("europe/denmark", layer, "2026-09-02")
               for layer in rail.LAYERS]
    base.write_text(json.dumps(rail.merge_manifest(carried)))

    assert rail.main(_manifest_argv(layer_dir, "--base", str(base))) == 0

    written = json.loads((layer_dir / rail.MANIFEST_NAME).read_text())
    assert sorted(e["layer"] for e in written["regions"]
                  if e["region"] == "europe/denmark") == ["bus", "ferry", "rail"]


# ---------------------------------------------------------------------------
# Only the published layers (RAIL_PUBLISH_LAYERS, I1-2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, layers", [
    (None, ["rail"]),
    ("", ["rail"]),
    ("ferry", ["rail", "ferry"]),
    ("bus, rail", ["rail", "bus"]),
    ("bus ferry rail", ["rail", "ferry", "bus"]),
])
def test_rail_is_always_published_and_the_order_is_fixed(text, layers):
    assert rail.parse_layers(text) == layers


@pytest.mark.parametrize("text", ["tram", "rail Bus", "ferry;bus"])
def test_a_name_that_is_not_a_layer_is_refused(text):
    with pytest.raises(ValueError, match="unknown layer"):
        rail.parse_layers(text)


def test_a_layer_not_published_is_neither_entered_nor_warned_about(layer_dir, capsys):
    """Its files may be on disk; the manifest still holds rail alone, and the
    absence of a layer the run does not publish is not a missing layer."""
    assert rail.main(_manifest_argv(layer_dir, "--layers", "rail")) == 0

    assert "::warning::" not in capsys.readouterr().out
    written = json.loads((layer_dir / rail.MANIFEST_NAME).read_text())
    assert {e["layer"] for e in written["regions"]} == {"rail"}


def test_a_published_layer_that_is_missing_still_warns(layer_dir, capsys):
    (layer_dir / "germany-ferry.osm.pbf").unlink()
    (layer_dir / rail.entry_name("germany", "ferry")).unlink()

    assert rail.main(_manifest_argv(layer_dir, "--layers", "rail ferry")) == 0

    out = capsys.readouterr().out
    assert "::warning::europe/germany ferry: no entry" in out
    assert "bus: no entry" not in out


def test_a_layer_switched_off_leaves_the_patched_release_too(layer_dir):
    """A subset run carrying a release that has Denmark's bus does not carry
    it once bus is off: switching a layer off takes it out of the manifest the
    boxes install, not only out of this run's builds."""
    base = layer_dir / "released.json"
    base.write_text(json.dumps(rail.merge_manifest(
        [rail.empty_entry("europe/denmark", layer, "2026-09-02")
         for layer in rail.LAYERS])))

    assert rail.main(_manifest_argv(layer_dir, "--base", str(base),
                                    "--layers", "rail ferry")) == 0

    written = json.loads((layer_dir / rail.MANIFEST_NAME).read_text())
    assert sorted((e["region"], e["layer"]) for e in written["regions"]) == [
        ("europe/denmark", "ferry"), ("europe/denmark", "rail"),
        ("europe/germany", "ferry"), ("europe/germany", "rail")]


def test_the_manifest_command_refuses_an_unknown_layer(layer_dir):
    with pytest.raises(ValueError, match="unknown layer"):
        rail.main(_manifest_argv(layer_dir, "--layers", "rail tram"))


def test_verify_rejects_an_unknown_layer(entry, filtered):
    path, _ = filtered
    with pytest.raises(ValueError, match="unknown layer"):
        rail.verify_manifest(rail.merge_manifest([{**entry, "layer": "tram"}]),
                             path.parent)


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

    entries = rail.build("europe/germany", out_dir, work_dir)

    assert {layer: e["status"] for layer, e in entries.items()} == {
        "rail": rail.STATUS_OK, "ferry": rail.STATUS_OK, "bus": rail.STATUS_OK}
    assert list(work_dir.iterdir()) == []
    # Rail's names are the ones every release before layers published.
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "germany-bus.entry.json", "germany-bus.osm.pbf",
        "germany-ferry.entry.json", "germany-ferry.osm.pbf",
        "germany-rail.entry.json", "germany-rail.osm.pbf",
    ]
    for layer, entry in entries.items():
        assert entry["layer"] == layer
        assert entry["file"] == f"germany-{layer}.osm.pbf"
        assert entry["bytes"] < FIXTURE.stat().st_size


def test_build_selects_only_the_layers_it_publishes(monkeypatch, tmp_path):
    """A layer the run does not publish is not selected at all — no file, no
    entry — which is what saves Germany's bus pass while bus is off."""
    monkeypatch.setattr(rail, "download", _fake_download(FIXTURE))
    monkeypatch.setattr(rail, "prefilter", _fake_prefilter)
    monkeypatch.setattr(rail, "source_date", lambda pbf: "2026-09-05")
    selected = []
    real_select = rail.select

    def spy(source, dests):
        selected.append(sorted(dests))
        return real_select(source, dests)

    monkeypatch.setattr(rail, "select", spy)
    out_dir, work_dir = tmp_path / "out", tmp_path / "work"

    assert rail.main(["build_rail_extract.py", "build", "europe/germany",
                      "--out-dir", str(out_dir), "--work-dir", str(work_dir),
                      "--layers", "ferry"]) == 0

    assert selected == [["ferry", "rail"]]
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "germany-ferry.entry.json", "germany-ferry.osm.pbf",
        "germany-rail.entry.json", "germany-rail.osm.pbf",
    ]


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

    entries = rail.build("europe/andorra", out_dir, work_dir)

    for entry in entries.values():
        assert entry["status"] == rail.STATUS_EMPTY
        assert set(entry) == EMPTY_CONTRACT_KEYS
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "andorra-bus.entry.json", "andorra-ferry.entry.json",
        "andorra-rail.entry.json"]


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

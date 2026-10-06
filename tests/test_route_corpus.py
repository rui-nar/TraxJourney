"""The route corpus runner (issue #345, U4).

Every test resolves real legs against a real store built from the Luxembourg
fixture extract — the same store the Phase 3 parity tests use — so what is
checked is the runner's verdict on a real resolve, not on a stubbed polyline.
Nothing here touches the network: the runner reads stores only.

The Luxembourg leg the tests turn on, Luxembourg Gare -> Ettelbruck, measures
(strategy relation_endpoints) 30 km, both ends within 0.25 km of the stations,
0.3 km from Mersch, which it runs through, and 10.9 km from Kleinbettingen,
which is on another line.

Ferry and bus legs (U10) resolve against ferry and bus stores built from
synthetic extracts beside that rail store, with Overpass and every socket
refused, so a ferry or bus answer is provably the stores'.
"""
import importlib.util
import os
import socket
import sys
from pathlib import Path

import pytest
import yaml

from src.services import overpass_service as ov
from tests.test_rail_source import FIXTURE, REGION, build_region, ok_entry, write_manifest
from tests.test_route_source_ferry_bus import (
    EAST,
    EAST_HALF,
    PORT_STOPS,
    WEST,
    WEST_HALF,
    ferry_relation,
    write_layer,
)
from tests.test_route_source_ferry_bus import build_region as build_layer
from tests.test_route_source_ferry_bus import entry as layer_entry

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "route_corpus", ROOT / "scripts" / "route_corpus.py")
rc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rc
_spec.loader.exec_module(rc)

GARE = {"label": "Luxembourg", "lat": 49.5999681, "lon": 6.1342493}
ETTELBRUCK = {"label": "Ettelbruck", "lat": 49.8475, "lon": 6.1036}
MERSCH = {"label": "Mersch", "lat": 49.7489, "lon": 6.1063}
KLEINBETTINGEN = {"label": "Kleinbettingen", "lat": 49.6385071, "lon": 5.9823816}

# What the leg really does, written as an expectation it meets.
TRUE_SHAPE = {
    "max_endpoint_km": 1,
    "length_km": [27, 33],
    "via": [{**MERSCH, "within_km": 2}],
    "avoid": [{**KLEINBETTINGEN, "within_km": 5}],
    "strategy_not": ["straight"],
    "degraded": False,
}


def leg(name="Luxembourg -> Ettelbruck", regions=(REGION,), **fields):
    return {"name": name, "mode": "rail", "from": dict(GARE), "to": dict(ETTELBRUCK),
            "regions": list(regions), **fields}


@pytest.fixture(scope="module")
def store_dir(tmp_path_factory):
    directory = str(tmp_path_factory.mktemp("raildata"))
    bbox = build_region(directory, REGION, FIXTURE)
    write_manifest(directory, [ok_entry(REGION, bbox)])
    return directory


def run_cli(tmp_path, store_dir, legs, *flags):
    corpus = tmp_path / "corpus.yml"
    corpus.write_text(yaml.safe_dump({"legs": legs}), encoding="utf-8")
    return rc.main(["route_corpus.py", store_dir, "--corpus", str(corpus), *flags])


def status_of(capsys, name="Luxembourg -> Ettelbruck"):
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if name in line)
    return line.split()[0], line


# ---------------------------------------------------------------------------
# Ordinary legs
# ---------------------------------------------------------------------------

def test_a_leg_that_meets_its_expectations_passes(tmp_path, store_dir, capsys):
    assert run_cli(tmp_path, store_dir, [leg(expect=TRUE_SHAPE)]) == 0
    status, line = status_of(capsys)
    assert status == rc.PASS
    assert "30 km" in line


def test_a_missed_via_fails_and_names_the_town(tmp_path, store_dir, capsys):
    expect = {**TRUE_SHAPE, "via": [{**KLEINBETTINGEN, "within_km": 2}],
              "avoid": []}
    assert run_cli(tmp_path, store_dir, [leg(expect=expect)]) == 1
    status, line = status_of(capsys)
    assert status == rc.FAIL
    assert "via Kleinbettingen" in line


def test_a_length_outside_the_band_fails(tmp_path, store_dir, capsys):
    assert run_cli(tmp_path, store_dir,
                   [leg(expect={**TRUE_SHAPE, "length_km": [40, 50]})]) == 1
    status, line = status_of(capsys)
    assert status == rc.FAIL
    assert "length 30 km outside [40, 50]" in line


def test_passing_near_an_avoided_town_fails(tmp_path, store_dir, capsys):
    expect = {**TRUE_SHAPE, "avoid": [{**MERSCH, "within_km": 2}]}
    assert run_cli(tmp_path, store_dir, [leg(expect=expect)]) == 1
    status, line = status_of(capsys)
    assert status == rc.FAIL
    assert "avoid Mersch" in line


def test_an_end_farther_than_the_limit_from_its_station_fails(
        tmp_path, store_dir, capsys):
    """The line stops 0.24 km from Ettelbruck; a 0.2 km limit is what an
    endpoint regression (#359, #363) looks like to the runner."""
    assert run_cli(tmp_path, store_dir,
                   [leg(expect={**TRUE_SHAPE, "max_endpoint_km": 0.2})]) == 1
    status, line = status_of(capsys)
    assert status == rc.FAIL
    assert "end 0.24 km off > 0.2" in line
    assert "start" not in line.split(";", 1)[1]


def test_a_resolve_that_raises_fails_rather_than_crashing_the_run(
        tmp_path, store_dir, capsys, monkeypatch):
    def boom(stops, source):
        raise RuntimeError("store exploded")
    monkeypatch.setattr(rc, "_resolve_rail", boom)
    assert run_cli(tmp_path, store_dir, [leg(expect=TRUE_SHAPE)]) == 1
    status, line = status_of(capsys)
    assert status == rc.FAIL
    assert "store exploded" in line


# ---------------------------------------------------------------------------
# known_bad legs
# ---------------------------------------------------------------------------

# A "right answer" this leg does not give: the tests' stand-in for a bug.
WRONG_EXPECT = {**TRUE_SHAPE, "via": [{**KLEINBETTINGEN, "within_km": 2}], "avoid": []}


def test_a_known_bad_leg_that_stays_bad_does_not_fail_the_run(
        tmp_path, store_dir, capsys):
    bad = leg(known_bad="goes the wrong way", expect=WRONG_EXPECT, current=TRUE_SHAPE)
    assert run_cli(tmp_path, store_dir, [bad]) == 0
    status, line = status_of(capsys)
    assert status == rc.KNOWN_BAD_UNCHANGED
    assert "goes the wrong way" in line


def test_a_known_bad_leg_that_starts_passing_fails_the_run(tmp_path, store_dir, capsys):
    """A fix must flip the entry in its own diff, so an unannounced one fails."""
    fixed = leg(known_bad="used to be straight", expect=TRUE_SHAPE,
                current={"strategy_not": ["relation_endpoints"], "degraded": True})
    assert run_cli(tmp_path, store_dir, [fixed]) == 1
    status, line = status_of(capsys)
    assert status == rc.KNOWN_BAD_CHANGED
    assert "now meets expect" in line


def test_a_known_bad_leg_that_goes_wrong_differently_fails_the_run(
        tmp_path, store_dir, capsys):
    drifted = leg(known_bad="was 40-50 km", expect=WRONG_EXPECT,
                  current={**TRUE_SHAPE, "length_km": [40, 50]})
    assert run_cli(tmp_path, store_dir, [drifted]) == 1
    status, line = status_of(capsys)
    assert status == rc.KNOWN_BAD_CHANGED
    assert "no longer as recorded" in line


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------

def test_a_leg_whose_region_is_absent_is_skipped_and_says_so(tmp_path, store_dir, capsys):
    legs = [leg(expect=TRUE_SHAPE),
            leg(name="needs Belgium", regions=[REGION, "europe/belgium"],
                expect=TRUE_SHAPE)]
    assert run_cli(tmp_path, store_dir, legs) == 0
    status, line = status_of(capsys, "needs Belgium")
    assert status == rc.SKIP
    assert "europe/belgium" in line


def test_a_missing_region_fails_under_require_all(tmp_path, store_dir, capsys):
    legs = [leg(name="needs Belgium", regions=["europe/belgium"], expect=TRUE_SHAPE)]
    assert run_cli(tmp_path, store_dir, legs, "--require-all") == 1
    status, line = status_of(capsys, "needs Belgium")
    assert status == rc.FAIL
    assert "europe/belgium" in line


def test_a_region_in_the_manifest_without_its_store_counts_as_absent(tmp_path):
    write_manifest(str(tmp_path), [ok_entry(REGION, (49.4, 5.7, 50.2, 6.5))])
    assert rc.available_regions(str(tmp_path)) == set()


# ---------------------------------------------------------------------------
# The corpus file
# ---------------------------------------------------------------------------

def test_an_unknown_expectation_key_is_refused(tmp_path):
    """A typo would be an expectation that is silently never checked."""
    corpus = tmp_path / "corpus.yml"
    corpus.write_text(yaml.safe_dump(
        {"legs": [leg(expect={"lenght_km": [1, 2]})]}), encoding="utf-8")
    with pytest.raises(rc.CorpusError, match="lenght_km"):
        rc.load_corpus(corpus)


def test_a_known_bad_leg_must_record_its_current_outcome(tmp_path):
    corpus = tmp_path / "corpus.yml"
    corpus.write_text(yaml.safe_dump(
        {"legs": [leg(known_bad="x", expect=TRUE_SHAPE)]}), encoding="utf-8")
    with pytest.raises(rc.CorpusError, match="current"):
        rc.load_corpus(corpus)


def test_the_shipped_corpus_is_valid_and_names_only_configured_regions():
    """A misspelt region would be skipped on every run, forever."""
    legs = rc.load_corpus(rc.DEFAULT_CORPUS)
    configured = set(yaml.safe_load(
        (ROOT / "config" / "rail_regions.yml").read_text(encoding="utf-8"))["regions"])
    named = {region for item in legs for region in item["regions"]}
    assert named <= configured, named - configured
    assert all(item["expect"].get("strategy_not") == ["straight"] for item in legs
               if not item["expect"].get("local_miss"))


def test_the_shipped_corpus_covers_every_mode_and_a_leg_that_must_miss():
    """Decision 3's corpus is rail, ferry and bus; one leg pins that the local
    stores do not invent a route where production must ask Overpass."""
    legs = rc.load_corpus(rc.DEFAULT_CORPUS)
    assert {item["mode"] for item in legs} == set(rc.MODES)
    assert any(item["expect"] == {"local_miss": True} and "known_bad" not in item
               for item in legs)


def test_distance_is_measured_to_the_segment_not_its_vertices():
    """A straight chord has no vertex near anything it crosses."""
    chord = [[6.0, 49.0], [6.0, 50.0]]          # [lon, lat]
    assert rc.distance_to_polyline_km(49.5, 6.0, chord) == pytest.approx(0, abs=1e-9)
    assert rc.distance_to_polyline_km(49.5, 6.1, chord) == pytest.approx(
        0.1 * 111.0 * 0.6494, rel=1e-3)


# ---------------------------------------------------------------------------
# Ferry and bus legs (U10)
# ---------------------------------------------------------------------------

SEA = "test/sea"
TOWN = "test/town"

# The bus line: a route relation from DEPOT to TERMINUS over two untagged ways,
# calling at both.
DEPOT = {"label": "Depot", "lat": 55.10, "lon": 11.00}
TERMINUS = {"label": "Terminus", "lat": 55.10, "lon": 11.10}
BUS_MIDPOINT = (55.12, 11.05)

PORT_W = {"label": "West port", "lat": WEST[0], "lon": WEST[1]}
PORT_E = {"label": "East port", "lat": EAST[0], "lon": EAST[1]}
# 33 km north of both ports, with no ferry way near either.
NORTH_W = {"label": "North west", "lat": 55.30, "lon": 11.00}
NORTH_E = {"label": "North east", "lat": 55.30, "lon": 11.40}

# What the two lines really do, written as expectations they meet: the crossing
# measures 25.9 km on strategy A and the bus line 7.7 km, each ending on its
# own stops.
FERRY_SHAPE = {
    "max_endpoint_km": 0.1,
    "length_km": [24, 28],
    "via": [{"label": "Mid crossing", "lat": 55.00, "lon": 11.20, "within_km": 0.5}],
    "strategy_not": ["straight"],
    "degraded": False,
}
BUS_SHAPE = {
    "max_endpoint_km": 0.1,
    "length_km": [7, 8],
    "via": [{"label": "Midpoint", "lat": BUS_MIDPOINT[0], "lon": BUS_MIDPOINT[1],
             "within_km": 0.5}],
    "strategy_not": ["straight"],
    "degraded": False,
}


@pytest.fixture(scope="module")
def layered_dir(tmp_path_factory):
    """The Luxembourg rail store, a ferry store holding the crossing (SEA) and
    a bus store holding the line (TOWN), under one schema 3 manifest."""
    directory = str(tmp_path_factory.mktemp("routedata"))
    rail_bbox = build_region(directory, REGION, FIXTURE)
    ferry_pbf = write_layer(os.path.join(directory, "sea.osm.pbf"),
                            {**WEST_HALF, **EAST_HALF}, [ferry_relation()], PORT_STOPS)
    bus_pbf = write_layer(
        os.path.join(directory, "town.osm.pbf"),
        {90: ([(DEPOT["lat"], DEPOT["lon"]), BUS_MIDPOINT], {}),
         91: ([BUS_MIDPOINT, (TERMINUS["lat"], TERMINUS["lon"])], {})},
        [(900, [("n", 3), ("w", 90), ("w", 91), ("n", 4)], {"route": "bus"})],
        {3: (DEPOT["lat"], DEPOT["lon"]), 4: (TERMINUS["lat"], TERMINUS["lon"])})
    write_manifest(directory, [
        {**ok_entry(REGION, rail_bbox), "layer": "rail"},
        build_layer(directory, SEA, "ferry", ferry_pbf),
        build_layer(directory, TOWN, "bus", bus_pbf),
    ])
    return directory


@pytest.fixture
def no_network(monkeypatch):
    """Overpass and every socket refused: a ferry or bus answer is the stores'."""
    def refuse(*args, **kwargs):
        raise AssertionError("the network was used")
    monkeypatch.setattr(ov, "_overpass", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def ferry_leg(name="West -> East", frm=PORT_W, to=PORT_E, regions=(SEA,), **fields):
    return {"name": name, "mode": "ferry", "from": dict(frm), "to": dict(to),
            "regions": list(regions), **fields}


def bus_leg(name="Depot -> Terminus", regions=(TOWN,), **fields):
    return {"name": name, "mode": "bus", "from": dict(DEPOT), "to": dict(TERMINUS),
            "regions": list(regions), **fields}


def test_a_ferry_leg_resolves_from_the_ferry_store(tmp_path, layered_dir, capsys,
                                                   no_network):
    assert run_cli(tmp_path, layered_dir, [ferry_leg(expect=FERRY_SHAPE)]) == 0
    status, line = status_of(capsys, "West -> East")
    assert status == rc.PASS, line
    assert "relation, 26 km" in line


def test_a_bus_leg_resolves_from_the_bus_store(tmp_path, layered_dir, capsys,
                                               no_network):
    assert run_cli(tmp_path, layered_dir, [bus_leg(expect=BUS_SHAPE)]) == 0
    status, line = status_of(capsys, "Depot -> Terminus")
    assert status == rc.PASS, line
    assert "relation, 7.7 km" in line


def test_a_ferry_leg_that_breaks_its_shape_fails(tmp_path, layered_dir, capsys,
                                                 no_network):
    assert run_cli(tmp_path, layered_dir,
                   [ferry_leg(expect={**FERRY_SHAPE, "length_km": [10, 20]})]) == 1
    status, line = status_of(capsys, "West -> East")
    assert status == rc.FAIL
    assert "length 26 km outside [10, 20]" in line


def test_a_bus_leg_that_breaks_its_shape_fails(tmp_path, layered_dir, capsys,
                                               no_network):
    expect = {**BUS_SHAPE, "avoid": [{**DEPOT, "within_km": 1}]}
    assert run_cli(tmp_path, layered_dir, [bus_leg(expect=expect)]) == 1
    status, line = status_of(capsys, "Depot -> Terminus")
    assert status == rc.FAIL
    assert "avoid Depot" in line


def test_a_leg_expected_to_miss_passes_when_the_stores_find_nothing(
        tmp_path, layered_dir, capsys, no_network):
    """The must-fall-back leg: production would ask Overpass here, and the
    stores must not have invented a route first."""
    leg = ferry_leg("North", NORTH_W, NORTH_E, expect={"local_miss": True})
    assert run_cli(tmp_path, layered_dir, [leg]) == 0
    status, line = status_of(capsys, "North")
    assert status == rc.PASS
    assert "local miss" in line


def test_a_leg_that_should_resolve_but_misses_fails_and_says_so(
        tmp_path, layered_dir, capsys, no_network):
    leg = ferry_leg("North", NORTH_W, NORTH_E, expect=FERRY_SHAPE)
    assert run_cli(tmp_path, layered_dir, [leg]) == 1
    status, line = status_of(capsys, "North")
    assert status == rc.FAIL
    assert "local miss: no route in the named regions" in line


def test_a_leg_expected_to_miss_fails_when_the_stores_find_a_route(
        tmp_path, layered_dir, capsys, no_network):
    assert run_cli(tmp_path, layered_dir,
                   [ferry_leg(expect={"local_miss": True})]) == 1
    status, line = status_of(capsys, "West -> East")
    assert status == rc.FAIL
    assert "resolved locally (relation), expected a local miss" in line


def test_a_known_bad_ferry_leg_that_starts_resolving_fails_the_run(
        tmp_path, layered_dir, capsys, no_network):
    """Recorded as a miss, it now resolves the way `expect` says: a fix, which
    must flip the entry in its own diff."""
    fixed = ferry_leg(known_bad="was a miss", expect=FERRY_SHAPE,
                      current={"local_miss": True})
    assert run_cli(tmp_path, layered_dir, [fixed]) == 1
    status, line = status_of(capsys, "West -> East")
    assert status == rc.KNOWN_BAD_CHANGED
    assert "now meets expect" in line


def test_a_known_bad_ferry_leg_that_stops_resolving_fails_the_run(
        tmp_path, layered_dir, capsys, no_network):
    """Recorded as a wrong route, it now misses: a different outcome, not a fix."""
    leg = ferry_leg("North", NORTH_W, NORTH_E, known_bad="invents a route",
                    expect=FERRY_SHAPE, current={"length_km": [30, 40]})
    assert run_cli(tmp_path, layered_dir, [leg]) == 1
    status, line = status_of(capsys, "North")
    assert status == rc.KNOWN_BAD_CHANGED
    assert "no longer as recorded: local miss" in line


@pytest.mark.parametrize("current, status", [
    (BUS_SHAPE, rc.KNOWN_BAD_UNCHANGED),
    ({**BUS_SHAPE, "length_km": [9, 10]}, rc.KNOWN_BAD_CHANGED),
], ids=["unchanged", "changed"])
def test_a_known_bad_bus_leg_is_held_to_its_recorded_outcome(
        tmp_path, layered_dir, capsys, no_network, current, status):
    leg = bus_leg(known_bad="should run the long way round",
                  expect={**BUS_SHAPE, "length_km": [12, 14]}, current=current)
    assert run_cli(tmp_path, layered_dir, [leg]) == int(status == rc.KNOWN_BAD_CHANGED)
    assert status_of(capsys, "Depot -> Terminus")[0] == status


def test_a_ferry_leg_reads_only_ferry_stores(tmp_path, layered_dir, capsys, no_network):
    """Luxembourg has a rail store and no ferry one: for a ferry leg it is
    absent — skipped, or failed under --require-all — whatever its rail."""
    leg = ferry_leg("Lux ferry", regions=[REGION], expect=FERRY_SHAPE)
    assert run_cli(tmp_path, layered_dir, [leg]) == 0
    status, line = status_of(capsys, "Lux ferry")
    assert status == rc.SKIP
    assert f"ferry regions absent: {REGION}" in line
    assert run_cli(tmp_path, layered_dir, [leg], "--require-all") == 1
    assert status_of(capsys, "Lux ferry")[0] == rc.FAIL


def test_a_bus_leg_reads_only_the_bus_regions_it_names(tmp_path, layered_dir, capsys,
                                                       no_network):
    """SEA is in the directory, as ferry: a bus leg naming it has no bus region."""
    assert run_cli(tmp_path, layered_dir,
                   [bus_leg(regions=[SEA], expect=BUS_SHAPE)], "--require-all") == 1
    status, line = status_of(capsys, "Depot -> Terminus")
    assert status == rc.FAIL
    assert f"bus regions absent: {SEA}" in line


def test_a_layer_counts_only_with_its_own_entry_and_its_own_file(layered_dir, tmp_path):
    assert rc.available_regions(layered_dir, "ferry") == {SEA}
    assert rc.available_regions(layered_dir, "bus") == {TOWN}
    assert rc.available_regions(layered_dir) == {REGION}
    # A ferry entry whose ferry store is not on disk is not ferry coverage.
    write_manifest(str(tmp_path),
                   [layer_entry(SEA, "ferry", bbox=(54.9, 10.9, 55.1, 11.5))])
    assert rc.available_regions(str(tmp_path), "ferry") == set()


@pytest.mark.parametrize("fields, match", [
    ({"mode": "tram"}, "mode must be one of"),
    ({"expect": {"local_miss": True}}, "a rail leg never misses"),
    ({"mode": "ferry", "expect": {"local_miss": True, "length_km": [1, 2]}},
     "local_miss is `true` and stands alone"),
    ({"mode": "bus", "expect": {"local_miss": False}},
     "local_miss is `true` and stands alone"),
], ids=["unknown mode", "rail cannot miss", "miss with a shape", "miss false"])
def test_a_malformed_mode_or_miss_is_refused(tmp_path, fields, match):
    corpus = tmp_path / "corpus.yml"
    corpus.write_text(yaml.safe_dump({"legs": [leg(**{"expect": TRUE_SHAPE, **fields})]}),
                      encoding="utf-8")
    with pytest.raises(rc.CorpusError, match=match):
        rc.load_corpus(corpus)

"""The route corpus runner (issue #345, U4).

Every test resolves real legs against a real store built from the Luxembourg
fixture extract — the same store the Phase 3 parity tests use — so what is
checked is the runner's verdict on a real resolve, not on a stubbed polyline.
Nothing here touches the network: the runner reads stores only.

The Luxembourg leg the tests turn on, Luxembourg Gare -> Ettelbruck, measures
(strategy relation_endpoints) 30 km, both ends within 0.25 km of the stations,
0.3 km from Mersch, which it runs through, and 10.9 km from Kleinbettingen,
which is on another line.
"""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from tests.test_rail_source import FIXTURE, REGION, build_region, ok_entry, write_manifest

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
    assert all(item["expect"].get("strategy_not") == ["straight"] for item in legs)


def test_distance_is_measured_to_the_segment_not_its_vertices():
    """A straight chord has no vertex near anything it crosses."""
    chord = [[6.0, 49.0], [6.0, 50.0]]          # [lon, lat]
    assert rc.distance_to_polyline_km(49.5, 6.0, chord) == pytest.approx(0, abs=1e-9)
    assert rc.distance_to_polyline_km(49.5, 6.1, chord) == pytest.approx(
        0.1 * 111.0 * 0.6494, rel=1e-3)

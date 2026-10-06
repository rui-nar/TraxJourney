"""Store schema 4: ferry and bus stores that locate their stops (#345, F7).

F5 made the builder place every stop a ferry or bus relation names, not only
the uic_ref ones, and strategy A bridges a broken relation only towards a stop
it can place. Same tables as schema 3, different contents — so the version
moves, which makes the box's first refresh rebuild every store (the sidecar
test is in test_rail_data_fetch.py), and a schema 3 ferry or bus store met
before then is refused rather than answering differently from Overpass.

A schema 3 *rail* store did not change and keeps routing: v1-v4 rail parity is
in test_rail_store_schema3.py.
"""
import logging
import os
import sqlite3
from unittest.mock import Mock

import pytest

from src.rail.store import RailStore, RailStoreError, store_filename
from src.services import overpass_service as ov
from tests.test_rail_store_schema3 import (
    _build,
    _downgrade_to_schema_3,
    _route_with_stops,
)
from tests.test_route_source_ferry_bus import (
    EAST,
    EAST_HALF,
    PORT_STOPS,
    WEST,
    WEST_HALF,
    build_region,
    ferry_relation,
    overpass_relation,
    write_layer,
    write_manifest,
)


@pytest.fixture(autouse=True)
def _forget_configured_sources():
    ov._local_source = ov._local_route = None
    yield
    ov._local_source = ov._local_route = None


@pytest.mark.parametrize("layer,route", [
    ("rail", "train"), ("ferry", "ferry"), ("bus", "bus")])
def test_a_newly_built_store_is_schema_4(tmp_path, layer, route):
    out, _ = _build(tmp_path, _route_with_stops(route), layer)
    conn = sqlite3.connect(str(out))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0] == "4"
    conn.close()
    with RailStore(out) as store:
        assert store.schema == 4


@pytest.mark.parametrize("layer,route", [("ferry", "ferry"), ("bus", "bus")])
def test_a_schema_3_ferry_or_bus_store_is_refused(tmp_path, layer, route):
    """Opened, it would answer: its stops are unlocated, so strategy A gives up
    on a relation Overpass bridges. Refused, it is a region not covered."""
    out, _ = _build(tmp_path, _route_with_stops(route), layer)
    old = _downgrade_to_schema_3(str(out), str(tmp_path / f"old.{layer}.sqlite"))
    with pytest.raises(RailStoreError, match=f"schema 3 {layer} store"):
        RailStore(old)


def test_a_schema_3_rail_store_is_still_read(tmp_path):
    out, _ = _build(tmp_path, _route_with_stops("train"), "rail")
    old = _downgrade_to_schema_3(str(out), str(tmp_path / "old.rail.sqlite"))
    with RailStore(out) as new, RailStore(old) as store:
        assert store.schema == 3
        assert store.relation_stops(300) == new.relation_stops(300)


def test_a_schema_3_ferry_store_is_not_used_to_answer_locally(
        tmp_path, monkeypatch, caplog):
    """End to end: the leg the store covers goes to Overpass, once, and the
    warning names the region — not a local answer from stale stops."""
    pbf = write_layer(tmp_path / "whole.osm.pbf", {**WEST_HALF, **EAST_HALF},
                      [ferry_relation()], PORT_STOPS)
    entry = build_region(str(tmp_path), "test/whole", "ferry", pbf)
    write_manifest(str(tmp_path), [entry])
    path = tmp_path / store_filename("test/whole", "ferry")
    old = _downgrade_to_schema_3(str(path), str(tmp_path / "old.sqlite"))
    os.replace(old, path)

    monkeypatch.setenv("RAIL_SOURCE", "local")
    monkeypatch.setenv("RAIL_DATA_DIR", str(tmp_path))
    transport = Mock(name="_overpass", return_value={"elements": [
        overpass_relation(500, {**WEST_HALF, **EAST_HALF}, PORT_STOPS)]})
    monkeypatch.setattr(ov, "_overpass", transport)

    with caplog.at_level(logging.WARNING, logger="src.services.rail_source"):
        result = ov.get_ferry_geometry(*WEST, *EAST)
    assert result.source == "overpass"
    assert transport.call_count == 1
    assert "ferry region test/whole" in caplog.text
    assert "does not locate its stops" in caplog.text

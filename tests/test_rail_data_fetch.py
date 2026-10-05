"""Delivering the published rail data onto the box (issue #345).

``scripts/fetch_rail_data.py`` is what takes a deployment from "no data" to
"``RAIL_DATA_DIR`` holds current, valid region stores plus the manifest". It
runs unattended, against files it did not build, beside workers that are
reading the directory it writes — so what is asserted here is not that it
copies bytes but that it cannot damage what is already there:

* a checksum mismatch, an HTTP failure or a manifest schema nobody knows about
  leaves the installed stores exactly as they were, and the directory still
  resolves routes;
* a re-run costs only the regions that actually changed;
* and a worker holding a store open across a refresh keeps reading valid data,
  which is the property the whole design exists for (#352, finding 2) and is
  tested by holding one open across a real refresh rather than by inspecting
  the code that performs it.

No network: the releases API, the manifest and every asset are served from
fixtures on disk, and the first-install test refuses ``socket.socket`` outright
to prove it.
"""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

import pytest

from src.rail.store import SCHEMA_VERSION, RailStore, store_filename
from src.services.rail_source import LocalRailSource, load_coverage

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "fetch_rail_data", ROOT / "scripts" / "fetch_rail_data.py"
)
fetch = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fetch
_spec.loader.exec_module(fetch)

# Two real rail-only extracts, so the store the delivery installs is a store the
# reader actually opens rather than a stand-in. They differ in size by 30x,
# which is how a test can tell which of two versions a reader is looking at.
LUXEMBOURG = ROOT / "tests" / "fixtures" / "rail" / "luxembourg-rail.osm.pbf"
MANNHEIM = ROOT / "tests" / "fixtures" / "rail_mannheim_filtered.osm.pbf"

BASE = "https://example.invalid/releases"


# ---------------------------------------------------------------------------
# A release, served from memory
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, body: bytes = b"", status: int = 200):
        self.content, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=None):
        step = chunk_size or len(self.content) or 1
        for start in range(0, len(self.content), step):
            yield self.content[start:start + step]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _transport(bodies: dict, fail: tuple = ()):
    """A fake ``requests.get`` over {url: bytes}, and the log of what it served."""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if url in fail:
            return _Response(status=500)
        if url not in bodies:
            return _Response(status=404)
        return _Response(bodies[url])

    return get, calls


def _entry(region: str, pbf: Path, source_date: str, bbox: list) -> dict:
    body = pbf.read_bytes()
    return {
        "region": region,
        "status": "ok",
        "file": region.rsplit("/", 1)[-1] + "-rail.osm.pbf",
        "source": f"https://download.geofabrik.de/{region}-latest.osm.pbf",
        "source_date": source_date,
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "bbox": bbox,
    }


# Real-ish extents: the manifest's bbox is [min_lon, min_lat, max_lon, max_lat]
# and Phase 3 selects regions by it, so the two must not overlap here or a
# per-region assertion could be answered by the other region's store.
LUX_BBOX = [5.73, 49.44, 6.53, 50.18]
DE_BBOX = [8.40, 49.40, 8.60, 49.55]


def _world(entries: list[dict], tag: str = "rail-data-2026-09-06",
           generated_at: str | None = None, schema: int = 2,
           extra_releases: list = ()) -> tuple[dict, dict]:
    """(bodies for the fake transport, the manifest that was published)."""
    if generated_at is None:
        generated_at = tag[len("rail-data-"):] + "T18:00:00Z"
    manifest = {"schema": schema, "generated_at": generated_at, "regions": entries}
    assets = [{"name": "manifest.json",
               "browser_download_url": f"{BASE}/{tag}/manifest.json"}]
    bodies = {
        fetch.RELEASES_URL.format(repo=fetch.DEFAULT_REPO): b"",
        f"{BASE}/{tag}/manifest.json":
            json.dumps(manifest).encode("utf-8"),
    }
    for entry in entries:
        if entry.get("status") != "ok":
            continue
        assets.append({"name": entry["file"],
                       "browser_download_url": f"{BASE}/{tag}/{entry['file']}"})
    release = {"tag_name": tag, "draft": False, "prerelease": True,
               "assets": assets}
    releases = [
        {"tag_name": "v0.9.0", "draft": False, "assets": []},
        *extra_releases,
        release,
    ]
    bodies[fetch.RELEASES_URL.format(repo=fetch.DEFAULT_REPO)] = json.dumps(
        releases).encode("utf-8")
    # GitHub answers for a tag whether or not it is still on page one of the
    # list — which is the whole point of asking by tag.
    bodies[fetch.RELEASE_BY_TAG_URL.format(repo=fetch.DEFAULT_REPO, tag=tag)] = (
        json.dumps(release).encode("utf-8"))
    return bodies, manifest


def _with_asset(bodies: dict, entry: dict, pbf: Path,
                tag: str = "rail-data-2026-09-06") -> dict:
    bodies[f"{BASE}/{tag}/{entry['file']}"] = pbf.read_bytes()
    return bodies


def _lux(source_date="2026-09-05"):
    return _entry("europe/luxembourg", LUXEMBOURG, source_date, LUX_BBOX)


def _de(pbf=MANNHEIM, source_date="2026-09-05"):
    return _entry("europe/germany", pbf, source_date, DE_BBOX)


def _empty(region="europe/andorra"):
    return {
        "region": region,
        "status": "empty",
        "source": f"https://download.geofabrik.de/{region}-latest.osm.pbf",
        "source_date": "2026-09-05",
    }


def _ways(dest: Path, region: str) -> int:
    with RailStore(dest / store_filename(region)) as store:
        return len(store.ways_in_bbox(*store.bbox))


def _incoming(dest: Path) -> list[str]:
    """What ``.incoming/`` holds besides the lock file — must be nothing."""
    return sorted(p.name for p in (dest / fetch.WORK_DIRNAME).iterdir()
                  if p.name != fetch.LOCK_NAME)


# ---------------------------------------------------------------------------
# First install
# ---------------------------------------------------------------------------

def test_first_install_builds_a_store_per_region(tmp_path, monkeypatch):
    """A clean directory ends up holding exactly what the manifest describes."""
    lux, de, andorra = _lux(), _de(), _empty()
    bodies, manifest = _world([lux, de, andorra])
    _with_asset(bodies, lux, LUXEMBOURG)
    _with_asset(bodies, de, MANNHEIM)
    get, calls = _transport(bodies)

    def _no_network(*a, **k):
        raise AssertionError("the delivery step opened a socket")

    monkeypatch.setattr(socket, "socket", _no_network)

    assert fetch.refresh(tmp_path, get=get) == 0

    for region in ("europe/luxembourg", "europe/germany"):
        assert (tmp_path / store_filename(region)).is_file()
    # The manifest is the release's, verbatim — including the `empty` entry,
    # which Phase 3 reads as "we know there is no rail here".
    written = json.loads((tmp_path / "manifest.json").read_text())
    assert written["schema"] == 2
    assert written["generated_at"] == manifest["generated_at"]
    assert {e["region"] for e in written["regions"]} == {
        "europe/luxembourg", "europe/germany", "europe/andorra"}
    assert [e for e in written["regions"] if e["region"] == "europe/andorra"] == [andorra]
    # Coverage is what Phase 3 will read back.
    assert sorted(r for r, _ in load_coverage(str(tmp_path))) == [
        "europe/germany", "europe/luxembourg"]
    # Nothing left behind: the extracts are deleted as soon as their store exists.
    assert _incoming(tmp_path) == []
    assert f"{BASE}/rail-data-2026-09-06/luxembourg-rail.osm.pbf" in calls


def test_installed_stores_answer_queries(tmp_path):
    """The delivered directory resolves, rather than merely existing."""
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)

    fetch.refresh(tmp_path, get=get)

    source = LocalRailSource(str(tmp_path))
    assert source.regions_for((49.5, 5.8, 49.7, 6.3)) == ["europe/luxembourg"]
    assert _ways(tmp_path, "europe/luxembourg") > 0


# ---------------------------------------------------------------------------
# Re-running
# ---------------------------------------------------------------------------

def test_rerun_skips_regions_already_at_the_manifest_checksum(tmp_path):
    lux, de = _lux(), _de()
    bodies, _ = _world([lux, de])
    _with_asset(bodies, lux, LUXEMBOURG)
    _with_asset(bodies, de, MANNHEIM)

    get, _ = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0
    before = (tmp_path / store_filename("europe/germany")).stat().st_mtime_ns

    get, calls = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0

    assert not [c for c in calls if c.endswith(".osm.pbf")]
    assert (tmp_path / store_filename("europe/germany")).stat().st_mtime_ns == before


def test_a_changed_checksum_rebuilds_only_that_region(tmp_path):
    """A monthly rebuild that only moved one country costs only that country."""
    lux, de = _lux(), _de()
    bodies, _ = _world([lux, de])
    _with_asset(bodies, lux, LUXEMBOURG)
    _with_asset(bodies, de, MANNHEIM)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)

    # Germany's next extract is different data under a new source date.
    de2 = _de(pbf=LUXEMBOURG, source_date="2026-10-05")
    bodies2, _ = _world([lux, de2], tag="rail-data-2026-10-06")
    _with_asset(bodies2, lux, LUXEMBOURG, tag="rail-data-2026-10-06")
    _with_asset(bodies2, de2, LUXEMBOURG, tag="rail-data-2026-10-06")
    get, calls = _transport(bodies2)

    assert fetch.refresh(tmp_path, get=get) == 0

    fetched = [c for c in calls if c.endswith(".osm.pbf")]
    assert fetched == [f"{BASE}/rail-data-2026-10-06/germany-rail.osm.pbf"]
    written = json.loads((tmp_path / "manifest.json").read_text())
    dates = {e["region"]: e["source_date"] for e in written["regions"]}
    assert dates == {"europe/luxembourg": "2026-09-05", "europe/germany": "2026-10-05"}


# ---------------------------------------------------------------------------
# Refusals — nothing good may be replaced by something broken
# ---------------------------------------------------------------------------

def test_checksum_mismatch_leaves_the_installed_store_untouched(tmp_path, capsys):
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)
    good = (tmp_path / store_filename("europe/luxembourg")).read_bytes()
    ways = _ways(tmp_path, "europe/luxembourg")

    # A newer release whose asset does not match the checksum it publishes.
    # The body served is the *right length and the wrong bytes* — the swapped
    # or tampered file the digest exists for. A body of a different length
    # would be caught by the byte count alone and the sha256 comparison would
    # never run, which is why the refusal line is asserted too: it is printed
    # only by the check that compares digests.
    raw = bytearray(LUXEMBOURG.read_bytes())
    intended = bytearray(raw)
    intended[len(raw) // 3] ^= 0xFF
    served = bytearray(raw)
    served[len(raw) // 2] ^= 0xFF

    corrupt = _lux(source_date="2026-10-05")
    corrupt["sha256"] = hashlib.sha256(bytes(intended)).hexdigest()
    bodies2, _ = _world([corrupt], tag="rail-data-2026-10-06")
    bodies2[f"{BASE}/rail-data-2026-10-06/{corrupt['file']}"] = bytes(served)
    assert len(served) == corrupt["bytes"]          # the size check cannot fire
    assert hashlib.sha256(bytes(served)).hexdigest() != corrupt["sha256"]
    get, _ = _transport(bodies2)

    assert fetch.refresh(tmp_path, get=get) == 1

    out = capsys.readouterr().out
    assert (f"downloaded {corrupt['bytes']} bytes sha256 "
            f"{hashlib.sha256(bytes(served)).hexdigest()}, manifest says "
            f"{corrupt['bytes']} bytes sha256 {corrupt['sha256']}") in out
    assert (tmp_path / store_filename("europe/luxembourg")).read_bytes() == good
    assert _ways(tmp_path, "europe/luxembourg") == ways
    # The manifest still describes what is on disk, not what was refused.
    written = json.loads((tmp_path / "manifest.json").read_text())
    assert [e["source_date"] for e in written["regions"]] == ["2026-09-05"]
    assert written["generated_at"] == "2026-09-06T18:00:00Z"
    assert _incoming(tmp_path) == []


def test_partial_failure_keeps_the_other_regions_usable(tmp_path):
    """One region failing must not cost the rest their data — or their coverage."""
    lux, de = _lux(), _de()
    bodies, _ = _world([lux, de])
    _with_asset(bodies, lux, LUXEMBOURG)
    _with_asset(bodies, de, MANNHEIM)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)
    de_ways = _ways(tmp_path, "europe/germany")

    # Both regions move on; Germany's asset is unreachable this time.
    lux2, de2 = _lux("2026-10-05"), _de(pbf=LUXEMBOURG, source_date="2026-10-05")
    bodies2, _ = _world([lux2, de2], tag="rail-data-2026-10-06")
    _with_asset(bodies2, lux2, LUXEMBOURG, tag="rail-data-2026-10-06")
    _with_asset(bodies2, de2, LUXEMBOURG, tag="rail-data-2026-10-06")
    get, _ = _transport(
        bodies2, fail=(f"{BASE}/rail-data-2026-10-06/germany-rail.osm.pbf",))

    assert fetch.refresh(tmp_path, get=get) == 1

    # Germany keeps the store and the manifest entry it had, so it still routes.
    assert _ways(tmp_path, "europe/germany") == de_ways
    written = json.loads((tmp_path / "manifest.json").read_text())
    entries = {e["region"]: e for e in written["regions"]}
    assert entries["europe/germany"]["source_date"] == "2026-09-05"
    assert entries["europe/luxembourg"]["source_date"] == "2026-10-05"
    # A partial run does not claim the release's freshness.
    assert written["generated_at"] == "2026-09-06T18:00:00Z"
    assert sorted(r for r, _ in load_coverage(str(tmp_path))) == [
        "europe/germany", "europe/luxembourg"]

    # And the re-run converges: only the region that failed is fetched again.
    get, calls = _transport(bodies2)
    assert fetch.refresh(tmp_path, get=get) == 0
    assert [c for c in calls if c.endswith(".osm.pbf")] == [
        f"{BASE}/rail-data-2026-10-06/germany-rail.osm.pbf"]
    written = json.loads((tmp_path / "manifest.json").read_text())
    assert {e["source_date"] for e in written["regions"]} == {"2026-10-05"}
    # Complete now, so the directory may claim the release's generation date.
    assert written["generated_at"] == "2026-10-06T18:00:00Z"


def test_a_region_the_new_release_drops_is_named_in_a_warning(tmp_path, capsys):
    """Review finding R1-6: a region leaving coverage must say so.

    The installed manifest is rewritten from the release alone, so the dropped
    region goes back to Overpass while the age check sees nothing wrong.
    """
    lux, de = _lux(), _de()
    bodies, _ = _world([lux, de, _empty()])
    _with_asset(bodies, lux, LUXEMBOURG)
    _with_asset(bodies, de, MANNHEIM)
    get, _ = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0
    assert "WARNING" not in capsys.readouterr().out

    lux2 = _lux("2026-10-05")
    bodies2, _ = _world([lux2], tag="rail-data-2026-10-06")
    _with_asset(bodies2, lux2, LUXEMBOURG, tag="rail-data-2026-10-06")
    get, _ = _transport(bodies2)
    assert fetch.refresh(tmp_path, get=get) == 0

    warnings = [line for line in capsys.readouterr().out.splitlines()
                if line.startswith("WARNING")]
    # Both: an empty entry dropped is as much a coverage change as an ok one.
    assert len(warnings) == 2
    assert "[europe/andorra]" in warnings[0] and "rail-data-2026-10-06" in warnings[0]
    assert "[europe/germany]" in warnings[1]
    regions = [e["region"] for e in
               json.loads((tmp_path / "manifest.json").read_text())["regions"]]
    assert regions == ["europe/luxembourg"]

    # Said once, when it happens — not on every later run.
    get, _ = _transport(bodies2)
    fetch.refresh(tmp_path, get=get)
    assert "WARNING" not in capsys.readouterr().out


def test_an_unknown_manifest_schema_is_refused(tmp_path):
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)
    before = sorted(p.name for p in tmp_path.iterdir())

    future = _lux("2026-10-05")
    bodies2, _ = _world([future], tag="rail-data-2026-10-06", schema=3)
    _with_asset(bodies2, future, MANNHEIM, tag="rail-data-2026-10-06")
    get, calls = _transport(bodies2)

    with pytest.raises(fetch.RailDataError, match="schema"):
        fetch.refresh(tmp_path, get=get)

    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not [c for c in calls if c.endswith(".osm.pbf")]
    assert json.loads((tmp_path / "manifest.json").read_text())["schema"] == 2


def test_no_rail_release_is_an_error_not_an_empty_install(tmp_path):
    bodies = {fetch.RELEASES_URL.format(repo=fetch.DEFAULT_REPO):
              json.dumps([{"tag_name": "v0.9.0", "draft": False, "assets": []}]).encode()}
    get, _ = _transport(bodies)

    with pytest.raises(fetch.RailDataError, match="rail-data"):
        fetch.refresh(tmp_path, get=get)

    assert not (tmp_path / "manifest.json").exists()


def test_an_empty_region_fetches_nothing(tmp_path):
    andorra = _empty()
    bodies, _ = _world([andorra])
    get, calls = _transport(bodies)

    assert fetch.refresh(tmp_path, get=get) == 0

    assert not (tmp_path / store_filename("europe/andorra")).exists()
    assert not [c for c in calls if c.endswith(".osm.pbf")]
    assert load_coverage(str(tmp_path)) == []
    assert json.loads((tmp_path / "manifest.json").read_text())["regions"] == [andorra]


def _republish(bodies: dict, manifest: dict,
               tag: str = "rail-data-2026-09-06") -> None:
    """Re-serialise a manifest whose entries were edited after ``_world``."""
    bodies[f"{BASE}/{tag}/manifest.json"] = json.dumps(manifest).encode("utf-8")


def test_a_status_this_version_does_not_know_is_refused(tmp_path, capsys):
    """Even with the asset sitting right there, ready to install.

    ``ok`` and ``empty`` are the two statuses whose meaning is settled. A third
    one means the manifest was written by something newer, and installing from
    an entry this version does not understand is the "looks like success"
    failure the module exists to refuse — so the asset being present must not
    be enough.
    """
    entry = _lux()
    bodies, manifest = _world([entry])
    _with_asset(bodies, entry, LUXEMBOURG)
    entry["status"] = "stale"
    _republish(bodies, manifest)
    get, _ = _transport(bodies)

    assert fetch.refresh(tmp_path, get=get) == 1

    assert "REFUSED: unknown status 'stale'" in capsys.readouterr().out
    assert not (tmp_path / store_filename("europe/luxembourg")).exists()
    assert load_coverage(str(tmp_path)) == []
    assert json.loads((tmp_path / "manifest.json").read_text())["regions"] == []


def test_an_ok_entry_with_no_sha256_is_refused(tmp_path, capsys):
    """Nothing to verify against means nothing may be installed.

    A missing digest compares equal to the *absent* sidecar of a region that
    was never installed, so without an explicit refusal the region is skipped
    as "up to date", the run exits 0 and the written manifest claims coverage
    with no store behind it — ``load_coverage`` would then hand Phase 3 a
    region whose file does not exist.
    """
    entry = _lux()
    bodies, manifest = _world([entry])
    _with_asset(bodies, entry, LUXEMBOURG)
    del entry["sha256"]
    _republish(bodies, manifest)
    get, _ = _transport(bodies)

    assert fetch.refresh(tmp_path, get=get) == 1

    assert "REFUSED: manifest entry has no sha256" in capsys.readouterr().out
    assert not (tmp_path / store_filename("europe/luxembourg")).exists()
    assert load_coverage(str(tmp_path)) == []
    assert json.loads((tmp_path / "manifest.json").read_text())["regions"] == []


def test_a_store_schema_bump_rebuilds_regions_the_extract_did_not_change(
        tmp_path, monkeypatch, capsys):
    """The republish that would otherwise silently not happen (#359).

    A schema bump changes what a store holds while leaving the published asset
    byte-identical, so a skip keyed on the asset digest alone marks all 49
    regions up to date and the box keeps stores the reader can only half use.
    That is worse than an error: the operator runs the refresh, sees "49 up to
    date", and believes the data is current.
    """
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)

    get, _ = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0
    assert fetch.installed_build(tmp_path, "europe/luxembourg") == (
        lux["sha256"], SCHEMA_VERSION)

    # Same release, same asset, same digest — and a rebuilt store is still the
    # only way to serve the new schema.
    monkeypatch.setattr(fetch, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    get, calls = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0

    assert "1 installed, 0 up to date" in capsys.readouterr().out
    assert [c for c in calls if c.endswith(".osm.pbf")] == [
        f"{BASE}/rail-data-2026-09-06/{lux['file']}"]
    assert fetch.installed_build(tmp_path, "europe/luxembourg") == (
        lux["sha256"], SCHEMA_VERSION + 1)

    # And having rebuilt once, it converges again at the new schema.
    get, calls = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0
    assert [c for c in calls if c.endswith(".osm.pbf")] == []


def test_a_sidecar_from_before_the_schema_was_recorded_forces_a_rebuild(tmp_path):
    """What the box actually holds on the first run after this ships: a sidecar
    written by the previous release, carrying a digest and nothing else."""
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0

    sidecar = tmp_path / (store_filename("europe/luxembourg") + fetch.SHA_SUFFIX)
    sidecar.write_text(lux["sha256"] + "\n", encoding="utf-8")
    assert fetch.installed_build(tmp_path, "europe/luxembourg") is None

    get, calls = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0
    assert [c for c in calls if c.endswith(".osm.pbf")] == [
        f"{BASE}/rail-data-2026-09-06/{lux['file']}"]


def test_the_digest_is_recorded_only_after_the_store_lands(tmp_path, monkeypatch):
    """A sidecar written before the rename would freeze the region forever.

    ``installed_build`` is what makes a re-run cheap, and the only thing that
    makes it *safe* is that it can never describe a store that is not there:
    record the new digest and then fail to publish the store, and every later
    run skips the region as up to date while the old data sits on disk. Nobody
    would find that without deleting the sidecar by hand.
    """
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)
    was_installed = fetch.installed_build(tmp_path, "europe/luxembourg")
    assert was_installed == (lux["sha256"], SCHEMA_VERSION)

    # The next release builds fine, but publishing the store fails — a full
    # disk, a permissions change, an interrupted container.
    new = _entry("europe/luxembourg", MANNHEIM, "2026-10-05", LUX_BBOX)
    bodies2, _ = _world([new], tag="rail-data-2026-10-06")
    _with_asset(bodies2, new, MANNHEIM, tag="rail-data-2026-10-06")
    real_replace = os.replace

    def no_publish(src, dst, *args, **kwargs):
        if str(dst).endswith(store_filename("europe/luxembourg")):
            raise OSError(errno.EIO, "the store could not be published")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(fetch.os, "replace", no_publish)
    get, _ = _transport(bodies2)
    assert fetch.refresh(tmp_path, get=get) == 1

    # The record still describes the store that is actually on disk.
    assert fetch.installed_build(tmp_path, "europe/luxembourg") == was_installed

    # So the next run really does install the new data, rather than skipping it.
    monkeypatch.undo()
    get, calls = _transport(bodies2)
    assert fetch.refresh(tmp_path, get=get) == 0
    assert [c for c in calls if c.endswith(".osm.pbf")] == [
        f"{BASE}/rail-data-2026-10-06/{new['file']}"]
    assert fetch.installed_build(tmp_path, "europe/luxembourg") == (new["sha256"], SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def test_an_explicit_tag_installs_that_release(tmp_path):
    """The rollback path: pin the previous release and get its data back.

    The old release is deliberately **not** in the list the releases endpoint
    returns. That list is one page of 100 and this repository publishes about
    fifteen releases a month, so within weeks the rollback target is off it —
    scanning the page would find nothing and rollback would be dead exactly
    when it is needed. Asking GitHub for the tag by name does not care.
    """
    # Last month's extract for the same region, holding different data.
    old = _entry("europe/luxembourg", MANNHEIM, "2026-08-05", LUX_BBOX)
    old_bodies, _ = _world([old], tag="rail-data-2026-08-06")
    _with_asset(old_bodies, old, MANNHEIM, tag="rail-data-2026-08-06")

    new = _lux("2026-09-05")
    bodies, _ = _world([new])
    _with_asset(bodies, new, LUXEMBOURG)
    # Everything of last month's release except its place in the listing.
    bodies.update({k: v for k, v in old_bodies.items()
                   if k != fetch.RELEASES_URL.format(repo=fetch.DEFAULT_REPO)})
    listed = {r["tag_name"] for r in json.loads(
        bodies[fetch.RELEASES_URL.format(repo=fetch.DEFAULT_REPO)])}
    assert "rail-data-2026-08-06" not in listed
    get, _ = _transport(bodies)

    assert fetch.refresh(tmp_path, get=get) == 0
    assert _ways(tmp_path, "europe/luxembourg") == _lux_ways()

    get, _ = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get, tag="rail-data-2026-08-06") == 0

    written = json.loads((tmp_path / "manifest.json").read_text())
    assert written["regions"][0]["source_date"] == "2026-08-05"
    assert _ways(tmp_path, "europe/luxembourg") != _lux_ways()


def test_an_unknown_tag_is_a_refusal_not_a_traceback(tmp_path):
    """A typo'd or deleted rollback tag: GitHub answers 404, the step says so."""
    bodies, _ = _world([_lux()])
    get, _ = _transport(bodies)

    with pytest.raises(fetch.RailDataError, match="rail-data-2020-01-01"):
        fetch.refresh(tmp_path, get=get, tag="rail-data-2020-01-01")

    assert not (tmp_path / "manifest.json").exists()


def _lux_ways(_cache={}):
    """Way count of the Luxembourg fixture, built once."""
    if "n" not in _cache:
        import tempfile

        from src.rail.builder import build_store

        out = Path(tempfile.mkdtemp()) / "lux.sqlite"
        build_store(LUXEMBOURG, out)
        with RailStore(out) as store:
            _cache["n"] = len(store.ways_in_bbox(*store.bbox))
    return _cache["n"]


# ---------------------------------------------------------------------------
# The image this runs in
# ---------------------------------------------------------------------------

def test_the_image_can_import_osmium():
    """The delivery step builds stores in the app image, so it needs libexpat.

    ``python:*-slim`` provides no libexpat.so.1 — CPython statically links its
    own copy for ``pyexpat`` — so pyosmium's extension module fails to import
    there however it was installed. Verified against the base image: with the
    whole of requirements.txt installed and no libexpat1, ``import osmium``
    raises ``ImportError: libexpat.so.1``.

    Nothing else in the image imports osmium, which is exactly why this is
    asserted: the apt layer looks like dead weight and deleting it breaks the
    rail refresh on the box, not in CI.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "libexpat1" in dockerfile


def _dockerignore_excludes(patterns: list[str], path: str) -> bool:
    """Whether ``docker build`` would drop *path* from the build context.

    Docker's rule (moby ``fileutils.PatternMatcher``): a path is excluded when
    the **last** pattern matching it — or matching any of its parent
    directories — is not a ``!`` exception. That last-match-wins ordering is
    the whole reason ``!scripts/fetch_rail_data.py`` has to sit *after*
    ``scripts/``, and it is what a string search for the filename would not
    catch. The patterns here are literal paths and simple globs, so
    ``fnmatch`` stands in for Go's ``filepath.Match`` exactly.
    """
    parts = path.split("/")
    candidates = ["/".join(parts[:i + 1]) for i in range(len(parts))]
    excluded = False
    for raw in patterns:
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        pattern = pattern.lstrip("!").rstrip("/")
        if any(fnmatch.fnmatchcase(c, pattern) for c in candidates):
            excluded = not negated
    return excluded


def test_the_delivery_step_is_in_the_image():
    """Every documented way to run this runs it *from inside the image*.

    ``.dockerignore`` drops ``scripts/`` from the build context, and the image
    is built with ``context: .`` — so without an explicit exception the file
    the deployment docs, ``.env.example`` and this script's own usage line all
    name simply is not there, and every documented invocation dies with
    ``can't open file '/app/scripts/fetch_rail_data.py'``.

    The exceptions are deliberately single files: the rest of ``scripts/`` is
    CI's, not the image's.
    """
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert not _dockerignore_excludes(patterns, "scripts/fetch_rail_data.py")
    assert _dockerignore_excludes(patterns, "scripts/build_rail_extract.py")


# ---------------------------------------------------------------------------
# The property the design exists for
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows has no share-delete rename: os.replace over a file SQLite "
           "holds open raises PermissionError, so the property cannot exist "
           "there. Production is Linux containers; this asserts the POSIX "
           "guarantee the design depends on.",
)
def test_a_reader_holding_a_store_open_survives_a_refresh(tmp_path):
    """The atomic-rename contract, exercised rather than asserted about.

    ``LocalRailSource`` guards *opening* a store, not querying one, so a worker
    that already holds a connection when the file underneath it changes raises
    ``sqlite3.DatabaseError`` out of the RQ job (#345 Phase 3's known gap, #352
    finding 2). Renaming a fully built store into place keeps the old inode
    alive for that connection, so the query in flight finishes against the data
    it started on and only the *next* open sees the new data.

    Replace the ``os.replace`` in ``install_region`` with anything that writes
    the destination in place and this test fails: the held-open reader either
    starts seeing the new region's rows or raises on a file that changed under
    it.
    """
    de = _de(pbf=MANNHEIM)
    bodies, _ = _world([de])
    _with_asset(bodies, de, MANNHEIM)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)

    path = tmp_path / store_filename("europe/germany")
    reader = RailStore(path)
    before = reader.ways_in_bbox(*reader.bbox)
    assert before

    # The next month's build for the same region is entirely different data.
    de2 = _de(pbf=LUXEMBOURG, source_date="2026-10-05")
    bodies2, _ = _world([de2], tag="rail-data-2026-10-06")
    _with_asset(bodies2, de2, LUXEMBOURG, tag="rail-data-2026-10-06")
    get, _ = _transport(bodies2)
    assert fetch.refresh(tmp_path, get=get) == 0

    # The open connection still reads the data it was opened on, in full.
    after = reader.ways_in_bbox(*reader.bbox)
    assert after == before
    reader.close()

    # And a reader opening it now gets the new data — the refresh did happen.
    with RailStore(path) as fresh:
        assert len(fresh.ways_in_bbox(*fresh.bbox)) != len(before)


def test_a_build_that_fails_never_shows_the_destination_half_built(
        tmp_path, monkeypatch):
    """Publishing by rename, as opposed to merely not overwriting an inode.

    The test above proves a *held-open* reader survives, which
    ``build_store``'s own "remove the output, then create it" happens to
    satisfy even when it builds straight into ``RAIL_DATA_DIR``. Two failures
    it does not: for the whole of a build — ~12 s for Germany — a reader
    *opening* the store sees a half-written database, and a build that raises
    leaves the region with no store at all instead of the one it had.

    So this asserts what the rename actually buys: at no moment during the
    build does the destination hold anything but the previous store, and a
    build that dies halfway costs nothing.
    """
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)

    path = tmp_path / store_filename("europe/luxembourg")
    original = path.read_bytes()
    ways = _ways(tmp_path, "europe/luxembourg")
    during = []

    def dies_halfway(pbf, out, **kwargs):
        Path(out).write_bytes(b"SQLite format 3\x00" + bytes(200))
        during.append(path.read_bytes() if path.exists() else None)
        raise RuntimeError("the build died halfway")

    monkeypatch.setattr(fetch, "build_store", dies_halfway)
    new = _entry("europe/luxembourg", MANNHEIM, "2026-10-05", LUX_BBOX)
    bodies2, _ = _world([new], tag="rail-data-2026-10-06")
    _with_asset(bodies2, new, MANNHEIM, tag="rail-data-2026-10-06")
    get, _ = _transport(bodies2)

    assert fetch.refresh(tmp_path, get=get) == 1

    # Mid-build, the installed store was neither partial nor missing.
    assert during == [original]
    # And afterwards the region still has the store it had, working.
    assert path.read_bytes() == original
    assert _ways(tmp_path, "europe/luxembourg") == ways
    assert _incoming(tmp_path) == []


# ---------------------------------------------------------------------------
# Two runs at once
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.name == "nt",
    reason="flock is POSIX and Windows has none; production is Linux "
           "containers. The per-run staging directory, which is what protects "
           "an unlocked run, is tested separately and does run here.",
)
def test_a_second_refresh_does_nothing_while_one_is_running(tmp_path, capsys):
    """Overlapping runs are the one way this step can publish a broken store.

    ``build_store`` removes its output and rebuilds it, so a second run
    building on the same staged path while the first renames it publishes a
    partially written file — and the sidecar written straight after records the
    *correct* digest, so every later run then reports the region up to date and
    the corruption never heals. Reproduced before this lock existed: an
    installed store of 2 kB, ``sqlite3.DatabaseError: file is not a database``,
    and a manifest still claiming coverage.

    A second run therefore does nothing and exits 0: the directory is being
    brought up to date by the run holding the lock, and blocking would turn a
    ``docker compose run`` into an apparent hang.
    """
    import fcntl

    work = tmp_path / fetch.WORK_DIRNAME
    work.mkdir()
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, calls = _transport(bodies)

    with (work / fetch.LOCK_NAME).open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert fetch.refresh(tmp_path, get=get) == 0

    assert "another refresh is already running" in capsys.readouterr().out
    assert calls == []
    assert not (tmp_path / store_filename("europe/luxembourg")).exists()
    assert not (tmp_path / "manifest.json").exists()


def test_each_run_stages_in_a_directory_of_its_own(tmp_path, monkeypatch):
    """Belt and braces: two runs cannot collide on a path even unlocked.

    Fixed staged names in a shared ``.incoming/`` are what makes overlapping
    runs dangerous in the first place. The lock is the real defence, but it is
    POSIX-only and one ``flock`` away from being lost in a refactor, so the
    staging directory carries this run's pid and a random suffix and is
    removed when the run ends.
    """
    staged_in = []
    real_build = fetch.build_store

    def spy(pbf, out, **kwargs):
        staged_in.append(Path(out).parent)
        return real_build(pbf, out, **kwargs)

    monkeypatch.setattr(fetch, "build_store", spy)

    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)
    assert fetch.refresh(tmp_path, get=get) == 0

    new = _entry("europe/luxembourg", MANNHEIM, "2026-10-05", LUX_BBOX)
    bodies2, _ = _world([new], tag="rail-data-2026-10-06")
    _with_asset(bodies2, new, MANNHEIM, tag="rail-data-2026-10-06")
    get, _ = _transport(bodies2)
    assert fetch.refresh(tmp_path, get=get) == 0

    assert len(staged_in) == 2
    assert staged_in[0] != staged_in[1]
    assert {d.parent for d in staged_in} == {tmp_path / fetch.WORK_DIRNAME}
    assert _incoming(tmp_path) == []


def test_what_a_killed_run_left_behind_is_cleared_by_the_next_one(tmp_path):
    """An OOM kill is the one failure the per-region cleanup cannot handle.

    ``docker compose run`` inherits the API service's memory limit and
    ``build_store`` peaks in the hundreds of MB for the largest regions, so a
    killed run is a real possibility — and it leaves the extract and the
    part-built store where they were. The next run holds the lock, so it can
    safely clear them, which is what makes "``.incoming/`` holds nothing but
    the lock between runs" true rather than aspirational.
    """
    work = tmp_path / fetch.WORK_DIRNAME
    work.mkdir()
    orphan = work / "manifest.json.part"
    orphan.write_text("{", encoding="utf-8")
    killed = work / "run-999-deadbeef"
    killed.mkdir()
    (killed / "germany-rail.osm.pbf").write_bytes(b"half an extract")
    (killed / "europe-germany.rail.sqlite.part").write_bytes(b"half a store")

    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)

    assert fetch.refresh(tmp_path, get=get) == 0

    assert not orphan.exists()
    assert not killed.exists()
    assert _incoming(tmp_path) == []


# ---------------------------------------------------------------------------
# A full disk
# ---------------------------------------------------------------------------

def _tear_writes(monkeypatch, marker: str) -> None:
    """Make writes to files named ``*marker*`` land half-written, then fail.

    What a full disk does: ``ENOSPC`` arrives partway through, leaving a
    truncated file behind. Which file that is — the staged copy or the live
    one — is the whole difference between the two ways of writing it.
    """
    real = Path.write_text

    def half(self, data, *args, **kwargs):
        if marker in self.name:
            real(self, data[:len(data) // 2], *args, **kwargs)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", half)


def test_a_full_disk_leaves_the_previous_manifest_whole(tmp_path, monkeypatch):
    """The manifest is renamed in too, and this is what that is worth.

    A worker re-reads this file every five minutes and a truncated one is not
    JSON: ``load_coverage`` would return nothing and every region would fall
    back to Overpass — the outage this issue exists to end — until someone
    noticed a file nobody looks at.
    """
    lux, de = _lux(), _de()
    bodies, _ = _world([lux, de])
    _with_asset(bodies, lux, LUXEMBOURG)
    _with_asset(bodies, de, MANNHEIM)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)
    before = (tmp_path / "manifest.json").read_bytes()

    lux2 = _entry("europe/luxembourg", MANNHEIM, "2026-10-05", LUX_BBOX)
    bodies2, _ = _world([lux2, de], tag="rail-data-2026-10-06")
    _with_asset(bodies2, lux2, MANNHEIM, tag="rail-data-2026-10-06")
    _with_asset(bodies2, de, MANNHEIM, tag="rail-data-2026-10-06")
    get, _ = _transport(bodies2)
    _tear_writes(monkeypatch, "manifest.json")

    with pytest.raises(OSError):
        fetch.refresh(tmp_path, get=get)

    assert (tmp_path / "manifest.json").read_bytes() == before
    assert sorted(r for r, _ in load_coverage(str(tmp_path))) == [
        "europe/germany", "europe/luxembourg"]


def test_a_full_disk_leaves_the_previous_sidecar_whole(tmp_path, monkeypatch):
    """The sidecar goes through a rename as well, so the claim holds for it.

    A torn sidecar costs no data — only this script reads it, and an
    unreadable one means a needless rebuild — but "every file appears by
    atomic rename" is a documented contract that Phases 4 and 5 will be built
    against, and an exception nobody can see is how a contract stops being one.
    """
    lux = _lux()
    bodies, _ = _world([lux])
    _with_asset(bodies, lux, LUXEMBOURG)
    get, _ = _transport(bodies)
    fetch.refresh(tmp_path, get=get)
    sidecar = tmp_path / (store_filename("europe/luxembourg") + fetch.SHA_SUFFIX)
    before = sidecar.read_bytes()

    new = _entry("europe/luxembourg", MANNHEIM, "2026-10-05", LUX_BBOX)
    bodies2, _ = _world([new], tag="rail-data-2026-10-06")
    _with_asset(bodies2, new, MANNHEIM, tag="rail-data-2026-10-06")
    get, _ = _transport(bodies2)
    _tear_writes(monkeypatch, fetch.SHA_SUFFIX)

    assert fetch.refresh(tmp_path, get=get) == 1

    assert sidecar.read_bytes() == before
    assert fetch.installed_build(tmp_path, "europe/luxembourg") == (lux["sha256"], SCHEMA_VERSION)


def test_a_full_disk_is_a_refusal_not_a_traceback(tmp_path, monkeypatch, capsys):
    """What the operator sees when the disk fills mid-run.

    The per-region lines have already been printed and the stores themselves
    are intact; a traceback out of the manifest write says none of that.
    """
    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fetch, "refresh", no_space)

    assert fetch.main(["--dest", str(tmp_path)]) == 1
    assert "error: [Errno 28]" in capsys.readouterr().out

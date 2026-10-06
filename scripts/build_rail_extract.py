#!/usr/bin/env python
"""Build the rail, ferry and bus OSM extracts for one region (issue #345, phase 1).

Route resolution needs four things from OpenStreetMap: railway ways, route
relations, the nodes a UIC code can be looked up on, and stations. Overpass
answers those over the network today, and the answers are large enough that
fair use bans us. The same data, filtered out of a Geofabrik country extract,
is three orders of magnitude smaller: Denmark 494 MB -> 0.8 MB, Germany
4.83 GB -> ~25 MB.

Ferry and bus resolution ask Overpass the same kind of question, so the same
download is filtered into three **layers**, one file each — ``rail``,
``ferry`` and ``bus`` (docs/LOCAL_TRANSPORT_DATA_PLAN.md, Decision 7). Separate
files and separate stores because bus ways are roads: one graph would let a rail
Dijkstra walk down a high street.

**The raw extracts must never reach the server, the repo, or an image.** Europe
raw is 34.9 GB and the VPS has 40 GB total. So this script runs in CI, on a
throwaway runner: download, filter, publish the small result, delete the rest.
It removes the source and the intermediate as soon as each is consumed rather
than at the end, so peak disk stays near one raw extract.

Two steps, in that order for a reason:

1. ``osmium tags-filter`` (C++) does the pass over the raw file. A pyosmium
   pass with a per-object Python callback measured 2,022 s for Germany in the
   spike; the CLI does the same reduction in a couple of minutes. But
   tags-filter can only OR tag patterns together — it cannot express "railway
   ways *without* a service tag" or "station *and* uic_ref" — so it can only
   over-select.
2. A pyosmium pass applies the exact selection, and is cheap because it runs
   over step 1's output (Denmark: 1 MB) rather than the raw extract.

The selection must match what src/services/overpass_service.py asks Overpass
for today, element for element. Anything else changes route results for reasons
unrelated to moving the data source, which is the one thing this migration must
not do. It is spelled out below against the queries themselves, because the
plan's summary of them was wrong once already (#349) and a filter that is
quietly too narrow looks exactly like one that works.

Usage:

    python scripts/build_rail_extract.py regions --json
    python scripts/build_rail_extract.py build europe/denmark --out-dir dist
    python scripts/build_rail_extract.py manifest --out-dir dist \
        --base released/manifest.json --expect '["europe/denmark"]'

``regions`` needs PyYAML alone — the workflow's plan job installs nothing else.

Requires the ``osmium`` CLI (Debian/Ubuntu: ``apt-get install osmium-tool``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

import yaml

# ``osmium`` and ``requests`` are imported inside the functions that need them.
# ``regions`` is the whole of the workflow's plan job and it runs on a runner
# that installs PyYAML and nothing else — importing the heavy pair at module
# scope made that job fail before it printed the matrix, so the build never ran.

_REPO_ROOT = Path(__file__).resolve().parent.parent
REGIONS_CONFIG = _REPO_ROOT / "config" / "rail_regions.yml"

GEOFABRIK_BASE = "https://download.geofabrik.de"

# Bumped only when the manifest's shape changes; phase 2 reads it to decide
# whether it understands the file at all. 2 added `status`, and with it entries
# that describe a region holding no rail rather than a published file. 3 added
# `layer`: one entry per region *and layer*.
MANIFEST_SCHEMA = 3
# What `manifest --base` merges into. A schema 2 release is the one this code
# replaces, and a subset recovery patching it must still work (R1-10): its
# entries are rail entries without the word, so they are carried as `rail`.
BASE_SCHEMAS = (2, MANIFEST_SCHEMA)

# The layers, in the order they are built and logged. src/rail/store.py has the
# same tuple; the two are not imported from each other (phases 1 and 2 share
# only the file and the manifest) and a test holds them equal.
RAIL = "rail"
LAYERS = (RAIL, "ferry", "bus")

# A region's outcome. `empty` is not a failure: a few configured regions have no
# railway at all (Andorra, Malta, the Azores) and one has only a line currently
# tagged `railway=construction` (Liechtenstein). Without a third outcome those
# are red matrix jobs every month, which buries a real failure — a renamed
# Geofabrik path, a mirror outage — in expected noise, and leaves the publish
# job unable to tell "no rail here" from "we did not build it".
STATUS_OK = "ok"
STATUS_EMPTY = "empty"

MANIFEST_NAME = "manifest.json"
# `<slug>-<layer>.entry.json` and `<slug>-<layer>.osm.pbf`: rail's names are
# the ones every release before layers used, so no published asset changes name.
ENTRY_SUFFIX = ".entry.json"
EXTRACT_SUFFIX = ".osm.pbf"


def extract_name(slug: str, layer: str) -> str:
    """``("germany", "bus")`` -> ``germany-bus.osm.pbf``, the release asset."""
    return f"{slug}-{layer}{EXTRACT_SUFFIX}"


def entry_name(slug: str, layer: str) -> str:
    return f"{slug}-{layer}{ENTRY_SUFFIX}"


# ---------------------------------------------------------------------------
# The selection — must mirror src/services/overpass_service.py
# ---------------------------------------------------------------------------
#
# Four rows, each one an Overpass query the resolver issues today. The queries
# are the specification; docs/LOCAL_RAIL_DATA_PLAN.md restates them and was
# wrong once already (#349), so check any change here against that source file
# rather than against the plan.
#
#   ways       way["railway"~"^(rail|narrow_gauge|light_rail)$"]["service"!~"."]
#              _via_coordinate_fallback's bounding-box query. The `service`
#              exclusion keeps sidings and yard tracks out of the graph; the
#              spike showed including them *breaks* routes that work today,
#              because _nearest_node snaps to the closest node regardless of
#              which connected component it lies in.
#   relations  rel["route"="train"], ["route"="railway"], ["route"="light_rail"]
#              _route_relation_segment lists all three; strategy B's
#              _ROUTE_TAGS is the same three written as one regex.
#   nodes      node["uic_ref"] — *any* node carrying the key, with no railway
#              filter at all. This is _route_relation_segment's
#              `node["uic_ref"="{uic}"]->.a`, which is how a relation is found
#              from a pair of UIC codes. Relations reference the *stop* node,
#              and stop nodes are routinely untagged as stations — Luxembourg
#              has 20 such members and not one is tagged station or halt — so
#              filtering these by railway= leaves strategy A finding nothing
#              where Overpass finds a relation (#349).
#   stations   node|way|rel ["railway"~"^(station|halt)$"]["uic_ref"]
#              _find_station_near, which queries all three element types with
#              `out center body` because some countries map a station as a
#              polygon rather than as a node.
#
# The station row is why some ways and relations are kept for reasons other
# than their own tags: a station way is useless without its nodes and a station
# relation without its member ways, since both only answer `out center` as a
# geometry. `select` calls that the geometry closure.
RAIL_WAY_TYPES = frozenset({"rail", "narrow_gauge", "light_rail"})
STATION_RAILWAY_TYPES = frozenset({"station", "halt"})
ROUTE_TYPES = frozenset({"train", "railway", "light_rail"})


def is_rail_way(tags: Mapping[str, str]) -> bool:
    """True for a way the coordinate-fallback graph is built from."""
    return tags.get("railway") in RAIL_WAY_TYPES and "service" not in tags


def is_route_relation(tags: Mapping[str, str]) -> bool:
    """True for a route relation strategies A and B search."""
    return tags.get("route") in ROUTE_TYPES


def is_uic_node(tags: Mapping[str, str]) -> bool:
    """True for any node a UIC lookup can land on — station or bare stop."""
    return bool(tags.get("uic_ref"))


def is_station(tags: Mapping[str, str]) -> bool:
    """True for a station _find_station_near can return, whatever its type."""
    return tags.get("railway") in STATION_RAILWAY_TYPES and bool(tags.get("uic_ref"))


# The ferry and bus layers mirror _get_route_geometry's three strategies, each
# one query:
#
#   A  rel["route"=MODE]          _via_route_relation_type, `out geom` — so the
#                                 relation's member ways come with it
#   B  way["route"=MODE]          _via_way_type_fallback
#   C  way["ferry"="yes"]         _via_ferry_yes_fallback, ferry only
#
# No node row: neither mode looks anything up by UIC code or asks for a
# station. A layer's nodes are its ways', and its relations' node members —
# the stops. `out geom` returns those located, and strategy A's bridge
# (_bridge_to_named_stops) joins a relation broken near a leg's end only towards
# a stop the relation names there: without them a broken bus route is drawn
# from whichever piece is longest, kilometres off its first stop. The resolver
# reads where a stop is and nothing else; it carries LAYER_TAGS like any node.
def is_ferry_route(tags: Mapping[str, str]) -> bool:
    """``route=ferry`` — strategy A's relations and strategy B's ways alike."""
    return tags.get("route") == "ferry"


def is_ferry_yes(tags: Mapping[str, str]) -> bool:
    """Strategy C's ways: island hoppers mapped with no route at all."""
    return tags.get("ferry") == "yes"


def is_bus_route(tags: Mapping[str, str]) -> bool:
    """``route=bus`` — strategy A's relations and strategy B's ways alike."""
    return tags.get("route") == "bus"


# `way.cls` in the store src/rail/builder.py builds from each file (Decision 9):
# bit 0 the layer's own way class (track for rail, route=MODE for ferry and
# bus), bit 1 `ferry=yes`, bit 2 a member of one of the layer's relations. Each
# layer's **routable set** is the bits below. It is what this script calls the
# layer `empty` on, what the builder refuses a store on, and what both measure
# the bbox over — one table, so CI never publishes a file the builder refuses
# and the manifest's box is the store's (R2-3). A bus layer is mostly relation
# members, because bus routes are mapped as relations over ordinary roads and
# `route=bus` ways are rare; bit 0 alone would make nearly every bus layer empty.
CLS_ROUTE = 1
CLS_FERRY_YES = 2
CLS_MEMBER = 4
ROUTABLE = {
    RAIL: CLS_ROUTE,
    "ferry": CLS_ROUTE | CLS_FERRY_YES | CLS_MEMBER,
    "bus": CLS_ROUTE | CLS_MEMBER,
}


def keeps_relation(layer: str, tags: Mapping[str, str]) -> bool:
    """Is this relation in *layer*'s file?"""
    if layer == RAIL:
        return is_route_relation(tags) or is_station(tags)
    if layer == "ferry":
        return is_ferry_route(tags)
    return is_bus_route(tags)


def way_class(layer: str, tags: Mapping[str, str], member: bool) -> int:
    """The ``cls`` bits a way has in *layer* — *member* being "a relation this
    layer keeps names it"."""
    if layer == RAIL:
        route = is_rail_way(tags)
    elif layer == "ferry":
        route = is_ferry_route(tags)
    else:
        route = is_bus_route(tags)
    return ((CLS_ROUTE if route else 0)
            | (CLS_FERRY_YES if is_ferry_yes(tags) else 0)
            | (CLS_MEMBER if member else 0))


def keeps_way(layer: str, tags: Mapping[str, str], member: bool) -> bool:
    """Is this way in *layer*'s file?

    Ferry and bus keep exactly their routable set. Rail also keeps what it
    needs for geometry and nothing routes over: station polygons, and the
    platforms and sidings a route relation names.
    """
    if layer == RAIL:
        return is_rail_way(tags) or is_station(tags) or member
    return bool(way_class(layer, tags, member) & ROUTABLE[layer])


# The tags src/rail/builder.py reads, by element kind — the only tags a ferry
# or bus file carries. A bus layer is millions of road nodes and ways whose
# `highway`, `surface`, `maxspeed` and `name` nothing reads: Germany's went
# from 160 MB to 112 MB without them, Denmark's from 3.6 to 2.8 MB. `uic_ref`
# and `railway` are there because the builder asks every element of every layer
# whether it is a station, so dropping them could change a store. Rail keeps
# every tag: its published fixture pins the file.
LAYER_TAGS = {
    "n": frozenset({"uic_ref", "railway"}),
    "w": frozenset({"route", "ferry", "uic_ref", "railway"}),
    "r": frozenset({"route", "name", "uic_ref", "railway"}),
}


def _strip(obj, keep: frozenset):
    """*obj* with only the tags in *keep*: itself if it has no others."""
    if all(tag.k in keep for tag in obj.tags):
        return obj
    return obj.replace(tags={tag.k: tag.v for tag in obj.tags if tag.k in keep})


@dataclass(frozen=True)
class Selection:
    """What one layer's filtered extract contains, and where it actually reaches."""

    # The layer's routable set (ROUTABLE): the discriminator between `ok` and
    # `empty`. For rail that is the track and nothing else, as it always was.
    ways: int
    # The layer's route relations; station relations are counted as stations.
    relations: int
    # Stations of every element type, which is what _find_station_near can
    # return. The manifest reports this one number rather than three. Rail
    # only: ferry and bus have no station lookup.
    stations: int
    # Not in the manifest — the contract fixes its keys — but counted because
    # this is the row a wrong filter silently empties, and it belongs in the
    # build log where a rebuild that lost it would be visible. Rail only.
    uic_nodes: int
    # Ways held because a kept relation references them and that are not of
    # the layer's own way class (bit 0). On rail they are geometry for
    # `out geom` parity, never track to route over — phase 2 flags them without
    # bit 0. On bus they are the roads the routes run on, and most of the file.
    member_ways: int
    # Ferry and bus: node members of the layer's relations — its stops — that
    # the file holds, located. Not in the manifest; in the build log, because a
    # zero here is what a broken stop closure looks like. Always 0 on rail,
    # whose relations' stops are #570.
    stop_nodes: int
    # Members of kept relations that this extract does not contain at all —
    # ways on the far side of a border, which live in the neighbouring
    # country's file. No filter can close that; it is phase 3's cross-border
    # case, and this is how big it is.
    #
    # Two ways of counting it, because they differ by 2.6x on the fixture and
    # by more on a country, and phase 3 needs to know which it is reading:
    # `member_ways_missing` counts *distinct way ids* nothing holds (Luxembourg
    # 82 % of the ids its relations name), `member_slots_missing` counts
    # *membership slots* — the same way named by six relations counts six times.
    member_ways_missing: int
    member_slots_missing: int
    member_slots: int
    # [min_lon, min_lat, max_lon, max_lat] over the nodes of the layer's
    # *routable set* — the rail ways, on rail — which is the same extent
    # src/rail/builder.py records for the store it builds from this file.
    # Phase 3 picks a region for a coordinate by this box, so the two phases
    # must not disagree about what the region covers: a bare uic_ref node or a
    # platform hundreds of kilometres from any track would otherwise claim
    # coverage here that the store does not report. Empty when the layer has no
    # routable way with a located node.
    bbox: list[float]


# ---------------------------------------------------------------------------
# Coverage configuration
# ---------------------------------------------------------------------------

def load_regions(path: Path = REGIONS_CONFIG) -> list[str]:
    """Return the configured Geofabrik region paths.

    Coverage is configuration (plan, phase 0): adding a country is an edit here
    plus a rebuild, never a code change.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    regions = data["regions"]
    if not regions:
        raise ValueError(f"{path} lists no regions")

    slugs = [region_slug(r) for r in regions]
    duplicates = sorted({s for s in slugs if slugs.count(s) > 1})
    if duplicates:
        # Output file names are keyed on the slug, so a collision would have one
        # region silently overwrite another in the published artifact.
        raise ValueError(f"{path}: region file names collide: {duplicates}")
    return list(regions)


def region_slug(region: str) -> str:
    """``europe/germany`` -> ``germany`` — the artifact's file-name stem."""
    return region.rstrip("/").rsplit("/", 1)[-1]


def source_url(region: str) -> str:
    return f"{GEOFABRIK_BASE}/{region}-latest.osm.pbf"


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_BACKOFF_SECONDS = 5.0


def _fetch_md5(get: Callable, url: str) -> str:
    """Geofabrik's ``<file>.md5``: the digest, then the file name."""
    response = get(url, timeout=60)
    response.raise_for_status()
    return response.text.split()[0].lower()


def _stream_to_file(get: Callable, url: str, dest: Path) -> tuple[str, str]:
    """Write *url* to *dest*, returning the md5 of what was written and the URL
    that actually served it, after redirects."""
    digest = hashlib.md5()
    with get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                digest.update(chunk)
                handle.write(chunk)
        served_by = response.url
    return digest.hexdigest(), served_by


def download(
    url: str,
    dest: Path,
    get: Callable | None = None,
    attempts: int = DOWNLOAD_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Stream a Geofabrik extract to disk and check it against its md5.

    Streamed, never held in memory: these are gigabytes.

    Everything downstream of this is checksummed twice — the manifest carries a
    sha256 of the artifact and the publish job re-checks it after the artifact
    round trip — while the input was the one step nobody verified. Geofabrik
    publishes ``<file>.md5`` beside every extract, so a truncated or corrupted
    download is detectable rather than something that shows up as a filter that
    quietly selected half a country.

    The ``.md5`` is fetched from beside the URL that *served* the file, not the
    one asked for. Geofabrik offloads its largest extracts to mirrors with a
    307, and for those the ``-latest`` checksum exists only on the mirror: the
    October 2026 scheduled run lost Germany to a 404 on
    ``download.geofabrik.de/europe/germany-latest.osm.pbf.md5`` while
    ``ftp5.gwdg.de/.../germany-latest.osm.pbf.md5`` answered. Asking the host
    that sent the bytes is also the only checksum that describes those bytes.

    Geofabrik runs on donated bandwidth and this workflow asks it for 49 files
    an hour, six at a time, so a transient failure is expected rather than
    exceptional: retry with a linear backoff, and only give up after that.
    """
    if get is None:
        import requests  # noqa: PLC0415 — CI-only, see the note beside the imports

        get = requests.get
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        try:
            digest, served_by = _stream_to_file(get, url, dest)
            expected = _fetch_md5(get, f"{served_by}.md5")
            if digest != expected:
                raise RuntimeError(
                    f"{url}: md5 mismatch — got {digest}, "
                    f"{served_by}.md5 says {expected}"
                )
            return dest
        except Exception as exc:  # noqa: BLE001 — every failure here is retryable
            if attempt == attempts:
                raise
            print(f"[{url}] attempt {attempt}/{attempts} failed: {exc}", flush=True)
            sleep(DOWNLOAD_BACKOFF_SECONDS * attempt)


def prefilter(source: Path, dest: Path) -> Path:
    """Reduce a raw extract to a superset of all three layers with the osmium CLI.

    Over-selects on purpose (see the module docstring): it keeps service ways
    and stations without a UIC code, which ``select`` then drops. What it buys
    is the two-orders-of-magnitude reduction that makes the exact pass
    affordable in Python.

    ``n/uic_ref`` matches on the key alone, with no value — the selection's
    node row has no railway filter, because the node a relation references for
    a UIC code is a stop, not necessarily a station.

    Referenced objects are kept — the default — because a way without its nodes
    has no geometry, and neither has a station relation without its member
    ways, nor a bus route without the roads it runs on. The same completion
    keeps a matching relation's node members, which are the ferry and bus
    layers' stops.
    """
    if shutil.which("osmium") is None:
        raise RuntimeError(
            "the osmium CLI is required (Debian/Ubuntu: apt-get install osmium-tool)"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["osmium", "tags-filter", "--overwrite",
                    "-o", str(dest), str(source), *prefilter_expressions()],
                   check=True)
    return dest


def prefilter_expressions() -> list[str]:
    """The tags-filter expressions: every predicate's tags, ORed together.

    Each layer's rows have to be here or that layer is empty in every region,
    silently — ``select`` can only narrow what this lets through.
    """
    stations = ",".join(sorted(STATION_RAILWAY_TYPES))
    return [
        "n/uic_ref",
        f"w/railway={','.join(sorted(RAIL_WAY_TYPES))}",
        f"w/railway={stations}",
        # Ferry and bus strategy B, and ferry strategy C.
        "w/route=bus,ferry",
        "w/ferry=yes",
        # Strategy A of all three modes in one expression.
        f"r/route={','.join(sorted(ROUTE_TYPES | {'bus', 'ferry'}))}",
        f"r/railway={stations}",
    ]


def select(source: Path, dests: Mapping[str, Path]) -> dict[str, Selection]:
    """Write the exact selection of each layer in *dests* to its file, and
    report what each holds.

    Three passes over ``source``, shared by every layer, because a PBF is
    ordered nodes, ways, relations and two of the things kept are only known
    from further down that order:

    1. relations — which members a kept relation needs: a station mapped as a
       polygon has no position of its own, a bus route is nothing but the
       roads it names, and a ferry or bus route's node members are its stops;
    2. ways — which nodes the kept ways need, for the same reason one level
       down. Only the kept ways: taking every node the prefilter over-selected
       would drag the file back up to the prefilter's size.
    3. write, every layer's file at once.

    Node ids are held in Python sets, not ``osmium.index.IdSet``: that is a
    dense bitset allocated in chunks across the id range, and a country's nodes
    are scattered over all 13 billion ids — 300,000 of them cost 1.5 GB, and
    Denmark's three layers peaked at 8.1 GB. A bus layer's nodes are millions
    of road nodes, so ferry and bus share one set for "kept" and "routable",
    and hold their stops — a few per route — in a set of their own.

    Per-object OSM metadata (version, timestamp, changeset, user) is dropped:
    nothing downstream reads it and it is ~15 % of the file. Ferry and bus
    also drop every tag but ``LAYER_TAGS``; rail keeps its tags.

    A layer with an empty routable set is not an error here — see
    ``STATUS_EMPTY``. The caller decides; ``ways`` is the discriminator,
    matching the refusal in src/rail/builder.py so that what phase 1 publishes
    is what phase 2 accepts.
    """
    import osmium  # noqa: PLC0415 — CI-only, see the note beside the imports

    unknown = set(dests) - set(LAYERS)
    if unknown:
        raise ValueError(f"unknown layer(s): {sorted(unknown)}")
    layers = [layer for layer in LAYERS if layer in dests]

    # Membership slots, not distinct ways: the same way named by six relations
    # is six slots. Both numbers are reported (see Selection).
    member_slot_counts: dict[str, Counter[int]] = {layer: Counter() for layer in layers}
    wanted_nodes: dict[str, set[int]] = {layer: set() for layer in layers}
    # Ferry and bus stops, apart from `wanted_nodes`: that set is also those
    # layers' routable nodes, which the bbox is measured over, and a stop is
    # not a way the store measures.
    stop_nodes: dict[str, set[int]] = {layer: set() for layer in layers}
    for rel in osmium.FileProcessor(str(source), osmium.osm.RELATION):
        for layer in layers:
            if not keeps_relation(layer, rel.tags):
                continue
            station = layer == RAIL and is_station(rel.tags)
            for member in rel.members:
                if member.type == "w":
                    member_slot_counts[layer][member.ref] += 1
                elif member.type == "n" and station:
                    wanted_nodes[layer].add(member.ref)
                elif member.type == "n" and layer != RAIL:
                    stop_nodes[layer].add(member.ref)

    # The nodes each bbox is measured over: the routable ways' own, no others.
    # Ferry and bus keep only their routable set, so for them it is the same
    # set; rail also keeps station and platform geometry, so it needs its own.
    routable_nodes = {layer: set() if layer == RAIL else wanted_nodes[layer]
                      for layer in layers}
    held_members: dict[str, set[int]] = {layer: set() for layer in layers}
    for way in osmium.FileProcessor(str(source), osmium.osm.WAY):
        refs = None
        for layer in layers:
            member = way.id in member_slot_counts[layer]
            if member:
                held_members[layer].add(way.id)
            if not keeps_way(layer, way.tags, member):
                continue
            if refs is None:
                refs = [node.ref for node in way.nodes]
            wanted_nodes[layer].update(refs)
            if way_class(layer, way.tags, member) & ROUTABLE[layer]:
                routable_nodes[layer].update(refs)

    counts = {layer: Counter() for layer in layers}
    extents = {layer: [180.0, 180.0, -180.0, -180.0] for layer in layers}
    writers = {
        layer: osmium.SimpleWriter(
            osmium.io.File(str(dests[layer]), "pbf,add_metadata=false"), overwrite=True)
        for layer in layers
    }
    try:
        for obj in osmium.FileProcessor(str(source)):
            tags = obj.tags
            # The tag-stripped copy ferry and bus write, made once for both.
            stripped = None
            if obj.is_node():
                for layer in layers:
                    # Rail's node row: every uic_ref, wherever it is.
                    uic = layer == RAIL and is_uic_node(tags)
                    stop = obj.id in stop_nodes[layer]
                    if not uic and not stop and obj.id not in wanted_nodes[layer]:
                        continue
                    counts[layer]["stop_nodes"] += stop
                    if layer == RAIL:
                        counts[layer]["uic_nodes"] += uic
                        counts[layer]["stations"] += is_station(tags)
                        writers[layer].add_node(obj)
                    else:
                        if stripped is None:
                            stripped = _strip(obj, LAYER_TAGS["n"])
                        writers[layer].add_node(stripped)
                    if obj.id in routable_nodes[layer]:
                        lon, lat = obj.location.lon, obj.location.lat
                        box = extents[layer]
                        box[0], box[2] = min(box[0], lon), max(box[2], lon)
                        box[1], box[3] = min(box[1], lat), max(box[3], lat)
                        counts[layer]["located"] = 1
            elif obj.is_way():
                for layer in layers:
                    member = obj.id in member_slot_counts[layer]
                    if not keeps_way(layer, tags, member):
                        continue
                    cls = way_class(layer, tags, member)
                    # `ways` counts the routable set and nothing else: a
                    # station polygon is counted as a station, and a member way
                    # outside the layer's own class is counted apart too.
                    counts[layer]["ways"] += bool(cls & ROUTABLE[layer])
                    counts[layer]["member_ways"] += member and not cls & CLS_ROUTE
                    if layer == RAIL:
                        counts[layer]["stations"] += is_station(tags)
                        writers[layer].add_way(obj)
                    else:
                        if stripped is None:
                            stripped = _strip(obj, LAYER_TAGS["w"])
                        writers[layer].add_way(stripped)
            else:
                for layer in layers:
                    if not keeps_relation(layer, tags):
                        continue
                    if layer == RAIL:
                        counts[layer]["stations"] += is_station(tags)
                    # Every ferry and bus relation kept is a route; a rail one
                    # may be a station instead.
                    counts[layer]["relations"] += layer != RAIL or is_route_relation(tags)
                    if layer == RAIL:
                        writers[layer].add_relation(obj)
                    else:
                        if stripped is None:
                            stripped = _strip(obj, LAYER_TAGS["r"])
                        writers[layer].add_relation(stripped)
    finally:
        for writer in writers.values():
            writer.close()

    selections = {}
    for layer in layers:
        held, slots, c = held_members[layer], member_slot_counts[layer], counts[layer]
        selections[layer] = Selection(
            ways=c["ways"],
            relations=c["relations"],
            stations=c["stations"],
            uic_nodes=c["uic_nodes"],
            member_ways=c["member_ways"],
            stop_nodes=c["stop_nodes"],
            member_ways_missing=len(slots) - len(held),
            member_slots_missing=sum(
                count for way_id, count in slots.items() if way_id not in held
            ),
            member_slots=sum(slots.values()),
            bbox=(
                [round(v, 5) for v in extents[layer]]
                # Gated on a routable node actually seen, not on `ways`: a way
                # whose nodes are all absent from the file would otherwise
                # publish the inverted starting values as the extent (#350).
                if c["located"] else []
            ),
        )
    return selections


def source_date(pbf: Path) -> str:
    """The extract's own date, from the PBF header — the artifact's version.

    Geofabrik stamps every extract with the replication timestamp it was cut
    at. Using it rather than the build date means a rebuild of an unchanged
    extract is recognisably the same data.
    """
    import osmium  # noqa: PLC0415 — CI-only, see the note beside the imports

    reader = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    try:
        stamp = reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()
    if not stamp:
        raise RuntimeError(f"{pbf} has no replication timestamp in its header")
    return stamp[:10]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entry(
    region: str, layer: str, extract: Path, selection: Selection, date: str
) -> dict:
    """One region's record for one layer (the phase 1 / phase 2 contract)."""
    return {
        "region": region,
        "layer": layer,
        "status": STATUS_OK,
        "file": extract.name,
        "source": source_url(region),
        "source_date": date,
        "sha256": sha256_file(extract),
        "bytes": extract.stat().st_size,
        "ways": selection.ways,
        "relations": selection.relations,
        "stations": selection.stations,
        "bbox": selection.bbox,
    }


def empty_entry(region: str, layer: str, date: str) -> dict:
    """A layer the pipeline built correctly and whose routable set is empty —
    for rail, a region that holds no rail ways.

    No file, so no checksum, size or bbox: there is nothing to download and
    nothing to cover. It is in the manifest so that phase 3 can tell "we know
    this region has no rail, use Overpass" from "we never built it", and so
    that the publish job's completeness check counts it as accounted for.
    """
    return {
        "region": region,
        "layer": layer,
        "status": STATUS_EMPTY,
        "source": source_url(region),
        "source_date": date,
    }


def merge_manifest(entries: Iterable[dict], generated_at: str | None = None) -> dict:
    """Combine per-region entries into the published manifest."""
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": MANIFEST_SCHEMA,
        "generated_at": generated_at,
        # By region, then layer — bus, ferry, rail — which is the order the
        # box's installed manifest uses (scripts/fetch_rail_data.py): a
        # reader from before layers that keys entries by region alone, last
        # one winning, then lands on rail.
        "regions": sorted(entries, key=lambda entry: (entry["region"], entry["layer"])),
    }


def verify_manifest(manifest: dict, directory: Path) -> None:
    """Check every entry against the file it describes.

    Runs in CI before publishing: a region whose build half-failed, or whose
    artifact was truncated in transit between jobs, must not be published as if
    it were whole.
    """
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"unknown manifest schema: {manifest.get('schema')!r}")
    for entry in manifest["regions"]:
        if entry.get("layer") not in LAYERS:
            raise ValueError(f"{entry['region']}: unknown layer {entry.get('layer')!r}")
        if entry["status"] == STATUS_EMPTY:
            # Nothing was published for it, so there is nothing to verify.
            continue
        if entry["status"] != STATUS_OK:
            raise ValueError(f"{entry['region']}: unknown status {entry['status']!r}")
        path = directory / entry["file"]
        if not path.is_file():
            raise ValueError(f"{entry['region']}: missing {entry['file']}")
        size = path.stat().st_size
        if size != entry["bytes"]:
            raise ValueError(
                f"{entry['region']}: {entry['file']} is {size} bytes, "
                f"manifest says {entry['bytes']}"
            )
        digest = sha256_file(path)
        if digest != entry["sha256"]:
            raise ValueError(f"{entry['region']}: {entry['file']} checksum mismatch")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def build(
    region: str,
    out_dir: Path,
    work_dir: Path,
    source: Path | None = None,
    keep_source: bool = False,
) -> dict[str, dict]:
    """Produce one region's filtered extracts and manifest entries, by layer."""
    slug = region_slug(region)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    downloaded = source is None
    if downloaded:
        source = work_dir / f"{slug}-source.osm.pbf"
        url = source_url(region)
        print(f"[{slug}] downloading {url}", flush=True)
        download(url, source)
    print(f"[{slug}] source {source.stat().st_size / 1e6:.0f} MB "
          f"(+{time.monotonic() - started:.0f}s)", flush=True)

    date = source_date(source)
    intermediate = work_dir / f"{slug}-prefilter.osm.pbf"
    prefilter(source, intermediate)
    print(f"[{slug}] prefiltered to {intermediate.stat().st_size / 1e6:.1f} MB "
          f"(+{time.monotonic() - started:.0f}s)", flush=True)

    # Freed the moment it is no longer needed, not at the end: a runner has to
    # hold one raw extract, never two.
    if downloaded and not keep_source:
        source.unlink()

    extracts = {layer: out_dir / extract_name(slug, layer) for layer in LAYERS}
    selections = select(intermediate, extracts)
    intermediate.unlink()

    entries = {}
    for layer in LAYERS:
        selection, extract = selections[layer], extracts[layer]
        if selection.ways:
            entry = manifest_entry(region, layer, extract, selection, date)
            size = f"{entry['bytes'] / 1e6:.2f} MB"
        else:
            # Nothing routable: the layer is `empty`, not failed. Phase 2
            # refuses a store whose routable set is empty, so publishing this
            # file would hand the next phase something it is right to reject.
            # Delete it and say so.
            extract.unlink()
            entry = empty_entry(region, layer, date)
            size = f"no {layer} ways — nothing published"
        entries[layer] = entry
        (out_dir / entry_name(slug, layer)).write_text(
            json.dumps(entry, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"[{slug} {layer}] {size}, status={entry['status']} "
            f"ways={selection.ways} relations={selection.relations} "
            f"stations={selection.stations} uic_nodes={selection.uic_nodes} "
            f"member_ways={selection.member_ways} stop_nodes={selection.stop_nodes} "
            f"member_ways_missing={selection.member_ways_missing} distinct ids "
            f"({selection.member_slots_missing} of {selection.member_slots} "
            f"membership slots) bbox={selection.bbox} source_date={date} "
            f"(+{time.monotonic() - started:.0f}s)",
            flush=True,
        )
    return entries


def _read_entries(out_dir: Path) -> list[dict]:
    """The entry files the build jobs of this run wrote into ``out_dir``."""
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(out_dir.glob(f"*{ENTRY_SUFFIX}"))
    ]


def collect_manifest(out_dir: Path, base: dict | None = None) -> dict:
    """Merge the entry files in ``out_dir`` into a verified manifest.

    *base* is the manifest of the release this run is updating, if it already
    has one. A rebuild of one region — the documented recovery path is
    ``workflow_dispatch`` with ``regions: europe/denmark`` — must not publish a
    manifest that disowns the 48 regions it did not touch: their assets are
    still attached to that release and phase 3 reads a missing entry as "not
    covered", so a one-region manifest silently sends most of Europe back to
    Overpass. So a region rebuilt in this run replaces its entries, and every
    other entry is carried through untouched.

    Replaced by region, not by (region, layer): what this run built for a region
    is the whole of what the manifest says about it, whichever of its layers
    made it. A layer that did not — dropped by the size guard — is then missing
    rather than last month's, which is what a full run would publish too, and
    the box carries its installed store for a release (U8).

    A schema 2 base has no ``layer``; its entries are rail entries, and are
    carried as ``layer: rail`` (R1-10).

    Only the rebuilt entries are verified against ``out_dir``: the carried ones
    describe files that are already release assets and were never downloaded.
    """
    entries = _read_entries(out_dir)
    if not entries:
        raise RuntimeError(f"no {ENTRY_SUFFIX} files in {out_dir}")
    verify_manifest(merge_manifest(entries), out_dir)

    rebuilt = {entry["region"] for entry in entries}
    carried = [
        {**entry, "layer": entry.get("layer", RAIL)}
        for entry in (base or {}).get("regions", [])
        if entry["region"] not in rebuilt
    ]
    manifest = merge_manifest(entries + carried)
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _regions(entries: Iterable[dict], layer: str = RAIL) -> set[str]:
    return {entry["region"] for entry in entries if entry.get("layer", RAIL) == layer}


def missing_regions(manifest: dict, expected: Iterable[str]) -> list[str]:
    """Expected regions the manifest does not account for.

    A region is accounted for whether it holds rail or not — an ``empty`` entry
    is an answer. What is missing is a region that was supposed to be in this
    manifest and is not, which is a build that failed, and phase 3 reads it as
    "not covered" and falls back to Overpass: the service that banned us.

    Rail only: rail is what completeness means. A missing ferry or bus layer is
    ``missing_layers``, and only a warning.
    """
    return sorted(set(expected) - _regions(manifest["regions"]))


def missing_layers(manifest: dict, expected: Iterable[str]) -> list[str]:
    """``"<region> <layer>"`` for each expected region's absent ferry or bus layer.

    Never a refusal: that region's ferry or bus keeps going to Overpass, as it
    did before layers existed, and its rail — the reason for this pipeline —
    must not wait for it.
    """
    return [f"{region} {layer}"
            for region in sorted(set(expected))
            for layer in LAYERS
            if layer != RAIL and region not in _regions(manifest["regions"], layer)]


def carried_regions(manifest: dict, expected: Iterable[str], out_dir: Path) -> list[str]:
    """Expected regions the manifest holds only because the base carried them.

    On a subset dispatch ``expected`` is the subset asked for, so a region that
    failed to build is still "covered" — by last month's entry from the base —
    and ``missing_regions`` passes it. The release is then republished as
    current around a ``source_date`` nobody rebuilt, with no message (#350).
    A region this run was asked to build has to have been built by it.
    """
    covered = _regions(manifest["regions"])
    rebuilt = _regions(_read_entries(out_dir))
    return sorted((set(expected) & covered) - rebuilt)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_regions = sub.add_parser("regions", help="list the configured regions")
    p_regions.add_argument("--json", action="store_true",
                           help="emit a JSON array (the workflow's build matrix)")

    p_build = sub.add_parser("build", help="filter one region")
    p_build.add_argument("region", help="Geofabrik path, e.g. europe/denmark")
    p_build.add_argument("--out-dir", type=Path, default=Path("dist/rail"))
    p_build.add_argument("--work-dir", type=Path, default=Path("dist/rail-work"))
    p_build.add_argument("--source", type=Path,
                         help="use a local .osm.pbf instead of downloading")
    p_build.add_argument("--keep-source", action="store_true",
                         help="keep the downloaded raw extract (local debugging only)")

    p_manifest = sub.add_parser("manifest", help="merge entry files into manifest.json")
    p_manifest.add_argument("--out-dir", type=Path, default=Path("dist/rail"))
    p_manifest.add_argument(
        "--base", type=Path,
        help="manifest.json of the release being updated (schema 2 or 3); regions "
             "not rebuilt in this run are carried through from it (missing file: "
             "ignored)")
    p_manifest.add_argument(
        "--expect", default=None,
        help="JSON array of the regions this run was supposed to cover "
             "(default: every region in the config). Publishing a manifest whose "
             "rail covers fewer is refused; a missing ferry or bus layer warns")
    p_manifest.add_argument(
        "--force", action="store_true",
        help="publish even though regions are missing (deliberate override)")

    args = parser.parse_args(argv[1:])

    if args.command == "regions":
        regions = load_regions()
        print(json.dumps(regions) if args.json else "\n".join(regions))
        return 0

    if args.command == "build":
        build(args.region, args.out_dir, args.work_dir,
              source=args.source, keep_source=args.keep_source)
        return 0

    base = None
    if args.base and args.base.is_file():
        base = json.loads(args.base.read_text(encoding="utf-8"))
        # Carried entries are written out unverified — collect_manifest only
        # re-checksums what this run built — so a base we do not understand
        # would publish a manifest of a shape nobody has validated. Refuse it
        # rather than merge two schemas into one file.
        # Schema 2 is understood: it is schema 3 with every entry rail.
        if base.get("schema") not in BASE_SCHEMAS:
            raise SystemExit(
                f"::error::{args.base} is manifest schema "
                f"{base.get('schema')!r}, not one of {BASE_SCHEMAS} — refusing "
                f"to merge into it"
            )
        print(f"merging into {len(base['regions'])} regions from {args.base}")

    manifest = collect_manifest(args.out_dir, base=base)
    expected = json.loads(args.expect) if args.expect else load_regions()
    missing = missing_regions(manifest, expected)
    carried = carried_regions(manifest, expected, args.out_dir)
    print(f"{len(manifest['regions'])} entries verified in {args.out_dir}")
    for absent in missing_layers(manifest, expected):
        print(f"::warning::{absent}: no entry — this layer is not published for "
              f"the region, which keeps resolving it through Overpass", flush=True)
    if missing:
        print(
            f"::error::{len(missing)} region(s) missing from the manifest — "
            f"publishing it would send them back to Overpass: "
            f"{', '.join(missing)}",
            flush=True,
        )
    if carried:
        print(
            f"::error::{len(carried)} region(s) this run was asked to rebuild "
            f"failed and would be republished from the previous build as if "
            f"current: {', '.join(carried)}",
            flush=True,
        )
    if missing or carried:
        if not args.force:
            return 1
        print("force requested: publishing an incomplete manifest anyway")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

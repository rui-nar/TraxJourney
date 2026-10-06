#!/usr/bin/env python
"""Check the route corpus against a directory of route stores (issue #345, U4, U10).

    python scripts/route_corpus.py <store dir> [--corpus config/route_corpus.yml]
                                               [--require-all]

The corpus (``config/route_corpus.yml``) is a list of real legs, each with the
shape its route is known to have: endpoints near the stations, a length band,
towns it must pass and towns it must not. Each leg is resolved exactly as the
server resolves it under ``RAIL_SOURCE=local`` — a rail leg by ``_resolve_rail``
over a ``LocalRailSource``, a ferry or bus leg by ``_resolve_route`` over a
``LocalRouteSource`` — and with no Overpass fallback, so nothing here touches
the network and the answer is the data's, not a public mirror's.

A ferry or bus leg the local stores find no route for is a *local miss*: where
production would ask Overpass. ``expect: {local_miss: true}`` records a leg
that must miss — the stores must not invent a route there — and any other leg
that misses fails, naming the miss. Rail never misses: it straight-lines,
which ``strategy_not: [straight]`` catches.

A leg is resolved against **only the regions it names**, in its own mode's
layer, whatever else the directory holds. The publish gate builds just the
regions and layers the corpus names, and a developer's directory may hold all
49; restricting each leg to its own regions is what makes the two runs give
the same answer. A leg whose route
genuinely reads a neighbour's data names that neighbour.

One line per leg:

* ``pass``                 every expectation holds;
* ``FAIL``                 one does not — each broken expectation is named;
* ``known-bad-unchanged``  a ``known_bad`` leg still has the outcome recorded
                           under ``current`` and still fails ``expect``;
* ``KNOWN-BAD-CHANGED``    a ``known_bad`` leg moved, in either direction: it
                           now passes ``expect`` (a fix — update the entry) or
                           no longer matches ``current`` (a different wrong);
* ``skip``                 a region the leg names has no store of the leg's
                           mode in the directory (``--require-all`` turns
                           this into ``FAIL``).

Exit status is 1 if any line is FAIL or KNOWN-BAD-CHANGED, else 0.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.rail.store import RailStoreCache, store_filename  # noqa: E402 — after the sys.path fix
from src.services.overpass_service import (  # noqa: E402
    OverpassError,
    RailGeometry,
    _polyline_km,
    _resolve_rail,
    _resolve_route,
)
from src.services.rail_source import LocalRailSource, load_coverage  # noqa: E402
from src.services.route_source import LocalRouteSource  # noqa: E402

DEFAULT_CORPUS = _REPO_ROOT / "config" / "route_corpus.yml"

# Each is also the name of the store layer its legs are resolved against.
MODES = ("rail", "ferry", "bus")
EXPECT_KEYS = {"max_endpoint_km", "length_km", "via", "avoid", "strategy_not",
               "degraded", "local_miss"}

PASS = "pass"
FAIL = "FAIL"
SKIP = "skip"
KNOWN_BAD_UNCHANGED = "known-bad-unchanged"
KNOWN_BAD_CHANGED = "KNOWN-BAD-CHANGED"
FAILING = {FAIL, KNOWN_BAD_CHANGED}

_KM_PER_DEG = 111.0   # the resolver's own equirectangular constant (_crow_km)


class CorpusError(Exception):
    """The corpus file itself is malformed — not a routing result."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _check_point(where: str, point, extra: tuple[str, ...] = ()) -> None:
    if not isinstance(point, dict):
        raise CorpusError(f"{where}: expected a mapping, got {point!r}")
    for key in ("lat", "lon", "label") + extra:
        if key not in point:
            raise CorpusError(f"{where}: missing {key!r}")


def _check_expect(where: str, expect, mode: str) -> None:
    if not isinstance(expect, dict) or not expect:
        raise CorpusError(f"{where}: expected a non-empty mapping")
    unknown = set(expect) - EXPECT_KEYS
    if unknown:
        raise CorpusError(f"{where}: unknown key(s) {sorted(unknown)}")
    if "local_miss" in expect:
        # A miss has no line to measure, so a shape beside it is never checked.
        if expect != {"local_miss": True}:
            raise CorpusError(f"{where}: local_miss is `true` and stands alone")
        if mode == "rail":
            raise CorpusError(f"{where}: a rail leg never misses, it straight-lines "
                              f"- use strategy_not: [straight]")
    band = expect.get("length_km")
    if band is not None and (len(band) != 2 or band[0] > band[1]):
        raise CorpusError(f"{where}.length_km: expected [min, max], got {band!r}")
    for kind in ("via", "avoid"):
        for i, point in enumerate(expect.get(kind) or []):
            _check_point(f"{where}.{kind}[{i}]", point, ("within_km",))


def load_corpus(path: str | os.PathLike) -> list[dict]:
    """The legs in *path*, validated. A typo in a key must not pass silently:
    an expectation the runner does not read is an expectation never checked."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    legs = (data or {}).get("legs")
    if not isinstance(legs, list) or not legs:
        raise CorpusError(f"{path}: no 'legs' list")
    names = set()
    for i, leg in enumerate(legs):
        name = leg.get("name") if isinstance(leg, dict) else None
        where = f"legs[{i}] ({name})"
        if not name:
            raise CorpusError(f"legs[{i}]: missing 'name'")
        if name in names:
            raise CorpusError(f"{where}: duplicate name")
        names.add(name)
        mode = leg.get("mode")
        if mode not in MODES:
            raise CorpusError(f"{where}: mode must be one of {MODES}")
        _check_point(f"{where}.from", leg.get("from"))
        _check_point(f"{where}.to", leg.get("to"))
        if not leg.get("regions"):
            raise CorpusError(f"{where}: missing 'regions'")
        _check_expect(f"{where}.expect", leg.get("expect"), mode)
        if "known_bad" in leg:
            if not leg["known_bad"]:
                raise CorpusError(f"{where}: known_bad needs a reason")
            _check_expect(f"{where}.current", leg.get("current"), mode)
        elif "current" in leg:
            raise CorpusError(f"{where}: 'current' is only for known_bad legs")
    return legs


def available_regions(store_dir: str | os.PathLike, layer: str = "rail") -> set[str]:
    """Regions the manifest lists as ``ok`` for *layer* *and* whose *layer*
    store file is present. A region's rail store says nothing about its ferry."""
    return {region for region, _ in load_coverage(str(store_dir), layer)
            if os.path.isfile(os.path.join(store_dir, store_filename(region, layer)))}


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat1 - lat2) * _KM_PER_DEG
    dlon = (lon1 - lon2) * _KM_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def distance_to_polyline_km(lat: float, lon: float, poly: list[list[float]]) -> float:
    """Shortest distance from (lat, lon) to the [[lon, lat], ...] polyline.

    Projected onto a plane centred on the point, which is exact enough at the
    few-km scale a ``within_km`` is written at, and measures to the segment
    rather than its vertices — a straight chord has no vertex near anything.
    """
    kx = _KM_PER_DEG * math.cos(math.radians(lat))
    best = math.inf
    pts = [((p[0] - lon) * kx, (p[1] - lat) * _KM_PER_DEG) for p in poly]
    if len(pts) == 1:
        return math.hypot(*pts[0])
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def measure(leg: dict, geometry) -> dict:
    """The outcome of one resolve; *geometry* None is a ferry or bus local miss."""
    if geometry is None:
        return {"local_miss": True}
    poly = geometry.polyline
    start, end = poly[0], poly[-1]
    return {
        "local_miss": False,
        "strategy": geometry.strategy,
        "degraded": geometry.degraded,
        "length_km": _polyline_km(poly),
        "start_km": _km(start[1], start[0], leg["from"]["lat"], leg["from"]["lon"]),
        "end_km": _km(end[1], end[0], leg["to"]["lat"], leg["to"]["lon"]),
        "poly": poly,
    }


def check(expect: dict, outcome: dict) -> list[str]:
    """Every expectation in *expect* that *outcome* breaks, worded for a human."""
    if expect.get("local_miss"):
        return [] if outcome["local_miss"] else [
            f"resolved locally ({outcome['strategy']}), expected a local miss"]
    if outcome["local_miss"]:
        return ["local miss: no route in the named regions"]
    broken = []
    if "degraded" in expect and outcome["degraded"] != expect["degraded"]:
        broken.append(f"degraded={outcome['degraded']}")
    if outcome["strategy"] in (expect.get("strategy_not") or []):
        broken.append(f"strategy={outcome['strategy']}")
    if "max_endpoint_km" in expect:
        limit = expect["max_endpoint_km"]
        for end in ("start", "end"):
            if outcome[f"{end}_km"] > limit:
                broken.append(f"{end} {outcome[f'{end}_km']:.2f} km off > {limit}")
    if "length_km" in expect:
        lo, hi = expect["length_km"]
        if not lo <= outcome["length_km"] <= hi:
            broken.append(f"length {_km_text(outcome['length_km'])} km "
                          f"outside [{lo}, {hi}]")
    for point in expect.get("via") or []:
        d = distance_to_polyline_km(point["lat"], point["lon"], outcome["poly"])
        if d > point["within_km"]:
            broken.append(f"via {point['label']} {d:.1f} km > {point['within_km']}")
    for point in expect.get("avoid") or []:
        d = distance_to_polyline_km(point["lat"], point["lon"], outcome["poly"])
        if d <= point["within_km"]:
            broken.append(f"avoid {point['label']} {d:.1f} km <= {point['within_km']}")
    return broken


def _km_text(km: float) -> str:
    """Whole km, as the rail bands are written, but a 3 km crossing to 0.1."""
    return f"{km:.0f}" if km >= 20 else f"{km:.1f}"


def _summary(outcome: dict) -> str:
    if outcome["local_miss"]:
        return "local miss"
    return (f"{outcome['strategy']}{' degraded' if outcome['degraded'] else ''}, "
            f"{_km_text(outcome['length_km'])} km, ends {outcome['start_km']:.2f}/"
            f"{outcome['end_km']:.2f} km off")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def judge(leg: dict, outcome: Optional[dict], error: Optional[str]) -> tuple[str, str]:
    """(status, detail) for one resolved leg."""
    if error is not None:
        broken, detail = [error], error
    else:
        broken, detail = check(leg["expect"], outcome), _summary(outcome)
    if "known_bad" not in leg:
        return (FAIL, f"{detail}; {'; '.join(broken)}") if broken else (PASS, detail)
    if not broken:
        return KNOWN_BAD_CHANGED, f"{detail}; now meets expect - update the entry"
    drift = [error] if error is not None else check(leg["current"], outcome)
    if drift:
        return KNOWN_BAD_CHANGED, f"{detail}; no longer as recorded: {'; '.join(drift)}"
    return KNOWN_BAD_UNCHANGED, f"{detail} ({leg['known_bad']})"


def _resolve(leg: dict, rail: LocalRailSource, route: LocalRouteSource):
    """The leg's geometry, or None for a ferry or bus leg with no local route.

    Ferry and bus go through ``_resolve_route``, the strategy chain
    ``get_ferry_geometry`` and ``get_bus_geometry`` run against the local
    source before they fall back. Its ``OverpassError`` is the "no route"
    that sends production to Overpass — here, the miss. Anything else it
    raises, the vertex ceiling's ``RailSourceOverload`` included, is reported
    as a crash, as for rail.
    """
    a, b = leg["from"], leg["to"]
    if leg["mode"] == "rail":
        return _resolve_rail([{"lat": a["lat"], "lon": a["lon"]},
                              {"lat": b["lat"], "lon": b["lon"]}], rail)
    try:
        poly, strategy = _resolve_route(leg["mode"], a["lat"], a["lon"],
                                        b["lat"], b["lon"], route)
    except OverpassError:
        return None
    return RailGeometry(poly, strategy, False, "local")


def run(legs: list[dict], store_dir: str | os.PathLike,
        require_all: bool = False) -> list[tuple[str, str, str]]:
    """(status, leg name, detail) per leg, in corpus order."""
    store_dir = str(store_dir)
    have = {mode: available_regions(store_dir, mode) for mode in MODES}
    full = {mode: load_coverage(store_dir, mode) for mode in MODES}
    # One source per kind for the whole run: every leg re-scopes its layer's
    # coverage, but a region opened for one leg stays open for the next.
    rail = LocalRailSource(store_dir, cache=RailStoreCache(store_dir, max_open=16))
    route = LocalRouteSource(store_dir)
    results = []
    for leg in legs:
        mode = leg["mode"]
        missing = sorted(set(leg["regions"]) - have[mode])
        if missing:
            status = FAIL if require_all else SKIP
            results.append((status, leg["name"],
                            f"{mode} regions absent: {', '.join(missing)}"))
            continue
        layer = rail if mode == "rail" else route._layer(mode)
        layer.coverage = [c for c in full[mode] if c[0] in leg["regions"]]
        outcome = error = None
        try:
            outcome = measure(leg, _resolve(leg, rail, route))
        except Exception as exc:  # noqa: BLE001 — a crash is an outcome, reported per leg
            error = f"resolve raised {type(exc).__name__}: {exc}"
        status, detail = judge(leg, outcome, error)
        results.append((status, leg["name"], detail))
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store_dir", help="directory holding manifest.json and the "
                                          "rail, ferry and bus stores")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--require-all", action="store_true",
                        help="a leg whose regions are absent fails instead of skipping")
    args = parser.parse_args(argv[1:])
    # Labels are free text; a cp1252 console must not crash the gate over one.
    sys.stdout.reconfigure(errors="backslashreplace")

    results = run(load_corpus(args.corpus), args.store_dir, args.require_all)
    width = max(len(name) for _, name, _ in results)
    for status, name, detail in results:
        print(f"{status:<19} {name:<{width}}  {detail}", flush=True)
    counts = {s: sum(1 for r in results if r[0] == s)
              for s in (PASS, FAIL, KNOWN_BAD_UNCHANGED, KNOWN_BAD_CHANGED, SKIP)}
    print(", ".join(f"{n} {s}" for s, n in counts.items()))
    return 1 if any(r[0] in FAILING for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

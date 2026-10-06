"""Where the ferry and bus resolvers get their elements from (issue #345).

``_get_route_geometry`` in ``src/services/overpass_service.py`` resolves a ferry
or bus leg with three strategies, and between them they ask two questions:

  A  the mode's route relations reaching a box, with member geometry
  B  the mode's own ways in a box                (``way.cls`` bit 0)
  C  ``ferry=yes`` ways in a box, ferry only     (``way.cls`` bit 1)

:class:`RouteSource` is those two questions. ``LocalRouteSource`` answers them
from the per-region ferry and bus stores, ``OverpassRouteSource`` — beside the
queries it wraps, in ``overpass_service`` — over the network. This is phase 3
of ``docs/LOCAL_RAIL_DATA_PLAN.md`` again, for the other two modes, and it is
built the same way: the strategies are unchanged and only read their elements
through the source (docs/LOCAL_TRANSPORT_DATA_PLAN.md, Decision 9).

A class *mask* rather than a strategy name, because the mask is what the store
indexes and what Decision 9 defines: strategy B asks for bit 0 and strategy C
for bit 1, so a way tagged both ``route=ferry`` and ``ferry=yes`` is in both
answers and a ``ferry=yes``-only way in C's alone — exactly what the two
Overpass queries return.

Everything about region boundaries is rail's, and is reused rather than copied:
every region whose layer box reaches the query is asked, results merge, and a
way or relation two extracts both hold is counted once (``LocalRailSource``,
``_merge_relations``). Ferry boxes are wide — a country's ferry layer reaches
every port its routes call at, so Denmark's runs from the Faroes to Finland —
and that is the case the merge exists for: a crossing is half in each extract,
and only the two together are the whole relation.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Sequence

from src.rail.store import RailStore
from src.services.rail_source import LocalRailSource, _ask, _merge_relations

# The modes this source answers for. Each is also the name of its store layer
# and the `route=` value its relations and bit-0 ways carry.
MODES = ("ferry", "bus")

Box = Sequence[float]   # (min_lat, min_lon, max_lat, max_lon)


class RouteSource(ABC):
    """The two questions the ferry and bus strategies ask, and nothing more.

    Element shapes are Overpass's ``out geom`` shapes, as for rail:
    ``_extract_relation_geometry`` and ``_build_rail_graph`` consume them
    unchanged.
    """

    @abstractmethod
    def relations_in_bbox(self, mode: str, bbox: Box) -> list[dict]:
        """``route=<mode>`` relations reaching *bbox*, with member geometry."""

    @abstractmethod
    def ways_in_bbox(self, mode: str, cls_mask: int, bbox: Box) -> list[dict]:
        """The *mode* layer's ways of class *cls_mask* whose geometry meets *bbox*."""


def _relation_ids_in_bbox(store: RailStore, mode: str, bbox: Box) -> list[int]:
    """Ids of *store*'s ``route=<mode>`` relations with a member way in *bbox*.

    Overpass's ``rel(bbox)`` also matches a relation by a member *node* in the
    box. A relation reaching a box only by a stop, with none of its ways'
    extents overlapping it, is the case ``RailStore.relations_near`` leaves out
    for rail on the same reasoning: a route's stops lie on its path. The way
    extents are a superset of "a node inside the box", which is the safe
    direction — a candidate strategy A then scores and may reject, never one it
    needed and did not see.

    Asked through the store's own locked connection: the store answers the
    rail strategies' questions by name and has no box query for relations, and
    this is one query, read-only, against tables the store documents.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    rows = store._query(
        "SELECT DISTINCT rw.rel_id FROM way_bbox b "
        "JOIN relation_way rw ON rw.way_id = b.id "
        "JOIN relation r ON r.id = rw.rel_id "
        "WHERE r.route = ? AND b.max_lon >= ? AND b.min_lon <= ? "
        "AND b.max_lat >= ? AND b.min_lat <= ?",
        (mode, min_lon, max_lon, min_lat, max_lat),
    )
    return [r[0] for r in rows]


class LocalRouteSource(RouteSource):
    """Answers from the ferry and bus stores in *directory*.

    One ``LocalRailSource`` per mode, each reading its own layer's coverage and
    holding its own bounded store cache, so a ferry query can never open a bus
    file and the two caches cannot evict each other. Raises whatever reading
    the manifest raises; the caller treats every exception as "no local
    coverage" (``overpass_service._local_route_source``), as for rail.
    """

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = str(directory)
        self._layers = {mode: LocalRailSource(self.directory, layer=mode)
                        for mode in MODES}

    def _layer(self, mode: str) -> LocalRailSource:
        try:
            return self._layers[mode]
        except KeyError:
            raise ValueError(f"unknown mode {mode!r}, expected one of "
                             f"{', '.join(MODES)}") from None

    def relations_in_bbox(self, mode: str, bbox: Box) -> list[dict]:
        """Every overlapping region's relations, each reassembled from all of them.

        Ids first, from every region, then geometry for the whole id set from
        every region, so a relation one extract names by a single member in the
        box is filled in by the extract holding the rest of it — the
        cross-border crossing (Decision 8).
        """
        box = tuple(bbox)
        stores = list(self._layer(mode)._stores_for(box))
        rel_ids = sorted({
            rel_id for store in stores
            for rel_id in _ask(store, _relation_ids_in_bbox, store, mode, box,
                               default=())
        })
        if not rel_ids:
            return []
        return _merge_relations(
            _ask(store, store.relation_geometry, rel_ids, default=[])
            for store in stores)

    def ways_in_bbox(self, mode: str, cls_mask: int, bbox: Box) -> list[dict]:
        """Merged and deduplicated by way id, under rail's merged vertex ceiling.

        The store selects by *extent* overlap; only the ways whose *geometry*
        meets the box are kept, which is what Overpass's ``way(bbox)`` returns.
        The difference is not a harmless superset here as it is for rail:
        strategies B and C snap each end to the nearest node with no distance
        limit, so a long crossing whose extent merely covers the box — the
        Ancona - Greece lines over the Bay of Naples — is a route to them, where
        Overpass has no ways and the leg falls back.

        Raises ``RailSourceOverload`` when the merged result is over the
        ceiling, which the resolver straight-lines rather than asking Overpass
        (see ``LocalRailSource.ways_in_bbox``). The ceiling is charged for the
        candidates, before this filter, because the candidates are what is
        decoded and held.
        """
        box = tuple(bbox)
        return [way for way in self._layer(mode).ways_in_bbox(*box, cls_mask=cls_mask)
                if _meets_box(way["geometry"], box)]


def _meets_box(geometry: list[dict], box: Box) -> bool:
    """Whether a way's polyline has a vertex in *box* or a segment crossing it.

    Plain lat/lon, as Overpass tests it. A single-vertex way is its vertex.
    """
    if len(geometry) == 1:
        point = geometry[0]
        return _segment_meets_box(point, point, box)
    return any(_segment_meets_box(a, b, box) for a, b in zip(geometry, geometry[1:]))


def _segment_meets_box(a: dict, b: dict, box: Box) -> bool:
    """Liang-Barsky: does the segment *a*-*b* have any point in *box*?

    Covers an end inside the box and a segment passing through it with both
    ends outside alike; a point on the boundary counts as inside.
    """
    min_lat, min_lon, max_lat, max_lon = box
    lat, lon = a["lat"], a["lon"]
    d_lat, d_lon = b["lat"] - lat, b["lon"] - lon
    enter, leave = 0.0, 1.0
    for p, q in ((-d_lon, lon - min_lon), (d_lon, max_lon - lon),
                 (-d_lat, lat - min_lat), (d_lat, max_lat - lat)):
        if p == 0:
            if q < 0:           # parallel to this edge and outside it
                return False
            continue
        t = q / p
        if p < 0:
            enter = max(enter, t)
        else:
            leave = min(leave, t)
        if enter > leave:
            return False
    return True

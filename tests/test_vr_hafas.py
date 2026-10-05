"""Tests for the two-endpoint train route-relation strategy in overpass_service.

Originally also covered the VR (Finnish Railways) HAFAS backend; that backend
was removed in issue #277 when train lookups moved to MOTIS/Transitous, which
indexes the same ``fi-digitraffic`` data. Finnish coverage now lives in
``test_motis_train_route.py::TestFinlandViaTheSameIndex``. Everything here is
about the Overpass geometry fallback, which trains only reach when no train
number is given or none could be matched.
"""

from unittest.mock import patch

import pytest

from src.services.overpass_service import (
    OverpassError,
    RailGeometry,
    _COMPONENT_BRIDGE_M,
    _best_relation_geometry,
    _extract_relation_geometry,
    _rail_length_ok,
    _via_train_relations_endpoints,
    get_rail_geometry,
)

# Helsinki → Oulu representative coordinates
_LAT1, _LON1 = 60.172097, 24.941249   # Helsinki Central Station
_LAT2, _LON2 = 65.010017, 25.484046   # Oulu Station

# Day-88 style: Hanko → Salo
_D88_LAT1, _D88_LON1 = 59.827043, 22.968801
_D88_LAT2, _D88_LON2 = 60.069381, 23.664059


# ---------------------------------------------------------------------------
# Two-endpoint train route-relation intersection tests
# ---------------------------------------------------------------------------

def _is_strategy_c(query: str) -> bool:
    """True for the coordinate-fallback bounding-box query (not UIC enrichment,
    not the Strategy-B relation-id lookup)."""
    return "uic_ref" not in query and "out ids" not in query


def _make_relation(lon_start, lat_start, lon_end, lat_end, mid_count=1):
    """Build a fake Overpass relation element."""
    geom = [{"lon": lon_start, "lat": lat_start}]
    for i in range(1, mid_count + 1):
        f = i / (mid_count + 1)
        geom.append({"lon": lon_start + f * (lon_end - lon_start),
                     "lat": lat_start + f * (lat_end - lat_start)})
    geom.append({"lon": lon_end, "lat": lat_end})
    return {"type": "relation", "id": 1, "members": [{"type": "way", "geometry": geom}]}


class TestTrainRelationsEndpoints:
    def test_finds_relation_covering_both_endpoints(self):
        """Strategy B returns geometry for a relation near both endpoints."""
        good_rel = _make_relation(_LON1, _LAT1, _LON2, _LAT2, mid_count=3)
        good_rel["id"] = 42

        # Both endpoint queries return the same relation ID.
        def _overpass_side_effect(query, purpose):
            if "out ids" in query:
                return {"elements": [{"id": 42}]}
            # Geometry fetch
            return {"elements": [good_rel]}

        with patch("src.services.overpass_service._overpass", side_effect=_overpass_side_effect):
            result = _via_train_relations_endpoints([
                {"lat": _LAT1, "lon": _LON1},
                {"lat": _LAT2, "lon": _LON2},
            ])

        assert len(result) >= 3
        # First point near start, last near end
        assert abs(result[0][1] - _LAT1) < 0.2
        assert abs(result[-1][1] - _LAT2) < 0.2

    def test_raises_when_no_common_relation(self):
        """No intersection → OverpassError, not a 2-point chord."""
        call_count = [0]

        def _overpass_side_effect(query, purpose):
            call_count[0] += 1
            if "out ids" in query:
                # First query returns id=1, second returns id=2 — no overlap.
                return {"elements": [{"id": call_count[0]}]}
            return {"elements": []}

        with patch("src.services.overpass_service._overpass", side_effect=_overpass_side_effect):
            with pytest.raises(OverpassError, match="No train route relation serves both"):
                _via_train_relations_endpoints([
                    {"lat": _LAT1, "lon": _LON1},
                    {"lat": _LAT2, "lon": _LON2},
                ])

    def test_raises_when_endpoints_too_far(self):
        """Relation found but its geometry doesn't match the query endpoints."""
        wrong_rel = _make_relation(18.0, 59.0, 18.5, 59.5, mid_count=2)
        wrong_rel["id"] = 99

        def _overpass_side_effect(query, purpose):
            if "out ids" in query:
                return {"elements": [{"id": 99}]}
            return {"elements": [wrong_rel]}

        with patch("src.services.overpass_service._overpass", side_effect=_overpass_side_effect):
            with pytest.raises(OverpassError, match="runs between the two stops"):
                _via_train_relations_endpoints([
                    {"lat": _LAT1, "lon": _LON1},
                    {"lat": _LAT2, "lon": _LON2},
                ])

    def test_get_rail_geometry_uses_strategy_b_when_uic_fails(self):
        """get_rail_geometry falls through to Strategy B when UIC enrichment fails."""
        good_rel = _make_relation(_LON1, _LAT1, _LON2, _LAT2, mid_count=3)
        good_rel["id"] = 42

        def _overpass_side_effect(query, purpose):
            # UIC enrichment queries return empty (no stations found).
            if "railway" in query and "uic_ref" in query:
                return {"elements": []}
            if "out ids" in query:
                return {"elements": [{"id": 42}]}
            return {"elements": [good_rel]}

        with patch("src.services.overpass_service._overpass", side_effect=_overpass_side_effect):
            result = get_rail_geometry([
                {"lat": _LAT1, "lon": _LON1},
                {"lat": _LAT2, "lon": _LON2},
            ])

        assert len(result.polyline) >= 3
        assert result.strategy == "relation_endpoints"
        assert result.degraded is False

    def test_get_rail_geometry_only_enriches_endpoints(self):
        """With N>2 stops, only first and last are enriched — not all N stops.

        VR returns uic='' for every stop; enriching all N triggers O(N) Overpass
        calls that cumulatively exceed the nginx proxy timeout on long routes
        like Helsinki→Rovaniemi.
        """
        good_rel = _make_relation(_LON1, _LAT1, _LON2, _LAT2, mid_count=3)
        good_rel["id"] = 42
        enrich_calls: list[str] = []

        def _overpass_side_effect(query, purpose):
            if "railway" in query and "uic_ref" in query:
                enrich_calls.append(query)
                return {"elements": []}  # no UIC found → falls through to B
            if "out ids" in query:
                return {"elements": [{"id": 42}]}
            return {"elements": [good_rel]}

        many_stops = [
            {"lat": _LAT1, "lon": _LON1},
            {"lat": 62.0,  "lon": 25.0},   # intermediate — must NOT be enriched
            {"lat": 63.5,  "lon": 25.2},   # intermediate — must NOT be enriched
            {"lat": _LAT2, "lon": _LON2},
        ]

        with patch("src.services.overpass_service._overpass", side_effect=_overpass_side_effect):
            result = get_rail_geometry(many_stops)

        # Exactly 2 enrichment calls (first + last), not 4.
        assert len(enrich_calls) == 2, f"Expected 2 enrichment calls, got {len(enrich_calls)}"
        assert len(result.polyline) >= 3


class TestCoordinateFallbackBoundedCalls:
    """Regression for the Helsinki–Rovaniemi multi-minute hang.

    When strategies A/B fail (no UIC, no covering route relation — common in
    Finland), resolution falls to the coordinate Dijkstra fallback. That used
    to issue one Overpass request *per consecutive stop pair*, so a ~20-stop
    route fired ~20 sequential 45 s-timeout requests. The fallback must now make
    a single whole-route query regardless of stop count.
    """

    def test_strategy_c_makes_single_overpass_query_for_long_route(self):
        n = 12
        stops = [
            {
                "lat": _LAT1 + (i / (n - 1)) * (_LAT2 - _LAT1),
                "lon": _LON1 + (i / (n - 1)) * (_LON2 - _LON1),
            }
            for i in range(n)
        ]

        # One fake railway way through every stop, so the shared-graph Dijkstra
        # connects each consecutive pair.
        rail_way = {
            "type": "way",
            "geometry": [{"lon": s["lon"], "lat": s["lat"]} for s in stops],
        }
        calls = {"enrich": 0, "ids": 0, "rail": 0, "total": 0}

        def _overpass_side_effect(query, purpose):
            calls["total"] += 1
            if "uic_ref" in query:        # endpoint UIC enrichment
                calls["enrich"] += 1
                return {"elements": []}   # no station → no UIC → skip Strategy A
            if "out ids" in query:        # Strategy B relation-id lookup
                calls["ids"] += 1
                return {"elements": []}   # none → Strategy B fails → Strategy C
            calls["rail"] += 1            # the single coordinate-fallback query
            return {"elements": [rail_way]}

        with patch("src.services.overpass_service._overpass", side_effect=_overpass_side_effect):
            result = get_rail_geometry(stops)

        # Exactly one whole-route query, regardless of the 12 stops.
        assert calls["rail"] == 1, f"Expected 1 rail query, got {calls['rail']}"
        # Whole resolution stays bounded (2 enrich + 1 Strategy-B id + 1 rail).
        assert calls["total"] <= 4, f"Expected <=4 Overpass calls, got {calls['total']}"
        assert len(result.polyline) >= 2
        assert result.strategy == "coordinate_dijkstra"
        assert result.degraded is False

    @pytest.mark.parametrize("a,b", [
        # Pathological cross-continent input, the case 12.0 was chosen for.
        ((0.0, 0.0), (40.0, 5.0)),
        # Offenburg → Hamburg: the real box from issue #277. Measured at
        # 14.28 deg^2 / 43.4 s / 61 MB and it *still* hit Overpass's own query
        # timeout, so there is nothing to gain by asking.
        ((48.4764, 7.9461), (53.5528, 10.0067)),
    ])
    def test_oversized_bbox_straight_lines_without_query(self, a, b):
        """A box Overpass cannot serve skips the query and returns a chord.

        Three mirrors x 45 s were previously spent (~135 s) to arrive at exactly
        the straight line this returns instantly.
        """
        stops = [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}]
        calls = {"rail": 0}

        def _overpass_side_effect(query, purpose):
            if "uic_ref" in query:
                return {"elements": []}
            if "out ids" in query:
                return {"elements": []}
            calls["rail"] += 1
            return {"elements": []}

        with patch("src.services.overpass_service._overpass", side_effect=_overpass_side_effect):
            result = get_rail_geometry(stops)

        assert calls["rail"] == 0, "Oversized bbox must not issue a rail query"
        assert result.polyline == [[a[1], a[0]], [b[1], b[0]]]
        # This is the silent-straight-line case the observability work surfaces:
        # the result must self-report as a degraded straight chord.
        assert result.degraded is True
        assert result.strategy == "straight"

    @pytest.mark.parametrize("a,b,why", [
        ((53.5528, 10.0067), (52.3770, 9.7410), "Hamburg-Hannover, 1.28 deg^2"),
        # The cap is on *area*, not span, precisely so a long leg through a
        # sparse network is not discarded along with a dense one: this is 6.3
        # degrees of latitude but only 8.80 deg^2, measured at 6.1 s / 4.2 MB.
        # It is also the route _via_coordinate_fallback's docstring exists for,
        # so a cap rejecting it would make that docstring describe a fiction.
        ((60.172097, 24.941249), (66.503948, 25.729391),
         "Helsinki-Rovaniemi, 8.80 deg^2"),
    ])
    def test_a_leg_inside_the_limit_still_queries(self, a, b, why):
        """The limit must not swallow legs Overpass has been measured to serve."""
        stops = [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}]
        calls = {"rail": 0}

        def _overpass_side_effect(query, purpose):
            if "uic_ref" in query or "out ids" in query:
                return {"elements": []}
            calls["rail"] += 1
            return {"elements": []}

        with patch("src.services.overpass_service._overpass", side_effect=_overpass_side_effect):
            get_rail_geometry(stops)

        assert calls["rail"] == 1, why


class TestTrainRelationGraphExtraction:
    """Regression for the Helsinki→Rovaniemi self-overlapping polyline.

    A train route relation contains parallel double-track ways, sidings and
    station tracks. Greedily chaining its member ways walks up one track and
    back down the other, producing a line several times longer than the real
    route that re-treads the same ground (observed: 3031 km rendered for a
    ~970 km line, 66% of points sharing grid cells). The graph-routed
    extraction must return a single clean start→end path instead.
    """

    @staticmethod
    def _way(points):
        return {"type": "way", "geometry": [{"lon": lon, "lat": lat} for lon, lat in points]}

    @staticmethod
    def _path_len(poly):
        return sum(
            ((poly[i + 1][0] - poly[i][0]) ** 2 + (poly[i + 1][1] - poly[i][1]) ** 2) ** 0.5
            for i in range(len(poly) - 1)
        )

    def _siding_relation(self):
        """A 1.0°-long north-bound main line with a dead-end siding branching
        east from the midpoint — the shape of a real station/passing track.

        Members are ordered so the greedy chain walks the south half, out along
        the siding, back, then the north half: an interior out-and-back that
        ``_trim`` cannot clip because it lies between the two endpoints.
        """
        south = [(0.0, 0.0), (0.0, 0.5)]
        siding = [(0.0, 0.5), (0.3, 0.5)]      # dead-end stub
        north = [(0.0, 0.5), (0.0, 1.0)]
        return {
            "type": "relation",
            "id": 1,
            "members": [self._way(south), self._way(siding), self._way(north)],
        }

    def test_graph_extraction_does_not_backtrack(self):
        rel = self._siding_relation()
        # Start at the south end, finish at the north end.
        geom = _extract_relation_geometry(rel, 0.0, 0.0, 1.0, 0.0)

        assert geom is not None and len(geom) >= 2
        # Net north span is 1.0°; the clean path is ~1.0 long. The siding
        # detour (out-and-back to lon 0.3) must be excluded entirely.
        assert self._path_len(geom) < 1.05, f"path too long: {self._path_len(geom)}"
        # No point wanders east onto the siding.
        assert max(pt[0] for pt in geom) < 0.01, "path entered the siding"
        # Latitude increases monotonically — no folding back south.
        lats = [pt[1] for pt in geom]
        assert lats == sorted(lats), "path folds back on itself"

    def test_disconnected_relation_never_chains_across_the_gap(self):
        """When the relation's ways don't connect start→end, nothing may bridge it.

        The Hanko→Salo garbage (116 km teleport, 6.2x the real distance) came
        from an old greedy chain that stitched disconnected ways together, and
        graph routing is what makes that structurally impossible: an edge exists
        only between vertices adjacent within one way.

        Since #359 the extraction returns the best *component* rather than None
        — that is what recovers a relation whose station throat is disconnected
        upstream — so the guarantee moved rather than weakened. What comes back
        never crosses the gap, and what a caller may ship is what
        ``_best_relation_geometry`` returns, which refuses this outright.
        """
        # Two ways with no shared node: one near the start, one near the end.
        rel = {"id": 1, "members": [
            self._way([(0.0, 0.0), (0.1, 0.0)]),   # component near start
            self._way([(5.0, 5.0), (5.1, 5.0)]),   # disconnected, near end
        ]}

        geom = _extract_relation_geometry(rel, 0.0, 0.0, 5.0, 5.0)
        # One component only — never a point from each, which is the teleport.
        assert max(pt[0] for pt in geom) < 1.0

        assert _best_relation_geometry([rel], 0.0, 0.0, 5.0, 5.0) is None

    def test_naming_both_stops_does_not_buy_a_way_across_that_gap(self):
        """#363 gave the extraction one bridge, and this is what bounds it.

        The relation now names a stop at each end, exactly as the Paris
        Montparnasse one does, so both endpoints anchor and both components are
        eligible to be joined to what they nearly touch. They are 780 km apart,
        against a ``_COMPONENT_BRIDGE_M`` of 250 m, so nothing is joined and the
        answer is byte-identical to the one above. A named stop is permission to
        cross a mapping gap, never a route to invent.
        """
        rel = {"id": 1, "members": [
            self._way([(0.0, 0.0), (0.1, 0.0)]),
            self._way([(5.0, 5.0), (5.1, 5.0)]),
            {"type": "node", "ref": 10, "role": "stop", "lat": 0.0, "lon": 0.0},
            {"type": "node", "ref": 11, "role": "stop", "lat": 5.0, "lon": 5.0},
        ]}

        assert _COMPONENT_BRIDGE_M < 1_000, "a bridge must stay a mapping gap"
        geom = _extract_relation_geometry(rel, 0.0, 0.0, 5.0, 5.0)
        assert max(pt[0] for pt in geom) < 1.0

        assert _best_relation_geometry([rel], 0.0, 0.0, 5.0, 5.0) is None


class TestRailGeometryPlausibilityGate:
    """Reject self-overlapping / wrong-relation geometry that is implausibly long
    for the trip, so it never ships as 'resolved'. Regression for Hanko→Salo
    (strategy B routed via Helsinki: 291 km for a 47 km crow / 6.2x)."""

    # Hanko → Salo
    _STOPS = [
        {"lat": 59.827043, "lon": 22.968801},
        {"lat": 60.069381, "lon": 23.664059},
    ]
    # Path that detours to Helsinki (lon 24.93) and back — ~280 km, the bug shape.
    _GARBAGE = [[22.97, 59.83], [24.93, 60.20], [22.97, 59.83], [23.66, 60.07]]
    # A clean ~50 km corridor path.
    _CLEAN = [[22.97, 59.83], [23.20, 59.90], [23.66, 60.07]]

    def test_rail_length_ok_rejects_overlong_and_accepts_clean(self):
        assert _rail_length_ok(self._CLEAN, self._STOPS) is True
        assert _rail_length_ok(self._GARBAGE, self._STOPS) is False

    def test_strategy_b_garbage_is_rejected_and_falls_through_to_c(self):
        with patch("src.services.overpass_service._enrich_uic", side_effect=lambda s, *_: s), \
             patch("src.services.overpass_service._via_train_relations_endpoints",
                   return_value=self._GARBAGE), \
             patch("src.services.overpass_service._via_coordinate_fallback",
                   return_value=self._CLEAN):
            result = get_rail_geometry(self._STOPS)
        assert result.strategy == "coordinate_dijkstra"  # B rejected → fell to C
        assert result.polyline == self._CLEAN
        assert result.degraded is False

    def test_plausible_strategy_b_is_accepted(self):
        with patch("src.services.overpass_service._enrich_uic", side_effect=lambda s, *_: s), \
             patch("src.services.overpass_service._via_train_relations_endpoints",
                   return_value=self._CLEAN):
            result = get_rail_geometry(self._STOPS)
        assert result.strategy == "relation_endpoints"
        assert result.degraded is False

    def test_garbage_strategy_c_is_straight_lined_and_flagged_degraded(self):
        with patch("src.services.overpass_service._enrich_uic", side_effect=lambda s, *_: s), \
             patch("src.services.overpass_service._via_train_relations_endpoints",
                   side_effect=OverpassError("no relation")), \
             patch("src.services.overpass_service._via_coordinate_fallback",
                   return_value=self._GARBAGE):
            result = get_rail_geometry(self._STOPS)
        # A self-overlapping last resort is worse than an honest straight line.
        assert result.strategy == "straight"
        assert result.degraded is True
        assert result.polyline == [[22.968801, 59.827043], [23.664059, 60.069381]]


class TestRailDegradedReporting:
    """Point-1 observability: rail must self-report when it silently fell back to
    a straight endpoint chord, so the UI/logs can tell an approximate line from a
    real resolved route. These are the exact production conditions (Overpass
    failing or returning nothing for the NAS) that previously looked like success.
    """

    _STOPS = [
        {"lat": _LAT1, "lon": _LON1},
        {"lat": _LAT2, "lon": _LON2},
    ]

    def test_overpass_failure_reports_degraded_straight_line(self):
        """Every Overpass call raises (network down/blocked) → strategies A/B/C
        all fail → straight chord, flagged degraded (not a silent 'resolved')."""
        calls = {"rail": 0}

        def _boom(query, purpose):
            if _is_strategy_c(query):
                calls["rail"] += 1
            raise OverpassError("Overpass timeout")

        with patch("src.services.overpass_service._overpass", side_effect=_boom):
            result = get_rail_geometry(self._STOPS)

        # These stops must actually reach Strategy C's bounding-box query. When
        # the guard was a max-*span* cap this pair was over it, so the fallback
        # short-circuited before issuing that query at all and the test passed
        # for a completely different reason than the one it claims to test.
        assert calls["rail"] == 1, "Strategy C's rail query was never issued"
        assert isinstance(result, RailGeometry)
        assert result.degraded is True
        assert result.strategy == "straight"
        assert result.polyline == [[_LON1, _LAT1], [_LON2, _LAT2]]

    def test_empty_overpass_reports_degraded_straight_line(self):
        """Overpass reachable but returns no rail elements → no graph → straight
        chord, flagged degraded."""
        calls = {"rail": 0}

        def _empty(query, purpose):
            if _is_strategy_c(query):
                calls["rail"] += 1
            return {"elements": []}

        with patch("src.services.overpass_service._overpass", side_effect=_empty):
            result = get_rail_geometry(self._STOPS)

        assert calls["rail"] == 1, "Strategy C's rail query was never issued"
        assert result.degraded is True
        assert result.strategy == "straight"

    def test_successful_relation_is_not_degraded(self):
        """A real relation result must NOT be flagged degraded."""
        good_rel = _make_relation(_LON1, _LAT1, _LON2, _LAT2, mid_count=3)
        good_rel["id"] = 7

        def _side_effect(query, purpose):
            if "uic_ref" in query:
                return {"elements": []}           # skip Strategy A
            if "out ids" in query:
                return {"elements": [{"id": 7}]}  # Strategy B candidate
            return {"elements": [good_rel]}

        with patch("src.services.overpass_service._overpass", side_effect=_side_effect):
            result = get_rail_geometry(self._STOPS)

        assert result.degraded is False
        assert result.strategy == "relation_endpoints"
        assert len(result.polyline) >= 3

    def test_two_point_chord_is_degraded_even_when_endpoints_snapped(self):
        """Regression for the `points=2 degraded=False strategy=coordinate_dijkstra`
        mislabel: _enrich_uic snaps endpoints to nearby stations, so the straight
        chord won't equal _straight(original coords). A 2-point result must still
        be reported as a degraded straight line, not a real route.
        """
        # Chord whose coords differ from the original stops (i.e. "snapped").
        snapped_chord = [[_LON1 + 0.02, _LAT1 + 0.02], [_LON2 + 0.02, _LAT2 + 0.02]]
        with patch("src.services.overpass_service._enrich_uic", side_effect=lambda s, *_: s), \
             patch("src.services.overpass_service._via_train_relations_endpoints",
                   side_effect=OverpassError("429")), \
             patch("src.services.overpass_service._via_coordinate_fallback",
                   return_value=snapped_chord):
            result = get_rail_geometry(self._STOPS)
        assert result.degraded is True
        assert result.strategy == "straight"


class TestOverpassMirrorFallback:
    """A rate-limited primary must not sink the whole resolve (the Helsinki→
    Rovaniemi 429 that fell to a 2-point straight line).

    How that is achieved changed twice on 2026-09-06. It briefly waited out a 429
    and retried the same host, which is the documented ban trigger and got this
    deployment blocked; it now marks the host as cooling and moves on without
    retrying. See tests/test_overpass_failover.py for the full contract. These
    tests keep the original end-to-end guarantee either way: a rate-limited
    primary still yields a real result.
    """

    class _Resp:
        def __init__(self, status, data=None):
            self.status_code = status
            self.ok = status < 400
            self._data = data
            # Real requests.Response always carries these; _overpass reads them
            # to find out how long a busy host wants us to wait.
            self.headers = {}
            self.text = ""
            self.content = b""

        def json(self):
            return self._data

    def test_falls_back_to_a_mirror_when_the_primary_is_rate_limited(self):
        """"Primary" means first in the list, not a particular hostname.

        This asserted on overpass-api.de by name, so demoting it below a
        working mirror broke a test about failover for a reason that had
        nothing to do with failover. The order is measured health and
        changes when the measurements do; the behaviour under test does not.
        """
        import src.services.overpass_service as ov
        primary = ov._OVERPASS_ENDPOINTS[0]
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            if url == primary:
                return self._Resp(429)
            return self._Resp(200, {"elements": [{"ok": 1}]})

        with patch("src.services.overpass_service.requests.post", side_effect=fake_post), \
             patch("time.sleep", lambda *_: None):
            data = ov._overpass("[out:json];", "rail")

        assert data == {"elements": [{"ok": 1}]}
        # Exactly one attempt on the primary: it is marked as cooling and this
        # query moves on. Retrying it in band is the ban trigger — see
        # tests/test_overpass_failover.py::TestBackOffRatherThanRetry.
        assert calls.count(primary) == 1
        assert any(u != primary for u in calls)

    def test_all_endpoints_429_raises_overpass_error(self):
        import src.services.overpass_service as ov

        with patch("src.services.overpass_service.requests.post",
                   side_effect=lambda url, **k: self._Resp(429)), \
             patch("time.sleep", lambda *_: None):
            with pytest.raises(OverpassError):
                ov._overpass("[out:json];", "rail")

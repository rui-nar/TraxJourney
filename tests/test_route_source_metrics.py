"""traxjourney_route_resolves_total: which source answered each route resolve
(issue #345). Counters only increase, so every test asserts on a delta."""

from unittest.mock import patch

import pytest

from api.segments import _compute_segment_geometry
from src.models.project import ConnectingSegment, SegmentEndpoint
from src.services import overpass_service as ov
from src.services.hafas_service import TrainRoute
from src.services.overpass_service import RailGeometry

_NAME = "traxjourney_route_resolves_total"
_STOPS = [{"lat": 49.6, "lon": 6.13}, {"lat": 49.64, "lon": 5.98}]


def _segment(kind):
    return ConnectingSegment(
        id="seg-1", segment_type=kind, label="A -> B",
        start=SegmentEndpoint(60.17, 24.94), end=SegmentEndpoint(66.5, 25.73))


def _geom(degraded=False):
    return RailGeometry([[6.13, 49.6], [5.98, 49.64]],
                        "straight" if degraded else "relation_uic", degraded)


class TestRailGeometrySource:
    def test_a_local_hit_is_local(self):
        with patch.object(ov, "_local_rail_source", return_value=object()), \
             patch.object(ov, "_resolve_rail", return_value=_geom()):
            assert ov.get_rail_geometry(_STOPS).source == "local"

    def test_a_local_miss_that_falls_back_is_overpass(self):
        with patch.object(ov, "_local_rail_source", return_value=object()), \
             patch.object(ov, "_resolve_rail",
                          side_effect=[_geom(degraded=True), _geom()]):
            assert ov.get_rail_geometry(_STOPS).source == "overpass"

    def test_no_local_source_is_overpass(self):
        with patch.object(ov, "_local_rail_source", return_value=None), \
             patch.object(ov, "_resolve_rail", return_value=_geom()):
            assert ov.get_rail_geometry(_STOPS).source == "overpass"


class TestResolveCounter:
    def test_a_motis_match_counts_motis(self, metric):
        seg = _segment("train")
        track = [[24.94, 60.17], [25.73, 66.5]]
        labels = dict(mode="train", source="motis", degraded="false")
        before = metric(_NAME, **labels)
        with patch("src.services.hafas_service.get_train_route",
                   return_value=TrainRoute(list(_STOPS), track)):
            _compute_segment_geometry(seg, {"train_number": "273"})
        assert metric(_NAME, **labels) == before + 1

    @pytest.mark.parametrize("source", ["local", "overpass"])
    def test_a_rail_resolve_counts_its_source(self, metric, source):
        geom = _geom()
        geom.source = source
        labels = dict(mode="train", source=source, degraded="false")
        before = metric(_NAME, **labels)
        with patch("src.services.overpass_service.get_rail_geometry",
                   return_value=geom):
            _compute_segment_geometry(_segment("train"), {})
        assert metric(_NAME, **labels) == before + 1

    def test_a_degraded_rail_resolve_says_so(self, metric):
        labels = dict(mode="train", source="overpass", degraded="true")
        before = metric(_NAME, **labels)
        with patch("src.services.overpass_service.get_rail_geometry",
                   return_value=_geom(degraded=True)):
            _compute_segment_geometry(_segment("train"), {})
        assert metric(_NAME, **labels) == before + 1

    @pytest.mark.parametrize("source", ["local", "overpass"])
    @pytest.mark.parametrize("kind,mode,getter", [
        ("boat", "boat", "get_ferry_geometry"),
        ("bus", "bus", "get_bus_geometry"),
    ])
    def test_ferry_and_bus_count_their_source(
            self, metric, kind, mode, getter, source):
        labels = dict(mode=mode, source=source, degraded="false")
        before = metric(_NAME, **labels)
        with patch(f"src.services.overpass_service.{getter}",
                   return_value=RailGeometry([[1.0, 1.0], [1.5, 1.5], [2.0, 2.0]],
                                             "relation", False, source)):
            polyline, _, degraded, _ = _compute_segment_geometry(_segment(kind), {})
        assert metric(_NAME, **labels) == before + 1
        assert polyline == [[1.0, 1.0], [1.5, 1.5], [2.0, 2.0]]
        assert degraded is False

    @pytest.mark.parametrize("kind,mode,getter", [
        ("boat", "boat", "get_ferry_geometry"),
        ("bus", "bus", "get_bus_geometry"),
    ])
    def test_a_box_refused_locally_counts_degraded(self, metric, kind, mode, getter):
        """The vertex ceiling straight-lines without asking Overpass, so the
        one degraded ferry or bus result is local, and it says so."""
        labels = dict(mode=mode, source="local", degraded="true")
        before = metric(_NAME, **labels)
        with patch(f"src.services.overpass_service.{getter}",
                   return_value=RailGeometry([[1.0, 1.0], [2.0, 2.0]],
                                             "straight", True, "local")):
            _, _, degraded, _ = _compute_segment_geometry(_segment(kind), {})
        assert metric(_NAME, **labels) == before + 1
        assert degraded is True


class TestFerryBusGeometrySource:
    """``_get_route_geometry`` decides the source the counter is labelled with."""

    @staticmethod
    def _route(poly):
        return poly, "relation"

    def test_a_local_hit_is_local(self):
        with patch.object(ov, "_local_route_source", return_value=object()),              patch.object(ov, "_resolve_route",
                          return_value=self._route([[1.0, 1.0], [2.0, 2.0]])):
            assert ov.get_ferry_geometry(1.0, 1.0, 2.0, 2.0).source == "local"

    def test_a_local_miss_that_falls_back_is_overpass(self):
        with patch.object(ov, "_local_route_source", return_value=object()),              patch.object(ov, "_resolve_route", side_effect=[
                 ov.OverpassError("nothing local"),
                 self._route([[1.0, 1.0], [2.0, 2.0]])]):
            assert ov.get_bus_geometry(1.0, 1.0, 2.0, 2.0).source == "overpass"

    def test_no_local_source_is_overpass(self):
        with patch.object(ov, "_local_route_source", return_value=None),              patch.object(ov, "_resolve_route",
                          return_value=self._route([[1.0, 1.0], [2.0, 2.0]])):
            assert ov.get_ferry_geometry(1.0, 1.0, 2.0, 2.0).source == "overpass"

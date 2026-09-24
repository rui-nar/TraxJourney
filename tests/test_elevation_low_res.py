"""Low-res-first elevation chart: the lightweight column + to_dict fallback.

The /meta (and shared /meta) responses defer the multi-MB full elevation profile
but now carry a downsampled copy in the same `elevation_profile` field, so the
chart renders immediately; the full profile upgrades it in the background.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from src.models.activity import Activity
from src.models.project import Project
from src.project.project_io import ProjectIO
from src.project.project_repo import ProjectRepo, _low_res_ep_json


def _act(full=None, low=None) -> Activity:
    a = Activity.from_strava_api({"id": 1, "name": "A", "type": "Ride"})
    a.elevation_profile = full
    a.elevation_profile_low_res = low
    return a


class TestToDictFallback:
    def test_uses_low_res_when_full_absent(self):
        a = _act(full=None, low=([0.0, 1.0, 2.0], [10.0, 20.0, 15.0]))
        d = ProjectIO.to_dict(Project(name="t", activities=[a]))
        assert d["activities"][0]["elevation_profile"] == [
            [0.0, 10.0], [1.0, 20.0], [2.0, 15.0],
        ]

    def test_prefers_full_over_low_res(self):
        a = _act(full=([0.0, 5.0], [100.0, 200.0]), low=([0.0], [1.0]))
        d = ProjectIO.to_dict(Project(name="t", activities=[a]))
        assert d["activities"][0]["elevation_profile"] == [[0.0, 100.0], [5.0, 200.0]]

    def test_none_when_neither_present(self):
        d = ProjectIO.to_dict(Project(name="t", activities=[_act(None, None)]))
        assert d["activities"][0]["elevation_profile"] is None


class TestLowResEpJson:
    def test_downsamples_full_profile(self):
        full = json.dumps({
            "distances_km": [i * 0.01 for i in range(2000)],
            "elevations_m": [float(i % 9) for i in range(2000)],
        })
        low = _low_res_ep_json(full)
        assert low is not None
        parsed = json.loads(low)
        assert 0 < len(parsed["distances_km"]) < 2000

    def test_handles_missing_or_garbage(self):
        assert _low_res_ep_json(None) is None
        assert _low_res_ep_json("not json") is None
        assert _low_res_ep_json(json.dumps({"distances_km": [], "elevations_m": []})) is None


class TestRowToActivityThreading:
    def _row(self):
        return SimpleNamespace(
            id=1, name="A", type="Ride", distance=0.0, moving_time=0, elapsed_time=0,
            total_elevation_gain=0.0, start_date="", start_date_local="", timezone="UTC",
            achievement_count=0, kudos_count=0, comment_count=0, athlete_count=0,
            photo_count=0, trainer=False, commute=False, manual=False, private=False,
            flagged=False, average_speed=0.0, max_speed=0.0,
            pr_count=0, total_photo_count=0, has_kudoed=False, gear_id=None,
            elev_high=None, elev_low=None,
            start_latlng_json=None, end_latlng_json=None, summary_polyline=None,
            elevation_profile_json=json.dumps(
                {"distances_km": [0.0, 1.0], "elevations_m": [5.0, 6.0]}),
            elevation_profile_low_res_json=json.dumps(
                {"distances_km": [0.0], "elevations_m": [5.0]}),
        )

    def test_meta_path_reads_low_res_not_full(self):
        # include_heavy=False mirrors the deferred-column meta load.
        a = ProjectRepo._row_to_activity(self._row(), include_heavy=False)
        assert a.elevation_profile is None
        assert a.elevation_profile_low_res == ([0.0], [5.0])

    def test_full_path_reads_both(self):
        a = ProjectRepo._row_to_activity(self._row(), include_heavy=True)
        assert a.elevation_profile == ([0.0, 1.0], [5.0, 6.0])
        assert a.elevation_profile_low_res == ([0.0], [5.0])

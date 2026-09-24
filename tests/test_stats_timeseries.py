"""Tests for _compute_stats ride_time_series field."""

from datetime import datetime

import pytest

from src.models.activity import Activity
from src.models.project import DayMeta, Project
from src.project.project_repo import _compute_stats


def _ride(aid: int, date_str: str, distance_m: float, moving_time_s: int, elevation_m: float) -> Activity:
    dt = datetime.fromisoformat(date_str)
    return Activity(
        id=aid,
        name=f"Ride {aid}",
        type="Ride",
        distance=distance_m,
        moving_time=moving_time_s,
        elapsed_time=moving_time_s,
        total_elevation_gain=elevation_m,
        start_date=dt,
        start_date_local=dt,
        average_speed=distance_m / moving_time_s if moving_time_s else 0.0,
        max_speed=10.0,
        timezone="UTC",
        achievement_count=0,
        kudos_count=0,
        comment_count=0,
        athlete_count=1,
        photo_count=0,
        trainer=False,
        commute=False,
        manual=False,
        private=False,
        flagged=False,
        pr_count=0,
        total_photo_count=0,
        has_kudoed=False,
    )


def _project(*activities: Activity, day_meta: dict | None = None) -> Project:
    p = Project(name="Test")
    p.activities = list(activities)
    p.day_meta = day_meta or {}
    return p


# ── Basic structure ───────────────────────────────────────────────────────────


def test_empty_when_no_activities():
    p = _project()
    assert _compute_stats(p)["ride_time_series"] == []


def test_empty_when_no_ride_activities():
    run = _ride(1, "2024-06-01", 10_000, 3600, 50)
    run.type = "Run"
    p = _project(run)
    assert _compute_stats(p)["ride_time_series"] == []


def test_single_ride_one_entry():
    p = _project(_ride(1, "2024-06-01", 50_000, 7200, 300))
    ts = _compute_stats(p)["ride_time_series"]
    assert len(ts) == 1
    assert ts[0]["date"] == "2024-06-01"


def test_two_different_days_two_entries():
    p = _project(
        _ride(1, "2024-06-01", 50_000, 7200, 300),
        _ride(2, "2024-06-03", 80_000, 10800, 500),
    )
    ts = _compute_stats(p)["ride_time_series"]
    assert len(ts) == 2


def test_entries_sorted_ascending_by_date():
    p = _project(
        _ride(2, "2024-06-10", 80_000, 10800, 500),
        _ride(1, "2024-06-01", 50_000, 7200, 300),
    )
    ts = _compute_stats(p)["ride_time_series"]
    assert ts[0]["date"] == "2024-06-01"
    assert ts[1]["date"] == "2024-06-10"


# ── Per-day aggregation ───────────────────────────────────────────────────────


def test_same_day_distance_summed():
    p = _project(
        _ride(1, "2024-06-01", 30_000, 3600, 100),
        _ride(2, "2024-06-01", 20_000, 1800, 50),
    )
    ts = _compute_stats(p)["ride_time_series"]
    assert len(ts) == 1
    assert ts[0]["distance_m"] == pytest.approx(50_000)


def test_same_day_time_summed():
    p = _project(
        _ride(1, "2024-06-01", 30_000, 3600, 100),
        _ride(2, "2024-06-01", 20_000, 1800, 50),
    )
    ts = _compute_stats(p)["ride_time_series"]
    assert ts[0]["moving_time_s"] == 5400


def test_same_day_elevation_summed():
    p = _project(
        _ride(1, "2024-06-01", 30_000, 3600, 100),
        _ride(2, "2024-06-01", 20_000, 1800, 50),
    )
    ts = _compute_stats(p)["ride_time_series"]
    assert ts[0]["elevation_m"] == pytest.approx(150)


def test_same_day_avg_speed_weighted():
    # 30 km in 3600 s + 20 km in 1800 s = 50 km in 5400 s → 50000/5400 m/s
    p = _project(
        _ride(1, "2024-06-01", 30_000, 3600, 0),
        _ride(2, "2024-06-01", 20_000, 1800, 0),
    )
    ts = _compute_stats(p)["ride_time_series"]
    assert ts[0]["avg_speed_ms"] == pytest.approx(50_000 / 5400)


def test_avg_speed_zero_when_no_time():
    """Guard: if moving_time_s is 0 for a day, avg_speed_ms is 0 not a ZeroDivisionError."""
    a = _ride(1, "2024-06-01", 10_000, 0, 0)
    p = _project(a)
    ts = _compute_stats(p)["ride_time_series"]
    assert ts[0]["avg_speed_ms"] == 0.0


# ── Non-ride activities excluded ──────────────────────────────────────────────


def test_non_ride_not_included():
    ride = _ride(1, "2024-06-01", 50_000, 7200, 300)
    run = _ride(2, "2024-06-01", 10_000, 3600, 50)
    run.type = "Run"
    p = _project(ride, run)
    ts = _compute_stats(p)["ride_time_series"]
    assert len(ts) == 1
    assert ts[0]["distance_m"] == pytest.approx(50_000)


# ── Tag filter ────────────────────────────────────────────────────────────────


def test_tag_filter_excludes_untagged_days():
    p = _project(
        _ride(1, "2024-06-01", 50_000, 7200, 300),
        _ride(2, "2024-06-02", 80_000, 10800, 500),
        day_meta={
            "2024-06-01": DayMeta(tags=["Alps"]),
            "2024-06-02": DayMeta(tags=["Pyrenees"]),
        },
    )
    ts = _compute_stats(p, tag_filter=["Alps"])["ride_time_series"]
    assert len(ts) == 1
    assert ts[0]["date"] == "2024-06-01"


def test_no_tag_filter_includes_all():
    p = _project(
        _ride(1, "2024-06-01", 50_000, 7200, 300),
        _ride(2, "2024-06-02", 80_000, 10800, 500),
    )
    ts = _compute_stats(p)["ride_time_series"]
    assert len(ts) == 2

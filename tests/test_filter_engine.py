"""Unit tests for FilterEngine and FilterCriteria."""

from datetime import date, datetime
from typing import List

import pytest

from src.filters.filter_engine import FilterCriteria, FilterEngine
from src.models.activity import Activity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_activity(
    id: int = 1,
    name: str = "Test",
    type: str = "Run",
    start_date_local: datetime = datetime(2024, 6, 15, 9, 0),
    distance: float = 10000.0,
) -> Activity:
    return Activity(
        id=id,
        name=name,
        type=type,
        distance=distance,
        moving_time=3600,
        elapsed_time=3700,
        total_elevation_gain=100.0,
        start_date=start_date_local,
        start_date_local=start_date_local,
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
        average_speed=2.77,
        max_speed=5.0,
        pr_count=0,
        total_photo_count=0,
        has_kudoed=False,
    )


ACTIVITIES = [
    _make_activity(id=1, type="Run",  start_date_local=datetime(2024, 1, 10)),
    _make_activity(id=2, type="Ride", start_date_local=datetime(2024, 3, 20)),
    _make_activity(id=3, type="Hike", start_date_local=datetime(2024, 6, 1)),
    _make_activity(id=4, type="Run",  start_date_local=datetime(2024, 9, 5)),
    _make_activity(id=5, type="Walk", start_date_local=datetime(2024, 11, 30)),
]


# ---------------------------------------------------------------------------
# FilterCriteria
# ---------------------------------------------------------------------------

def test_criteria_default_is_empty():
    assert FilterCriteria().is_empty()


def test_criteria_with_start_date_not_empty():
    assert not FilterCriteria(start_date=date(2024, 1, 1)).is_empty()


def test_criteria_with_end_date_not_empty():
    assert not FilterCriteria(end_date=date(2024, 12, 31)).is_empty()


def test_criteria_with_types_not_empty():
    assert not FilterCriteria(activity_types={"Run"}).is_empty()


# ---------------------------------------------------------------------------
# FilterEngine.apply — no filter
# ---------------------------------------------------------------------------

def test_apply_empty_criteria_returns_all():
    result = FilterEngine.apply(ACTIVITIES, FilterCriteria())
    assert len(result) == len(ACTIVITIES)


def test_apply_none_criteria_returns_all():
    result = FilterEngine.apply(ACTIVITIES, None)
    assert len(result) == len(ACTIVITIES)


def test_apply_returns_new_list_not_same_object():
    result = FilterEngine.apply(ACTIVITIES, FilterCriteria())
    assert result is not ACTIVITIES


# ---------------------------------------------------------------------------
# FilterEngine.apply — date filtering
# ---------------------------------------------------------------------------

def test_apply_start_date_filters_older():
    criteria = FilterCriteria(start_date=date(2024, 6, 1))
    result = FilterEngine.apply(ACTIVITIES, criteria)
    ids = {a.id for a in result}
    assert ids == {3, 4, 5}


def test_apply_end_date_filters_newer():
    criteria = FilterCriteria(end_date=date(2024, 3, 20))
    result = FilterEngine.apply(ACTIVITIES, criteria)
    ids = {a.id for a in result}
    assert ids == {1, 2}


def test_apply_date_range_both_bounds():
    criteria = FilterCriteria(
        start_date=date(2024, 3, 1),
        end_date=date(2024, 6, 30),
    )
    result = FilterEngine.apply(ACTIVITIES, criteria)
    ids = {a.id for a in result}
    assert ids == {2, 3}


def test_apply_date_range_exclusive_of_outside():
    criteria = FilterCriteria(
        start_date=date(2024, 4, 1),
        end_date=date(2024, 5, 31),
    )
    result = FilterEngine.apply(ACTIVITIES, criteria)
    assert result == []


def test_apply_start_date_boundary_inclusive():
    criteria = FilterCriteria(start_date=date(2024, 3, 20))
    result = FilterEngine.apply(ACTIVITIES, criteria)
    ids = {a.id for a in result}
    assert 2 in ids  # exactly on the boundary


def test_apply_end_date_boundary_inclusive():
    criteria = FilterCriteria(end_date=date(2024, 3, 20))
    result = FilterEngine.apply(ACTIVITIES, criteria)
    ids = {a.id for a in result}
    assert 2 in ids


def test_apply_tz_aware_datetime_handled():
    """Activities with tz-aware start_date_local should filter correctly via .date()."""
    from datetime import timezone
    aware_activity = _make_activity(
        id=99,
        start_date_local=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    criteria = FilterCriteria(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )
    result = FilterEngine.apply([aware_activity], criteria)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# FilterEngine.apply — type filtering
# ---------------------------------------------------------------------------

def test_apply_single_type_filter():
    criteria = FilterCriteria(activity_types={"Run"})
    result = FilterEngine.apply(ACTIVITIES, criteria)
    assert all(a.type == "Run" for a in result)
    assert len(result) == 2


def test_apply_multiple_type_filter():
    criteria = FilterCriteria(activity_types={"Run", "Hike"})
    result = FilterEngine.apply(ACTIVITIES, criteria)
    types = {a.type for a in result}
    assert types == {"Run", "Hike"}


def test_apply_type_filter_case_insensitive():
    criteria = FilterCriteria(activity_types={"run"})
    result = FilterEngine.apply(ACTIVITIES, criteria)
    assert len(result) == 2


def test_apply_empty_type_set_returns_nothing():
    criteria = FilterCriteria(activity_types=set())
    result = FilterEngine.apply(ACTIVITIES, criteria)
    assert result == []


def test_apply_none_types_means_all_pass():
    criteria = FilterCriteria(activity_types=None)
    result = FilterEngine.apply(ACTIVITIES, criteria)
    assert len(result) == len(ACTIVITIES)


# ---------------------------------------------------------------------------
# FilterEngine.apply — combined filters
# ---------------------------------------------------------------------------

def test_apply_date_and_type_combined():
    criteria = FilterCriteria(
        start_date=date(2024, 6, 1),
        activity_types={"Run"},
    )
    result = FilterEngine.apply(ACTIVITIES, criteria)
    ids = {a.id for a in result}
    assert ids == {4}  # only the Sept Run


def test_apply_on_empty_list_returns_empty():
    result = FilterEngine.apply([], FilterCriteria(start_date=date(2024, 1, 1)))
    assert result == []


# ---------------------------------------------------------------------------
# FilterEngine.extract_activity_types
# ---------------------------------------------------------------------------

def test_extract_types_returns_sorted_unique():
    types = FilterEngine.extract_activity_types(ACTIVITIES)
    assert types == ["Hike", "Ride", "Run", "Walk"]


def test_extract_types_empty_list():
    assert FilterEngine.extract_activity_types([]) == []


def test_extract_types_single_type():
    activities = [_make_activity(type="Swim"), _make_activity(type="Swim")]
    assert FilterEngine.extract_activity_types(activities) == ["Swim"]

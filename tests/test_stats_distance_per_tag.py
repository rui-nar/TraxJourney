"""Tests for _compute_stats distance_per_tag field."""

from datetime import datetime

import pytest

from src.models.activity import Activity
from src.models.project import DayMeta, Project
from src.project.project_repo import _compute_stats


def _activity(aid: int, date_str: str, distance_m: float, atype: str = "ride") -> Activity:
    dt = datetime.fromisoformat(date_str)
    return Activity(
        id=aid,
        name=f"Activity {aid}",
        type=atype,
        distance=distance_m,
        moving_time=3600,
        elapsed_time=3600,
        total_elevation_gain=100.0,
        start_date=dt,
        start_date_local=dt,
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
        average_speed=5.0,
        max_speed=10.0,
        pr_count=0,
        total_photo_count=0,
        has_kudoed=False,
    )


@pytest.fixture
def project_with_tags():
    p = Project(name="Tag Test")
    p.day_meta = {
        "2024-06-01": DayMeta(tags=["Alps"]),
        "2024-06-02": DayMeta(tags=["Alps"]),
        "2024-06-03": DayMeta(tags=["Pyrenees"]),
        "2024-06-04": DayMeta(tags=["Alps", "Pyrenees"]),  # multi-tag day
    }
    p.activities = [
        _activity(1, "2024-06-01", 50_000),
        _activity(2, "2024-06-02", 80_000),
        _activity(3, "2024-06-03", 60_000),
        _activity(4, "2024-06-04", 40_000),
    ]
    return p


class TestDistancePerTag:
    def test_keys_match_tags(self, project_with_tags):
        result = _compute_stats(project_with_tags)
        assert set(result["distance_per_tag"].keys()) == {"Alps", "Pyrenees"}

    def test_single_tag_day_counts_once(self, project_with_tags):
        result = _compute_stats(project_with_tags)
        dpt = result["distance_per_tag"]
        # Alps days: Jun-01 (50k) + Jun-02 (80k) + Jun-04 (40k) = 170k
        assert dpt["Alps"] == pytest.approx(170_000)

    def test_multi_tag_day_counts_for_each_tag(self, project_with_tags):
        result = _compute_stats(project_with_tags)
        dpt = result["distance_per_tag"]
        # Pyrenees days: Jun-03 (60k) + Jun-04 (40k) = 100k
        assert dpt["Pyrenees"] == pytest.approx(100_000)

    def test_non_ride_activities_excluded(self):
        p = Project(name="Mixed")
        p.day_meta = {"2024-06-01": DayMeta(tags=["Stage"])}
        p.activities = [
            _activity(1, "2024-06-01", 30_000, atype="ride"),
            _activity(2, "2024-06-01", 20_000, atype="run"),
        ]
        result = _compute_stats(p)
        assert result["distance_per_tag"]["Stage"] == pytest.approx(30_000)

    def test_empty_when_no_tags(self):
        p = Project(name="No Tags")
        p.day_meta = {"2024-06-01": DayMeta(tags=[])}
        p.activities = [_activity(1, "2024-06-01", 50_000)]
        result = _compute_stats(p)
        assert result["distance_per_tag"] == {}

    def test_ignores_tag_filter_arg(self, project_with_tags):
        """distance_per_tag is always computed over all activities."""
        filtered = _compute_stats(project_with_tags, tag_filter=["Alps"])
        unfiltered = _compute_stats(project_with_tags)
        assert filtered["distance_per_tag"] == unfiltered["distance_per_tag"]


class TestDistancePerTagUntagged:
    """A ride day with no tags of its own falls into "Untagged" rather than
    being dropped from the breakdown entirely (issue #203 follow-up)."""

    @pytest.fixture
    def project_with_untagged_day(self):
        p = Project(name="Untagged Test")
        p.day_meta = {
            "2024-06-01": DayMeta(tags=["Alps"]),
            "2024-06-02": DayMeta(tags=[]),  # explicitly no tags
            "2024-06-03": DayMeta(),  # no tags data at all
        }
        p.activities = [
            _activity(1, "2024-06-01", 50_000),
            _activity(2, "2024-06-02", 30_000),
            _activity(3, "2024-06-03", 20_000),
        ]
        return p

    def test_untagged_ride_days_are_bucketed(self, project_with_untagged_day):
        result = _compute_stats(project_with_untagged_day)
        dpt = result["distance_per_tag"]
        assert dpt["Alps"] == pytest.approx(50_000)
        # Jun-02 (30k) + Jun-03 (20k) = 50k, whether explicitly [] or unset.
        assert dpt["Untagged"] == pytest.approx(50_000)

    def test_no_untagged_bucket_when_project_never_uses_tags(self):
        """No tag has ever been defined on this project — the whole
        breakdown stays absent rather than showing a lone 100% "Untagged"
        slice for a feature nobody has touched."""
        p = Project(name="No Tags At All")
        p.day_meta = {"2024-06-01": DayMeta(tags=[])}
        p.activities = [_activity(1, "2024-06-01", 50_000)]
        result = _compute_stats(p)
        assert result["distance_per_tag"] == {}


class TestTagOptionsUntagged:
    """"Untagged" also shows up as a selectable option in tag_options — the
    field that drives the stats page's tag-filter chip row — so the filter
    isn't limited to real tags (issue #203 follow-up)."""

    def test_untagged_appended_when_a_day_has_no_tags(self, project_with_untagged_day):
        result = _compute_stats(project_with_untagged_day)
        assert result["tag_options"] == ["Alps", "Untagged"]

    def test_untagged_absent_when_every_day_is_tagged(self, project_with_tags):
        result = _compute_stats(project_with_tags)
        assert "Untagged" not in result["tag_options"]

    def test_untagged_absent_when_project_never_uses_tags(self):
        p = Project(name="No Tags At All")
        p.day_meta = {"2024-06-01": DayMeta(tags=[])}
        result = _compute_stats(p)
        assert result["tag_options"] == []

    @pytest.fixture
    def project_with_untagged_day(self):
        p = Project(name="Untagged Test")
        p.day_meta = {
            "2024-06-01": DayMeta(tags=["Alps"]),
            "2024-06-02": DayMeta(tags=[]),
        }
        p.activities = [_activity(1, "2024-06-01", 50_000)]
        return p


class TestTagFilterUntagged:
    """Selecting "Untagged" in the stats page's tag filter restricts the
    overall stats to days with no tags of their own (issue #203 follow-up).
    Mirrors test_stats_timeseries.py's tag-filter coverage, one level up at
    the ride_time_series/total-distance layer that tag_filter actually gates."""

    @pytest.fixture
    def project_mixed(self):
        p = Project(name="Mixed Tag Filter Test")
        p.day_meta = {
            "2024-06-01": DayMeta(tags=["Alps"]),
            "2024-06-02": DayMeta(tags=[]),
            "2024-06-03": DayMeta(),
        }
        p.activities = [
            _activity(1, "2024-06-01", 50_000),
            _activity(2, "2024-06-02", 30_000),
            _activity(3, "2024-06-03", 20_000),
        ]
        return p

    def test_untagged_filter_keeps_only_untagged_days(self, project_mixed):
        result = _compute_stats(project_mixed, tag_filter=["Untagged"])
        ts = result["ride_time_series"]
        assert {p["date"] for p in ts} == {"2024-06-02", "2024-06-03"}

    def test_untagged_filter_combines_with_a_real_tag(self, project_mixed):
        """Selecting "Alps" + "Untagged" is an OR: every day matches one or
        the other, so nothing is excluded here."""
        result = _compute_stats(project_mixed, tag_filter=["Alps", "Untagged"])
        ts = result["ride_time_series"]
        assert {p["date"] for p in ts} == {"2024-06-01", "2024-06-02", "2024-06-03"}

    def test_real_tag_filter_alone_still_excludes_untagged_days(self, project_mixed):
        result = _compute_stats(project_mixed, tag_filter=["Alps"])
        ts = result["ride_time_series"]
        assert {p["date"] for p in ts} == {"2024-06-01"}

"""Tests for ActivityCache."""

import json
import os
import pytest
from datetime import datetime, timezone, timedelta

from src.cache.activity_cache import ActivityCache
from src.models.activity import Activity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_activity(id: int, offset_days: int = 0) -> Activity:
    base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    start = base + timedelta(days=offset_days)
    return Activity(
        id=id,
        name=f"Activity {id}",
        type="Run",
        distance=10000.0,
        moving_time=3600,
        elapsed_time=3700,
        total_elevation_gain=100.0,
        start_date=start,
        start_date_local=start,
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
        average_speed=2.78,
        max_speed=5.0,
        pr_count=0,
        total_photo_count=0,
        has_kudoed=False,
    )


@pytest.fixture
def cache_dir(tmp_path):
    return str(tmp_path / "cache")


@pytest.fixture
def cache(cache_dir):
    return ActivityCache(cache_dir)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

class TestActivityCacheInit:
    def test_creates_cache_dir(self, tmp_path):
        d = str(tmp_path / "new_cache")
        ActivityCache(d)
        assert os.path.isdir(d)

    def test_load_returns_empty_list_when_no_cache(self, cache):
        assert cache.load() == []

    def test_count_returns_zero_when_no_cache(self, cache):
        assert cache.count() == 0

    def test_last_sync_returns_none_when_no_cache(self, cache):
        assert cache.last_sync() is None

    def test_most_recent_start_returns_none_when_no_cache(self, cache):
        assert cache.most_recent_start() is None


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------

class TestSaveLoad:
    def test_save_and_load_single_activity(self, cache):
        a = _make_activity(1)
        cache.save([a])
        loaded = cache.load()
        assert len(loaded) == 1
        assert loaded[0].id == 1
        assert loaded[0].name == "Activity 1"

    def test_save_and_load_multiple_activities(self, cache):
        acts = [_make_activity(i, offset_days=i) for i in range(5)]
        cache.save(acts)
        loaded = cache.load()
        assert {a.id for a in loaded} == {0, 1, 2, 3, 4}

    def test_save_sorts_newest_first(self, cache):
        acts = [_make_activity(i, offset_days=i) for i in range(3)]
        cache.save(acts)
        loaded = cache.load()
        assert loaded[0].start_date >= loaded[1].start_date >= loaded[2].start_date

    def test_load_preserves_datetime_fields(self, cache):
        a = _make_activity(42, offset_days=10)
        cache.save([a])
        loaded = cache.load()[0]
        assert loaded.start_date == a.start_date

    def test_load_returns_empty_on_corrupt_json(self, cache):
        os.makedirs(cache.cache_dir, exist_ok=True)
        with open(cache._cache_path, "w") as fh:
            fh.write("NOT JSON {{{")
        assert cache.load() == []

    def test_save_updates_count_metadata(self, cache):
        cache.save([_make_activity(1), _make_activity(2)])
        assert cache.count() == 2

    def test_save_updates_last_sync_metadata(self, cache):
        cache.save([_make_activity(1)])
        assert cache.last_sync() is not None

    def test_last_sync_is_recent(self, cache):
        cache.save([_make_activity(1)])
        delta = datetime.now(timezone.utc) - cache.last_sync()
        assert delta.total_seconds() < 5


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

class TestMerge:
    def test_merge_adds_new_activities(self, cache):
        cache.save([_make_activity(1)])
        combined = cache.merge([_make_activity(2, offset_days=1)])
        assert {a.id for a in combined} == {1, 2}

    def test_merge_deduplicates_by_id(self, cache):
        cache.save([_make_activity(1)])
        combined = cache.merge([_make_activity(1)])
        assert len(combined) == 1

    def test_merge_new_version_wins_on_conflict(self, cache):
        old = _make_activity(1)
        cache.save([old])
        updated = _make_activity(1)
        updated.name = "Updated Name"
        cache.merge([updated])
        assert cache.load()[0].name == "Updated Name"

    def test_merge_persists_to_disk(self, cache):
        cache.save([_make_activity(1)])
        cache.merge([_make_activity(2, offset_days=1)])
        # Re-create cache instance to verify persistence
        cache2 = ActivityCache(cache.cache_dir)
        assert len(cache2.load()) == 2

    def test_merge_empty_list_preserves_existing(self, cache):
        cache.save([_make_activity(1)])
        cache.merge([])
        assert len(cache.load()) == 1


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

class TestClear:
    def test_clear_removes_cache_file(self, cache):
        cache.save([_make_activity(1)])
        cache.clear()
        assert not os.path.exists(cache._cache_path)

    def test_clear_removes_meta_file(self, cache):
        cache.save([_make_activity(1)])
        cache.clear()
        assert not os.path.exists(cache._meta_path)

    def test_load_after_clear_returns_empty(self, cache):
        cache.save([_make_activity(1)])
        cache.clear()
        assert cache.load() == []

    def test_count_after_clear_returns_zero(self, cache):
        cache.save([_make_activity(1)])
        cache.clear()
        assert cache.count() == 0

    def test_clear_on_empty_cache_does_not_raise(self, cache):
        cache.clear()  # should not raise


# ---------------------------------------------------------------------------
# most_recent_start
# ---------------------------------------------------------------------------

class TestMostRecentStart:
    def test_returns_newest_start_date(self, cache):
        acts = [_make_activity(i, offset_days=i) for i in range(3)]
        cache.save(acts)
        expected = max(a.start_date for a in acts)
        assert cache.most_recent_start() == expected

    def test_returns_none_after_clear(self, cache):
        cache.save([_make_activity(1)])
        cache.clear()
        assert cache.most_recent_start() is None


# ---------------------------------------------------------------------------
# Activity.to_strava_dict round-trip
# ---------------------------------------------------------------------------

class TestActivityRoundTrip:
    def test_to_strava_dict_round_trips_all_fields(self):
        a = _make_activity(99, offset_days=5)
        d = a.to_strava_dict()
        b = Activity.from_strava_api(d)
        assert b.id == a.id
        assert b.name == a.name
        assert b.distance == a.distance
        assert b.start_date == a.start_date

    def test_to_strava_dict_round_trips_summary_polyline(self):
        a = _make_activity(1)
        a.summary_polyline = "abc123"
        d = a.to_strava_dict()
        b = Activity.from_strava_api(d)
        assert b.summary_polyline == "abc123"

    def test_to_strava_dict_handles_none_polyline(self):
        a = _make_activity(1)
        a.summary_polyline = None
        d = a.to_strava_dict()
        b = Activity.from_strava_api(d)
        assert b.summary_polyline is None

    def test_to_strava_dict_is_json_serialisable(self):
        a = _make_activity(1)
        json.dumps(a.to_strava_dict())  # should not raise

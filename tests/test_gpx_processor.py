"""Unit tests for GPXProcessor (merge, save, validate)."""

import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest
import gpxpy

from src.models.track import Track, TrackPoint
from src.gpx.processor import GPXProcessor, ExportOptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_track(activity_id=1, name="Run", start_hour=8, n_points=3):
    """Build a Track with n_points at 1-second intervals."""
    start = datetime(2025, 6, 1, start_hour, 0, tzinfo=timezone.utc)
    points = [
        TrackPoint(
            lat=48.0 + i * 0.001,
            lon=2.0 + i * 0.001,
            elevation=100.0 + i,
            time=start + timedelta(seconds=i),
        )
        for i in range(n_points)
    ]
    return Track(activity_id=activity_id, activity_name=name,
                 start_time=start, points=points)


# ---------------------------------------------------------------------------
# merge — structure
# ---------------------------------------------------------------------------

class TestGPXProcessorMerge:

    def test_returns_gpx_object(self):
        gpx = GPXProcessor.merge([_make_track()])
        assert isinstance(gpx, gpxpy.gpx.GPX)

    def test_single_track_produces_one_gpx_track(self):
        gpx = GPXProcessor.merge([_make_track()])
        assert len(gpx.tracks) == 1

    def test_multiple_tracks_produce_correct_count(self):
        gpx = GPXProcessor.merge([_make_track(1), _make_track(2), _make_track(3)])
        assert len(gpx.tracks) == 3

    def test_empty_list_produces_no_tracks(self):
        gpx = GPXProcessor.merge([])
        assert gpx.tracks == []

    def test_track_name_preserved(self):
        gpx = GPXProcessor.merge([_make_track(name="Evening Ride")])
        assert gpx.tracks[0].name == "Evening Ride"

    def test_point_count_matches(self):
        gpx = GPXProcessor.merge([_make_track(n_points=5)])
        segment = gpx.tracks[0].segments[0]
        assert len(segment.points) == 5

    def test_lat_lon_preserved(self):
        track = _make_track(n_points=1)
        gpx = GPXProcessor.merge([track])
        pt = gpx.tracks[0].segments[0].points[0]
        assert pt.latitude == pytest.approx(track.points[0].lat)
        assert pt.longitude == pytest.approx(track.points[0].lon)

    def test_elevation_preserved(self):
        track = _make_track(n_points=1)
        gpx = GPXProcessor.merge([track])
        pt = gpx.tracks[0].segments[0].points[0]
        assert pt.elevation == pytest.approx(track.points[0].elevation)

    def test_timestamps_are_timezone_aware(self):
        gpx = GPXProcessor.merge([_make_track(n_points=2)])
        for seg in gpx.tracks[0].segments:
            for pt in seg.points:
                if pt.time is not None:
                    assert pt.time.tzinfo is not None

    def test_creator_set(self):
        gpx = GPXProcessor.merge([_make_track()])
        assert gpx.creator == "TraxJourney"


# ---------------------------------------------------------------------------
# merge — ordering
# ---------------------------------------------------------------------------

class TestGPXProcessorMergeOrdering:

    def test_tracks_sorted_chronologically(self):
        """Activities given in reverse order must appear sorted in GPX."""
        t1 = _make_track(activity_id=1, name="First",  start_hour=6)
        t2 = _make_track(activity_id=2, name="Second", start_hour=8)
        t3 = _make_track(activity_id=3, name="Third",  start_hour=10)
        # Pass in reverse
        gpx = GPXProcessor.merge([t3, t1, t2])
        names = [t.name for t in gpx.tracks]
        assert names == ["First", "Second", "Third"]

    def test_already_sorted_order_preserved(self):
        t1 = _make_track(activity_id=1, name="A", start_hour=7)
        t2 = _make_track(activity_id=2, name="B", start_hour=9)
        gpx = GPXProcessor.merge([t1, t2])
        assert gpx.tracks[0].name == "A"
        assert gpx.tracks[1].name == "B"


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

class TestGPXProcessorValidate:

    def test_valid_gpx_returns_no_warnings(self):
        gpx = GPXProcessor.merge([_make_track(n_points=3)])
        assert GPXProcessor.validate(gpx) == []

    def test_empty_gpx_warns_no_tracks(self):
        gpx = GPXProcessor.merge([])
        warnings = GPXProcessor.validate(gpx)
        assert any("no tracks" in w.lower() for w in warnings)

    def test_single_point_track_warns(self):
        gpx = GPXProcessor.merge([_make_track(n_points=1)])
        warnings = GPXProcessor.validate(gpx)
        assert any("fewer than 2" in w for w in warnings)

    def test_two_points_no_warning(self):
        gpx = GPXProcessor.merge([_make_track(n_points=2)])
        assert GPXProcessor.validate(gpx) == []

    def test_duplicate_timestamps_warn(self):
        start = datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc)
        # Two points with identical timestamps
        pts = [
            TrackPoint(lat=48.0, lon=2.0, time=start),
            TrackPoint(lat=48.001, lon=2.001, time=start),
        ]
        track = Track(activity_id=1, activity_name="Dup",
                      start_time=start, points=pts)
        gpx = GPXProcessor.merge([track])
        warnings = GPXProcessor.validate(gpx)
        assert any("duplicate" in w.lower() for w in warnings)

    def test_no_duplicate_timestamps_no_warning(self):
        gpx = GPXProcessor.merge([_make_track(n_points=4)])
        assert GPXProcessor.validate(gpx) == []

    def test_multiple_tracks_all_checked(self):
        """One valid track + one 1-point track → warning for the short one only."""
        gpx = GPXProcessor.merge([
            _make_track(activity_id=1, n_points=3),
            _make_track(activity_id=2, n_points=1),
        ])
        warnings = GPXProcessor.validate(gpx)
        assert len(warnings) == 1
        assert "fewer than 2" in warnings[0]


# ---------------------------------------------------------------------------
# save / round-trip
# ---------------------------------------------------------------------------

class TestGPXProcessorSave:

    def test_save_creates_file(self, tmp_path):
        path = str(tmp_path / "output.gpx")
        GPXProcessor.save(GPXProcessor.merge([_make_track()]), path)
        assert os.path.exists(path)

    def test_saved_file_is_valid_xml(self, tmp_path):
        path = str(tmp_path / "output.gpx")
        GPXProcessor.save(GPXProcessor.merge([_make_track()]), path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert content.startswith("<?xml") or "<gpx" in content

    def test_round_trip_track_count(self, tmp_path):
        path = str(tmp_path / "output.gpx")
        original = GPXProcessor.merge([_make_track(1), _make_track(2)])
        GPXProcessor.save(original, path)
        with open(path, encoding="utf-8") as f:
            reloaded = gpxpy.parse(f)
        assert len(reloaded.tracks) == 2

    def test_round_trip_point_count(self, tmp_path):
        path = str(tmp_path / "output.gpx")
        GPXProcessor.save(GPXProcessor.merge([_make_track(n_points=5)]), path)
        with open(path, encoding="utf-8") as f:
            reloaded = gpxpy.parse(f)
        total = sum(len(seg.points) for t in reloaded.tracks for seg in t.segments)
        assert total == 5

    def test_round_trip_track_name(self, tmp_path):
        path = str(tmp_path / "output.gpx")
        GPXProcessor.save(GPXProcessor.merge([_make_track(name="My Route")]), path)
        with open(path, encoding="utf-8") as f:
            reloaded = gpxpy.parse(f)
        assert reloaded.tracks[0].name == "My Route"


# ---------------------------------------------------------------------------
# ExportOptions — concatenate
# ---------------------------------------------------------------------------

class TestExportOptionsConcatenate:

    def test_default_is_not_concatenated(self):
        gpx = GPXProcessor.merge([_make_track(1), _make_track(2)])
        assert len(gpx.tracks) == 2

    def test_concatenate_produces_single_track(self):
        opts = ExportOptions(concatenate=True)
        gpx = GPXProcessor.merge([_make_track(1), _make_track(2)], opts)
        assert len(gpx.tracks) == 1

    def test_concatenate_single_track_name(self):
        opts = ExportOptions(concatenate=True)
        gpx = GPXProcessor.merge([_make_track(1), _make_track(2)], opts)
        assert gpx.tracks[0].name == "Merged track"

    def test_concatenate_all_points_present(self):
        opts = ExportOptions(concatenate=True)
        gpx = GPXProcessor.merge([_make_track(n_points=3), _make_track(n_points=4)], opts)
        total = sum(len(seg.points) for seg in gpx.tracks[0].segments)
        assert total == 7

    def test_concatenate_empty_list(self):
        opts = ExportOptions(concatenate=True)
        gpx = GPXProcessor.merge([], opts)
        assert gpx.tracks == []

    def test_concatenate_single_source_track(self):
        opts = ExportOptions(concatenate=True)
        gpx = GPXProcessor.merge([_make_track(n_points=5)], opts)
        total = sum(len(seg.points) for seg in gpx.tracks[0].segments)
        assert total == 5

    def test_concatenate_sorted_chronologically(self):
        """Points from the earlier track must come first in the merged segment."""
        t1 = _make_track(activity_id=1, name="Early", start_hour=6, n_points=2)
        t2 = _make_track(activity_id=2, name="Late",  start_hour=10, n_points=2)
        opts = ExportOptions(concatenate=True)
        # Pass in reverse order
        gpx = GPXProcessor.merge([t2, t1], opts)
        pts = gpx.tracks[0].segments[0].points
        assert pts[0].time < pts[2].time


# ---------------------------------------------------------------------------
# ExportOptions — include_time / include_elevation
# ---------------------------------------------------------------------------

class TestExportOptionsDataContent:

    def test_default_includes_timestamps(self):
        gpx = GPXProcessor.merge([_make_track(n_points=2)])
        pt = gpx.tracks[0].segments[0].points[0]
        assert pt.time is not None

    def test_no_timestamps_when_disabled(self):
        opts = ExportOptions(include_time=False)
        gpx = GPXProcessor.merge([_make_track(n_points=2)], opts)
        for seg in gpx.tracks[0].segments:
            for pt in seg.points:
                assert pt.time is None

    def test_default_includes_elevation(self):
        gpx = GPXProcessor.merge([_make_track(n_points=2)])
        pt = gpx.tracks[0].segments[0].points[0]
        assert pt.elevation is not None

    def test_no_elevation_when_disabled(self):
        opts = ExportOptions(include_elevation=False)
        gpx = GPXProcessor.merge([_make_track(n_points=2)], opts)
        for seg in gpx.tracks[0].segments:
            for pt in seg.points:
                assert pt.elevation is None

    def test_gps_only_mode_no_time_no_elevation(self):
        opts = ExportOptions(include_time=False, include_elevation=False)
        gpx = GPXProcessor.merge([_make_track(n_points=3)], opts)
        for seg in gpx.tracks[0].segments:
            for pt in seg.points:
                assert pt.time is None
                assert pt.elevation is None

    def test_lat_lon_always_present(self):
        opts = ExportOptions(include_time=False, include_elevation=False)
        gpx = GPXProcessor.merge([_make_track(n_points=2)], opts)
        for seg in gpx.tracks[0].segments:
            for pt in seg.points:
                assert pt.latitude is not None
                assert pt.longitude is not None

    def test_options_none_uses_defaults(self):
        """Passing options=None must behave identically to ExportOptions()."""
        gpx_default = GPXProcessor.merge([_make_track(n_points=2)], None)
        gpx_explicit = GPXProcessor.merge([_make_track(n_points=2)], ExportOptions())
        pt_d = gpx_default.tracks[0].segments[0].points[0]
        pt_e = gpx_explicit.tracks[0].segments[0].points[0]
        assert (pt_d.time is None) == (pt_e.time is None)
        assert (pt_d.elevation is None) == (pt_e.elevation is None)

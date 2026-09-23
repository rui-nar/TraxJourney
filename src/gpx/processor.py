"""GPX processing: merge tracks and export to GPX format."""

from dataclasses import dataclass
from datetime import timezone
from typing import Dict, List

import gpxpy
import gpxpy.gpx

from src.brand import APP_NAME
from src.models.track import Track


@dataclass
class ExportOptions:
    """Controls how tracks are merged and what data is written.

    Attributes:
        concatenate: When True, all points are merged into a single GPX track
            (sorted chronologically). When False (default), each source
            activity becomes its own GPX track so names are preserved.
        include_time: Include per-point timestamps in the output.
        include_elevation: Include per-point elevation in the output.
    """

    concatenate: bool = False
    include_time: bool = True
    include_elevation: bool = True


class GPXProcessor:
    """Merges Track objects and exports them as a GPX document."""

    @staticmethod
    def merge(tracks: List[Track], options: ExportOptions | None = None) -> gpxpy.gpx.GPX:
        """Combine tracks into one GPX document.

        Args:
            tracks: Source tracks, typically one per Strava activity.
            options: Export settings; defaults to ExportOptions() if omitted.

        Returns:
            A gpxpy.gpx.GPX instance ready for validation and saving.
        """
        if options is None:
            options = ExportOptions()

        gpx = gpxpy.gpx.GPX()
        gpx.creator = APP_NAME

        sorted_tracks = sorted(tracks, key=lambda t: t.start_time)

        if options.concatenate:
            if sorted_tracks:
                gpx_track = gpxpy.gpx.GPXTrack(name="Merged track")
                segment = gpxpy.gpx.GPXTrackSegment()
                for track in sorted_tracks:
                    for pt in track.points:
                        segment.points.append(
                            GPXProcessor._make_point(pt, options)
                        )
                gpx_track.segments.append(segment)
                gpx.tracks.append(gpx_track)
        else:
            for track in sorted_tracks:
                gpx_track = gpxpy.gpx.GPXTrack(name=track.activity_name)
                segment = gpxpy.gpx.GPXTrackSegment()
                for pt in track.points:
                    segment.points.append(
                        GPXProcessor._make_point(pt, options)
                    )
                gpx_track.segments.append(segment)
                gpx.tracks.append(gpx_track)

        return gpx

    @staticmethod
    def _make_point(pt, options: ExportOptions) -> gpxpy.gpx.GPXTrackPoint:
        """Build a GPXTrackPoint, respecting include_time / include_elevation."""
        time = None
        if options.include_time and pt.time is not None:
            time = pt.time
            if time.tzinfo is None:
                time = time.replace(tzinfo=timezone.utc)

        elevation = pt.elevation if options.include_elevation else None

        return gpxpy.gpx.GPXTrackPoint(
            latitude=pt.lat,
            longitude=pt.lon,
            elevation=elevation,
            time=time,
        )

    @staticmethod
    def merge_with_segments(
        tracks: List[Track],
        project_items,          # List[ProjectItem] — avoid circular import
        options: "ExportOptions | None" = None,
    ) -> gpxpy.gpx.GPX:
        """Build a GPX document respecting project item order (activities + segments).

        Activity items are matched to their corresponding Track by activity_id.
        Segment items produce a great-circle arc track (50 points, no timestamps).

        Args:
            tracks: Full-resolution GPS tracks, keyed by activity_id.
            project_items: Ordered list of ProjectItem from the open project.
            options: Export settings.
        """
        from src.models.great_circle import great_circle_points

        if options is None:
            options = ExportOptions()

        gpx = gpxpy.gpx.GPX()
        gpx.creator = APP_NAME

        track_map: Dict[int, Track] = {t.activity_id: t for t in tracks}

        for item in project_items:
            if item.item_type == "activity":
                t = track_map.get(item.activity_id)
                if t is None:
                    continue
                gpx_track = gpxpy.gpx.GPXTrack(name=t.activity_name)
                seg = gpxpy.gpx.GPXTrackSegment()
                for pt in t.points:
                    seg.points.append(GPXProcessor._make_point(pt, options))
                gpx_track.segments.append(seg)
                gpx.tracks.append(gpx_track)

            elif item.item_type == "segment" and item.segment is not None:
                s = item.segment
                coords = great_circle_points(
                    s.start.lat, s.start.lon, s.end.lat, s.end.lon
                )
                label = s.label or s.segment_type
                gpx_track = gpxpy.gpx.GPXTrack(name=label)
                gpx_track.type = s.segment_type
                seg = gpxpy.gpx.GPXTrackSegment()
                for lat, lon in coords:
                    seg.points.append(gpxpy.gpx.GPXTrackPoint(
                        latitude=lat, longitude=lon
                    ))
                gpx_track.segments.append(seg)
                gpx.tracks.append(gpx_track)

        return gpx

    @staticmethod
    def save(gpx: gpxpy.gpx.GPX, path: str) -> None:
        """Write GPX document to *path* as indented XML."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(gpx.to_xml())

    @staticmethod
    def validate(gpx: gpxpy.gpx.GPX) -> List[str]:
        """Return a list of warning strings; empty list means the file is valid.

        Checks:
        - At least one track
        - Each track has at least 2 points
        - No duplicate timestamps within a track segment
        """
        warnings: List[str] = []

        if not gpx.tracks:
            warnings.append("GPX contains no tracks.")
            return warnings

        for track in gpx.tracks:
            name = track.name or "(unnamed)"
            total_points = sum(len(seg.points) for seg in track.segments)

            if total_points < 2:
                warnings.append(
                    f"Track '{name}' has fewer than 2 points ({total_points})."
                )

            for seg in track.segments:
                times = [p.time for p in seg.points if p.time is not None]
                if len(times) != len(set(times)):
                    warnings.append(
                        f"Track '{name}' contains duplicate timestamps."
                    )
                    break

        return warnings

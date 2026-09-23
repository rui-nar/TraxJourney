"""Activity data model for Strava activities."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from src.utils.logging import get_logger

_log = get_logger(__name__)


@dataclass
class Activity:
    """Represents a Strava activity with metadata."""

    # All required fields (no defaults) must come first
    id: Optional[int]
    name: str
    type: str
    distance: float  # meters
    moving_time: int  # seconds
    elapsed_time: int  # seconds
    total_elevation_gain: float  # meters
    start_date: datetime
    start_date_local: datetime
    timezone: str
    achievement_count: int
    kudos_count: int
    comment_count: int
    athlete_count: int
    photo_count: int
    trainer: bool
    commute: bool
    manual: bool
    private: bool
    flagged: bool
    average_speed: float  # m/s
    max_speed: float  # m/s
    has_heartrate: bool
    pr_count: int
    total_photo_count: int
    has_kudoed: bool
    
    # Optional fields with defaults (must come last)
    gear_id: Optional[str] = None
    average_heartrate: Optional[float] = None
    max_heartrate: Optional[int] = None
    heartrate_opt_out: bool = False
    display_hide_heartrate_option: bool = False
    elev_high: Optional[float] = None
    elev_low: Optional[float] = None
    start_latlng: Optional[List[float]] = None  # [lat, lng]
    end_latlng: Optional[List[float]] = None    # [lat, lng]
    summary_polyline: Optional[str] = None       # Google-encoded polyline
    # Cached elevation profile — populated on first fetch and persisted in project file
    elevation_profile: Optional[Tuple[List[float], List[float]]] = None  # (distances_km, elevations_m)
    # Downsampled profile for the low-res-first chart (DB-derived, not part of the
    # .traxj file format). Same shape as elevation_profile. Served when the full
    # profile is deferred (meta / low-res loads).
    elevation_profile_low_res: Optional[Tuple[List[float], List[float]]] = None
    # Geometry-edit flag (issue #31). True when the track was edited locally, so
    # Strava sync must skip this activity. Round-trips through the DB and the REST
    # API; the pre-edit snapshot itself stays DB-only.
    is_edited: bool = False
    # The piece this activity was cut directly out of (issue #143), None if it
    # never was. Round-trips through the DB and the REST API so the editor can
    # walk it to find the pieces cut out of an activity: resetting destroys those
    # pieces, and the user has to be warned first (#141). The DB's split_root_id
    # is deliberately NOT surfaced — it points every piece at the family's first
    # piece however deep the chain, which cannot answer "what is below this one".
    split_parent_id: Optional[int] = None
    # Async Strava re-fetch state (issue #148). Read-only as far as the client is
    # concerned: set by the re-fetch job's bookkeeping, surfaced here so the
    # client can poll /meta for a verdict instead of holding a request open for
    # the minutes a re-fetch can take. NULL status = never re-fetched.
    refresh_status: Optional[str] = None
    refresh_started_at: Optional[str] = None
    refresh_error: Optional[str] = None
    # Import origin. None = from Strava (today's implicit default), "gpx" = imported
    # from a GPX file.
    source: Optional[str] = None
    source_id: Optional[str] = None

    # Client-side E2EE ciphertext passthrough (issue #29). start_latlng/end_latlng/
    # elevation_profile are parsed structures (list/tuple) — they can't carry a
    # ciphertext envelope string, so when the corresponding DB column
    # (start_latlng_json/end_latlng_json/elevation_profile_json, or its low-res
    # copy as a fallback) holds an encrypted envelope instead of parseable JSON,
    # the parsed field above is left None and the raw envelope is carried here
    # instead. The client decrypts these, JSON-decodes the recovered plaintext,
    # and writes the result back into the plain fields (see
    # flutter_client/lib/src/projects/project_notifier.dart's activity reveal step).
    start_latlng_enc: Optional[str] = None
    end_latlng_enc: Optional[str] = None
    elevation_profile_enc: Optional[str] = None

    def to_strava_dict(self) -> dict:
        """Serialise to a dict that can be round-tripped via from_strava_api()."""
        def _iso(dt: datetime) -> str:
            return dt.isoformat().replace("+00:00", "Z") if dt else ""

        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "distance": self.distance,
            "moving_time": self.moving_time,
            "elapsed_time": self.elapsed_time,
            "total_elevation_gain": self.total_elevation_gain,
            "start_date": _iso(self.start_date),
            "start_date_local": _iso(self.start_date_local),
            "timezone": self.timezone,
            "achievement_count": self.achievement_count,
            "kudos_count": self.kudos_count,
            "comment_count": self.comment_count,
            "athlete_count": self.athlete_count,
            "photo_count": self.photo_count,
            "trainer": self.trainer,
            "commute": self.commute,
            "manual": self.manual,
            "private": self.private,
            "flagged": self.flagged,
            "average_speed": self.average_speed,
            "max_speed": self.max_speed,
            "has_heartrate": self.has_heartrate,
            "pr_count": self.pr_count,
            "total_photo_count": self.total_photo_count,
            "has_kudoed": self.has_kudoed,
            "gear_id": self.gear_id,
            "average_heartrate": self.average_heartrate,
            "max_heartrate": self.max_heartrate,
            "heartrate_opt_out": self.heartrate_opt_out,
            "display_hide_heartrate_option": self.display_hide_heartrate_option,
            "elev_high": self.elev_high,
            "elev_low": self.elev_low,
            "start_latlng": self.start_latlng,
            "end_latlng": self.end_latlng,
            "map": {"summary_polyline": self.summary_polyline},
            "elevation_profile": {
                "distances_km": self.elevation_profile[0],
                "elevations_m": self.elevation_profile[1],
            } if self.elevation_profile else None,
            "is_edited": self.is_edited,
            "split_parent_id": self.split_parent_id,
            "refresh_status": self.refresh_status,
            "refresh_started_at": self.refresh_started_at,
            "refresh_error": self.refresh_error,
            "source": self.source,
            "source_id": self.source_id,
            "start_latlng_enc": self.start_latlng_enc,
            "end_latlng_enc": self.end_latlng_enc,
            "elevation_profile_enc": self.elevation_profile_enc,
        }

    def __str__(self) -> str:
        """Return string representation of activity."""
        distance_km = self.distance / 1000
        return f"{self.name} ({self.type}) - {distance_km:.1f} km"

    def __repr__(self) -> str:
        """Return detailed string representation."""
        return f"Activity(id={self.id}, name='{self.name}', type='{self.type}')"

    @classmethod
    def from_strava_api(cls, data: dict) -> "Activity":
        """Create an Activity instance from Strava API response data."""
        return cls(
            id=data.get("id"),
            name=data.get("name", ""),
            type=data.get("type", ""),
            distance=data.get("distance", 0.0),
            moving_time=data.get("moving_time", 0),
            elapsed_time=data.get("elapsed_time", 0),
            total_elevation_gain=data.get("total_elevation_gain", 0.0),
            start_date=datetime.fromisoformat(data.get("start_date", "").replace("Z", "+00:00")) if data.get("start_date") else datetime.now(),
            start_date_local=datetime.fromisoformat(data.get("start_date_local", "").replace("Z", "+00:00")) if data.get("start_date_local") else datetime.now(),
            timezone=data.get("timezone", "UTC"),
            achievement_count=data.get("achievement_count", 0),
            kudos_count=data.get("kudos_count", 0),
            comment_count=data.get("comment_count", 0),
            athlete_count=data.get("athlete_count", 0),
            photo_count=data.get("photo_count", 0),
            trainer=data.get("trainer", False),
            commute=data.get("commute", False),
            manual=data.get("manual", False),
            private=data.get("private", False),
            flagged=data.get("flagged", False),
            average_speed=data.get("average_speed", 0.0),
            max_speed=data.get("max_speed", 0.0),
            has_heartrate=data.get("has_heartrate", False),
            pr_count=data.get("pr_count", 0),
            total_photo_count=data.get("total_photo_count", 0),
            has_kudoed=data.get("has_kudoed", False),
            gear_id=data.get("gear_id"),
            average_heartrate=data.get("average_heartrate"),
            max_heartrate=data.get("max_heartrate"),
            heartrate_opt_out=data.get("heartrate_opt_out", False),
            display_hide_heartrate_option=data.get("display_hide_heartrate_option", False),
            elev_high=data.get("elev_high"),
            elev_low=data.get("elev_low"),
            start_latlng=data.get("start_latlng"),
            end_latlng=data.get("end_latlng"),
            summary_polyline=data.get("map", {}).get("summary_polyline") or None,
            elevation_profile=(
                (ep["distances_km"], ep["elevations_m"])
                if (ep := data.get("elevation_profile"))
                else None
            ),
            is_edited=data.get("is_edited", False),
            split_parent_id=data.get("split_parent_id"),
            # Absent from a real Strava payload — present only when round-tripping
            # our own to_strava_dict() output (e.g. a .traxj file).
            refresh_status=data.get("refresh_status"),
            refresh_started_at=data.get("refresh_started_at"),
            refresh_error=data.get("refresh_error"),
            source=data.get("source"),
            source_id=data.get("source_id"),
            start_latlng_enc=data.get("start_latlng_enc"),
            end_latlng_enc=data.get("end_latlng_enc"),
            elevation_profile_enc=data.get("elevation_profile_enc"),
        )


def parse_activities_or_log(raw_list: List[Dict[str, Any]], source: str) -> List["Activity"]:
    """Parse raw Strava-shaped activity dicts, skipping malformed ones.

    Every call site that used to wrap ``Activity.from_strava_api(raw)`` in a
    silent ``except Exception: pass`` shared the same bug: a malformed
    activity vanished with zero trace. This centralises that parsing so the
    fix (and its test coverage) lives in one place. If any entries are
    dropped, logs a single ``WARNING`` naming ``source`` and the count
    dropped vs. total — one line per call, not one per bad activity, so a
    large import failing on many activities doesn't spam the log.
    """
    parsed: List[Activity] = []
    dropped = 0
    for raw in raw_list:
        try:
            parsed.append(Activity.from_strava_api(raw))
        except Exception:
            dropped += 1
    if dropped:
        _log.warning(
            "%s: dropped %d/%d malformed activities", source, dropped, len(raw_list)
        )
    return parsed

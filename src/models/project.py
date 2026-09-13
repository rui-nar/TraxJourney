"""Project data model — a named, ordered journey of activities and connecting segments."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Literal, Optional

from src.models.activity import Activity
from src.models.encounter import Encounter
from src.models.journal import JournalEntry
from src.models.memory import Memory
from src.models.person import Person
from src.models.person_group import PersonGroup

SegmentType = Literal["train", "flight", "boat", "bus"]

DEFAULT_SLEEPING_OPTIONS = [
    "Camping", "Bivouac", "Shelter",
    "Pension/Guesthouse", "Hotel", "Apartment",
]

DEFAULT_SLEEPING_GROUPS: Dict[str, str] = {
    'Camping': 'Outdoors', 'Bivouac': 'Outdoors', 'Shelter': 'Outdoors',
    'Hotel': 'Indoors', 'Pension/Guesthouse': 'Indoors',
    'Apartment': 'Indoors', 'Warmshower': 'Indoors',
    'Friend': 'Other', 'Transportation': 'Other',
}

DifficultyLevel = Literal["easy", "normal", "hard", "super_hard"]
WeatherCondition = Literal["hot", "clear", "cloudy", "some_rain", "heavy_rain"]


@dataclass
class Counter:
    name: str
    start: float = 0.0


@dataclass
class CounterEntry:
    """A single per-day logged occurrence of a project counter.

    The same counter may be logged several times in one day, so a day holds a
    *list* of these entries rather than a name→value map.
    """
    name: str
    value: float = 0.0


def day_counters_to_json(entries: List["CounterEntry"]) -> List[Dict[str, Any]]:
    """Serialise per-day counter entries to the stored list-of-dicts form."""
    return [{"name": e.name, "value": e.value} for e in entries]


def day_counters_from_json(raw: Any) -> List["CounterEntry"]:
    """Parse per-day counters from stored JSON.

    Accepts both the current list form ``[{"name", "value"}, …]`` and the
    legacy ``{name: value}`` map written before counters could repeat in a day.
    """
    if isinstance(raw, dict):  # legacy map form
        return [CounterEntry(name=k, value=float(v)) for k, v in raw.items()]
    if isinstance(raw, list):
        out: List[CounterEntry] = []
        for e in raw:
            if isinstance(e, dict) and "name" in e:
                out.append(CounterEntry(name=e["name"], value=float(e.get("value", 0))))
        return out
    return []


@dataclass
class DayMeta:
    """User-authored metadata for a single trip day."""
    difficulty: Optional[DifficultyLevel] = None
    sleeping: Optional[str] = None
    weather: Optional[WeatherCondition] = None
    journal: Optional[str] = None
    # None = no own tags (inherit from an earlier day, issue #18); [] = this day
    # explicitly has no tags and must not inherit (issue #203).
    tags: Optional[List[str]] = None
    # Per-day counter occurrences; the same counter name may appear more than once.
    counters: List[CounterEntry] = field(default_factory=list)


# Pseudo-tag standing in for "this day has no tags of its own" wherever tags
# are offered as a filterable option (issue #203 follow-up) — never stored in
# a DayMeta.tags list itself.
UNTAGGED_LABEL = "Untagged"


def tag_options_with_untagged(tags_by_day: Iterable[Optional[List[str]]]) -> List[str]:
    """Distinct tags used across *tags_by_day*, plus :data:`UNTAGGED_LABEL`
    appended when at least one day has no tags of its own.

    Returns ``[]`` when the project has never used tags at all — a lone
    "Untagged" option would be meaningless noise for a feature nobody has
    touched.
    """
    real: set[str] = set()
    has_untagged = False
    for tags in tags_by_day:
        if tags:
            real.update(tags)
        else:
            has_untagged = True
    options = sorted(real)
    if options and has_untagged:
        options.append(UNTAGGED_LABEL)
    return options


@dataclass
class SegmentEndpoint:
    lat: float
    lon: float
    source: Literal["auto", "manual"] = "auto"


@dataclass
class ConnectingSegment:
    """A manually-defined great-circle segment between two project items."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    segment_type: SegmentType = "flight"
    start: SegmentEndpoint = field(default_factory=lambda: SegmentEndpoint(0.0, 0.0))
    end: SegmentEndpoint = field(default_factory=lambda: SegmentEndpoint(0.0, 0.0))
    label: str = ""   # e.g. "Basel → Paris (TGV)"
    date: Optional[str] = None  # ISO date "YYYY-MM-DD"
    # Route-track fields — relevant for train, boat, and bus segments
    route_mode: Literal["great_circle", "rail", "ferry", "bus"] = "great_circle"
    train_number: Optional[str] = None      # e.g. "ICE 596"
    hafas_provider: Optional[str] = None    # e.g. "db"
    route_polyline: Optional[str] = None    # JSON-encoded [[lon,lat],…], stored once
    # Async route-resolution status — see api.projects._resolve_route_job
    route_status: Literal["idle", "pending", "resolved", "failed"] = "idle"
    route_error: Optional[str] = None       # short message shown in the UI on failure
    route_started_at: Optional[str] = None  # ISO timestamp the pending job began (stale-recovery)
    route_degraded: bool = False            # rail only: resolved to a straight endpoint-chord, not
    #   real track (Overpass found no usable geometry). Surfaced to the UI so a straight segment
    #   isn't silently mistaken for a real resolved route.
    route_hafas_failed: bool = False        # train only: the HAFAS lookup for the selected
    #   train_number/date failed and resolution fell through to the generic two-point OSM
    #   strategy instead (route_status is still "resolved" — independent of route_degraded,
    #   which means OSM itself found no usable track). Surfaced to the UI so the user knows
    #   their specific train selection wasn't what actually got resolved.
    route_degrade_retries: int = 0          # scheduled sweep attempts since the segment last
    #   became degraded (issue #207) — reset to 0 by any successful resolve, incremented by
    #   sweep_degraded_segments before each automatic retry, capped at MAX_DEGRADE_RETRIES.
    route_edited: bool = False              # True after a manual track edit (issue #150) — guards
    #   a subsequent auto-resolve from silently discarding the user's hand-drawn geometry.
    # ── Provenance (issue #364) ───────────────────────────────────────────────
    # Every field above records whether a resolve *failed*. Neither of these
    # does: they record how the stored geometry was *produced*, which is what a
    # confidently-wrong result (#359 resolved successfully, degraded=False, and
    # started 13.9 km from the station it claimed to leave) leaves no trace of.
    route_resolver_version: int = 0         # which generation of the rail resolver
    #   produced route_polyline. 0 is the deliberate default for a row written
    #   before the stamp existed — see RESOLVER_VERSION in src/jobs/route_jobs.py
    #   for what a bump means, what it costs, and why 0 means "re-resolve me".
    route_strategy: Optional[str] = None    # how the geometry was obtained:
    #   relation_uic | relation_endpoints | coordinate_dijkstra | straight |
    #   motis_trip | ferry | bus | manual. RailGeometry.strategy was computed
    #   and logged on every resolve and then discarded, so "which trips does
    #   this resolver bug affect" was unanswerable; persisting it costs nothing
    #   and makes it one query.

    def to_dict(self) -> dict:
        """Serialise to a dict that can be round-tripped via from_dict()."""
        return {
            "id": self.id,
            "segment_type": self.segment_type,
            "label": self.label,
            "date": self.date,
            "start": {
                "lat": self.start.lat,
                "lon": self.start.lon,
                "source": self.start.source,
            },
            "end": {
                "lat": self.end.lat,
                "lon": self.end.lon,
                "source": self.end.source,
            },
            "route_mode": self.route_mode,
            "train_number": self.train_number,
            "hafas_provider": self.hafas_provider,
            "route_polyline": self.route_polyline,
            "route_status": self.route_status,
            "route_error": self.route_error,
            "route_started_at": self.route_started_at,
            "route_degraded": self.route_degraded,
            "route_hafas_failed": self.route_hafas_failed,
            "route_degrade_retries": self.route_degrade_retries,
            "route_edited": self.route_edited,
            "route_resolver_version": self.route_resolver_version,
            "route_strategy": self.route_strategy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConnectingSegment":
        """Create a ConnectingSegment instance from a dict produced by to_dict()."""
        return cls(
            id=d.get("id", ""),
            segment_type=d.get("segment_type", "flight"),
            label=d.get("label", ""),
            date=d.get("date"),
            start=SegmentEndpoint(
                lat=d.get("start", {}).get("lat", 0.0),
                lon=d.get("start", {}).get("lon", 0.0),
                source=d.get("start", {}).get("source", "auto"),
            ),
            end=SegmentEndpoint(
                lat=d.get("end", {}).get("lat", 0.0),
                lon=d.get("end", {}).get("lon", 0.0),
                source=d.get("end", {}).get("source", "auto"),
            ),
            route_mode=d.get("route_mode", "great_circle"),
            train_number=d.get("train_number"),
            hafas_provider=d.get("hafas_provider"),
            route_polyline=d.get("route_polyline"),
            route_status=d.get("route_status", "idle"),
            route_error=d.get("route_error"),
            route_started_at=d.get("route_started_at"),
            route_degraded=d.get("route_degraded", False),
            route_hafas_failed=d.get("route_hafas_failed", False),
            route_degrade_retries=d.get("route_degrade_retries", 0),
            route_edited=d.get("route_edited", False),
            # Absent on every row written before #364 — and "absent" has to mean
            # "produced by a resolver older than any we stamped", not "unknown,
            # leave it alone". The routes #359 got wrong are exactly the ones
            # with no stamp, so treating them as unknown would make the sweep
            # reach nothing on the day it ships and only ever help future bugs.
            route_resolver_version=d.get("route_resolver_version", 0),
            route_strategy=d.get("route_strategy"),
        )


@dataclass
class ProjectItem:
    """One entry in the project's ordered item list."""
    item_type: Literal["activity", "segment", "memory", "journal", "encounter"]
    activity_id: Optional[int] = None
    segment: Optional[ConnectingSegment] = None
    memory: Optional[Memory] = None
    journal: Optional[JournalEntry] = None
    encounter: Optional[Encounter] = None
    # Durable row identity carried across a load → mutate → save cycle so the
    # item persistence can diff rather than rebuild (issue #173). Not part of
    # the .traxj file format or the API shape — mirrors Project.lock_version.
    # None on a freshly constructed item; assigned on first persist.
    uid: Optional[str] = None


@dataclass
class ProjectFilterState:
    """Serialised FilterWidget state, stored per project."""
    start_date: Optional[str] = None    # "YYYY-MM-DD"
    end_date: Optional[str] = None      # "YYYY-MM-DD"
    activity_types: Optional[List[str]] = None  # None = all types


@dataclass
class Project:
    """In-memory representation of a .traxj project file."""
    name: str
    version: int = 1
    # Optimistic-lock value captured at load time (DBProject.lock_version); not
    # part of the .traxj file format — used only to detect concurrent writes.
    lock_version: int = 0
    # True when `items` is a filtered view rather than the whole timeline —
    # set by a per-user journal load (issue #106), where other users' journal
    # entries and their timeline items are deliberately absent. Such a project
    # is read-only: save_project rewrites the item list from `items`, so
    # persisting one would delete the filtered-out rows. Not part of the
    # .traxj file format. See ProjectRepo.save_project.
    partial_items: bool = False
    items: List[ProjectItem] = field(default_factory=list)
    filter_state: ProjectFilterState = field(default_factory=ProjectFilterState)
    trip_start: Optional[str] = None  # ISO "YYYY-MM-DD" — overrides inferred day-1 date
    trip_end: Optional[str] = None    # ISO "YYYY-MM-DD" — when set and in the past, auto-fill stops
    # Day metadata keyed by "YYYY-MM-DD"
    day_meta: Dict[str, DayMeta] = field(default_factory=dict)
    # Project-specific list of sleeping options and their group assignments
    sleeping_options: List[str] = field(default_factory=lambda: list(DEFAULT_SLEEPING_OPTIONS))
    sleeping_option_groups: Dict[str, str] = field(default_factory=dict)  # name → "Outdoors"|"Indoors"|"Other"
    # Project-defined counters
    counters: List[Counter] = field(default_factory=list)
    # Full Strava data cached here for offline use
    activities: List[Activity] = field(default_factory=list)
    memories: List[Memory] = field(default_factory=list)
    journal_entries: List[JournalEntry] = field(default_factory=list)
    # People met on the trip (issue #40); referenced by encounter items.
    people: List[Person] = field(default_factory=list)
    # Groups of people (issue #50); members are people whose group_id points here.
    groups: List[PersonGroup] = field(default_factory=list)
    # Track display style — persisted per project
    track_color: str = "#F97316"
    track_secondary_color: Optional[str] = None  # None = auto-derive from primary
    track_width: float = 2.5
    alternating_track_colors: bool = False
    # Elevation chart style
    elevation_chart_color: Optional[str] = None  # None = use black
    elevation_chart_show_line: bool = True
    # Per-activity/segment-type colour+style overrides (issue #95). Off by
    # default — existing projects keep the flat track_color line rendering.
    color_by_type: bool = False
    # Keyed by activity bucket ("ride"/"run"/"hike"/"other") or segment type
    # ("flight"/"train"/"bus"/"boat"); each value e.g. {"color": "#RRGGBB",
    # "style": "solid"|"dashed"|"dotted"}. Missing key/field = built-in default.
    type_styles: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # ISO 639-1 language codes available for memory translation, e.g. ["fr", "de"]
    languages: List[str] = field(default_factory=list)
    # Derived lookup — rebuilt after load, not serialised
    _activity_map: Dict[int, Activity] = field(
        default_factory=dict, repr=False, compare=False
    )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def rebuild_map(self) -> None:
        """Rebuild the id → Activity lookup after loading or merging."""
        self._activity_map = {a.id: a for a in self.activities if a.id is not None}

    def activity_by_id(self, aid: int) -> Optional[Activity]:
        if not self._activity_map:
            self.rebuild_map()
        return self._activity_map.get(aid)

    def ordered_activities(self) -> List[Activity]:
        """Activity objects in project item order (segments excluded)."""
        result = []
        for item in self.items:
            if item.item_type == "activity" and item.activity_id is not None:
                a = self.activity_by_id(item.activity_id)
                if a is not None:
                    result.append(a)
        return result

    def add_activities(self, new_activities: List[Activity]) -> int:
        """Merge new activities into the project pool, inserting each at the
        chronologically correct position rather than always appending to the end.

        Returns the count of activities actually added (not already present).
        """
        self.rebuild_map()
        existing_ids = {a.id for a in self.activities if a.id is not None}
        added = 0
        for act in sorted(new_activities, key=lambda a: a.start_date or ""):
            if act.id not in existing_ids:
                self.activities.append(act)
                self._activity_map[act.id] = act
                existing_ids.add(act.id)
                self.items.insert(
                    self._chronological_insert_pos(act),
                    ProjectItem(item_type="activity", activity_id=act.id),
                )
                added += 1
        if added:
            self._fill_day_gaps()
        return added

    def _chronological_insert_pos(self, act: "Activity") -> int:
        """Return the index at which *act* should be inserted to keep items in
        chronological order.  Scans forward through the existing items and
        returns the position immediately after the last activity whose
        start_date is <= act.start_date.  Non-activity items (segments,
        memories, journals) are skipped so they are not disrupted.
        """
        if not act.start_date:
            return len(self.items)
        insert_pos = 0
        for idx, item in enumerate(self.items):
            if item.item_type == "activity" and item.activity_id is not None:
                existing = self._activity_map.get(item.activity_id)
                if existing and existing.start_date and existing.start_date <= act.start_date:
                    insert_pos = idx + 1
        return insert_pos

    def _fill_day_gaps(self) -> None:
        """Create empty DayMeta entries for any dates missing between the first and last activity."""
        dates = {a.start_date_local.date() for a in self.activities}
        if not dates:
            return
        first, last = min(dates), max(dates)
        cur = first
        while cur <= last:
            key = cur.isoformat()
            if key not in self.day_meta:
                self.day_meta[key] = DayMeta()
            cur += timedelta(days=1)

    def remove_item(self, index: int) -> None:
        """Remove item at *index* from the ordered list (does not remove activity data)."""
        if 0 <= index < len(self.items):
            self.items.pop(index)

    def move_item(self, from_index: int, to_index: int) -> None:
        """Move item from *from_index* to *to_index* (before the element currently there)."""
        n = len(self.items)
        if from_index == to_index or not (0 <= from_index < n):
            return
        to_index = max(0, min(n - 1, to_index))
        item = self.items.pop(from_index)
        self.items.insert(to_index, item)

    def sort_activities_by_date(self) -> None:
        """Sort activity slots by start_date; segment positions are preserved by index."""
        self._sort_activity_slots(key=lambda a: a.start_date)

    def sort_activities_by_name(self) -> None:
        """Sort activity slots by name; segment positions are preserved by index."""
        self._sort_activity_slots(key=lambda a: a.name.lower())

    def _sort_activity_slots(self, key) -> None:
        activity_items = [item for item in self.items if item.item_type == "activity"]
        activity_items.sort(
            key=lambda item: key(self.activity_by_id(item.activity_id))
            if item.activity_id and self.activity_by_id(item.activity_id) else ""
        )
        it = iter(activity_items)
        for i, item in enumerate(self.items):
            if item.item_type == "activity":
                self.items[i] = next(it)

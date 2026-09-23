"""Serialize / deserialize a Project to/from a .traxj JSON file."""

from __future__ import annotations

import json
from typing import Any, Dict

from src.models.activity import Activity, parse_activities_or_log
from src.models.encounter import Encounter
from src.models.journal import JournalEntry
from src.models.memory import Memory
from src.models.person import Person
from src.models.person_group import PersonGroup
from src.models.project import (
    ConnectingSegment,
    DayMeta,
    day_counters_to_json,
    DEFAULT_SLEEPING_OPTIONS,
    Project,
    ProjectFilterState,
    ProjectItem,
)


# Dispatch tables for ProjectItem serialisation, keyed by item_type. "activity"
# is handled as a special case in _serialise_item/_deserialise_item (it's just
# an activity_id, not a nested dict) and doesn't fit this shape. "segment" is
# the implicit default (no explicit item_type match) on the deserialise side,
# matching the original if/elif chain's trailing `else` branch.
_ITEM_TYPE_SERIALIZERS = {
    "memory": lambda item: {"memory": item.memory.to_dict()},
    "journal": lambda item: {"journal": item.journal.to_dict()},
    "encounter": lambda item: {"encounter": item.encounter.to_dict()},
}

_ITEM_TYPE_DESERIALIZERS = {
    "memory": lambda d: ProjectItem(item_type="memory", memory=Memory.from_dict(d.get("memory", {}))),
    "journal": lambda d: ProjectItem(item_type="journal", journal=JournalEntry.from_dict(d.get("journal", {}))),
    "encounter": lambda d: ProjectItem(item_type="encounter", encounter=Encounter.from_dict(d.get("encounter", {}))),
}


def _person_to_dict(p: Person) -> Dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "email": p.email,
        "phone": p.phone,
        "polarsteps": p.polarsteps,
        "notes": p.notes,
        "avatar_photo": p.avatar_photo,
        "socials": p.socials,
        "nationalities": p.nationalities,
        "residence": p.residence,
        "group_id": p.group_id,
    }


def _person_from_dict(d: Dict[str, Any]) -> Person:
    return Person(
        id=d.get("id"),
        name=d.get("name"),
        email=d.get("email"),
        phone=d.get("phone"),
        polarsteps=d.get("polarsteps"),
        notes=d.get("notes"),
        avatar_photo=d.get("avatar_photo"),
        socials=d.get("socials") or [],
        nationalities=d.get("nationalities") or [],
        residence=d.get("residence"),
        group_id=d.get("group_id"),
    )


def _group_to_dict(g: PersonGroup) -> Dict[str, Any]:
    return {
        "id": g.id,
        "name": g.name,
        "nationalities": g.nationalities,
        "socials": g.socials,
    }


def _group_from_dict(d: Dict[str, Any]) -> PersonGroup:
    return PersonGroup(
        id=d.get("id"),
        name=d.get("name"),
        nationalities=d.get("nationalities") or [],
        socials=d.get("socials") or [],
    )


class ProjectIO:
    """Load and save .traxj project files."""

    EXTENSION = ".traxj"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def new(name: str) -> Project:
        """Create a blank in-memory project with the given name."""
        return Project(name=name)

    @staticmethod
    def save(project: Project, path: str) -> None:
        """Serialise *project* to *path* as indented JSON."""
        data: Dict[str, Any] = {
            "version": project.version,
            "name": project.name,
            "trip_start": project.trip_start,
            "trip_end": project.trip_end,
            "filter_state": {
                "start_date": project.filter_state.start_date,
                "end_date": project.filter_state.end_date,
                "activity_types": project.filter_state.activity_types,
            },
            "items": [ProjectIO._serialise_item(i) for i in project.items],
            "activities": [a.to_strava_dict() for a in project.activities],
            "people": [_person_to_dict(p) for p in project.people],
            "groups": [_group_to_dict(g) for g in project.groups],
            "day_meta": {
                dk: {
                    **{k: v for k, v in {
                        "difficulty": dm.difficulty, "sleeping": dm.sleeping,
                        "weather": dm.weather, "journal": dm.journal,
                    }.items() if v is not None},
                    **({"tags": dm.tags} if dm.tags is not None else {}),
                }
                for dk, dm in project.day_meta.items()
            },
            "sleeping_options": project.sleeping_options,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

    @staticmethod
    def to_dict(project: Project) -> Dict[str, Any]:
        """Return project data as a dict suitable for the REST API.

        Differs from :meth:`save` in one way: ``elevation_profile`` is
        converted from the storage format ``{"distances_km": [...], "elevations_m": [...]}``
        to a list of ``[dist_km, elev_m]`` pairs so the Flutter client can
        iterate over them directly.
        """
        def _ep_pairs(a: Activity) -> Any:
            # Prefer the full profile; fall back to the downsampled low-res copy
            # so meta / low-res responses (where the full profile is deferred)
            # still render the chart immediately. The client overwrites it when
            # the full profile loads in the background.
            ep = a.elevation_profile or getattr(a, "elevation_profile_low_res", None)
            if not ep:
                return None
            return [list(pair) for pair in zip(ep[0], ep[1])]

        activities_out = []
        for a in project.activities:
            d = a.to_strava_dict()
            d["elevation_profile"] = _ep_pairs(a)
            activities_out.append(d)

        return {
            "version": project.version,
            # Optimistic-lock counter (DBProject.lock_version), surfaced read-only
            # so clients can tell whether a previously cached geo/elevation
            # payload for this project is still valid without re-downloading it.
            "lock_version": project.lock_version,
            "name": project.name,
            "trip_start": project.trip_start,
            "trip_end": project.trip_end,
            "filter_state": {
                "start_date": project.filter_state.start_date,
                "end_date": project.filter_state.end_date,
                "activity_types": project.filter_state.activity_types,
            },
            "items": [ProjectIO._serialise_item(i) for i in project.items],
            "activities": activities_out,
            "people": [_person_to_dict(p) for p in project.people],
            "groups": [_group_to_dict(g) for g in project.groups],
            "day_meta": {
                dk: {
                    **{k: v for k, v in {
                        "difficulty": dm.difficulty, "sleeping": dm.sleeping,
                        "weather": dm.weather, "journal": dm.journal,
                    }.items() if v is not None},
                    **({"tags": dm.tags} if dm.tags is not None else {}),
                    **({"counters": day_counters_to_json(dm.counters)} if dm.counters else {}),
                }
                for dk, dm in project.day_meta.items()
            },
            "sleeping_options": project.sleeping_options,
            "sleeping_option_groups": project.sleeping_option_groups,
            "counters": [{"name": c.name, "start": c.start} for c in project.counters],
            "track_color": project.track_color,
            "track_secondary_color": project.track_secondary_color,
            "track_width": project.track_width,
            "alternating_track_colors": project.alternating_track_colors,
            "elevation_chart_color": project.elevation_chart_color,
            "elevation_chart_show_line": project.elevation_chart_show_line,
            "color_by_type": project.color_by_type,
            "type_styles": project.type_styles,
            "languages": project.languages,
        }

    @staticmethod
    def load(path: str) -> Project:
        """Deserialise a .traxj file and return a :class:`Project`."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)

        fs_raw = data.get("filter_state", {}) or {}
        filter_state = ProjectFilterState(
            start_date=fs_raw.get("start_date"),
            end_date=fs_raw.get("end_date"),
            activity_types=fs_raw.get("activity_types"),
        )

        activities = parse_activities_or_log(data.get("activities", []), "project_io_load")

        items = [ProjectIO._deserialise_item(i) for i in data.get("items", [])]

        raw_dm = data.get("day_meta") or {}
        day_meta = {
            dk: DayMeta(
                difficulty=v.get("difficulty"),
                sleeping=v.get("sleeping"),
                weather=v.get("weather"),
                journal=v.get("journal"),
                tags=v.get("tags"),
            )
            for dk, v in raw_dm.items()
        }
        raw_opts = data.get("sleeping_options")
        sleeping_options = (
            list(raw_opts) if isinstance(raw_opts, list) and raw_opts
            else list(DEFAULT_SLEEPING_OPTIONS)
        )

        people = [_person_from_dict(p) for p in data.get("people", [])]
        groups = [_group_from_dict(g) for g in data.get("groups", [])]

        project = Project(
            name=data.get("name", "Untitled"),
            version=data.get("version", 1),
            trip_start=data.get("trip_start"),
            items=items,
            filter_state=filter_state,
            activities=activities,
            people=people,
            groups=groups,
            day_meta=day_meta,
            sleeping_options=sleeping_options,
        )
        project.rebuild_map()
        return project

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialise_item(item: ProjectItem) -> Dict[str, Any]:
        d: Dict[str, Any] = {"item_type": item.item_type}
        if item.item_type == "activity":
            d["activity_id"] = item.activity_id
        elif item.item_type in _ITEM_TYPE_SERIALIZERS and getattr(item, item.item_type) is not None:
            d.update(_ITEM_TYPE_SERIALIZERS[item.item_type](item))
        else:
            d["segment"] = item.segment.to_dict()
        return d

    @staticmethod
    def _deserialise_item(d: Dict[str, Any]) -> ProjectItem:
        item_type = d.get("item_type")
        if item_type == "activity":
            return ProjectItem(item_type="activity", activity_id=d.get("activity_id"))
        if item_type in _ITEM_TYPE_DESERIALIZERS:
            return _ITEM_TYPE_DESERIALIZERS[item_type](d)
        # segment — implicit default when item_type is missing/None or "segment"
        seg = ConnectingSegment.from_dict(d.get("segment", {}))
        return ProjectItem(item_type="segment", segment=seg)

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
    Counter,
    DayMeta,
    day_counters_from_json,
    day_counters_to_json,
    DEFAULT_SLEEPING_OPTIONS,
    Project,
    ProjectFilterState,
    ProjectItem,
)
from src.project import traxj_schema


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


#: The trip settings a .traxj file carries at its top level, all read back by
#: the import (issue #465). Everything else the writers emit is content (items,
#: activities, people, groups, day_meta, sleeping_options, filter_state,
#: version) or the server's own and never taken from a file: lock_version (the
#: optimistic-lock counter) and name (a trip is named by the uploaded file).
SETTINGS = (
    "trip_start", "trip_end", "sleeping_option_groups", "counters",
    "track_color", "track_secondary_color", "track_width",
    "alternating_track_colors", "elevation_chart_color",
    "elevation_chart_show_line", "color_by_type", "type_styles", "languages",
)


def _flag(data: Dict[str, Any], key: str, defaults: Project) -> bool:
    value = data.get(key)
    return getattr(defaults, key) if value is None else value


class InvalidProjectFile(ValueError):
    """The document is not a readable .traxj trip (issue #451).

    Raised only for a fault in the document itself (its encoding, its JSON, or
    a value the format does not allow: see :mod:`src.project.traxj_schema`,
    issue #462), never for a bug in the
    code reading it, so a caller can put it to whoever supplied the file. The
    message names what is wrong without echoing the file's content.
    """


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
        with open(path, "wb") as fh:
            fh.write(ProjectIO.dumps(data))

    @staticmethod
    def dumps(data: Dict[str, Any]) -> bytes:
        """A .traxj document as the bytes of its file: compact UTF-8 JSON.

        Compact rather than indented (issue #454): indentation more than
        doubled the size of an export, while the import takes any valid JSON
        and is capped (MAX_IMPORT_BYTES), so an indented export of a long trip
        could be refused by the very server that wrote it. Non-ASCII text is
        written as UTF-8, not escaped, as it always was.
        """
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @staticmethod
    def to_dict(project: Project) -> Dict[str, Any]:
        """Return project data as a dict suitable for the REST API.

        Differs from :meth:`save` in two ways: ``elevation_profile`` is
        converted from the storage format ``{"distances_km": [...], "elevations_m": [...]}``
        to a list of ``[dist_km, elev_m]`` pairs so the Flutter client can
        iterate over them directly, and an activity loaded from the database
        carries ``plain_fields``, its E2EE columns still in plaintext, and
        ``shared_with_others``, whether another user's trip holds it; every
        activity carries ``has_gain_snapshot``.
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
            if a.plain_fields is not None:
                d["plain_fields"] = a.plain_fields
            d["has_gain_snapshot"] = a.has_gain_snapshot
            if a.shared_with_others is not None:
                d["shared_with_others"] = a.shared_with_others
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
            return ProjectIO.from_dict(json.load(fh))

    @staticmethod
    def from_bytes(raw: bytes) -> Project:
        """Build a :class:`Project` from the bytes of an uploaded .traxj file.

        Raises :class:`InvalidProjectFile` when the bytes are not a trip.

        A .traxj file is UTF-8 JSON. A leading UTF-8 byte order mark is
        accepted: Windows tools add one (Windows PowerShell's ``-Encoding
        UTF8``, Notepad before 2019) to text that is otherwise UTF-8 byte for
        byte, and RFC 8259 lets a parser ignore it. Any other encoding, UTF-16
        included, is refused rather than guessed at: the exporter writes UTF-8
        only, and widening the format is a decision, not a parsing detail.
        """
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise InvalidProjectFile("it is not UTF-8 text") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidProjectFile(
                f"it is not valid JSON (line {exc.lineno}, column {exc.colno})"
            ) from None
        # json.loads reads nothing but the text it is given, so whatever else
        # it raises is the document's fault, never a bug of ours: a plain
        # ValueError for an integer past Python's digit limit, a RecursionError
        # for nesting past the parser's depth.
        except ValueError:
            raise InvalidProjectFile("it holds a number too long to read") from None
        except RecursionError:
            raise InvalidProjectFile("it is nested too deeply") from None
        return ProjectIO.from_dict(data)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> Project:
        """Build a :class:`Project` from a parsed .traxj document.

        Refuses, with :class:`InvalidProjectFile`, a document the app could
        not have written: its structure, and every value it holds, are checked
        against the format (:mod:`src.project.traxj_schema`) before anything
        is read from it.
        """
        # Every exporter writes "items"; an object without it (a GeoJSON file,
        # a settings file) would otherwise import as an empty trip.
        if not isinstance(data, dict) or "items" not in data:
            raise InvalidProjectFile("it does not contain a trip")
        fault = traxj_schema.check(data)
        if fault is not None:
            raise InvalidProjectFile(fault)
        fs_raw = data.get("filter_state", {}) or {}
        filter_state = ProjectFilterState(
            start_date=fs_raw.get("start_date"),
            end_date=fs_raw.get("end_date"),
            activity_types=fs_raw.get("activity_types"),
        )

        activities = parse_activities_or_log(data.get("activities", []), "project_io_load")

        items = [ProjectIO._deserialise_item(i) for i in data["items"]]

        day_meta = {}
        for dk, v in (data.get("day_meta") or {}).items():
            day_meta[dk] = DayMeta(
                difficulty=v.get("difficulty"),
                sleeping=v.get("sleeping"),
                weather=v.get("weather"),
                journal=v.get("journal"),
                tags=v.get("tags"),
                counters=day_counters_from_json(v.get("counters")),
            )
        raw_opts = data.get("sleeping_options")
        sleeping_options = (
            list(raw_opts) if isinstance(raw_opts, list) and raw_opts
            else list(DEFAULT_SLEEPING_OPTIONS)
        )

        people = [_person_from_dict(p) for p in data.get("people", [])]
        groups = [_group_from_dict(g) for g in data.get("groups", [])]

        # The trip's settings (issue #465); a missing or null one is its
        # default, and only those the file carries are "carried".
        defaults = Project(name="")
        project = Project(
            name=data.get("name", "Untitled"),
            version=data.get("version", 1),
            trip_start=data.get("trip_start") or None,
            trip_end=data.get("trip_end") or None,
            items=items,
            filter_state=filter_state,
            activities=activities,
            people=people,
            groups=groups,
            day_meta=day_meta,
            sleeping_options=sleeping_options,
            sleeping_option_groups=dict(data.get("sleeping_option_groups") or {}),
            counters=[Counter(name=c["name"], start=float(c.get("start", 0)))
                      for c in data.get("counters") or []],
            track_color=data.get("track_color") or defaults.track_color,
            track_secondary_color=data.get("track_secondary_color"),
            track_width=float(data.get("track_width") or defaults.track_width),
            alternating_track_colors=_flag(data, "alternating_track_colors", defaults),
            elevation_chart_color=data.get("elevation_chart_color"),
            elevation_chart_show_line=_flag(data, "elevation_chart_show_line", defaults),
            color_by_type=_flag(data, "color_by_type", defaults),
            type_styles=dict(data.get("type_styles") or {}),
            languages=list(data.get("languages") or []),
            settings_carried=frozenset(k for k in SETTINGS if k in data),
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

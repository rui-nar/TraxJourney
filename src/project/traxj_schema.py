"""The values a .traxj trip file may hold (issue #462).

:meth:`ProjectIO.from_dict` reads a parsed trip document into a Project. This
module says which documents it may read: :func:`fault` names the first value a
file written by the app could not hold, or answers None.

Two passes:

* one over the whole document, whatever the field: no number that is not
  finite (``NaN``, ``Infinity``, or a literal too large for a float), no
  integer past 64 bits, no text holding a lone UTF-16 surrogate. The app's
  database, its JSON responses and the client's parser refuse each of these;
* the models below, one per object of the format, which type every field the
  import reads, as the writers emit it (``ProjectIO.to_dict``, the export
  endpoints, each model's ``to_dict``).

The models mirror the writers, and every version of them: a field any past
version left out may be missing, and one it wrote as null may be null. A field
the writers emit but the import does not read is listed as ``Any`` and never
refused. A field no writer emits is ignored, whatever it holds.

A fault is described by where it is (``items[3].memory.lat``), never by what
it holds: the message goes back to whoever uploaded the file, and must not
echo it. Nor may it hold a double quote, which ends the message the client
lifts out of the response.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional, Tuple

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, TypeAdapter, ValidationError,
)
from pydantic_core import PydanticCustomError

from src.models.value_bounds import (
    DISTANCE_MAX_M, ELEVATION_MAX_M, ELEVATION_MIN_M, Count, Distance, Duration,
    Elevation, Gain, HeartRate, Lat, Lon, Speed,
)
from src.utils.photo_paths import is_photo_name

#: An integer the database can bind: a 64-bit INTEGER.
_INT_MIN, _INT_MAX = -(2 ** 63), 2 ** 63 - 1

def _refuse(message: str) -> PydanticCustomError:
    return PydanticCustomError("traxj", message)


# ── Value types ─────────────────────────────────────────────────────────────



def _latlng(value: List[float]) -> List[float]:
    """An activity's start or end: ``[lat, lng]``, or ``[]`` when Strava
    has no position for it (an indoor ride)."""
    if value and (len(value) != 2 or not -90 <= value[0] <= 90
                  or not -180 <= value[1] <= 180):
        raise _refuse("is not a latitude and longitude")
    return value


LatLng = Annotated[List[float], AfterValidator(_latlng)]


def _date_time(value: str) -> str:
    """An activity's start, as ``to_strava_dict`` writes it: ISO 8601, or
    empty for none."""
    if value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise _refuse("is not a date and time") from None
    return value


DateTime = Annotated[str, AfterValidator(_date_time)]


def _photo_name(value: Optional[str]) -> Optional[str]:
    """A photo the app stored itself names it with a uuid (issue #471)."""
    if value is not None and not is_photo_name(value):
        raise _refuse("is not a photo name")
    return value


PhotoName = Annotated[Optional[str], AfterValidator(_photo_name)]


def _route_polyline(value: Optional[str]) -> Optional[str]:
    """A resolved route: JSON text of ``[[lon, lat], …]``, which the map
    draws in place of a great circle (``_segment_feature`` in api/geo.py).

    Checked whatever the segment's route_mode: every writer stored it in this
    form, and switching a segment back to a great circle leaves it in place.
    """
    if not value:
        return value

    def _no_constant(_name: str) -> Any:
        raise ValueError

    try:
        points = json.loads(value, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise _refuse("is not a route") from None
    if not isinstance(points, list) or not all(
        isinstance(p, list) and len(p) >= 2
        and all(type(c) in (int, float) for c in p)
        and -180 <= p[0] <= 180 and -90 <= p[1] <= 90
        for p in points
    ):
        raise _refuse("is not a route")
    return value


RoutePolyline = Annotated[Optional[str], AfterValidator(_route_polyline)]


def _numbers(lo: float, hi: float) -> Any:
    """A list of numbers within [*lo*, *hi*], checked where it stands. An
    elevation profile can hold millions of them, and ``List[float]`` would
    validate a copy."""
    def check(value: Any) -> Any:
        if type(value) is not list:
            raise _refuse("is not a list")
        if not set(map(type, value)) <= {int, float}:
            raise _refuse("is not a list of numbers")
        if value and not (lo <= min(value) and max(value) <= hi):
            raise _refuse("holds a number out of range")
        return value
    return AfterValidator(check)


#: A profile's distances, km, and elevations, m (src/models/value_bounds.py).
Distances = Annotated[Any, _numbers(0, DISTANCE_MAX_M / 1000)]
Elevations = Annotated[Any, _numbers(ELEVATION_MIN_M, ELEVATION_MAX_M)]


# ── The format ──────────────────────────────────────────────────────────────

class _Model(BaseModel):
    # strict: a value of another type is refused, never converted ("45.9"
    # is not a latitude, 1 is not true); a float field takes an integer, as
    # JSON does not tell 45 from 45.0. extra="ignore": unknown fields stay
    # ignored, as they always were.
    model_config = ConfigDict(strict=True, extra="ignore")


class _Social(_Model):
    network: Optional[str] = None
    handle: Optional[str] = None


class _Person(_Model):
    id: Optional[int] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    polarsteps: Optional[str] = None
    notes: Optional[str] = None
    avatar_photo: PhotoName = None
    socials: Optional[List[_Social]] = None
    nationalities: Optional[List[str]] = None
    residence: Optional[str] = None
    group_id: Optional[int] = None


class _Group(_Model):
    id: Optional[int] = None
    name: Optional[str] = None
    nationalities: Optional[List[str]] = None
    socials: Optional[List[_Social]] = None


class _Memory(_Model):
    id: Optional[int] = None
    public_id: Optional[str] = None
    name: Optional[str] = None
    date: str = ""
    time: Optional[str] = None
    description: Optional[str] = None
    photos: List[PhotoName] = []
    photo_refs: Any = None          # the ZIP export's photo paths
    geo_mode: str = "start_of_day"
    lat: Optional[Lat] = None
    lon: Optional[Lon] = None
    comment_count: Any = None
    like_count: Any = None


class _Journal(_Model):
    id: Optional[int] = None
    date: str = ""
    time: Optional[str] = None
    description: Optional[str] = None
    photos: List[PhotoName] = []
    geo_mode: str = "start_of_day"
    lat: Optional[Lat] = None
    lon: Optional[Lon] = None


class _Encounter(_Model):
    id: Optional[int] = None
    person_id: Optional[int] = None
    group_id: Optional[int] = None
    date: str = ""
    time: Optional[str] = None
    description: Optional[str] = None
    geo_mode: str = "start_of_day"
    lat: Optional[Lat] = None
    lon: Optional[Lon] = None


class _Endpoint(_Model):
    lat: Lat = 0.0
    lon: Lon = 0.0
    source: str = "auto"


class _Segment(_Model):
    id: str = ""
    segment_type: str = "flight"
    label: str = ""
    date: Optional[str] = None
    start: _Endpoint = _Endpoint()
    end: _Endpoint = _Endpoint()
    route_mode: str = "great_circle"
    train_number: Optional[str] = None
    hafas_provider: Optional[str] = None
    route_polyline: RoutePolyline = None
    route_status: str = "idle"
    route_error: Optional[str] = None
    route_started_at: Optional[str] = None
    route_degraded: bool = False
    route_hafas_failed: bool = False
    route_degrade_retries: int = 0
    route_edited: bool = False
    route_resolver_version: int = 0
    route_strategy: Optional[str] = None


class _ActivityItem(_Model):
    item_type: Optional[str] = None
    activity_id: Optional[int] = None


class _MemoryItem(_Model):
    item_type: Optional[str] = None
    memory: _Memory = _Memory()


class _JournalItem(_Model):
    item_type: Optional[str] = None
    journal: _Journal = _Journal()


class _EncounterItem(_Model):
    item_type: Optional[str] = None
    encounter: _Encounter = _Encounter()


class _SegmentItem(_Model):
    item_type: Optional[str] = None
    segment: _Segment = _Segment()


#: Each item's model, by the kind ProjectIO reads it as: any item_type it does
#: not know, or none, is read as a segment.
_ITEMS = {
    "activity": _ActivityItem,
    "memory": _MemoryItem,
    "journal": _JournalItem,
    "encounter": _EncounterItem,
}


class _Map(_Model):
    summary_polyline: Optional[str] = None


class _ElevationProfile(_Model):
    distances_km: Distances
    elevations_m: Elevations


class _Activity(_Model):
    """``Activity.to_strava_dict``: a Strava activity, plus the app's own.

    Its numbers are typed as numbers, not as the int or float of the
    dataclass: Strava sends ``max_heartrate`` as a float, SQLite hands a
    whole REAL back as an int, and the database binds either. Each is bounded
    by physical plausibility (src/models/value_bounds.py): past the bounds,
    the trip's totals overflow.
    """
    id: Optional[int] = None
    name: str = ""
    type: str = ""
    distance: Distance = 0.0
    moving_time: Duration = 0
    elapsed_time: Duration = 0
    total_elevation_gain: Gain = 0.0
    start_date: DateTime = ""
    start_date_local: DateTime = ""
    timezone: str = "UTC"
    achievement_count: Count = 0
    kudos_count: Count = 0
    comment_count: Count = 0
    athlete_count: Count = 0
    photo_count: Count = 0
    trainer: bool = False
    commute: bool = False
    manual: bool = False
    private: bool = False
    flagged: bool = False
    average_speed: Speed = 0.0
    max_speed: Speed = 0.0
    has_heartrate: bool = False
    pr_count: Count = 0
    total_photo_count: Count = 0
    has_kudoed: bool = False
    gear_id: Optional[str] = None
    average_heartrate: Optional[HeartRate] = None
    max_heartrate: Optional[HeartRate] = None
    heartrate_opt_out: bool = False
    display_hide_heartrate_option: bool = False
    elev_high: Optional[Elevation] = None
    elev_low: Optional[Elevation] = None
    start_latlng: Optional[LatLng] = None
    end_latlng: Optional[LatLng] = None
    map: _Map = _Map()
    elevation_profile: Optional[_ElevationProfile] = None
    is_edited: bool = False
    split_parent_id: Optional[int] = None
    refresh_status: Optional[str] = None
    refresh_started_at: Optional[str] = None
    refresh_error: Optional[str] = None
    source: Optional[str] = None
    source_id: Optional[str] = None
    start_latlng_enc: Optional[str] = None
    end_latlng_enc: Optional[str] = None
    elevation_profile_enc: Optional[str] = None


class _FilterState(_Model):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    activity_types: Optional[List[str]] = None


class _DayMeta(_Model):
    difficulty: Optional[str] = None
    sleeping: Optional[str] = None
    weather: Optional[str] = None
    journal: Optional[str] = None
    tags: Optional[List[str]] = None
    counters: Any = None


class _Trip(_Model):
    version: int = 1
    name: Optional[str] = None
    trip_start: Optional[str] = None
    filter_state: Optional[_FilterState] = None
    # Each item is checked by its own kind's model, below.
    items: List[Dict[str, Any]]
    activities: List[_Activity] = []
    people: List[_Person] = []
    groups: List[_Group] = []
    day_meta: Optional[Dict[str, _DayMeta]] = None
    sleeping_options: Optional[List[str]] = None
    # Written, not read back by the import.
    lock_version: Any = None
    trip_end: Any = None
    sleeping_option_groups: Any = None
    counters: Any = None
    track_color: Any = None
    track_secondary_color: Any = None
    track_width: Any = None
    alternating_track_colors: Any = None
    elevation_chart_color: Any = None
    elevation_chart_show_line: Any = None
    color_by_type: Any = None
    type_styles: Any = None
    languages: Any = None


# ── Reporting ───────────────────────────────────────────────────────────────

def _field_names(model: type[BaseModel], into: set) -> set:
    for name, info in model.model_fields.items():
        into.add(name)
        for arg in _nested_models(info.annotation):
            _field_names(arg, into)
    return into


def _nested_models(annotation: Any):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in getattr(annotation, "__args__", ()) or ():
        yield from _nested_models(arg)


#: Every field name of the format: the only keys a message names. Any other
#: key is the file's own text (a day, or a field no writer emits).
_NAMES = frozenset().union(*(
    _field_names(m, set()) for m in (_Trip, _SegmentItem, *_ITEMS.values())))

#: What a message says of each kind of fault pydantic reports.
_SAYS = {
    "string_type": "is not text",
    "int_type": "is not a whole number",
    "float_type": "is not a number",
    "bool_type": "is not true or false",
    "list_type": "is not a list",
    "dict_type": "is not an object",
    "model_type": "is not an object",
    "missing": "is missing",
    "greater_than_equal": "is out of range",
    "less_than_equal": "is out of range",
}


def _where(path: Tuple[Any, ...]) -> str:
    """*path* as ``items[3].memory.lat``, naming only the format's fields."""
    out = ""
    for n, part in enumerate(path):
        if isinstance(part, int):
            out += f"[{part}]"
            continue
        if n and path[n - 1] == "day_meta":
            part = "<day>"
        elif part not in _NAMES:
            part = "<key>"
        out += f".{part}" if out else part
    return out or "the trip"


def _described(exc: ValidationError, prefix: Tuple[Any, ...] = ()) -> str:
    error = exc.errors(include_url=False, include_context=False, include_input=False)[0]
    says = error["msg"] if error["type"] == "traxj" else _SAYS.get(error["type"], "is not valid")
    return f"{_where(prefix + tuple(error['loc']))} {says}"


def _bad_text(text: str) -> bool:
    """True if *text* holds a lone surrogate, which UTF-8 cannot encode."""
    if text.isascii():
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _bad_value(document: Any) -> Optional[str]:
    """The first value no field of the format may hold, wherever it is.

    Depth first, holding one open iterator and one key per level of nesting,
    never a path per container: a file of a million tiny lists would
    otherwise cost a queued path for each, well past the memory reading it
    takes. The path is only put together once a fault is found.
    """
    keys: List[Any] = []            # the key each open level was entered by
    stack = [_pairs(document)]
    while stack:
        for key, value in stack[-1]:
            if type(key) is str and _bad_text(key):
                return f"{_where(tuple(keys))} has a field name that is not valid text"
            kind = type(value)
            if kind is float:
                if value - value != 0:  # inf - inf and nan - nan are nan
                    return f"{_where((*keys, key))} is not a finite number"
            elif kind is str:
                if _bad_text(value):
                    return f"{_where((*keys, key))} is not valid text"
            elif kind is int:
                if not _INT_MIN <= value <= _INT_MAX:
                    return f"{_where((*keys, key))} is a number out of range"
            elif kind is dict or kind is list:
                keys.append(key)
                stack.append(_pairs(value))
                break
        else:
            stack.pop()
            if keys:
                keys.pop()
    return None


def _pairs(node: Any):
    return iter(node.items()) if type(node) is dict else enumerate(node)


def fault(document: Dict[str, Any]) -> Optional[str]:
    """What is wrong with the trip *document*, or None if the app could
    have written it. *document* is the parsed JSON object of a trip file."""
    found = _bad_value(document)
    if found is not None:
        return found
    try:
        _Trip.model_validate(document)
    except ValidationError as exc:
        return _described(exc)
    for n, item in enumerate(document["items"]):
        kind = item.get("item_type")
        model = _ITEMS.get(kind, _SegmentItem) if isinstance(kind, str) else _SegmentItem
        try:
            model.model_validate(item)
        except ValidationError as exc:
            return _described(exc, ("items", n))
    return None


# ── The same rules, for a part of a trip written through the API ────────────
#
# Every trip the app holds must export to a file this module accepts, so the
# endpoints that write a part of one check it by the same models.

_DAYS = TypeAdapter(Dict[str, _DayMeta])
_LATLNG = TypeAdapter(Optional[LatLng], config=ConfigDict(strict=True))
_PROFILE = TypeAdapter(Optional[_ElevationProfile])


def _part_fault(adapter: TypeAdapter, value: Any, where: Tuple[Any, ...]) -> Optional[str]:
    """The fault of *value*, named by where it sits in a trip file."""
    wrapped = value
    for key in reversed(where):
        wrapped = {key: wrapped}
    found = _bad_value(wrapped)
    if found is not None:
        return found
    try:
        adapter.validate_python(value)
    except ValidationError as exc:
        return _described(exc, where)
    return None


def day_meta_fault(day_meta: Any) -> Optional[str]:
    """What is wrong with a trip's day notes, or None."""
    return _part_fault(_DAYS, day_meta, ("day_meta",))


def activity_fault(activity: Dict[str, Any], n: int) -> Optional[str]:
    """What is wrong with the *n*-th activity of a trip, as
    ``Activity.to_strava_dict`` writes it, or None."""
    return _part_fault(TypeAdapter(_Activity), activity, ("activities", n))


def stored_json_fault(field: str, text: Optional[str]) -> Optional[str]:
    """What is wrong with the plaintext an activity stores in its
    ``start_latlng_json``/``end_latlng_json`` (a start or end) or one of its
    ``*elevation_profile*_json`` columns (a profile), or None. The export
    parses and writes it as that field."""
    try:
        value = json.loads(text) if text is not None else None
    except (ValueError, RecursionError):
        return f"{field} is not valid JSON"
    adapter, exported_as = (
        (_LATLNG, field[:-len("_json")]) if "latlng" in field
        else (_PROFILE, "elevation_profile"))
    found = _part_fault(adapter, value, (exported_as,))
    return None if found is None else f"{field} holds what cannot be exported: {found}"

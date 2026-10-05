"""How old the installed local rail data is — issue #345, plan Decision 2.

The rail stores under ``RAIL_DATA_DIR`` are refreshed monthly, and a refresh
that stops working fails quietly: the old stores keep answering, every route
still resolves, and the only symptom is geometry drifting away from the map.
This check is what makes that visible — a gauge Grafana can alert on, and a
``WARNING`` once a day for as long as the data is too old or cannot be read.

Cheap on purpose (one small JSON read, no store opened), so the refresh job can
call it straight after installing new data rather than leave the gauge a day
behind.
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timezone

from src.services.overpass_service import _RAIL_DATA_DIR_ENV, _RAIL_SOURCE_ENV
from src.services.rail_source import MANIFEST_NAME, MANIFEST_SCHEMA
from src.utils.logging import get_logger
from src.utils.metrics import RAIL_DATA_AGE_DAYS, RAIL_DATA_REGIONS

_log = get_logger(__name__)

# One monthly build cycle plus a missed retry, for val (refreshes on the 4th)
# and prod (the 5th) alike — docs/LOCAL_TRANSPORT_DATA_PLAN.md, Decision 2.
RAIL_DATA_MAX_AGE_DAYS = 40

_STATUS_OK = "ok"
_STATUS_EMPTY = "empty"
_STATUS_INVALID = "invalid"


def _publish(age: float | None, counts: dict[str, int]) -> None:
    RAIL_DATA_AGE_DAYS.set(math.nan if age is None else age)
    for status in (_STATUS_OK, _STATUS_EMPTY, _STATUS_INVALID):
        RAIL_DATA_REGIONS.labels(status=status).set(counts.get(status, 0))


def _read_manifest(path: str) -> dict:
    """The manifest at *path*, or raise if the server could not use it.

    The schema check is the one ``rail_source.load_coverage`` makes: a manifest
    the resolver refuses is, for this check, as good as no manifest at all.
    """
    with open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("not a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"schema {manifest.get('schema')!r}, expected {MANIFEST_SCHEMA}")
    regions = manifest.get("regions", [])
    if not isinstance(regions, list):
        raise ValueError("'regions' is not a list")
    return manifest


def _check(directory: str | None, local: bool) -> float | None:
    # Warnings only matter while the resolver actually reads this data; with the
    # local source off, the gauges still describe whatever is on disk.
    warn = _log.warning if local else _log.debug
    if not directory:
        warn("%s is unset — no rail data to check", _RAIL_DATA_DIR_ENV)
        _publish(None, {})
        return None
    path = os.path.join(directory, MANIFEST_NAME)
    if not os.path.isfile(path):
        warn("no rail manifest at %s — every rail resolve goes to Overpass", path)
        _publish(None, {})
        return None
    try:
        manifest = _read_manifest(path)
    except (OSError, ValueError) as exc:
        warn("rail manifest %s is unreadable (%s) — every rail resolve goes to "
             "Overpass", path, exc)
        _publish(None, {})
        return None

    today = datetime.now(timezone.utc).date()
    counts = {_STATUS_OK: 0, _STATUS_EMPTY: 0, _STATUS_INVALID: 0}
    oldest: tuple[date, str] | None = None
    for entry in manifest.get("regions", []):
        if not isinstance(entry, dict):
            counts[_STATUS_INVALID] += 1
            continue
        region = entry.get("region")
        status = entry.get("status", _STATUS_OK)
        if status == _STATUS_EMPTY:
            counts[_STATUS_EMPTY] += 1
            continue
        if status != _STATUS_OK:
            counts[_STATUS_INVALID] += 1
            warn("rail manifest entry %r has unknown status %r", region, status)
            continue
        try:
            source_date = date.fromisoformat(str(entry.get("source_date", "")))
        except ValueError:
            # An ok region whose age nobody can tell must not quietly drop out
            # of the oldest-region figure: it is exactly the kind that goes stale.
            counts[_STATUS_INVALID] += 1
            warn("rail manifest entry %r has no usable source_date (%r)",
                 region, entry.get("source_date"))
            continue
        counts[_STATUS_OK] += 1
        if oldest is None or source_date < oldest[0]:
            oldest = (source_date, region)

    if oldest is None:
        warn("rail manifest %s lists no usable region — every rail resolve goes "
             "to Overpass", path)
        _publish(None, counts)
        return None

    age = float((today - oldest[0]).days)
    _publish(age, counts)
    if age > RAIL_DATA_MAX_AGE_DAYS:
        warn("rail data is %d days old (over %d): oldest region %s, source date %s",
             age, RAIL_DATA_MAX_AGE_DAYS, oldest[1], oldest[0].isoformat())
    return age


def check_rail_data_age(directory: str | None = None) -> float | None:
    """Export the installed rail data's age, in days; warn while it is too old.

    *directory* defaults to ``RAIL_DATA_DIR``. Returns the age of the oldest
    ``ok`` region, or None when there is no usable manifest. Never raises: it
    runs on the scheduler, and a check that breaks must not take that down.
    """
    local = os.environ.get(_RAIL_SOURCE_ENV, "").strip().lower() == "local"
    if directory is None:
        directory = os.environ.get(_RAIL_DATA_DIR_ENV, "").strip()
    try:
        return _check(directory, local)
    except Exception:  # noqa: BLE001 — a broken check must not take the scheduler down
        _log.exception("rail data age check failed")
        return None

"""Refreshing the local rail data, and how old it is — issue #345.

Two jobs. The monthly refresh (plan Decision 5) installs the newest published
release on its own; the daily age check (Decision 2) says when that stopped
working.

The rail stores under ``RAIL_DATA_DIR`` are refreshed monthly, and a refresh
that stops working fails quietly: the old stores keep answering, every route
still resolves, and the only symptom is geometry drifting away from the map.
This check is what makes that visible — a gauge Grafana can alert on, and a
``WARNING`` once a day for as long as the data is too old or cannot be read.

Cheap on purpose (one small JSON read, no store opened), so the refresh job can
call it straight after installing new data rather than leave the gauge a day
behind.

The refresh is ``scripts/fetch_rail_data.py`` — the exact command the runbook
documents (docs/DEPLOYMENT_VPS.md §9) — run as a subprocess of an RQ job on the
``default`` queue. A subprocess, so the store builds' memory belongs to a
process that exits; RQ, so it never runs in the API process.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from src.jobs.queue import QUEUE_DEFAULT, enqueue, queue_available
from src.services.overpass_service import _RAIL_DATA_DIR_ENV, _RAIL_SOURCE_ENV
from src.services.rail_source import MANIFEST_NAME, MANIFEST_SCHEMA
from src.utils.logging import get_logger
from src.utils.metrics import RAIL_DATA_AGE_DAYS, RAIL_DATA_REGIONS

_log = get_logger(__name__)

# One monthly build cycle plus a missed retry, for val (refreshes on the 4th)
# and prod (the 5th) alike — docs/LOCAL_TRANSPORT_DATA_PLAN.md, Decision 2.
RAIL_DATA_MAX_AGE_DAYS = 40

# The monthly refresh. Off with RAIL_AUTO_REFRESH=0; the day of the month it
# runs is RAIL_AUTO_REFRESH_DAY — val sets 4, so a bad release reaches val a day
# before prod and the two stacks never build at once on the shared host.
_AUTO_REFRESH_ENV = "RAIL_AUTO_REFRESH"
_AUTO_REFRESH_DAY_ENV = "RAIL_AUTO_REFRESH_DAY"
RAIL_REFRESH_DEFAULT_DAY = 5
# 28, not 31: a cron day every month has.
_REFRESH_DAY_MAX = 28

# The subprocess is killed at 55 minutes, before RQ's own limit at 60 kills the
# work-horse: a timeout this module raises says what timed out, a killed horse
# does not. A full refresh of rail-data-2026-10-05 measured 162 s and 361 MB
# peak RSS (Linux, 49 regions), a month with nothing new 1.5 s: this is for a
# hang, not for a slow month.
RAIL_REFRESH_JOB_TIMEOUT_S = 3600
RAIL_REFRESH_SCRIPT_TIMEOUT_S = 3300

# Resolved from this file, not the working directory: the image puts scripts/
# beside src/ (.dockerignore keeps fetch_rail_data.py), whatever a worker's cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FETCH_SCRIPT = _REPO_ROOT / "scripts" / "fetch_rail_data.py"

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


# ---------------------------------------------------------------------------
# The monthly refresh
# ---------------------------------------------------------------------------

def rail_refresh_day() -> int:
    """The day of the month the refresh is scheduled on, from the environment.

    A bad value falls back to the default with a ``WARNING`` rather than raise:
    this is read while the API starts, and a typo in an optional setting must
    not take the scheduler — the backup, every sweep — down with it.
    """
    raw = os.environ.get(_AUTO_REFRESH_DAY_ENV, "").strip()
    if not raw:
        return RAIL_REFRESH_DEFAULT_DAY
    try:
        day = int(raw)
    except ValueError:
        day = 0
    if not 1 <= day <= _REFRESH_DAY_MAX:
        _log.warning("%s=%r is not a day from 1 to %d — refreshing rail data on "
                     "the %d instead", _AUTO_REFRESH_DAY_ENV, raw,
                     _REFRESH_DAY_MAX, RAIL_REFRESH_DEFAULT_DAY)
        return RAIL_REFRESH_DEFAULT_DAY
    return day


def enqueue_rail_data_refresh() -> bool:
    """Queue this month's rail data refresh. Returns whether it was queued.

    Nothing unless ``RAIL_SOURCE=local`` with a ``RAIL_DATA_DIR``, and
    ``RAIL_AUTO_REFRESH`` is not ``0``. Never runs the refresh here: this is
    the API process, and 49 store builds inside its memory limit is the failure
    review finding R1-4 rules out. With no broker it says the refresh is manual
    on this deployment and returns; with a broker that refuses the job it
    raises, so the scheduler's job metrics record the month as failed.
    """
    if os.environ.get(_RAIL_SOURCE_ENV, "").strip().lower() != "local":
        return False
    if os.environ.get(_AUTO_REFRESH_ENV, "").strip() == "0":
        _log.info("%s=0 — monthly rail data refresh skipped", _AUTO_REFRESH_ENV)
        return False
    if not os.environ.get(_RAIL_DATA_DIR_ENV, "").strip():
        _log.warning("%s=local but %s is unset — no rail data to refresh",
                     _RAIL_SOURCE_ENV, _RAIL_DATA_DIR_ENV)
        return False
    if not queue_available():
        _log.warning(
            "monthly rail data refresh not run: no job queue (REDIS_URL unset), "
            "and it never runs in the API process — refresh is manual on this "
            "deployment, see docs/DEPLOYMENT_VPS.md, \"Rail data\"")
        return False
    # No retry: a refused region is rarely transient, a missed month still
    # leaves the data under the 40-day alert, and the manual run is the retry.
    if not enqueue(QUEUE_DEFAULT, run_rail_data_refresh,
                   job_timeout=RAIL_REFRESH_JOB_TIMEOUT_S, allow_inline=False,
                   max_retries=0):
        raise RuntimeError("monthly rail data refresh could not be queued")
    _log.info("monthly rail data refresh queued")
    return True


def run_rail_data_refresh() -> None:
    """Install the newest rail data release into ``RAIL_DATA_DIR``. RQ job.

    Raises when the script exits non-zero — a region refused, the release
    unusable — or runs past its timeout, so RQ records the job as failed. The
    age check runs either way: a partial install changes what is on disk too.
    """
    directory = os.environ.get(_RAIL_DATA_DIR_ENV, "").strip()
    if not directory:
        raise RuntimeError(f"{_RAIL_DATA_DIR_ENV} is unset — nothing to refresh")
    try:
        # Output is inherited, not captured: the script's per-region lines land
        # in the worker's log as they happen, where the runbook says to look.
        subprocess.run(
            [sys.executable, str(_FETCH_SCRIPT), "--dest", directory],
            cwd=_REPO_ROOT, timeout=RAIL_REFRESH_SCRIPT_TIMEOUT_S, check=True)
    finally:
        check_rail_data_age(directory)

"""The daily rail-data age check (issue #345, U2): src/jobs/rail_data_jobs.py."""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

import pytest

from src.jobs import rail_data_jobs
from src.jobs.rail_data_jobs import RAIL_DATA_MAX_AGE_DAYS, check_rail_data_age
from src.utils.metrics import RAIL_DATA_AGE_DAYS, RAIL_DATA_REGIONS

_LOGGER = rail_data_jobs.__name__


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()


def _ok(region: str, days: int) -> dict:
    return {"region": region, "status": "ok", "source_date": _days_ago(days),
            "bbox": [5.7, 49.4, 6.5, 50.2], "sha256": "x", "bytes": 1}


def _empty(region: str) -> dict:
    return {"region": region, "status": "empty", "source_date": "2001-01-01"}


def _write(directory, regions) -> None:
    manifest = {"schema": 2, "generated_at": "2026-09-05T00:00:00Z", "regions": regions}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == _LOGGER and r.levelno >= logging.WARNING]


def _regions(status: str) -> float:
    return RAIL_DATA_REGIONS.labels(status=status)._value.get()


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setenv("RAIL_SOURCE", "local")
    monkeypatch.setenv("RAIL_DATA_DIR", str(tmp_path))
    return tmp_path


def test_fresh_manifest_exports_age_without_warning(local, caplog):
    _write(local, [_ok("europe/luxembourg", 3), _ok("europe/germany", 10)])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() == 10.0
    assert _warnings(caplog) == []
    assert RAIL_DATA_AGE_DAYS._value.get() == 10.0
    assert _regions("ok") == 2 and _regions("empty") == 0 and _regions("invalid") == 0


def test_one_stale_region_among_fresh_ones_is_named(local, caplog):
    stale_age = RAIL_DATA_MAX_AGE_DAYS + 5
    _write(local, [_ok("europe/luxembourg", 3), _ok("europe/france", stale_age),
                   _ok("europe/germany", 10)])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() == float(stale_age)
    [message] = _warnings(caplog)
    assert "europe/france" in message and _days_ago(stale_age) in message


def test_exactly_at_the_threshold_is_not_stale(local, caplog):
    _write(local, [_ok("europe/luxembourg", RAIL_DATA_MAX_AGE_DAYS)])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() == float(RAIL_DATA_MAX_AGE_DAYS)
    assert _warnings(caplog) == []


def test_empty_entries_are_ignored(local, caplog):
    # An `empty` entry's date never moves (nothing is rebuilt for it), so it
    # must neither set the age nor raise a warning.
    _write(local, [_empty("europe/andorra"), _ok("europe/luxembourg", 3)])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() == 3.0
    assert _warnings(caplog) == []
    assert _regions("ok") == 1 and _regions("empty") == 1


def test_missing_status_counts_as_ok(local):
    # fetch_rail_data and rail_source both default a missing status to `ok`.
    entry = _ok("europe/luxembourg", 7)
    del entry["status"]
    _write(local, [entry])
    assert check_rail_data_age() == 7.0


def test_missing_manifest_warns_under_local(local, caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() is None
    [message] = _warnings(caplog)
    assert "manifest" in message
    assert math.isnan(RAIL_DATA_AGE_DAYS._value.get())


def test_unset_data_dir_warns_under_local(monkeypatch, caplog):
    monkeypatch.setenv("RAIL_SOURCE", "local")
    monkeypatch.delenv("RAIL_DATA_DIR", raising=False)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() is None
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("source", [None, "overpass", ""])
def test_silent_when_source_is_not_local(monkeypatch, tmp_path, caplog, source):
    if source is None:
        monkeypatch.delenv("RAIL_SOURCE", raising=False)
    else:
        monkeypatch.setenv("RAIL_SOURCE", source)
    monkeypatch.setenv("RAIL_DATA_DIR", str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() is None            # missing manifest
        _write(tmp_path, [_ok("europe/france", RAIL_DATA_MAX_AGE_DAYS + 30)])
        assert check_rail_data_age() == float(RAIL_DATA_MAX_AGE_DAYS + 30)  # stale
    assert _warnings(caplog) == []


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    '{"schema": 1, "regions": []}',
    '{"schema": 2, "regions": {"a": 1}}',
    '{"schema": 2, "regions": [1, "x", null]}',
])
def test_malformed_manifest_does_not_raise(local, caplog, content):
    (local / "manifest.json").write_text(content, encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() is None
    assert len(_warnings(caplog)) == 1
    assert math.isnan(RAIL_DATA_AGE_DAYS._value.get())


def test_ok_region_without_a_usable_date_is_named(local, caplog):
    bad = _ok("europe/spain", 0)
    bad["source_date"] = "yesterday"
    _write(local, [bad, _ok("europe/luxembourg", 3)])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() == 3.0
    [message] = _warnings(caplog)
    assert "europe/spain" in message
    assert _regions("invalid") == 1 and _regions("ok") == 1


def test_an_unexpected_failure_is_logged_not_raised(local, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("check exploded")
    monkeypatch.setattr(rail_data_jobs, "_check", boom)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() is None
    [record] = [r for r in caplog.records if r.name == _LOGGER]
    assert record.levelno == logging.ERROR and record.exc_info is not None


def test_explicit_directory_overrides_the_environment(local, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    _write(other, [_ok("europe/luxembourg", 12)])
    _write(local, [_ok("europe/luxembourg", 1)])
    assert check_rail_data_age(str(other)) == 12.0


def test_the_api_schedules_the_check_daily(monkeypatch):
    """The lifespan registers the job at 05:15 under its id, and runs it once now."""
    import api.router as router
    from fastapi.testclient import TestClient

    jobs = {}

    class _Scheduler:
        def add_job(self, func, trigger, **kwargs):
            jobs[kwargs["id"]] = (func, trigger, kwargs)
        def add_listener(self, *a, **k): pass
        def start(self): pass
        def shutdown(self, *a, **k): pass

    monkeypatch.setattr(router, "_IS_API_PROCESS", True)
    monkeypatch.setattr(router.alembic_command, "upgrade", lambda *a, **k: None)
    monkeypatch.setattr(router, "_check_schema_contract", lambda: None)
    monkeypatch.setattr(router, "seed_admin", lambda: None)
    monkeypatch.setattr(router, "sweep_orphaned_jobs", lambda: 0)
    monkeypatch.setattr(router, "sweep_orphaned_poster_jobs", lambda: 0)
    monkeypatch.setattr(router, "move_companion_avatars", lambda: None)
    monkeypatch.setattr(router, "_scheduler", _Scheduler())

    with TestClient(router.app):
        pass
    func, trigger, kwargs = jobs["rail_data_age"]
    assert func is check_rail_data_age and trigger == "cron"
    assert (kwargs["hour"], kwargs["minute"]) == (5, 15)
    assert kwargs["next_run_time"] is not None


def test_a_stale_ferry_layer_is_named_with_its_region(local, caplog):
    stale_age = RAIL_DATA_MAX_AGE_DAYS + 5
    ferry = _ok("europe/denmark", stale_age)
    ferry.update({"layer": "ferry", "carried": True})
    _write(local, [_ok("europe/denmark", 3), ferry])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert check_rail_data_age() == float(stale_age)
    [message] = _warnings(caplog)
    assert "europe/denmark ferry" in message and _days_ago(stale_age) in message


def test_a_stale_rail_entry_message_is_unchanged(local, caplog):
    stale_age = RAIL_DATA_MAX_AGE_DAYS + 5
    _write(local, [_ok("europe/france", stale_age)])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        check_rail_data_age()
    assert _warnings(caplog) == [
        "rail data is %d days old (over %d): oldest region europe/france, "
        "source date %s" % (stale_age, RAIL_DATA_MAX_AGE_DAYS, _days_ago(stale_age))]

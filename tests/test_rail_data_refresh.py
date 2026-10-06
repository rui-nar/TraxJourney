"""The monthly rail data refresh (issue #345, U6): src/jobs/rail_data_jobs.py.

Two halves. ``enqueue_rail_data_refresh`` runs in the API process on the 4th or
5th and must never do the refresh there — review finding R1-4: with no broker,
``enqueue``'s usual fallback would build 49 stores inside the API's memory
limit. ``run_rail_data_refresh`` is the RQ job: the runbook's own command as a
subprocess, a failure RQ can see, and the age gauge moved the same day.
"""
from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from src.jobs import queue as queue_module
from src.jobs import rail_data_jobs
from src.jobs.queue import QUEUE_DEFAULT
from src.jobs.rail_data_jobs import (
    RAIL_REFRESH_DEFAULT_DAY,
    enqueue_rail_data_refresh,
    rail_refresh_day,
    run_rail_data_refresh,
)

_LOGGER = rail_data_jobs.__name__


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == _LOGGER and r.levelno >= logging.WARNING]


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setenv("RAIL_SOURCE", "local")
    monkeypatch.setenv("RAIL_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAIL_AUTO_REFRESH", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    return tmp_path


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail the test if anything starts the refresh script."""
    calls = []
    monkeypatch.setattr(rail_data_jobs.subprocess, "run",
                        lambda *a, **k: calls.append(a) or pytest.fail("refresh ran"))
    return calls


@pytest.fixture
def queued(monkeypatch):
    """A broker that accepts every job; returns the enqueue calls it saw."""
    calls = []

    def _enqueue(queue_name, func, *args, **kwargs):
        calls.append((queue_name, func, args, kwargs))
        return True

    monkeypatch.setattr(rail_data_jobs, "queue_available", lambda: True)
    monkeypatch.setattr(rail_data_jobs, "enqueue", _enqueue)
    return calls


# ---------------------------------------------------------------------------
# Enqueue — the API side
# ---------------------------------------------------------------------------

def test_enqueued_once_under_local(local, queued):
    assert enqueue_rail_data_refresh() is True
    assert len(queued) == 1
    queue_name, func, args, kwargs = queued[0]
    assert queue_name == QUEUE_DEFAULT and func is run_rail_data_refresh
    assert args == ()
    assert kwargs["job_timeout"] == 3600
    assert kwargs["allow_inline"] is False


@pytest.mark.parametrize("source", ["", "overpass", "LOCALISH"])
def test_not_enqueued_when_source_is_not_local(local, queued, monkeypatch, source):
    monkeypatch.setenv("RAIL_SOURCE", source)
    assert enqueue_rail_data_refresh() is False
    assert queued == []


def test_not_enqueued_when_switched_off(local, queued, monkeypatch):
    monkeypatch.setenv("RAIL_AUTO_REFRESH", "0")
    assert enqueue_rail_data_refresh() is False
    assert queued == []


def test_any_other_switch_value_leaves_it_on(local, queued, monkeypatch):
    monkeypatch.setenv("RAIL_AUTO_REFRESH", "1")
    assert enqueue_rail_data_refresh() is True
    assert len(queued) == 1


def test_not_enqueued_without_a_data_dir(local, queued, monkeypatch, caplog):
    monkeypatch.setenv("RAIL_DATA_DIR", "")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert enqueue_rail_data_refresh() is False
    assert queued == []
    assert any("RAIL_DATA_DIR" in m for m in _warnings(caplog))


def test_no_broker_runs_nothing_in_process_and_warns(local, no_subprocess, caplog):
    """R1-4: the real enqueue, no REDIS_URL — the refresh must not run here."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert enqueue_rail_data_refresh() is False
    assert no_subprocess == []
    warnings = _warnings(caplog)
    assert len(warnings) == 1 and "manual on this deployment" in warnings[0]


def test_a_broker_that_refuses_the_job_runs_nothing_and_raises(
        local, no_subprocess, monkeypatch):
    """Through the real enqueue: a failing broker must not fall back inline.

    Raising is what makes the scheduler's job metrics show the month failed.
    """
    class _Broken:
        def enqueue(self, *a, **k):
            raise ConnectionError("broker gone")

    monkeypatch.setattr(rail_data_jobs, "queue_available", lambda: True)
    monkeypatch.setattr(queue_module, "get_queue", lambda name: _Broken())
    with pytest.raises(RuntimeError, match="could not be queued"):
        enqueue_rail_data_refresh()
    assert no_subprocess == []


# ---------------------------------------------------------------------------
# The day it runs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, day", [("", 5), ("4", 4), (" 1 ", 1), ("28", 28)])
def test_refresh_day_reads_the_environment(monkeypatch, raw, day):
    monkeypatch.setenv("RAIL_AUTO_REFRESH_DAY", raw)
    assert rail_refresh_day() == day


@pytest.mark.parametrize("raw", ["0", "29", "31", "-4", "fourth", "4.5"])
def test_a_bad_refresh_day_falls_back_with_a_warning(monkeypatch, caplog, raw):
    monkeypatch.setenv("RAIL_AUTO_REFRESH_DAY", raw)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert rail_refresh_day() == RAIL_REFRESH_DEFAULT_DAY
    assert any("RAIL_AUTO_REFRESH_DAY" in m for m in _warnings(caplog))


def _scheduled_jobs(monkeypatch) -> dict:
    """Start the API's lifespan against a fake scheduler; return its jobs by id."""
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
    return jobs


@pytest.mark.parametrize("raw, day", [(None, 5), ("4", 4), ("40", 5)])
def test_the_api_schedules_the_refresh_on_the_configured_day(monkeypatch, raw, day):
    if raw is None:
        monkeypatch.delenv("RAIL_AUTO_REFRESH_DAY", raising=False)
    else:
        monkeypatch.setenv("RAIL_AUTO_REFRESH_DAY", raw)
    func, trigger, kwargs = _scheduled_jobs(monkeypatch)["rail_data_refresh"]
    assert func is enqueue_rail_data_refresh and trigger == "cron"
    assert (kwargs["day"], kwargs["hour"], kwargs["minute"]) == (day, 4, 10)
    # Not at start-up: a deploy must not trigger a 49-region refresh.
    assert "next_run_time" not in kwargs


# ---------------------------------------------------------------------------
# The job — the worker side
# ---------------------------------------------------------------------------

@pytest.fixture
def script(monkeypatch):
    """Record the subprocess call and the age checks, in order."""
    events = []

    def _run(cmd, **kwargs):
        events.append(("run", cmd, kwargs))
        if exit_code[0]:
            raise subprocess.CalledProcessError(exit_code[0], cmd)
        return subprocess.CompletedProcess(cmd, 0)

    exit_code = [0]
    monkeypatch.setattr(rail_data_jobs.subprocess, "run", _run)
    monkeypatch.setattr(rail_data_jobs, "check_rail_data_age",
                        lambda directory=None: events.append(("age", directory)))
    return events, exit_code


def test_the_job_runs_the_runbook_command_then_the_age_check(local, script):
    events, _ = script
    run_rail_data_refresh()
    assert [e[0] for e in events] == ["run", "age"]
    _, cmd, kwargs = events[0]
    assert cmd[0] == sys.executable
    # The script by absolute path, not relative to whatever the cwd is.
    assert cmd[1] == str(rail_data_jobs._FETCH_SCRIPT)
    assert rail_data_jobs._FETCH_SCRIPT.is_file()
    assert cmd[2:] == ["--dest", str(local)]
    assert kwargs["timeout"] == 3300 and kwargs["check"] is True
    assert events[1] == ("age", str(local))


def test_a_non_zero_exit_raises(local, script):
    events, exit_code = script
    exit_code[0] = 1
    with pytest.raises(subprocess.CalledProcessError):
        run_rail_data_refresh()
    # A partial install changed the data too: the gauge still moves.
    assert [e[0] for e in events] == ["run", "age"]


def test_a_real_non_zero_exit_raises(local, monkeypatch):
    """Unmocked subprocess: the script refusing its arguments fails the job."""
    monkeypatch.setattr(rail_data_jobs, "check_rail_data_age", lambda d=None: None)
    real_run = subprocess.run
    monkeypatch.setattr(
        rail_data_jobs.subprocess, "run",
        lambda cmd, **k: real_run([*cmd, "--no-such-flag"], **k))
    with pytest.raises(subprocess.CalledProcessError):
        run_rail_data_refresh()


def test_the_job_refuses_an_unset_data_dir(local, script, monkeypatch):
    events, _ = script
    monkeypatch.setenv("RAIL_DATA_DIR", "")
    with pytest.raises(RuntimeError, match="RAIL_DATA_DIR"):
        run_rail_data_refresh()
    assert events == []

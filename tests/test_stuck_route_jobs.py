"""Hourly warning for route jobs left pending/running too long (R1-5).

The startup sweep re-queues orphaned jobs, but a worker killed while the API
stays up leaves its job unfinished until the next restart. The hourly check
only logs it: it must never change a row.
"""
from __future__ import annotations

import inspect
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from models.project_db import DBProject, DBRouteJob
from models.user import UserInfo
from src.jobs.route_jobs import STUCK_JOB_AFTER_S, warn_stuck_route_jobs

LOGGER = "src.jobs.route_jobs"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="A", email="a@e.com")
        sess.add(user); sess.commit(); sess.refresh(user)
        proj = DBProject(user_info_id=user.id, name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        return engine, user.id, proj.id


def _iso_minutes_ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _add_job(engine, user_id, project_id, seg_id, *, status="pending",
             started_at=None, created_at=None) -> int:
    with Session(engine) as sess:
        job = DBRouteJob(
            project_id=project_id, user_info_id=user_id, project_name="Trip",
            segment_id=seg_id, status=status, started_at=started_at,
        )
        if created_at is not None:
            job.created_at = created_at
        sess.add(job); sess.commit(); sess.refresh(job)
        return job.id


def _snapshot(engine):
    with Session(engine) as sess:
        return [j.model_dump() for j in
                sess.exec(select(DBRouteJob).order_by(DBRouteJob.id)).all()]


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and r.name == LOGGER]


def test_threshold_is_thirty_minutes():
    assert STUCK_JOB_AFTER_S == 30 * 60


def test_old_pending_job_logs_with_ids(env, caplog):
    engine, user_id, project_id = env
    job_id = _add_job(engine, user_id, project_id, "seg-old",
                      started_at=_iso_minutes_ago(31))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert warn_stuck_route_jobs() == 1

    [line] = _warnings(caplog)
    assert f"route job {job_id} " in line
    assert f"project={project_id}" in line
    assert "seg=seg-old" in line
    assert "pending" in line


def test_old_running_job_logs(env, caplog):
    engine, user_id, project_id = env
    _add_job(engine, user_id, project_id, "seg-run", status="running",
             started_at=_iso_minutes_ago(31))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert warn_stuck_route_jobs() == 1
    [line] = _warnings(caplog)
    assert "running" in line and "seg=seg-run" in line


def test_young_job_does_not_log(env, caplog):
    engine, user_id, project_id = env
    _add_job(engine, user_id, project_id, "seg-young",
             started_at=_iso_minutes_ago(29))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert warn_stuck_route_jobs() == 0
    assert _warnings(caplog) == []


@pytest.mark.parametrize("status", ["done", "failed"])
def test_finished_job_does_not_log(env, caplog, status):
    engine, user_id, project_id = env
    _add_job(engine, user_id, project_id, "seg-fin", status=status,
             started_at=_iso_minutes_ago(120))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert warn_stuck_route_jobs() == 0
    assert _warnings(caplog) == []


def test_zulu_token_is_parsed(env, caplog):
    """Trigger tokens may end in "Z" (see test_route_job_recovery.TOKEN)."""
    engine, user_id, project_id = env
    old = (datetime.now(timezone.utc) - timedelta(minutes=31)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    _add_job(engine, user_id, project_id, "seg-z", started_at=old)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert warn_stuck_route_jobs() == 1


@pytest.mark.parametrize("started_at", [None, "", "not-a-date"])
def test_missing_start_falls_back_to_created_at(env, caplog, started_at):
    engine, user_id, project_id = env
    _add_job(engine, user_id, project_id, "seg-old",
             started_at=started_at, created_at=time.time() - 31 * 60)
    _add_job(engine, user_id, project_id, "seg-young",
             started_at=started_at, created_at=time.time() - 29 * 60)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert warn_stuck_route_jobs() == 1
    [line] = _warnings(caplog)
    assert "seg=seg-old" in line


def test_no_row_is_modified(env, caplog):
    engine, user_id, project_id = env
    _add_job(engine, user_id, project_id, "a", started_at=_iso_minutes_ago(31))
    _add_job(engine, user_id, project_id, "b", status="running",
             started_at=_iso_minutes_ago(90))
    _add_job(engine, user_id, project_id, "c", started_at=_iso_minutes_ago(5))
    _add_job(engine, user_id, project_id, "d", status="done",
             started_at=_iso_minutes_ago(90))
    before = _snapshot(engine)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert warn_stuck_route_jobs() == 2

    assert _snapshot(engine) == before


def test_scheduled_hourly():
    import api.router as router
    src = inspect.getsource(router.lifespan)
    assert 'add_job(warn_stuck_route_jobs, "interval", hours=1' in src

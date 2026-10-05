"""The hourly poster sweep: finished jobs' rows and files go 30 days after
``completed_at`` (docs/E2EE_REMNANTS_PLAN.md, decision 11, unit U5)."""
from __future__ import annotations

import time

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import src.poster.poster_job_runner as job_runner_module
from models.project_db import DBPosterJob, DBProject
from models.user import UserInfo

_DAY = 24 * 3600
_NOW = 2_000_000_000.0


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setattr(db_module, "engine", engine)
    data = tmp_path / "data"
    monkeypatch.setattr(job_runner_module, "_DATA_DIR", data)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="A", email="a@e.com")
        sess.add(user); sess.commit(); sess.refresh(user)
        project = DBProject(user_info_id=user.id, name="Trip")
        sess.add(project); sess.commit(); sess.refresh(project)
        ids = (user.id, project.id)
    return engine, data, ids


def _job(engine, data, ids, *, status, completed_days_ago=None):
    """A job row plus its rendered files on disk."""
    uid, pid = ids
    completed = None if completed_days_ago is None else _NOW - completed_days_ago * _DAY
    with Session(engine) as sess:
        job = DBPosterJob(project_id=pid, user_info_id=uid, status=status,
                          created_at=_NOW - 40 * _DAY, completed_at=completed)
        sess.add(job); sess.commit(); sess.refresh(job)
        job_id = job.id
    folder = data / "users" / str(uid) / "posters" / str(job_id)
    folder.mkdir(parents=True)
    (folder / "poster.png").write_bytes(b"png")
    (folder / "poster.pdf").write_bytes(b"pdf")
    return job_id, folder


def _ids(engine):
    with Session(engine) as sess:
        return {j.id for j in sess.exec(select(DBPosterJob)).all()}


def test_sweep_deletes_old_finished_jobs_and_keeps_the_rest(env):
    engine, data, ids = env
    old_done, old_done_dir = _job(engine, data, ids, status="done", completed_days_ago=31)
    old_failed, old_failed_dir = _job(engine, data, ids, status="failed", completed_days_ago=31)
    recent, recent_dir = _job(engine, data, ids, status="done", completed_days_ago=29)
    running, running_dir = _job(engine, data, ids, status="running")
    pending, pending_dir = _job(engine, data, ids, status="pending")

    assert job_runner_module.sweep_poster_jobs(now=_NOW) == 2

    assert _ids(engine) == {recent, running, pending}
    assert not old_done_dir.exists() and not old_failed_dir.exists()
    for kept in (recent_dir, running_dir, pending_dir):
        assert (kept / "poster.png").exists() and (kept / "poster.pdf").exists()


def test_sweep_never_touches_a_non_terminal_job_even_with_an_old_completed_at(env):
    """A non-terminal row with a stale completed_at is still never swept."""
    engine, data, ids = env
    running, running_dir = _job(engine, data, ids, status="running", completed_days_ago=60)
    pending, pending_dir = _job(engine, data, ids, status="pending", completed_days_ago=60)

    assert job_runner_module.sweep_poster_jobs(now=_NOW) == 0

    assert _ids(engine) == {running, pending}
    assert running_dir.exists() and pending_dir.exists()


def test_a_second_run_is_a_no_op(env):
    engine, data, ids = env
    _job(engine, data, ids, status="done", completed_days_ago=31)
    recent, recent_dir = _job(engine, data, ids, status="done", completed_days_ago=29)

    assert job_runner_module.sweep_poster_jobs(now=_NOW) == 1
    assert job_runner_module.sweep_poster_jobs(now=_NOW) == 0

    assert _ids(engine) == {recent}
    assert recent_dir.exists()


def test_a_row_whose_files_are_already_gone_is_still_deleted(env):
    """An interrupted run (files deleted, row not) is finished by the next."""
    engine, data, ids = env
    job_id, folder = _job(engine, data, ids, status="done", completed_days_ago=31)
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()

    assert job_runner_module.sweep_poster_jobs(now=_NOW) == 1
    assert _ids(engine) == set()


def test_sweep_defaults_to_the_current_time(env):
    engine, data, ids = env
    now = time.time()
    with Session(engine) as sess:
        sess.add(DBPosterJob(project_id=ids[1], user_info_id=ids[0], status="done",
                             completed_at=now - 31 * _DAY))
        sess.add(DBPosterJob(project_id=ids[1], user_info_id=ids[0], status="done",
                             completed_at=now - 29 * _DAY))
        sess.commit()

    assert job_runner_module.sweep_poster_jobs() == 1


def test_a_broken_sweep_does_not_raise(env, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(job_runner_module, "get_session", _boom)
    assert job_runner_module.sweep_poster_jobs(now=_NOW) == 0

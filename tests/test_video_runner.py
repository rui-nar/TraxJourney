"""Trip video job lifecycle: runner, interrupt path, sweeps (docs/TRIP_VIDEO_PLAN.md, U6).

Every transition is a compare-and-set (Convention 6), and the consent
geometry of D2 is gone after every terminal path (Convention 2). The renderer
(U5) is faked through ``job_runner._renderer``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import polyline as polyline_lib
import pytest
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
import src.jobs.worker as worker_mod
import src.video.job_runner as runner
from models.project_db import DBProject, DBVideoJob
from models.user import UserInfo
from src.video import paths

SECRET_LINE = polyline_lib.encode([(48.123456, 2.654321), (48.2, 2.7)])


class _FakeMail:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(paths, "_DATA_DIR", tmp_path / "data")
    mail = _FakeMail()
    monkeypatch.setattr(runner, "get_email_service", lambda: mail)
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://app.example")
    with Session(engine) as sess:
        user = UserInfo(display_name="A", email="a@example.com")
        sess.add(user); sess.commit(); sess.refresh(user)
        proj = DBProject(user_info_id=user.id, name="My Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        uid, pid = user.id, proj.id

    def make_job(status="pending", geometry=True, **kw) -> int:
        with Session(engine) as sess:
            job = DBVideoJob(project_id=pid, user_info_id=uid, status=status,
                             request_json=json.dumps({"length_s": 30, "height": 720}),
                             download_token=f"tok-{time.time_ns()}", **kw)
            sess.add(job); sess.commit(); sess.refresh(job)
            job_id = job.id
        if geometry:
            paths.write_job_geometry(uid, job_id, {7: SECRET_LINE})
        return job_id

    def job(job_id) -> DBVideoJob:
        with Session(engine) as sess:
            return sess.get(DBVideoJob, job_id)

    return type("Env", (), dict(engine=engine, uid=uid, pid=pid, mail=mail,
                                make_job=staticmethod(make_job), job=staticmethod(job)))


def _fake_renderer(monkeypatch, *, raises=None, during=None):
    calls = []

    def render_video(*, job_id, user_info_id, project_id, request, out_path,
                     geometry, progress):
        calls.append(dict(job_id=job_id, geometry=geometry, request=request))
        progress(0.5, "frames")
        Path(out_path).write_bytes(b"\x00" * 2048)
        if during is not None:
            during(job_id)
        if raises is not None:
            raise raises
        return Path(out_path)

    monkeypatch.setattr(runner, "_renderer", lambda: render_video)
    return calls


def _geometry_exists(env, job_id) -> bool:
    return paths.geometry_path(env.uid, job_id).exists()


# ── run_video_job ────────────────────────────────────────────────────────────

def test_happy_path_renders_emails_and_deletes_the_geometry(env, monkeypatch):
    calls = _fake_renderer(monkeypatch)
    job_id = env.make_job()

    runner.run_video_job(job_id)

    job = env.job(job_id)
    assert job.status == "done"
    assert job.started_at is not None and job.progress == 1.0
    assert job.expires_at == pytest.approx(job.completed_at + 30 * 24 * 3600)
    assert Path(job.result_path) == paths.result_path(env.uid, job_id)
    assert Path(job.result_path).exists() and job.size_bytes == 2048
    assert calls[0]["geometry"] == {7: SECRET_LINE}
    assert not _geometry_exists(env, job_id)
    assert len(env.mail.sent) == 1
    assert f"https://app.example/video/{job.download_token}" in env.mail.sent[0].text_body
    assert SECRET_LINE not in env.mail.sent[0].text_body


def test_the_stored_camera_reaches_the_renderer(env, monkeypatch):
    """The job's request, camera included, is what the renderer gets
    (docs/VIDEO_CAMERA_QUALITY_PLAN.md D1); a row written before the field
    existed reaches it without one, which ``render_video`` reads as
    ``"variable"`` (tests/test_video_camera_render.py)."""
    calls = _fake_renderer(monkeypatch)
    old = env.make_job()
    new = env.make_job()
    with Session(env.engine) as sess:
        job = sess.get(DBVideoJob, new)
        job.request_json = json.dumps({"length_s": 30, "height": 720, "width": 1280,
                                       "camera": "overview"})
        sess.add(job); sess.commit()

    runner.run_video_job(old)
    runner.run_video_job(new)

    assert "camera" not in calls[0]["request"]
    assert calls[1]["request"]["camera"] == "overview"


@pytest.mark.parametrize("exc, reason", [
    (ValueError(f"bad line {SECRET_LINE}"), runner.REASON_RENDER_FAILED),
    (MemoryError(), runner.REASON_OUT_OF_MEMORY),
])
def test_a_failed_render_stores_a_fixed_reason_never_the_exception_text(
        env, monkeypatch, caplog, exc, reason):
    _fake_renderer(monkeypatch, raises=exc)
    job_id = env.make_job()

    with caplog.at_level(logging.DEBUG):
        runner.run_video_job(job_id)

    job = env.job(job_id)
    assert job.status == "failed"
    assert job.error_message == reason
    assert "bad line" not in job.error_message
    assert SECRET_LINE not in caplog.text
    assert "bad line" not in caplog.text  # logged by type and traceback only
    assert not _geometry_exists(env, job_id)
    assert not paths.result_path(env.uid, job_id).exists()  # partial MP4 removed
    assert len(env.mail.sent) == 1 and "could not be made" in env.mail.sent[0].subject
    assert SECRET_LINE not in env.mail.sent[0].text_body


def test_a_missing_renderer_fails_the_job_cleanly(env, monkeypatch):
    def _missing():
        raise ImportError("no module named src.video.renderer")
    monkeypatch.setattr(runner, "_renderer", _missing)
    job_id = env.make_job()

    runner.run_video_job(job_id)

    assert env.job(job_id).status == "failed"
    assert env.job(job_id).error_message == runner.REASON_RENDER_FAILED
    assert not _geometry_exists(env, job_id)


def test_a_job_failed_by_a_sweep_stays_failed_when_the_runner_picks_it_up(env, monkeypatch):
    calls = _fake_renderer(monkeypatch)
    job_id = env.make_job()
    runner.mark_video_job_interrupted(job_id, runner.REASON_NEVER_STARTED)
    assert len(env.mail.sent) == 1

    runner.run_video_job(job_id)

    job = env.job(job_id)
    assert job.status == "failed"
    assert job.error_message == runner.REASON_NEVER_STARTED
    assert calls == []
    assert not paths.result_path(env.uid, job_id).exists()
    assert not paths.video_dir(env.uid, job_id).exists()
    assert len(env.mail.sent) == 1  # no second email


def test_a_job_failed_while_rendering_discards_the_render_and_sends_nothing_more(
        env, monkeypatch):
    def sweep_meanwhile(job_id):
        runner.mark_video_job_interrupted(job_id, runner.REASON_TIMED_OUT)

    _fake_renderer(monkeypatch, during=sweep_meanwhile)
    job_id = env.make_job()

    runner.run_video_job(job_id)

    job = env.job(job_id)
    assert job.status == "failed" and job.error_message == runner.REASON_TIMED_OUT
    assert job.result_path is None
    assert not paths.result_path(env.uid, job_id).exists()
    assert not _geometry_exists(env, job_id)
    assert [m.subject for m in env.mail.sent] == ["Your video of My Trip could not be made"]


def test_progress_is_not_written_once_the_row_left_running(env, monkeypatch):
    def fail_then_progress(job_id):
        runner.mark_video_job_interrupted(job_id, runner.REASON_TIMED_OUT)

    seen = {}

    def render_video(*, job_id, progress, out_path, **_):
        fail_then_progress(job_id)
        progress(0.9, "encoding")
        seen["stage"] = env.job(job_id).stage
        return Path(out_path)

    monkeypatch.setattr(runner, "_renderer", lambda: render_video)
    job_id = env.make_job()
    runner.run_video_job(job_id)
    assert seen["stage"] != "encoding"
    assert env.job(job_id).status == "failed"


# ── mark_video_job_interrupted and the worker mapping ────────────────────────

def test_interrupted_fails_a_running_job_deletes_geometry_and_emails_once(env):
    job_id = env.make_job(status="running", started_at=time.time())

    runner.mark_video_job_interrupted(job_id, "killed")
    runner.mark_video_job_interrupted(job_id, "killed again")

    job = env.job(job_id)
    assert job.status == "failed" and job.error_message == "killed"
    assert not _geometry_exists(env, job_id)
    assert len(env.mail.sent) == 1


def test_interrupted_is_a_no_op_on_a_done_job(env):
    job_id = env.make_job(status="done", geometry=False)
    runner.mark_video_job_interrupted(job_id, "killed")
    assert env.job(job_id).status == "done"
    assert env.mail.sent == []


class _FakeRQJob:
    def __init__(self, args):
        self.args = args
        self.id = "rq-1"


def test_a_killed_video_work_horse_fails_its_job(env):
    job_id = env.make_job(status="running", started_at=time.time())

    worker_mod._work_horse_killed_handler(
        _FakeRQJob((runner.run_video_job, job_id)), 123, 9, None)

    job = env.job(job_id)
    assert job.status == "failed"
    assert "likely out of memory" in job.error_message
    assert not _geometry_exists(env, job_id)


# ── worker-startup sweep ─────────────────────────────────────────────────────

def test_worker_startup_sweep_fails_running_rows_and_leaves_pending_ones(env):
    running = env.make_job(status="running", started_at=time.time())
    pending = env.make_job(status="pending")

    assert runner.sweep_stale_running_video_jobs() == 1

    assert env.job(running).status == "failed"
    assert env.job(running).error_message == runner.REASON_WORKER_RESTARTED
    assert not _geometry_exists(env, running)
    assert env.job(pending).status == "pending"
    assert _geometry_exists(env, pending)


class _FakeWorker:
    order = []

    def __init__(self, queues, connection=None, work_horse_killed_handler=None):
        self.queues = queues

    def work(self, with_scheduler):
        _FakeWorker.order.append(("work", tuple(self.queues)))


@pytest.mark.parametrize("queues, swept", [(["video"], True), (["resolve", "poster"], False),
                                           ([], True)])
def test_the_worker_sweeps_only_when_it_consumes_video_and_before_working(
        monkeypatch, queues, swept):
    _FakeWorker.order = []
    monkeypatch.setenv("REDIS_URL", "redis://example:6379/0")
    monkeypatch.setattr(worker_mod, "_connect_with_retry", lambda: object())
    monkeypatch.setattr("rq.Worker", _FakeWorker)
    monkeypatch.setattr(runner, "sweep_stale_running_video_jobs",
                        lambda: _FakeWorker.order.append(("sweep",)))

    assert worker_mod.main(queues) == 0

    names = [step[0] for step in _FakeWorker.order]
    assert names == (["sweep", "work"] if swept else ["work"])


# ── hourly sweep ─────────────────────────────────────────────────────────────

def test_hourly_sweep_fails_stale_jobs_by_age_and_leaves_younger_ones(env):
    now = time.time()
    old_running = env.make_job(status="running", started_at=now - runner.JOB_TIMEOUT_S - 301)
    young_running = env.make_job(status="running", started_at=now - runner.JOB_TIMEOUT_S - 60)
    old_pending = env.make_job(status="pending", created_at=now - 24 * 3600 - 60)
    young_pending = env.make_job(status="pending", created_at=now - 23 * 3600)

    result = runner.sweep_video_jobs(now=now)

    assert result["failed"] == 2
    assert env.job(old_running).status == "failed"
    assert env.job(old_running).error_message == runner.REASON_TIMED_OUT
    assert env.job(old_pending).status == "failed"
    assert env.job(old_pending).error_message == runner.REASON_NEVER_STARTED
    assert env.job(young_running).status == "running"
    assert env.job(young_pending).status == "pending"
    assert not _geometry_exists(env, old_running)
    assert not _geometry_exists(env, old_pending)


def test_hourly_sweep_never_deletes_geometry_of_a_pending_or_running_job(env):
    """However old the file, and however long past the job timeout."""
    now = time.time()
    pending = env.make_job(status="pending", created_at=now - 23 * 3600)
    running = env.make_job(status="running", started_at=now - 10 * 60)
    for job_id in (pending, running):
        old = now - 10 * runner.JOB_TIMEOUT_S
        os.utime(paths.geometry_path(env.uid, job_id), (old, old))

    runner.sweep_video_jobs(now=now)

    assert _geometry_exists(env, pending)
    assert _geometry_exists(env, running)


def test_hourly_sweep_deletes_geometry_left_by_a_terminal_or_missing_job(env):
    done = env.make_job(status="done")
    failed = env.make_job(status="failed")
    orphan = paths.write_job_geometry(env.uid, 999_999, {1: SECRET_LINE})

    result = runner.sweep_video_jobs()

    assert result["geometry_deleted"] == 3
    assert not _geometry_exists(env, done)
    assert not _geometry_exists(env, failed)
    assert not orphan.exists()


def test_hourly_sweep_expires_old_videos_and_deletes_the_file(env):
    now = time.time()
    expired = env.make_job(status="done", geometry=False, expires_at=now - 1)
    kept = env.make_job(status="done", geometry=False, expires_at=now + 3600)
    for job_id in (expired, kept):
        out = paths.result_path(env.uid, job_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")

    assert runner.sweep_video_jobs(now=now)["expired"] == 1

    assert env.job(expired).status == "expired"
    assert not paths.result_path(env.uid, expired).exists()
    assert env.job(kept).status == "done"
    assert paths.result_path(env.uid, kept).exists()
    assert env.mail.sent == []


# ── paths ────────────────────────────────────────────────────────────────────

def test_geometry_round_trips_and_delete_is_idempotent(env):
    path = paths.write_job_geometry(env.uid, 5, {3: SECRET_LINE})
    assert path.parent == paths.video_dir(env.uid, 5)
    assert paths.read_job_geometry(env.uid, 5) == {3: SECRET_LINE}
    paths.delete_job_geometry(5)
    paths.delete_job_geometry(5)
    assert paths.read_job_geometry(env.uid, 5) is None

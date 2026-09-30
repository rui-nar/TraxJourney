"""Trip video preview endpoints (docs/VIDEO_PREVIEW_PLAN.md, U3).

Runs on the same app and file-backed SQLite fixture as tests/test_video_api.py:
the concurrency test sends two requests from two threads, which an in-memory
StaticPool database can't serve. The rate limit's clock is ``api.video._now``;
the tests that check ``Retry-After`` pin it, so the early and the locked check
read the same instant and the value is exact (review U1R3-1).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest
from sqlmodel import Session

import api.video as video_api
import src.video.job_runner as runner
from api.deps import get_current_user
from api.router import app
from models.project_db import DBVideoJob
from src.video import paths
from tests.test_video_api import (  # noqa: F401 — env and fake_broker are fixtures
    SECRET_ENDPOINTS,
    SECRET_TRACK,
    _free_plan_enforced,
    _jobs,
    _spy_decode,
    env,
    fake_broker,
)

WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(range(24))
GEOMETRY = {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS}


def _fake_preview_renderer(monkeypatch, *, raises=None):
    seen = []

    def render_preview(*, job_id, user_info_id, project_id, request, out_path,
                       geometry, progress):
        seen.append(dict(geometry=geometry, request=request, project_id=project_id))
        progress(0.5, "rendering")
        Path(out_path).write_bytes(WEBP)
        if raises is not None:
            raise raises
        return Path(out_path)

    monkeypatch.setattr(runner, "_preview_renderer", lambda: render_preview)
    return seen


def _run_previews_inline(env, monkeypatch):
    """Run what is queued on ``default`` at once; queue anything else."""
    def _enqueue(queue, func, *args, **kw):
        env.enqueued.append((queue, func, args, kw))
        if queue == "default":
            func(*args)
        return True
    monkeypatch.setattr(video_api, "enqueue", _enqueue)


def _fixed_now(monkeypatch, now=None) -> float:
    now = time.time() if now is None else now
    monkeypatch.setattr(video_api, "_now", lambda: now)
    return now


def _seed(env, created_ats, *, uid=None, status="done", started=True):
    """Preview rows of the requester, as the rate limit reads them."""
    with Session(env.engine) as sess:
        for created in created_ats:
            sess.add(DBVideoJob(
                project_id=env.trip_id, user_info_id=uid or env.owner, kind="preview",
                status=status, request_json="{}", created_at=created,
                started_at=created + 1 if started else None,
                completed_at=created + 30 if status in ("done", "failed") else None))
        sess.commit()


def _previews(env):
    return [j for j in _jobs(env) if j.kind == "preview"]


def _post(env, trip="Trip", **body):
    return env.client.post(f"/api/projects/{trip}/video/preview",
                           json={"length_s": 30, **body})


# ── happy path ───────────────────────────────────────────────────────────────

def test_happy_path_queues_on_default_renders_and_serves_the_webp(env, monkeypatch):
    seen = _fake_preview_renderer(monkeypatch)
    _run_previews_inline(env, monkeypatch)

    r = _post(env, height=1080, camera="overview")
    assert r.status_code == 201, r.text
    job_id = r.json()["job_id"]

    queue, func, args, kw = env.enqueued[0]
    assert (queue, func, args) == ("default", runner.run_video_preview_job, (job_id,))
    assert kw == {"max_retries": 0, "allow_inline": False, "job_timeout": 300}
    assert seen[0]["request"] == {"length_s": 30, "camera": "overview",
                                  "width": 1920, "height": 1080}
    assert seen[0]["geometry"] is None

    [job] = _jobs(env)
    assert job.kind == "preview" and job.download_token is None
    assert json.loads(job.request_json) == seen[0]["request"]

    status = env.client.get(f"/api/projects/Trip/video/preview/{job_id}").json()
    assert status["status"] == "done" and status["progress"] == 1.0
    assert status["expires_at"] == pytest.approx(job.completed_at + 3600)

    r = env.client.get(f"/api/projects/Trip/video/preview/{job_id}/bytes")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"
    assert r.content == WEBP
    assert env.mail == []


def test_status_and_bytes_before_done(env):
    job_id = _post(env).json()["job_id"]
    assert env.client.get(
        f"/api/projects/Trip/video/preview/{job_id}").json()["status"] == "pending"
    assert env.client.get(
        f"/api/projects/Trip/video/preview/{job_id}/bytes").status_code == 404


@pytest.mark.parametrize("body", [{"length_s": 45}, {"length_s": 30, "camera": "zoom"},
                                  {"length_s": 30, "height": 480}])
def test_an_invalid_body_is_422_with_nothing_written(env, body):
    r = env.client.post("/api/projects/Trip/video/preview", json=body)
    assert r.status_code == 422, r.text
    assert _jobs(env) == [] and env.enqueued == []


def test_nothing_to_animate_is_422(env):
    assert _post(env, trip="Empty").status_code == 422
    assert _jobs(env) == []


# ── free, and never a video (D3, D7) ─────────────────────────────────────────

def test_a_preview_is_free_on_free_and_not_limited_by_the_plans_height(env, monkeypatch):
    _free_plan_enforced(monkeypatch)
    _fake_preview_renderer(monkeypatch)
    _run_previews_inline(env, monkeypatch)

    assert _post(env, height=1080).status_code == 201
    assert _post(env).status_code == 201
    assert [j.status for j in _previews(env)] == ["done", "done"]

    plan = env.client.post("/api/projects/Trip/video/plan", json={}).json()
    assert plan["quota"] == {"limit": 1, "used": 0, "remaining": 1}
    # The one free video is still there, and it is the only one.
    assert env.client.post("/api/projects/Trip/video", json={"length_s": 30}).status_code == 201
    assert env.client.post("/api/projects/Trip/video", json={"length_s": 30}).status_code == 402
    assert env.mail == []


def test_a_failed_preview_sends_no_email_and_deletes_its_geometry(env, monkeypatch):
    _fake_preview_renderer(monkeypatch, raises=ValueError(f"bad {SECRET_TRACK}"))
    _run_previews_inline(env, monkeypatch)

    r = _post(env, trip="Secret", decrypted_geometry=GEOMETRY)
    assert r.status_code == 201, r.text
    [job] = _jobs(env)
    assert job.status == "failed" and job.error_message == runner.REASON_RENDER_FAILED
    assert not paths.video_dir(env.owner, job.id).exists()
    assert env.mail == []


# ── consent (D8) ─────────────────────────────────────────────────────────────

def test_an_encrypted_trip_is_409_with_previews_left(env, monkeypatch):
    now = _fixed_now(monkeypatch)
    _seed(env, [now - 60, now - 50, now - 40])

    r = _post(env, trip="Secret")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "consent_required"
    assert detail["consent_required"] == [201, 202]
    assert detail["previews_left"] == 7
    assert detail["available"] is True
    assert len(_previews(env)) == 3 and env.enqueued == []


def test_consent_geometry_is_used_then_deleted_when_done(env, monkeypatch):
    seen = _fake_preview_renderer(monkeypatch)
    r = _post(env, trip="Secret", decrypted_geometry=GEOMETRY)
    assert r.status_code == 201, r.text
    job_id = r.json()["job_id"]

    [job] = _jobs(env)
    assert SECRET_TRACK not in job.request_json and SECRET_ENDPOINTS not in job.request_json
    assert paths.read_job_geometry(env.owner, job_id) == {201: SECRET_TRACK,
                                                          202: SECRET_ENDPOINTS}
    runner.run_video_preview_job(job_id)
    assert seen[0]["geometry"] == {201: SECRET_TRACK, 202: SECRET_ENDPOINTS}
    assert not paths.geometry_path(env.owner, job_id).exists()
    assert env.client.get(
        f"/api/projects/Secret/video/preview/{job_id}").json()["status"] == "done"


@pytest.mark.parametrize("state, age_field, age", [
    ("running", "started_at", 300 + 5 * 60 + 1),
    ("pending", "created_at", 3600 + 1),
])
def test_consent_geometry_is_deleted_when_the_hourly_sweep_fails_a_preview(
        env, state, age_field, age):
    job_id = _post(env, trip="Secret", decrypted_geometry=GEOMETRY).json()["job_id"]
    now = time.time()
    with Session(env.engine) as sess:
        job = sess.get(DBVideoJob, job_id)
        job.status = state
        setattr(job, age_field, now - age)
        sess.add(job); sess.commit()
    assert paths.geometry_path(env.owner, job_id).exists()

    assert runner.sweep_video_jobs(now=now)["failed"] == 1

    assert _jobs(env)[0].status == "failed"
    assert not paths.geometry_path(env.owner, job_id).exists()
    assert env.mail == []


def test_the_video_startup_sweep_leaves_a_running_preview_and_its_geometry(env):
    job_id = _post(env, trip="Secret", decrypted_geometry=GEOMETRY).json()["job_id"]
    with Session(env.engine) as sess:
        job = sess.get(DBVideoJob, job_id)
        job.status, job.started_at = "running", time.time()
        sess.add(job); sess.commit()

    assert runner.sweep_stale_running_video_jobs() == 0

    assert _jobs(env)[0].status == "running"
    assert paths.geometry_path(env.owner, job_id).exists()


def test_logs_never_contain_the_consent_geometry(env, monkeypatch, caplog):
    _fake_preview_renderer(monkeypatch, raises=ValueError(f"bad {SECRET_TRACK}"))
    _run_previews_inline(env, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        _post(env, trip="Secret", decrypted_geometry=GEOMETRY)
        _post(env, trip="Trip", decrypted_geometry={"101": SECRET_TRACK})
    assert len(caplog.records) > 0
    for secret in (SECRET_TRACK, SECRET_ENDPOINTS):
        assert secret not in caplog.text


# ── rate limit (D6) ──────────────────────────────────────────────────────────

def test_the_eleventh_preview_in_an_hour_is_429_until_a_slot_frees(env, monkeypatch):
    now = _fixed_now(monkeypatch)
    # The oldest leaves the window at now + 599.5 s.
    _seed(env, [now - 3000.5 + 10 * i for i in range(10)])

    r = _post(env)
    assert r.status_code == 429, r.text
    assert r.headers["Retry-After"] == "600"
    detail = r.json()["detail"]
    assert detail["code"] == "preview_rate_limited"
    assert detail["retry_after_s"] == 600 and detail["limit"] == 10
    assert len(_previews(env)) == 10 and env.enqueued == []

    _fixed_now(monkeypatch, now + 599)
    r = _post(env)
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"

    _fixed_now(monkeypatch, now + 600)
    assert _post(env).status_code == 201
    assert len(_previews(env)) == 11


def test_the_locked_check_gives_the_early_checks_retry_after(env, monkeypatch):
    """A preview committed between the early check and the lock: the locked
    check refuses it with the same Retry-After the early one would give."""
    now = _fixed_now(monkeypatch)
    _seed(env, [now - 3000.5 + 10 * i for i in range(9)])
    real_lock = video_api.lock_account

    def lock_after_a_racing_insert(sess, uid):
        _seed(env, [now - 100])
        real_lock(sess, uid)

    monkeypatch.setattr(video_api, "lock_account", lock_after_a_racing_insert)
    r = _post(env)
    assert r.status_code == 429, r.text
    assert r.headers["Retry-After"] == "600"
    assert r.json()["detail"]["retry_after_s"] == 600

    monkeypatch.setattr(video_api, "lock_account", real_lock)
    early = _post(env)
    assert early.status_code == 429 and early.headers["Retry-After"] == "600"
    assert len(_previews(env)) == 10 and env.enqueued == []


def test_two_concurrent_previews_at_nine_make_exactly_one_job(env, monkeypatch):
    """The account lock serialises them: without it both count 9 in the window
    below, and both insert."""
    now = _fixed_now(monkeypatch)
    _seed(env, [now - 100 - i for i in range(9)])
    barrier = threading.Barrier(2)
    real_lock, real_slot = video_api.lock_account, video_api.preview_slot_frees_at

    def lock(sess, uid):
        barrier.wait(timeout=10)
        real_lock(sess, uid)

    def slow_slot(sess, uid, now=None, limit=10):
        frees_at = real_slot(sess, uid, now, limit)
        time.sleep(0.3)  # the window a racing request would slip into
        return frees_at

    monkeypatch.setattr(video_api, "lock_account", lock)
    monkeypatch.setattr(video_api, "preview_slot_frees_at", slow_slot)

    codes = []

    def post():
        codes.append(_post(env).status_code)

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(codes) == [201, 429]
    assert len(_previews(env)) == 10


def test_over_the_limit_on_an_encrypted_trip_is_429_before_any_consent(env, monkeypatch):
    now = _fixed_now(monkeypatch)
    _seed(env, [now - 60 - i for i in range(10)])
    calls = _spy_decode(monkeypatch)

    r = _post(env, trip="Secret")
    assert r.status_code == 429, r.text
    r = _post(env, trip="Secret", decrypted_geometry=GEOMETRY)
    assert r.status_code == 429, r.text
    assert calls == []
    assert len(_previews(env)) == 10
    assert not any(paths.geometry_path(env.owner, j.id).exists() for j in _jobs(env))


@pytest.mark.parametrize("state", ["pending", "running"])
def test_a_preview_left_in_flight_over_15_minutes_stops_counting(env, monkeypatch, state):
    now = _fixed_now(monkeypatch)
    _seed(env, [now - 60 - i for i in range(9)])
    _seed(env, [now - 899], status=state, started=state == "running")
    assert _post(env).status_code == 429

    _fixed_now(monkeypatch, now + 2)   # the in-flight one is now 901 s old
    assert _post(env).status_code == 201


def test_the_limit_is_per_requester(env, monkeypatch):
    now = _fixed_now(monkeypatch)
    _seed(env, [now - 60 - i for i in range(10)])
    assert _post(env).status_code == 429
    env.who["uid"] = env.friend
    assert env.client.post(f"/api/projects/Trip/video/preview?owner={env.owner}",
                           json={"length_s": 30}).status_code == 201


# ── companions (R1-5) ────────────────────────────────────────────────────────

def test_a_companion_creates_polls_and_fetches_a_preview_of_a_shared_trip(env, monkeypatch):
    _fake_preview_renderer(monkeypatch)
    _run_previews_inline(env, monkeypatch)
    env.who["uid"] = env.friend
    q = f"?owner={env.owner}"

    r = env.client.post(f"/api/projects/Trip/video/preview{q}", json={"length_s": 30})
    assert r.status_code == 201, r.text
    job_id = r.json()["job_id"]
    [job] = _jobs(env)
    assert job.user_info_id == env.friend and job.project_id == env.trip_id

    status = env.client.get(f"/api/projects/Trip/video/preview/{job_id}{q}")
    assert status.status_code == 200 and status.json()["status"] == "done"
    r = env.client.get(f"/api/projects/Trip/video/preview/{job_id}/bytes{q}")
    assert r.status_code == 200 and r.content == WEBP

    # Not the owner's, and not a stranger's.
    env.who["uid"] = env.owner
    assert env.client.get(f"/api/projects/Trip/video/preview/{job_id}").status_code == 404
    env.who["uid"] = env.stranger
    assert env.client.get(f"/api/projects/Trip/video/preview/{job_id}{q}").status_code == 404
    assert env.client.post(f"/api/projects/Trip/video/preview{q}",
                           json={"length_s": 30}).status_code == 404


# ── availability (D5) ────────────────────────────────────────────────────────

def test_no_default_worker_is_503_with_nothing_written(env, monkeypatch):
    asked = []

    def has_workers(name):
        asked.append(name)
        return name != "default"
    monkeypatch.setattr(video_api, "queue_has_workers", has_workers)

    r = _post(env, trip="Secret", decrypted_geometry=GEOMETRY)
    assert r.status_code == 503
    assert asked == ["default"]
    assert _jobs(env) == [] and env.enqueued == []
    assert not (paths._DATA_DIR / "users").exists()


def test_previews_need_a_default_worker_not_a_video_one_nor_ffmpeg(env, fake_broker, monkeypatch):
    import fakeredis
    from rq import Queue, Worker

    monkeypatch.setattr(video_api, "ffmpeg_available", lambda: False)
    connection = fakeredis.FakeStrictRedis(server=fake_broker)
    Worker([Queue("video", connection=connection)], connection=connection).register_birth()
    assert _post(env).status_code == 503

    Worker([Queue("default", connection=connection)], connection=connection).register_birth()
    assert _post(env).status_code == 201


def test_an_enqueue_failure_fails_the_row_deletes_the_geometry_and_costs_no_slot(
        env, monkeypatch):
    now = _fixed_now(monkeypatch)
    _seed(env, [now - 60 - i for i in range(9)])
    monkeypatch.setattr(video_api, "enqueue", lambda *a, **k: False)

    r = _post(env, trip="Secret", decrypted_geometry=GEOMETRY)
    assert r.status_code == 503
    job = _previews(env)[-1]
    assert job.status == "failed" and job.error_message == runner.REASON_UNAVAILABLE
    assert not paths.geometry_path(env.owner, job.id).exists()
    assert env.mail == []

    monkeypatch.setattr(video_api, "enqueue", lambda *a, **k: True)
    assert _post(env).status_code == 201


# ── full-video routes ignore previews ────────────────────────────────────────

def test_video_routes_404_on_a_preview_and_preview_routes_on_a_video(env, monkeypatch):
    _fake_preview_renderer(monkeypatch)
    _run_previews_inline(env, monkeypatch)
    preview_id = _post(env).json()["job_id"]
    video_id = env.client.post("/api/projects/Trip/video", json={"length_s": 30}).json()["job_id"]

    assert env.client.get(f"/api/projects/Trip/video/{preview_id}").status_code == 404
    assert env.client.get(f"/api/projects/Trip/video/{preview_id}/download").status_code == 404
    assert env.client.get(f"/api/projects/Trip/video/preview/{video_id}").status_code == 404
    assert env.client.get(
        f"/api/projects/Trip/video/preview/{video_id}/bytes").status_code == 404

    # Even a preview row given a token is never reached by the token routes.
    with Session(env.engine) as sess:
        job = sess.get(DBVideoJob, preview_id)
        job.download_token = "preview-token"
        sess.add(job); sess.commit()
    app.dependency_overrides.pop(get_current_user, None)
    assert env.client.get("/api/video/preview-token").status_code == 404
    assert env.client.get("/api/video/preview-token/download").status_code == 404

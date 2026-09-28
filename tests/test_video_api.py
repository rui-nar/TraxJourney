"""Trip video endpoints (docs/TRIP_VIDEO_PLAN.md, U6).

Runs against the real app (for the 402 mapping and the request logging) on a
file-backed SQLite database: the concurrency test sends two requests from two
threads, which an in-memory StaticPool database can't serve.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time

import polyline as polyline_lib
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import api.video as video_api
import models.db as db_module
import src.video.job_runner as runner
from api.deps import get_current_user
from api.router import app
from models.project_db import (
    DBActivity,
    DBProject,
    DBProjectItem,
    DBProjectMember,
    DBVideoJob,
)
from models.user import UserInfo
from src.video import paths
from src.video.timeline import timeline_for_project

ENVELOPE = "v1.d3JhcHBlZA.Y2lwaGVy"
PLAIN_LINE = polyline_lib.encode([(48.0, 2.0), (48.01, 2.01), (48.02, 2.03)])
# What the client sends after consent: the encrypted track, and the 2-point
# line of the trackless activity whose endpoints are encrypted.
SECRET_TRACK = polyline_lib.encode([(45.123456, 6.654321), (45.2, 6.7), (45.3, 6.8)])
SECRET_ENDPOINTS = polyline_lib.encode([(46.111111, 7.222222), (46.3, 7.4)])

_BILLING_ENV = ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_VIDEOS_PER_MONTH", "FREE_MAX_VIDEO_HEIGHT")


def _activity(sess, id, uid, **kw):
    base = dict(id=id, user_info_id=uid, name=f"act {id}", type="Ride",
                distance=5000.0, moving_time=1200, elapsed_time=1300,
                start_date="2026-06-10T09:00:00Z", start_date_local="2026-06-10T09:00:00Z",
                start_latlng_json="[48.0, 2.0]", end_latlng_json="[48.02, 2.03]")
    base.update(kw)
    sess.add(DBActivity(**base))


def _trip(sess, uid, name, activity_ids):
    proj = DBProject(user_info_id=uid, name=name)
    sess.add(proj); sess.commit(); sess.refresh(proj)
    for pos, aid in enumerate(activity_ids):
        sess.add(DBProjectItem(project_id=proj.id, position=pos, uid=f"{name}-{aid}",
                               item_type="activity", activity_id=aid))
    sess.commit()
    return proj.id


class _Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(paths, "_DATA_DIR", tmp_path / "data")
    for var in _BILLING_ENV:
        monkeypatch.delenv(var, raising=False)

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@example.com")
        friend = UserInfo(display_name="Friend", email="friend@example.com")
        stranger = UserInfo(display_name="Stranger", email="stranger@example.com")
        sess.add_all([owner, friend, stranger]); sess.commit()
        for u in (owner, friend, stranger):
            sess.refresh(u)
        e = _Env()
        e.owner, e.friend, e.stranger = owner.id, friend.id, stranger.id

        _activity(sess, 101, e.owner, summary_polyline=PLAIN_LINE)
        # Encrypted track, and a trackless activity with encrypted endpoints.
        _activity(sess, 201, e.owner, summary_polyline=ENVELOPE,
                  start_latlng_json=ENVELOPE, end_latlng_json=ENVELOPE)
        _activity(sess, 202, e.owner, summary_polyline=None,
                  start_latlng_json=ENVELOPE, end_latlng_json=ENVELOPE)
        sess.commit()
        e.trip_id = _trip(sess, e.owner, "Trip", [101])
        e.secret_id = _trip(sess, e.owner, "Secret", [201, 202])
        _trip(sess, e.owner, "Empty", [])
        sess.add(DBProjectMember(project_id=e.trip_id, user_info_id=e.friend, role="viewer",
                               invited_by=e.owner))
        sess.commit()

    e.engine = engine
    e.who = {"uid": e.owner}
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(e.who["uid"])}

    # A broker is there; the job is queued, not run, unless a test says so.
    e.enqueued = []
    monkeypatch.setattr(video_api, "queue_available", lambda: True)
    monkeypatch.setattr(video_api, "ffmpeg_available", lambda: True)

    def _enqueue(queue, func, *args, **kw):
        e.enqueued.append((queue, func, args, kw))
        return True
    monkeypatch.setattr(video_api, "enqueue", _enqueue)

    e.mail = []

    class _Mail:
        async def send(self, message):
            e.mail.append(message)
    monkeypatch.setattr(runner, "get_email_service", lambda: _Mail())

    e.client = TestClient(app)
    try:
        yield e
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _jobs(env):
    with Session(env.engine) as sess:
        return sess.exec(select(DBVideoJob).order_by(DBVideoJob.id)).all()


def _free_plan_enforced(monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")


def _fake_renderer(monkeypatch):
    seen = []

    def render_video(*, job_id, user_info_id, project_id, request, out_path,
                     geometry, progress):
        seen.append(dict(geometry=geometry, request=request, project_id=project_id))
        progress(0.5, "frames")
        out_path.write_bytes(bytes(range(256)) * 8)
        return out_path

    monkeypatch.setattr(runner, "_renderer", lambda: render_video)
    return seen


def _run_inline(env, monkeypatch):
    def _enqueue(queue, func, *args, **kw):
        env.enqueued.append((queue, func, args, kw))
        func(*args)
        return True
    monkeypatch.setattr(video_api, "enqueue", _enqueue)


# ── happy path, status, download, token routes ───────────────────────────────

def test_happy_path_renders_and_downloads(env, monkeypatch):
    seen = _fake_renderer(monkeypatch)
    _run_inline(env, monkeypatch)

    r = env.client.post("/api/projects/Trip/video", json={"length_s": 30})
    assert r.status_code == 201, r.text
    job_id = r.json()["job_id"]

    queue, func, args, kw = env.enqueued[0]
    assert (queue, func, args) == ("video", runner.run_video_job, (job_id,))
    assert kw == {"max_retries": 0, "allow_inline": False, "job_timeout": 1800}
    assert seen[0]["request"] == {"length_s": 30, "height": 720, "width": 1280}
    assert seen[0]["geometry"] is None

    status = env.client.get(f"/api/projects/Trip/video/{job_id}").json()
    assert status["status"] == "done" and status["progress"] == 1.0
    assert status["error_message"] is None and status["expires_at"] is not None

    r = env.client.get(f"/api/projects/Trip/video/{job_id}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert len(r.content) == 2048
    r = env.client.get(f"/api/projects/Trip/video/{job_id}/download",
                       headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == bytes(range(10))
    assert len(env.mail) == 1 and "is ready" in env.mail[0].subject


def test_token_routes_serve_status_and_file_without_a_session(env, monkeypatch):
    _fake_renderer(monkeypatch)
    job_id = env.client.post("/api/projects/Trip/video", json={"length_s": 60}).json()["job_id"]
    with Session(env.engine) as sess:
        token = sess.get(DBVideoJob, job_id).download_token

    app.dependency_overrides.pop(get_current_user, None)
    assert env.client.get(f"/api/video/{token}").json()["status"] == "pending"
    assert env.client.get(f"/api/video/{token}/download").status_code == 404  # not ready
    assert env.client.get("/api/video/not-a-token").status_code == 404
    assert env.client.get("/api/video/not-a-token/download").status_code == 404

    runner.run_video_job(job_id)
    assert env.client.get(f"/api/video/{token}").json()["status"] == "done"
    r = env.client.get(f"/api/video/{token}/download", headers={"Range": "bytes=2-3"})
    assert r.status_code == 206 and r.content == bytes([2, 3])


def test_ownership_404s(env):
    job_id = env.client.post("/api/projects/Trip/video", json={"length_s": 30}).json()["job_id"]

    # A stranger can't reach the trip, with or without ?owner=.
    env.who["uid"] = env.stranger
    assert env.client.get(f"/api/projects/Trip/video/{job_id}").status_code == 404
    assert env.client.get(
        f"/api/projects/Trip/video/{job_id}?owner={env.owner}").status_code == 404
    assert env.client.post(f"/api/projects/Trip/video?owner={env.owner}",
                           json={"length_s": 30}).status_code == 404

    # A companion reaches the trip but not the owner's job.
    env.who["uid"] = env.friend
    url = f"/api/projects/Trip/video/{job_id}?owner={env.owner}"
    assert env.client.get(url).status_code == 404
    assert env.client.get(
        f"/api/projects/Trip/video/{job_id}/download?owner={env.owner}").status_code == 404

    # The owner: an unknown job, a job under another trip's name, a job not done.
    env.who["uid"] = env.owner
    assert env.client.get("/api/projects/Trip/video/9999").status_code == 404
    assert env.client.get(f"/api/projects/Empty/video/{job_id}").status_code == 404
    assert env.client.get(f"/api/projects/Trip/video/{job_id}/download").status_code == 404


# ── validation ───────────────────────────────────────────────────────────────

def test_nothing_to_animate_is_422(env):
    for url in ("/api/projects/Empty/video", "/api/projects/Empty/video/plan"):
        r = env.client.post(url, json={"length_s": 30})
        assert r.status_code == 422, r.text
    assert _jobs(env) == []


@pytest.mark.parametrize("length", [0, 45, 120, "sixty"])
def test_length_other_than_30_60_90_is_422(env, length):
    for url in ("/api/projects/Trip/video", "/api/projects/Trip/video/plan"):
        assert env.client.post(url, json={"length_s": length}).status_code == 422
    assert _jobs(env) == []


def test_plan_reports_clips_quota_and_resolutions(env):
    r = env.client.post("/api/projects/Trip/video/plan", json={"length_s": 60})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["legs"] == 1 and len(body["clips"]) == 1
    assert body["clip_counts"] == {"30": 1, "60": 1, "90": 1}
    assert body["skipped"] == [] and body["consent_required"] == []
    assert body["quota"] == {"limit": None, "used": 0, "remaining": None}
    assert body["resolutions"] == [720, 1080]  # billing off ⇒ 1080p
    assert _jobs(env) == []


def test_plan_reports_unavailable_without_ffmpeg(env, monkeypatch):
    monkeypatch.setattr(video_api, "ffmpeg_available", lambda: False)
    assert env.client.post("/api/projects/Trip/video/plan", json={}).json()["available"] is False


# ── consent (D2) ─────────────────────────────────────────────────────────────

def test_an_encrypted_only_trip_gets_409_consent_not_422(env):
    for url in ("/api/projects/Secret/video", "/api/projects/Secret/video/plan"):
        r = env.client.post(url, json={"length_s": 30})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "consent_required"
        # The trackless activity with encrypted endpoints is listed too (R3-2).
        assert detail["consent_required"] == [201, 202]
    assert _jobs(env) == []


def test_consent_geometry_is_accepted_and_drawn(env, monkeypatch):
    geometry = {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS}
    plan = env.client.post("/api/projects/Secret/video/plan",
                           json={"length_s": 30, "decrypted_geometry": geometry})
    assert plan.status_code == 200, plan.text
    assert plan.json()["legs"] == 2 and plan.json()["skipped"] == []

    seen = _fake_renderer(monkeypatch)
    r = env.client.post("/api/projects/Secret/video",
                        json={"length_s": 30, "decrypted_geometry": geometry})
    assert r.status_code == 201, r.text
    job_id = r.json()["job_id"]

    job = _jobs(env)[0]
    assert SECRET_TRACK not in job.request_json and SECRET_ENDPOINTS not in job.request_json
    assert paths.read_job_geometry(env.owner, job_id) == {201: SECRET_TRACK,
                                                          202: SECRET_ENDPOINTS}
    runner.run_video_job(job_id)
    assert seen[0]["geometry"] == {201: SECRET_TRACK, 202: SECRET_ENDPOINTS}
    assert not paths.geometry_path(env.owner, job_id).exists()

    # The 2-point line of the trackless activity becomes a leg of its own.
    from src.project.project_repo import ProjectRepo
    with Session(env.engine) as sess:
        project = ProjectRepo().get_project(sess, env.owner, "Secret")
    legs = timeline_for_project(project, 30.0, geometry=seen[0]["geometry"]).legs
    trackless = next(leg for leg in legs if leg.ref == 202)
    assert trackless.coords == ((7.22222, 46.11111), (7.4, 46.3))


def test_partial_consent_is_still_409(env):
    r = env.client.post("/api/projects/Secret/video",
                        json={"length_s": 30, "decrypted_geometry": {"201": SECRET_TRACK}})
    assert r.status_code == 409
    assert r.json()["detail"]["consent_required"] == [202]


@pytest.mark.parametrize("trip, geometry", [
    ("Trip", {"101": SECRET_TRACK}),                                     # plaintext
    ("Secret", {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS,
                "101": SECRET_TRACK}),                                   # other trip's
    ("Secret", {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS,
                "999": SECRET_TRACK}),                                   # unknown
    ("Secret", {"201": SECRET_TRACK, "202": "not a polyline"}),         # invalid
])
def test_geometry_is_refused_for_plaintext_foreign_or_invalid_lines(env, trip, geometry):
    for url in (f"/api/projects/{trip}/video", f"/api/projects/{trip}/video/plan"):
        r = env.client.post(url, json={"length_s": 30, "decrypted_geometry": geometry})
        assert r.status_code == 422, r.text
        assert SECRET_TRACK not in r.text
    assert _jobs(env) == []
    assert not (paths._DATA_DIR / "users").exists()


def test_logs_never_contain_the_consent_geometry(env, monkeypatch, caplog):
    _fake_renderer(monkeypatch)
    _run_inline(env, monkeypatch)
    good = {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS}
    with caplog.at_level(logging.DEBUG):
        env.client.post("/api/projects/Secret/video/plan",
                        json={"length_s": 30, "decrypted_geometry": good})
        env.client.post("/api/projects/Secret/video",
                        json={"length_s": 30, "decrypted_geometry": good})
        env.client.post("/api/projects/Trip/video",
                        json={"length_s": 30, "decrypted_geometry": {"101": SECRET_TRACK}})
    assert len(caplog.records) > 0
    for secret in (SECRET_TRACK, SECRET_ENDPOINTS):
        assert secret not in caplog.text
        for record in caplog.records:
            assert secret not in record.getMessage()


# ── quota and resolution (D3, D10, D12) ──────────────────────────────────────

def test_a_second_free_video_is_402(env, monkeypatch):
    _free_plan_enforced(monkeypatch)
    assert env.client.post("/api/projects/Trip/video", json={"length_s": 30}).status_code == 201
    r = env.client.post("/api/projects/Trip/video", json={"length_s": 30})
    assert r.status_code == 402
    assert r.json()["resource"] == "videos"
    assert len(_jobs(env)) == 1

    plan = env.client.post("/api/projects/Trip/video/plan", json={}).json()
    assert plan["quota"] == {"limit": 1, "used": 1, "remaining": 0}


def test_1080p_on_free_is_402(env, monkeypatch):
    _free_plan_enforced(monkeypatch)
    for url in ("/api/projects/Trip/video", "/api/projects/Trip/video/plan"):
        r = env.client.post(url, json={"length_s": 30, "height": 1080})
        assert r.status_code == 402, r.text
        assert r.json()["resource"] == "video_height"
    assert _jobs(env) == []
    plan = env.client.post("/api/projects/Trip/video/plan", json={"height": 720}).json()
    assert plan["resolutions"] == [720]


def test_a_companions_render_is_charged_to_the_companion(env, monkeypatch):
    _free_plan_enforced(monkeypatch)
    env.who["uid"] = env.friend
    url = f"/api/projects/Trip/video?owner={env.owner}"
    assert env.client.post(url, json={"length_s": 30}).status_code == 201
    job = _jobs(env)[0]
    assert job.user_info_id == env.friend and job.project_id == env.trip_id
    assert env.client.post(url, json={"length_s": 30}).status_code == 402

    # The owner's own allowance is untouched.
    env.who["uid"] = env.owner
    assert env.client.post("/api/projects/Trip/video", json={"length_s": 30}).status_code == 201


def test_a_failed_job_does_not_count(env, monkeypatch):
    _free_plan_enforced(monkeypatch)
    job_id = env.client.post("/api/projects/Trip/video", json={"length_s": 30}).json()["job_id"]
    runner.mark_video_job_interrupted(job_id, runner.REASON_NEVER_STARTED)
    assert env.client.post("/api/projects/Trip/video", json={"length_s": 30}).status_code == 201


def test_two_concurrent_free_posts_make_exactly_one_job(env, monkeypatch):
    """The account lock serialises them: without it both read 0 videos this
    month in the window below, and both insert."""
    _free_plan_enforced(monkeypatch)
    barrier = threading.Barrier(2)
    real_lock, real_quota = video_api.lock_account, video_api.ensure_video_quota

    def lock(sess, uid):
        barrier.wait(timeout=10)
        real_lock(sess, uid)

    def slow_quota(sess, uid, now=None):
        real_quota(sess, uid, now)
        time.sleep(0.3)  # the window a racing request would slip into

    monkeypatch.setattr(video_api, "lock_account", lock)
    monkeypatch.setattr(video_api, "ensure_video_quota", slow_quota)

    codes = []

    def post():
        codes.append(env.client.post("/api/projects/Trip/video",
                                     json={"length_s": 30}).status_code)

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(codes) == [201, 402]
    assert len(_jobs(env)) == 1


# ── no broker, enqueue failure (D7) ──────────────────────────────────────────

def test_no_broker_is_503_with_no_row_and_no_file(env, monkeypatch):
    monkeypatch.setattr(video_api, "queue_available", lambda: False)
    r = env.client.post("/api/projects/Secret/video", json={
        "length_s": 30, "decrypted_geometry": {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS}})
    assert r.status_code == 503
    assert _jobs(env) == [] and env.enqueued == []
    assert not (paths._DATA_DIR / "users").exists()


def test_enqueue_failure_fails_the_row_and_deletes_the_geometry(env, monkeypatch):
    _free_plan_enforced(monkeypatch)
    monkeypatch.setattr(video_api, "enqueue", lambda *a, **k: False)
    r = env.client.post("/api/projects/Secret/video", json={
        "length_s": 30, "decrypted_geometry": {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS}})
    assert r.status_code == 503

    [job] = _jobs(env)
    assert job.status == "failed" and job.error_message == runner.REASON_UNAVAILABLE
    assert not paths.geometry_path(env.owner, job.id).exists()
    assert env.mail == []

    # It didn't cost the free video.
    monkeypatch.setattr(video_api, "enqueue", lambda *a, **k: True)
    assert env.client.post("/api/projects/Trip/video", json={"length_s": 30}).status_code == 201


def test_geometry_file_is_written_0600_after_the_row(env):
    r = env.client.post("/api/projects/Secret/video", json={
        "length_s": 30, "decrypted_geometry": {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS}})
    job_id = r.json()["job_id"]
    path = paths.geometry_path(env.owner, job_id)
    assert json.loads(path.read_text()) == {"201": SECRET_TRACK, "202": SECRET_ENDPOINTS}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

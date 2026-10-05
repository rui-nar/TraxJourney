"""Tests for the asynchronous segment route-resolution flow.

The resolve-route endpoint no longer runs HAFAS + Overpass synchronously (which
caused proxy 504s on long routes). Instead it marks the segment
``route_status="pending"`` and returns 202; a background task
(``_resolve_route_job``) does the slow work and writes the result back.

Covers:
  1. Trigger returns 202 and marks the segment pending (job mocked out)
  2. Trigger 404s for a missing segment, 400s for a non-transport segment
  3. _resolve_route_job success path writes polyline + route_status="resolved"
  4. _resolve_route_job failure path writes route_status="failed" + route_error
     and leaves the great-circle geometry intact
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import api.segments as segments_mod
from api.deps import get_current_user
from api.segments import router as segments_router, _compute_segment_geometry, _resolve_route_job
from models.project_db import DBProject, DBProjectItem
from models.user import UserInfo
from src.models.project import ConnectingSegment, ProjectItem, SegmentEndpoint
from src.project.project_io import ProjectIO
from src.project.project_repo import StaleWriteError, bump_lock_version


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_app(user_payload: dict) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: user_payload
    app.include_router(segments_router)
    return app


def _segment_json(seg: ConnectingSegment) -> str:
    item = ProjectItem(item_type="segment", segment=seg)
    return json.dumps(ProjectIO._serialise_item(item)["segment"])


def _add_segment(engine, project_id: int, seg: ConnectingSegment) -> None:
    with Session(engine) as sess:
        sess.add(DBProjectItem(
            project_id=project_id, position=0, item_type="segment",
            segment_json=_segment_json(seg),
        ))
        sess.commit()


def _load_segment(engine, user_id: int, name: str, seg_id: str) -> ConnectingSegment:
    project = segments_mod._repo.get_project(_open(engine), user_id, name)
    return next(s for i in project.items
               if (s := i.segment) and i.item_type == "segment" and s.id == seg_id)


def _open(engine) -> Session:
    return Session(engine)


def _read_segment_row(engine, project_id: int, seg_id: str) -> ConnectingSegment:
    """Read a segment straight off its DBProjectItem row, bypassing
    ProjectRepo.get_project — needed by tests that monkeypatch get_project
    itself and must inspect state without going back through it."""
    with Session(engine) as sess:
        row = sess.exec(
            select(DBProjectItem).where(
                DBProjectItem.project_id == project_id,
                DBProjectItem.item_type == "segment",
            )
        ).all()
    for item in row:
        seg = ConnectingSegment.from_dict(json.loads(item.segment_json or "{}"))
        if seg.id == seg_id:
            return seg
    raise AssertionError(f"segment {seg_id} not found")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def env(monkeypatch):
    """In-memory DB + TestClient wired to one user and one project."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    # Keep the geo-cache bust cheap and side-effect-free.
    monkeypatch.setattr(segments_mod, "bust_geo_cache", lambda *a, **k: None)

    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        user_id = user.id
        project = DBProject(user_info_id=user_id, name="My Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        project_id = project.id

    client = TestClient(_make_app({"sub": str(user_id), "email": "alice@example.com"}))
    yield client, user_id, project_id, test_engine


def _train_segment(seg_id: str = "seg-1") -> ConnectingSegment:
    return ConnectingSegment(
        id=seg_id, segment_type="train", label="Helsinki → Rovaniemi",
        start=SegmentEndpoint(60.1719, 24.9414),
        end=SegmentEndpoint(66.5039, 25.7294),
    )


# ── 1. Trigger returns 202 + pending ────────────────────────────────────────────

def test_trigger_returns_202_and_marks_pending(env, monkeypatch):
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _train_segment())

    # Mock the background job so the trigger is tested in isolation (TestClient
    # otherwise runs background tasks synchronously after the response).
    calls: list = []
    monkeypatch.setattr(segments_mod, "_resolve_route_job",
                        lambda *a: calls.append(a))

    resp = client.post(
        "/api/projects/My Trip/segments/seg-1/resolve-route",
        json={"hafas_provider": "vr", "train_number": "273"},
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "pending", "route_status": "pending"}

    seg = _load_segment(engine, user_id, "My Trip", "seg-1")
    assert seg.route_status == "pending"
    assert seg.route_started_at is not None
    assert seg.train_number == "273"
    # The background task was scheduled with the right identifiers.
    assert calls and calls[0][:3] == (user_id, "My Trip", "seg-1")


def test_trigger_rearms_the_automatic_retry_budget(env, monkeypatch):
    """A person tapping "retry" re-arms sweep_degraded_segments (issue #207).

    The *job* only resets this counter on a genuinely good result — resetting it
    on a degraded one is what stopped the hourly sweep ever terminating. The
    deliberate trigger is the other half of that rule: without it, a segment
    whose budget the sweep had already spent could never be picked up
    automatically again, however often the user retried by hand.
    """
    client, user_id, project_id, engine = env
    seg = _train_segment()
    seg.route_degrade_retries = 5
    seg.route_degraded = True
    _add_segment(engine, project_id, seg)
    monkeypatch.setattr(segments_mod, "_resolve_route_job", lambda *a: None)

    resp = client.post(
        "/api/projects/My Trip/segments/seg-1/resolve-route", json={})
    assert resp.status_code == 202

    stored = _load_segment(engine, user_id, "My Trip", "seg-1")
    assert stored.route_degrade_retries == 0


def test_trigger_404_for_missing_segment(env, monkeypatch):
    client, *_ = env
    monkeypatch.setattr(segments_mod, "_resolve_route_job", lambda *a: None)
    resp = client.post(
        "/api/projects/My Trip/segments/nope/resolve-route", json={})
    assert resp.status_code == 404


def test_trigger_400_for_non_transport_segment(env, monkeypatch):
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id,
                 ConnectingSegment(id="flt", segment_type="flight"))
    monkeypatch.setattr(segments_mod, "_resolve_route_job", lambda *a: None)
    resp = client.post(
        "/api/projects/My Trip/segments/flt/resolve-route", json={})
    assert resp.status_code == 400


# ── 2. Background job success ────────────────────────────────────────────────────

def test_job_success_writes_resolved(env, monkeypatch):
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _train_segment())

    fake_line = [[24.94, 60.17], [25.0, 62.0], [25.73, 66.50]]
    # (polyline, stop_count, degraded, strategy)
    monkeypatch.setattr(segments_mod, "_compute_segment_geometry",
                        lambda seg, params: (fake_line, 3, False, "relation_endpoints"))

    _resolve_route_job(user_id, "My Trip", "seg-1",
                       {"hafas_provider": "vr", "train_number": "273"})

    seg = _load_segment(engine, user_id, "My Trip", "seg-1")
    assert seg.route_status == "resolved"
    assert seg.route_mode == "rail"
    assert json.loads(seg.route_polyline) == fake_line
    assert seg.route_error is None
    assert seg.route_started_at is None
    assert seg.route_degraded is False


# ── 3. Background job failure ────────────────────────────────────────────────────
#
# HafasError/OverpassError from _compute_segment_geometry are the *expected*
# "we tried, no route exists" outcome and must still resolve to a clean
# "failed" write, exactly as before this fix (see the "unexpected exception"
# tests below for what changed).

def test_job_failure_marks_failed_and_keeps_great_circle(env, monkeypatch):
    from src.services.overpass_service import OverpassError

    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _train_segment())

    def _boom(seg, params):
        raise OverpassError("overpass exploded")

    monkeypatch.setattr(segments_mod, "_compute_segment_geometry", _boom)

    _resolve_route_job(user_id, "My Trip", "seg-1", {})

    seg = _load_segment(engine, user_id, "My Trip", "seg-1")
    assert seg.route_status == "failed"
    assert "overpass exploded" in seg.route_error
    # Geometry untouched → map still renders the great-circle arc.
    assert seg.route_mode == "great_circle"
    assert seg.route_polyline is None
    assert seg.route_started_at is None
    assert seg.route_degraded is False


# ── 3b. Crash safety net — never leave a segment stuck "pending" ─────────────────

def _pending_segment(seg_id: str = "seg-1") -> ConnectingSegment:
    seg = _train_segment(seg_id)
    seg.route_status = "pending"
    seg.route_started_at = "2026-06-14T10:00:00Z"
    return seg


def test_mark_segment_failed_flips_pending(env):
    """The safety-net helper flips a still-pending segment to failed."""
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _pending_segment())

    segments_mod._mark_segment_failed(user_id, "My Trip", "seg-1", "db boom")

    seg = _load_segment(engine, user_id, "My Trip", "seg-1")
    assert seg.route_status == "failed"
    assert seg.route_error == "db boom"
    assert seg.route_started_at is None


def test_mark_segment_failed_leaves_terminal_state_untouched(env):
    """It must not clobber a segment that already resolved (e.g. a late crash
    after a successful persist)."""
    client, user_id, project_id, engine = env
    seg = _train_segment()
    seg.route_status = "resolved"
    seg.route_mode = "rail"
    seg.route_polyline = json.dumps([[24.9, 60.1], [25.7, 66.5]])
    _add_segment(engine, project_id, seg)

    segments_mod._mark_segment_failed(user_id, "My Trip", "seg-1", "boom")

    seg = _load_segment(engine, user_id, "My Trip", "seg-1")
    assert seg.route_status == "resolved"  # unchanged
    assert seg.route_error is None


def test_job_unexpected_exception_propagates_for_rq_retry(env, monkeypatch):
    """An unexpected failure (a DB hiccup, unrelated to route lookup — here the
    project load raises a plain RuntimeError) must NOT be written as a clean
    "failed" verdict on the spot. It must propagate out of _resolve_route_job
    so RQ's configured retry/backoff (src/jobs/queue.py) actually gets a
    chance to retry a transient failure, rather than every such failure being
    silently converted into a permanent result before RQ ever sees it.

    The segment is left "pending" (untouched) rather than "failed" — a
    still-pending segment plus a non-terminal DBRouteJob row is exactly what
    sweep_orphaned_jobs (src/jobs/route_jobs.py) re-queues at the next
    startup, the final backstop this fix must not weaken."""
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _pending_segment())

    def _boom(*a, **k):
        raise RuntimeError("transient load crash")

    monkeypatch.setattr(segments_mod._repo, "get_project", _boom)
    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="transient load crash"):
        _resolve_route_job(user_id, "My Trip", "seg-1",
                           {"hafas_provider": "vr", "train_number": "273"})

    # get_project itself is monkeypatched above, so read the row directly
    # rather than through the (now permanently broken) _load_segment helper.
    seg = _read_segment_row(engine, project_id, "seg-1")
    assert seg.route_status == "pending"          # NOT prematurely failed
    assert seg.route_started_at == "2026-06-14T10:00:00Z"


def test_job_recovers_on_a_later_retry_after_an_unexpected_crash(env, monkeypatch):
    """A concrete demonstration of the retry this fix enables: the first
    attempt crashes on an unrelated DB hiccup and propagates (previous test);
    a later attempt — standing in for RQ's automatic retry — succeeds and
    resolves the segment normally, having never been marked "failed" in
    between."""
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _pending_segment())

    real_get = segments_mod._repo.get_project
    state = {"n": 0}

    def flaky_get(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient load crash")
        return real_get(*a, **k)

    monkeypatch.setattr(segments_mod._repo, "get_project", flaky_get)
    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)
    fake_line = [[24.94, 60.17], [25.73, 66.50]]
    monkeypatch.setattr(segments_mod, "_compute_segment_geometry",
                        lambda seg, params: (fake_line, 2, False, "relation_endpoints"))

    with pytest.raises(RuntimeError):
        _resolve_route_job(user_id, "My Trip", "seg-1",
                           {"hafas_provider": "vr", "train_number": "273"})
    assert _load_segment(engine, user_id, "My Trip", "seg-1").route_status == "pending"

    # RQ's retry re-invokes the same job — the second attempt succeeds.
    _resolve_route_job(user_id, "My Trip", "seg-1",
                       {"hafas_provider": "vr", "train_number": "273"})

    seg = _load_segment(engine, user_id, "My Trip", "seg-1")
    assert seg.route_status == "resolved"
    assert json.loads(seg.route_polyline) == fake_line


def test_job_degraded_straight_line_is_flagged(env, monkeypatch):
    """A rail resolve that fell back to a straight chord persists resolved BUT
    route_degraded=True — so the UI can tell it apart from a real route instead
    of the old silent 'looks resolved' behaviour."""
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _train_segment())

    straight = [[24.94, 60.17], [25.73, 66.50]]
    # (polyline, stop_count, degraded, strategy)
    monkeypatch.setattr(segments_mod, "_compute_segment_geometry",
                        lambda seg, params: (straight, 2, True, "straight"))

    _resolve_route_job(user_id, "My Trip", "seg-1",
                       {"hafas_provider": "vr", "train_number": "273"})

    seg = _load_segment(engine, user_id, "My Trip", "seg-1")
    assert seg.route_status == "resolved"
    assert seg.route_degraded is True
    assert json.loads(seg.route_polyline) == straight


# ── 4. Optimistic concurrency lock ───────────────────────────────────────────────

def test_save_project_optimistic_lock_detects_conflict(env):
    """Two writers that loaded the same version can't both commit silently."""
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _train_segment())
    repo = segments_mod._repo

    # Two independent loads observe the same lock_version.
    with Session(engine) as s:
        p_a = repo.get_project(s, user_id, "My Trip")
    with Session(engine) as s:
        p_b = repo.get_project(s, user_id, "My Trip")
    assert p_a.lock_version == p_b.lock_version

    # Writer A commits first — bumps lock_version.
    with Session(engine) as s:
        repo.save_project(s, user_id, p_a, check_version=True)

    # Writer B still holds the stale version → conflict.
    with Session(engine) as s:
        with pytest.raises(StaleWriteError):
            repo.save_project(s, user_id, p_b, check_version=True)


def test_blind_save_does_not_raise(env):
    """Default save_project (check_version=False) overwrites regardless."""
    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _train_segment())
    repo = segments_mod._repo

    with Session(engine) as s:
        p = repo.get_project(s, user_id, "My Trip")
    with Session(engine) as s:
        repo.save_project(s, user_id, p, check_version=True)   # bump to 1
    # A blind save with a stale in-memory version still succeeds.
    p.lock_version = 0
    with Session(engine) as s:
        repo.save_project(s, user_id, p)  # no check → no raise


# ── 5. Concurrent resolves (issues #172, #173) ─────────────────────────────────

def _add_segments(engine, project_id: int, segs) -> None:
    """Seed several segment items in one project, at successive positions."""
    import uuid as _uuid
    with Session(engine) as sess:
        for pos, seg in enumerate(segs):
            sess.add(DBProjectItem(
                project_id=project_id, position=pos, item_type="segment",
                uid=_uuid.uuid4().hex, segment_id=seg.id,
                segment_json=_segment_json(seg),
            ))
        sess.commit()


def _ferry_segment(seg_id: str) -> ConnectingSegment:
    return ConnectingSegment(
        id=seg_id, segment_type="boat", label="Helsinki → Tallinn",
        start=SegmentEndpoint(60.1719, 24.9414),
        end=SegmentEndpoint(59.4370, 24.7536),
    )


def test_three_concurrent_resolves_all_reach_a_terminal_status(env, monkeypatch):
    """The headline case from #172: three ferries resolving at once.

    Each used to be a whole-project save under the optimistic lock, with a
    retry budget of one — so a third writer could exhaust both attempts, write
    no verdict, and leave the tile spinning on "pending" for five minutes. As
    single-row payload writes they no longer contend at all.
    """
    client, user_id, project_id, engine = env
    seg_ids = ["ferry-1", "ferry-2", "ferry-3"]
    _add_segments(engine, project_id, [_ferry_segment(s) for s in seg_ids])

    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        segments_mod, "_compute_segment_geometry",
        lambda seg, params: ([[24.9, 60.1], [24.7, 59.4]], 2, False, "ferry"),
    )

    # Run sequentially: this asserts each job reaches a verdict, which is the
    # user-visible symptom. That three of them no longer *contend* is what
    # test_concurrent_resolves_do_not_touch_the_optimistic_lock proves — the
    # lock is the shared resource they used to fight over.
    for seg_id in seg_ids:
        _resolve_route_job(user_id, "My Trip", seg_id, {})

    for seg_id in seg_ids:
        seg = _load_segment(engine, user_id, "My Trip", seg_id)
        assert seg.route_status == "resolved", f"{seg_id} did not reach a verdict"
        assert seg.route_started_at is None
        assert json.loads(seg.route_polyline) == [[24.9, 60.1], [24.7, 59.4]]


def test_a_resolve_never_takes_the_optimistic_lock(env, monkeypatch):
    """A resolve must not *take* the CAS — that is what made resolves collide.

    It does still *advance* the lock version; see
    test_a_structural_save_cannot_clobber_a_landed_resolve for why. The property
    that matters here is that a resolve can never itself fail or retry because
    of another writer, so it always reaches a verdict.
    """
    client, user_id, project_id, engine = env
    _add_segments(engine, project_id, [_ferry_segment("ferry-1"), _ferry_segment("ferry-2")])

    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        segments_mod, "_compute_segment_geometry",
        lambda seg, params: ([[24.9, 60.1], [24.7, 59.4]], 2, False, "ferry"),
    )

    # Move the lock out from under the second job before it writes. A writer
    # that took the CAS would fail here; a payload write must not notice.
    _resolve_route_job(user_id, "My Trip", "ferry-1", {})
    with Session(engine) as sess:
        bump_lock_version(sess, project_id)
        sess.commit()
    _resolve_route_job(user_id, "My Trip", "ferry-2", {})

    for seg_id in ("ferry-1", "ferry-2"):
        assert _load_segment(engine, user_id, "My Trip", seg_id).route_status == "resolved"


def test_a_structural_save_cannot_clobber_a_landed_resolve(env, monkeypatch):
    """A reorder loaded before a resolve lands must not revert it.

    ``save_project`` rewrites every item's payload from the caller's snapshot,
    so a payload write invisible to the CAS would be silently reinstated as
    "pending". The narrow write advances lock_version precisely to be seen here.
    """
    client, user_id, project_id, engine = env
    _add_segments(engine, project_id, [_ferry_segment("ferry-1"), _ferry_segment("ferry-2")])

    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        segments_mod, "_compute_segment_geometry",
        lambda seg, params: ([[24.9, 60.1], [24.7, 59.4]], 2, False, "ferry"),
    )

    # A structural mutation loads its snapshot while both segments are pending.
    with Session(engine) as sess:
        snapshot = segments_mod._repo.get_project(sess, user_id, "My Trip")

    _resolve_route_job(user_id, "My Trip", "ferry-1", {})
    assert _load_segment(engine, user_id, "My Trip", "ferry-1").route_status == "resolved"

    # Saving the stale snapshot must be refused, not silently applied.
    snapshot.items.reverse()
    with Session(engine) as sess:
        with pytest.raises(StaleWriteError):
            segments_mod._repo.save_project(sess, user_id, snapshot, check_version=True)

    assert _load_segment(engine, user_id, "My Trip", "ferry-1").route_status == "resolved"


def test_a_superseded_job_cannot_overwrite_a_newer_verdict(env, monkeypatch):
    """A job whose trigger was superseded must discard its result.

    Without the token guard the slow first job would land on top of the second
    trigger's fresh state, reporting geometry the user already re-requested.
    """
    client, user_id, project_id, engine = env
    _add_segments(engine, project_id, [_ferry_segment("ferry-1")])

    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        segments_mod, "_compute_segment_geometry",
        lambda seg, params: ([[1.0, 1.0], [2.0, 2.0]], 2, False, "ferry"),
    )

    # The segment currently belongs to a *newer* trigger.
    with Session(engine) as sess:
        segments_mod._repo.update_segment_fields(
            sess, project_id, "ferry-1",
            {"route_status": "pending", "route_started_at": "2026-07-30T10:00:00Z"},
        )
        sess.commit()

    # An older job finishes and tries to write with its own stale token.
    _resolve_route_job(user_id, "My Trip", "ferry-1", {},
                       "2026-07-30T09:00:00Z")

    seg = _load_segment(engine, user_id, "My Trip", "ferry-1")
    assert seg.route_status == "pending", "stale verdict overwrote the newer trigger"
    assert seg.route_started_at == "2026-07-30T10:00:00Z"


def test_two_triggers_in_flight_both_succeed(env, monkeypatch):
    """Neither trigger 409s — the second used to lose the CAS and never start."""
    client, user_id, project_id, engine = env
    _add_segments(engine, project_id, [_ferry_segment("ferry-1"), _ferry_segment("ferry-2")])
    monkeypatch.setattr(segments_mod, "_resolve_route_job", lambda *a: None)

    for seg_id in ("ferry-1", "ferry-2"):
        resp = client.post(
            f"/api/projects/My Trip/segments/{seg_id}/resolve-route", json={})
        assert resp.status_code == 202, resp.text

    for seg_id in ("ferry-1", "ferry-2"):
        assert _load_segment(engine, user_id, "My Trip", seg_id).route_status == "pending"


# ── 6. Dispatch goes through the job queue (issue #173, phase C) ───────────────

def test_the_trigger_enqueues_onto_the_resolve_queue(env, monkeypatch):
    """The resolve must be queued, not handed to BackgroundTasks.

    A BackgroundTask dies with the process; a queued job survives a restart and
    is bounded by the resolve worker count (the Overpass politeness bound).
    """
    client, user_id, project_id, engine = env
    _add_segments(engine, project_id, [_ferry_segment("ferry-1")])

    seen: dict = {}

    def _spy_enqueue(queue_name, func, *args, **kwargs):
        seen["queue"] = queue_name
        seen["func"] = func
        seen["args"] = args
        return True

    monkeypatch.setattr(segments_mod, "enqueue", _spy_enqueue)

    resp = client.post(
        "/api/projects/My Trip/segments/ferry-1/resolve-route", json={})
    assert resp.status_code == 202

    assert seen["queue"] == segments_mod.QUEUE_RESOLVE
    assert seen["func"] is segments_mod._resolve_route_job
    assert seen["args"][:3] == (user_id, "My Trip", "ferry-1")
    # The started_at token is carried to the job so a superseded verdict is
    # discarded — see test_a_superseded_job_cannot_overwrite_a_newer_verdict.
    assert seen["args"][4] == _load_segment(
        engine, user_id, "My Trip", "ferry-1").route_started_at


# ── HAFAS-failure fallback logging ──────────────────────
#
# _compute_segment_geometry's `except HafasError: pass` used to swallow every
# HAFAS failure silently and fall back to two-point (great-circle-endpoint)
# geometry with no trace anywhere. It must now log a WARNING naming the
# segment before falling through — and the fallback behaviour itself (still
# resolving via get_rail_geometry on the two-point stop list) must be
# unchanged.

def test_hafas_failure_logs_warning_and_falls_back_unchanged(caplog):
    from src.services.hafas_service import HafasError, TrainRoute

    seg = _train_segment()
    straight = [[24.9414, 60.1719], [25.7294, 66.5039]]

    with patch("src.services.hafas_service.get_train_route",
               side_effect=HafasError("Train '273' not found departing "
                                      "Helsinki on 2026-06-01")), \
         patch("src.services.overpass_service.get_rail_geometry") as mock_rail:
        mock_rail.return_value = type(
            "R", (), {"polyline": straight, "degraded": True, "strategy": "straight", "source": "overpass"})()

        with caplog.at_level(logging.WARNING, logger="api.segments"):
            polyline, stop_count, degraded, strategy = _compute_segment_geometry(
                seg, {"hafas_provider": "vr", "train_number": "273"})

    # Fallback behaviour unchanged: two-point geometry still resolved via
    # get_rail_geometry, called with the plain start/end stops (no HAFAS stops).
    called_stops = mock_rail.call_args[0][0]
    assert called_stops == [
        {"lat": seg.start.lat, "lon": seg.start.lon},
        {"lat": seg.end.lat, "lon": seg.end.lon},
    ]
    assert polyline == straight
    assert stop_count == 2
    assert degraded is True
    assert strategy == "straight"
    # The specific train's HAFAS lookup failed — recorded on the segment even
    # though this fallback also happened to degrade.
    assert seg.route_hafas_failed is True

    # Visibility added: a WARNING naming the segment was logged before the fallback.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log for the HAFAS fallback"
    assert seg.id in warnings[0].getMessage()
    assert "straight" in warnings[0].getMessage() or "fall" in warnings[0].getMessage().lower()


def test_hafas_success_does_not_log_warning(caplog):
    """No degradation, no log — the WARNING is specific to the fallback path."""
    from src.services.hafas_service import TrainRoute

    seg = _train_segment()
    hafas_stops = [
        {"lat": seg.start.lat, "lon": seg.start.lon, "uic": "1"},
        {"lat": seg.end.lat, "lon": seg.end.lon, "uic": "2"},
    ]

    with patch("src.services.hafas_service.get_train_route",
               return_value=TrainRoute(hafas_stops, [])), \
         patch("src.services.overpass_service.get_rail_geometry") as mock_rail:
        mock_rail.return_value = type(
            "R", (), {"polyline": [[1.0, 1.0]], "degraded": False, "strategy": "relation_uic", "source": "overpass"})()

        with caplog.at_level(logging.WARNING, logger="api.segments"):
            _compute_segment_geometry(
                seg, {"hafas_provider": "vr", "train_number": "273"})

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert seg.route_hafas_failed is False


# ── MOTIS geometry short-circuits Overpass (issue #277) ──
#
# The whole point of moving trains to MOTIS is that /trip already carries the
# real track, so a matched train must not pay for an Overpass query at all —
# that cascade (3 mirrors x 45 s per query) is what made a long resolve take
# minutes.

def test_matched_train_uses_motis_geometry_and_skips_overpass():
    from src.services.hafas_service import TrainRoute

    seg = _train_segment()
    stops = [
        {"lat": seg.start.lat, "lon": seg.start.lon, "name": "A", "uic": ""},
        {"lat": 62.0, "lon": 25.0, "name": "B", "uic": ""},
        {"lat": seg.end.lat, "lon": seg.end.lon, "name": "C", "uic": ""},
    ]
    track = [[24.9414, 60.1719], [25.0, 62.0], [25.7294, 66.5039]]

    with patch("src.services.hafas_service.get_train_route",
               return_value=TrainRoute(stops, track)), \
         patch("src.services.overpass_service.get_rail_geometry") as mock_rail:
        polyline, stop_count, degraded, strategy = _compute_segment_geometry(
            seg, {"train_number": "273"})

    mock_rail.assert_not_called()
    assert polyline == track
    assert stop_count == 3
    assert degraded is False
    assert strategy == "motis_trip"
    assert seg.route_hafas_failed is False


def test_train_number_alone_triggers_the_lookup():
    """hafas_provider is vestigial — a segment with only a train number, which
    the old code skipped outright, must now resolve."""
    from src.services.hafas_service import TrainRoute

    seg = _train_segment()
    with patch("src.services.hafas_service.get_train_route") as mock_lookup:
        mock_lookup.return_value = TrainRoute(
            [{"lat": seg.start.lat, "lon": seg.start.lon, "name": "A", "uic": ""},
             {"lat": seg.end.lat, "lon": seg.end.lon, "name": "B", "uic": ""}],
            [[24.9414, 60.1719], [25.7294, 66.5039]])
        _compute_segment_geometry(seg, {"train_number": "273"})

    assert mock_lookup.call_count == 1
    assert "provider" not in mock_lookup.call_args.kwargs


# ── Distinguishing a HAFAS-fallback resolve from a fully clean one ──
#
# Both land as route_status="resolved" and both can be non-degraded (OSM finds
# real track either way) — route_hafas_failed is the only thing telling them
# apart, so the user still learns their specific train selection didn't
# resolve as chosen even when the generic fallback happens to succeed well.

def test_job_hafas_fallback_flagged_distinctly_from_clean_resolve(env, monkeypatch):
    from src.services.hafas_service import HafasError, TrainRoute

    client, user_id, project_id, engine = env
    _add_segments(engine, project_id, [
        _train_segment("seg-hafas-fail"), _train_segment("seg-clean"),
    ])
    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)

    real_line = [[24.9414, 60.1719], [25.7294, 66.5039]]

    with patch("src.services.hafas_service.get_train_route",
               side_effect=HafasError("Train '273' not found departing "
                                      "Helsinki on 2026-06-01")), \
         patch("src.services.overpass_service.get_rail_geometry") as mock_rail:
        mock_rail.return_value = type(
            "R", (), {"polyline": real_line, "degraded": False, "strategy": "straight", "source": "overpass"})()
        _resolve_route_job(user_id, "My Trip", "seg-hafas-fail",
                           {"hafas_provider": "vr", "train_number": "273"})

    hafas_stops = [
        {"lat": 60.1719, "lon": 24.9414, "uic": "1"},
        {"lat": 66.5039, "lon": 25.7294, "uic": "2"},
    ]
    with patch("src.services.hafas_service.get_train_route",
               return_value=TrainRoute(hafas_stops, [])), \
         patch("src.services.overpass_service.get_rail_geometry") as mock_rail:
        mock_rail.return_value = type(
            "R", (), {"polyline": real_line, "degraded": False, "strategy": "relation_uic", "source": "overpass"})()
        _resolve_route_job(user_id, "My Trip", "seg-clean",
                           {"hafas_provider": "vr", "train_number": "273"})

    fallback_seg = _load_segment(engine, user_id, "My Trip", "seg-hafas-fail")
    clean_seg = _load_segment(engine, user_id, "My Trip", "seg-clean")

    # Neither is "degraded" — OSM found real track in both cases — so
    # route_hafas_failed is the only signal distinguishing them.
    assert fallback_seg.route_status == "resolved"
    assert clean_seg.route_status == "resolved"
    assert fallback_seg.route_degraded is False
    assert clean_seg.route_degraded is False
    assert fallback_seg.route_hafas_failed is True
    assert clean_seg.route_hafas_failed is False


# ── The provider's own reason survives to the client (issue #277) ──
#
# A dead train-schedule provider used to reach the user as nothing but
# "approximate route shown": the HafasError was logged and thrown away, so the
# resolved segment carried no hint of *why* the chosen train never resolved.
# The reason is now kept on the resolved segment's route_error.

def test_hafas_failure_reason_is_persisted_on_the_resolved_segment(env, monkeypatch):
    from src.services.hafas_service import HafasError

    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _train_segment("seg-why"))
    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)

    with patch("src.services.hafas_service.get_train_route",
               side_effect=HafasError("HAFAS request failed: 503 Service Unavailable")), \
         patch("src.services.overpass_service.get_rail_geometry") as mock_rail:
        mock_rail.return_value = type(
            "R", (), {"polyline": [[24.9414, 60.1719], [25.7294, 66.5039]],
                      "degraded": True, "strategy": "straight", "source": "overpass"})()
        _resolve_route_job(user_id, "My Trip", "seg-why",
                           {"hafas_provider": "db", "train_number": "ICE 75"})

    seg = _load_segment(engine, user_id, "My Trip", "seg-why")
    assert seg.route_status == "resolved"
    assert seg.route_hafas_failed is True
    assert seg.route_error
    assert "503" in seg.route_error
    # Same 200-char cap the failure path already applies.
    assert len(seg.route_error) <= 200


def test_a_clean_resolve_leaves_route_error_empty(env, monkeypatch):
    """The reason is specific to the fallback — a good resolve stays clean."""
    from src.services.hafas_service import TrainRoute

    client, user_id, project_id, engine = env
    _add_segment(engine, project_id, _train_segment("seg-ok"))
    monkeypatch.setattr(segments_mod, "warm_geo_cache", lambda *a, **k: None)

    hafas_stops = [
        {"lat": 60.1719, "lon": 24.9414, "uic": "1"},
        {"lat": 66.5039, "lon": 25.7294, "uic": "2"},
    ]
    with patch("src.services.hafas_service.get_train_route",
               return_value=TrainRoute(hafas_stops, [])), \
         patch("src.services.overpass_service.get_rail_geometry") as mock_rail:
        mock_rail.return_value = type(
            "R", (), {"polyline": [[1.0, 1.0], [2.0, 2.0]],
                      "degraded": False, "strategy": "relation_uic", "source": "overpass"})()
        _resolve_route_job(user_id, "My Trip", "seg-ok",
                           {"hafas_provider": "vr", "train_number": "273"})

    seg = _load_segment(engine, user_id, "My Trip", "seg-ok")
    assert seg.route_status == "resolved"
    assert seg.route_error is None


def test_a_long_hafas_reason_is_truncated_to_200_chars():
    """Matches the failure path's cap, so a provider's HTML dump can't flood the UI."""
    from src.services.hafas_service import HafasError

    seg = _train_segment()
    with patch("src.services.hafas_service.get_train_route",
               side_effect=HafasError("x" * 500)), \
         patch("src.services.overpass_service.get_rail_geometry") as mock_rail:
        mock_rail.return_value = type(
            "R", (), {"polyline": [[1.0, 1.0]], "degraded": True, "strategy": "straight", "source": "overpass"})()
        _compute_segment_geometry(seg, {"hafas_provider": "db", "train_number": "ICE 75"})

    assert len(seg.route_error) == 200

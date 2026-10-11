"""Regression tests for the add_activities lost-write race (mutation-propagation
audit, api/activities.py).

``add_activities`` used to load a project snapshot, mutate it in memory, and
call ``save_project`` with the default ``check_version=False`` — a blind
full-item-list replace. A concurrent writer (another ``add_activities`` call,
a ``split_activity``, a point-edit) that loaded the project *after* this
snapshot was taken but committed *before* this call's save landed had its own
committed changes silently overwritten: no error, no 409, the data was just
gone.

The fix retrofits ``add_activities`` onto ``save_project_with_retry``
(src/project/repo_retry.py) — the load→mutate→``check_version=True``→retry
pattern already used by api/project_items.py — so a version conflict is
caught and retried against freshly reloaded state instead of silently
clobbering it.

The interleaving below is pinned deterministically (a monkeypatch on
``Project.add_activities`` injects the "concurrent write" between this
request's load and its save) rather than raced with threads — same technique
as ``tests/test_item_write_granularity.py``. The ordering is what matters,
not the concurrency primitive.
"""
from __future__ import annotations

import polyline as polyline_lib
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.models.project import Project
from src.project.project_repo import bump_lock_version

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]


def _raw_strava_activity(act_id: int, start: str, name: str = "Ride") -> dict:
    """Minimal raw Strava API activity dict, as sent by the client's add-activities call."""
    return {
        "id": act_id, "name": name, "type": "Ride",
        "distance": 1000.0, "moving_time": 100, "elapsed_time": 120,
        "total_elevation_gain": 0.0,
        "start_date": start, "start_date_local": start,
        "map": {"summary_polyline": polyline_lib.encode(_TRACK)},
    }


@pytest.fixture
def env(monkeypatch):
    """In-memory DB + the real app (needed for the QuotaExceeded -> 402 mapping),
    one project ("Trip") owned by user 1 with one pre-existing activity (111)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_TRIP_DAYS", "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        sess.add(UserInfo(id=1, email="owner@example.com", display_name="Owner"))
        proj = DBProject(id=1, user_info_id=1, name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        sess.add(DBActivity(
            id=111, user_info_id=1, name="Existing ride", type="Ride",
            distance=1000.0, moving_time=100, elapsed_time=120,
            total_elevation_gain=0.0,
            start_date="2026-06-10T09:00:00Z",
            start_date_local="2026-06-10T09:00:00Z",
        ))
        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="activity", activity_id=111))
        sess.commit()
        project_id = proj.id

    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "1", "email": "owner@example.com"
    }
    from fastapi.testclient import TestClient
    try:
        yield TestClient(app), engine, project_id
    finally:
        app.dependency_overrides.clear()


def _enforce_quota(monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")


def _inject_concurrent_write(monkeypatch, engine, project_id, act_id, start):
    """Patch Project.add_activities so that, the FIRST time it runs, it commits
    a second, fully independent write directly to the DB before returning —
    modeling a concurrent add_activities/split_activity call that loaded after
    our snapshot but commits before our save. Only fires once: the retry's own
    reload + add_activities call must not re-trigger it.
    """
    real_add_activities = Project.add_activities
    fired = {"done": False}

    def _patched(self, new_activities):
        if not fired["done"]:
            fired["done"] = True
            with Session(engine) as sess:
                sess.add(DBActivity(
                    id=act_id, user_info_id=1, name="Concurrent ride", type="Ride",
                    distance=2000.0, moving_time=200, elapsed_time=240,
                    total_elevation_gain=0.0,
                    start_date=start, start_date_local=start,
                    start_latlng_json="[48.0, 2.0]", end_latlng_json="[48.5, 2.5]",
                ))
                sess.add(DBProjectItem(project_id=project_id, position=99,
                                       item_type="activity", activity_id=act_id))
                bump_lock_version(sess, project_id)
                sess.commit()
        return real_add_activities(self, new_activities)

    monkeypatch.setattr(Project, "add_activities", _patched)


def _activity_ids(engine) -> set[int]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBProjectItem.activity_id)
                             .where(DBProjectItem.item_type == "activity")).all())


class TestConcurrentAddDoesNotDropTheOtherWritersActivity:
    """THE regression test for the lost-write bug: a concurrent commit landing
    between this request's load and its save must survive, not be silently
    overwritten by this request's blind full-item-list replace.

    RED on the pre-fix code: add_activities called save_project with the
    default check_version=False, so this request's stale snapshot (which
    never saw activity 333) blindly replaced the item list — the response
    came back 200, but activity 333 was gone. GREEN on the fix: the version
    mismatch is caught, the retry reloads state that includes 333, re-applies
    this request's own addition (222) on top of it, and all three activities
    survive.
    """

    def test_concurrent_commit_survives_the_race(self, env, monkeypatch):
        client, engine, project_id = env
        _inject_concurrent_write(monkeypatch, engine, project_id, act_id=333,
                                 start="2026-06-11T09:00:00Z")

        resp = client.post("/api/projects/Trip/activities", json={
            "activities": [_raw_strava_activity(222, "2026-06-12T09:00:00Z")],
        })

        assert resp.status_code == 200, resp.text
        # All three must be present: the pre-existing one (111), the
        # concurrent writer's (333), and this request's own (Strava 222, in a
        # row of its own whose id is allocated, #592). Losing 333 is exactly
        # the silent-overwrite bug this test guards against.
        with Session(engine) as sess:
            added = sess.exec(select(DBActivity.id)
                              .where(DBActivity.strava_id == 222)).all()
        assert len(added) == 1
        assert _activity_ids(engine) == {111, added[0], 333}


class TestQuotaStillEnforcedAfterARetry:
    """A retry forced by a version conflict must re-check the plan's trip-length
    quota against the freshly reloaded state, not skip it or reuse a stale
    verdict — i.e. the retry must not accidentally bypass quota enforcement.
    """

    def test_over_quota_add_is_still_rejected_after_a_retry(self, env, monkeypatch):
        client, engine, project_id = env
        _enforce_quota(monkeypatch)  # Free plan: max 10 trip days (default)

        # Our own add (day -5 relative to the seeded activity) is fine in
        # isolation: it only stretches the trip to 6 days, well inside the
        # 10-day Free limit — so the FIRST attempt's quota check must pass.
        # The concurrent write commits a distant, already-too-long trip
        # extension (day +52) directly to the DB — bypassing quota entirely,
        # exactly like a trip that was already over the limit before
        # enforcement was turned on (see TestAlreadyTooLong in
        # test_trip_days_quota.py). This is what the retry's reload sees.
        _inject_concurrent_write(monkeypatch, engine, project_id, act_id=333,
                                 start="2026-08-01T09:00:00Z")

        resp = client.post("/api/projects/Trip/activities", json={
            "activities": [_raw_strava_activity(222, "2026-06-05T09:00:00Z")],
        })

        # Attempt 1's quota check (span 2026-06-05..2026-06-10 = 6 days) would
        # have passed — the request only fails because the retry's quota
        # check, run against the reloaded state, correctly sees that adding
        # 222 stretches the now-already-too-long trip even further
        # (2026-06-05..2026-08-01) and rejects it. A retry loop that forgot to
        # re-check quota (or reused attempt 1's stale "quota OK" verdict)
        # would incorrectly let this succeed.
        assert resp.status_code == 402, resp.text
        assert resp.json()["resource"] == "trip_days"
        # Rejected: 222 must not have been persisted. 333 (the concurrent
        # writer's own already-committed activity) must still be there.
        assert _activity_ids(engine) == {111, 333}

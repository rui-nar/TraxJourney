"""The per-trip day limit, end to end through the real app (issue #121).

Free trips are capped at 10 days here, so the interesting cases are the ones
that *stretch* a trip: a memory dated outside its span, an imported activity, a
declared trip range. Dating something inside the span is always allowed, however
full the plan is.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.strava as strava_module
import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.billing import Subscription
from models.project_db import DBMemory, DBProject, DBProjectMember
from models.user import StravaToken, UserInfo
from src.billing.plans import TIER_1, TIER_3


@pytest.fixture
def env(monkeypatch):
    """In-memory DB + the real app, as user 1 who owns "Trip 1"."""
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
        sess.add(UserInfo(id=2, email="mate@example.com", display_name="Mate"))
        sess.add(DBProject(id=1, user_info_id=1, name="Trip 1"))
        sess.commit()

    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "1", "email": "owner@example.com"
    }
    try:
        yield TestClient(app), engine
    finally:
        app.dependency_overrides.clear()


def _enforcing(monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")


def _seed_memory(engine, date, project_id=1):
    with Session(engine) as sess:
        sess.add(DBMemory(project_id=project_id, date=date, geo_mode="custom",
                          photos_json=json.dumps([])))
        sess.commit()


def _add_memory(client, date, project="Trip 1"):
    return client.post("/api/memories/", json={
        "project_name": project, "date": date, "geo_mode": "custom",
        "name": "A day", "lat": 1.0, "lon": 2.0,
    })


def _add_journal(client, date, project="Trip 1"):
    return client.post("/api/journal/", json={
        "project_name": project, "date": date, "geo_mode": "custom",
        "description": "notes", "lat": 1.0, "lon": 2.0,
    })


def _raw_strava_activity(act_id, start):
    return {
        "id": act_id, "name": "Ride", "type": "Ride",
        "distance": 1000.0, "moving_time": 100, "elapsed_time": 120,
        "total_elevation_gain": 0.0,
        "start_date": start, "start_date_local": start,
    }


def _seed_strava_token(engine, user_id=1):
    with Session(engine) as sess:
        sess.add(StravaToken(user_info_id=user_id, access_token="tok",
                             refresh_token="ref", expires_at=9e9))
        sess.commit()


class _FakeStravaClient:
    """Stands in for StravaAPI: no Strava config needed, and it never
    refreshes, so no token is persisted."""
    token_data = {"access_token": "tok", "refresh_token": "ref", "expires_at": 9e9}


class TestMemoryDates:
    def test_a_day_inside_the_span_is_always_fine(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-01")
        _seed_memory(engine, "2026-06-10")   # exactly 10 days, the Free limit
        assert _add_memory(client, "2026-06-05").status_code == 201

    def test_the_day_that_lands_exactly_on_the_limit_is_allowed(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-01")
        assert _add_memory(client, "2026-06-10").status_code == 201

    def test_the_day_that_goes_one_past_the_limit_is_refused(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-01")
        res = _add_memory(client, "2026-06-11")
        assert res.status_code == 402

    def test_the_402_says_what_is_wrong(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-01")
        body = _add_memory(client, "2026-06-30").json()
        assert body["code"] == "quota_exceeded"
        assert body["resource"] == "trip_days"
        assert body["limit"] == 10
        assert body["used"] == 1
        assert "30 days" in body["detail"]

    def test_empty_days_in_the_middle_count(self, env, monkeypatch):
        """Two memories 20 days apart are a 20-day trip, not a 2-day one."""
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-01")
        assert _add_memory(client, "2026-06-21").status_code == 402

    def test_a_date_before_the_trip_stretches_it_too(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-20")
        assert _add_memory(client, "2026-06-01").status_code == 402

    def test_the_first_memory_of_an_empty_trip_is_fine(self, env, monkeypatch):
        client, _ = env
        _enforcing(monkeypatch)
        assert _add_memory(client, "2026-06-01").status_code == 201


class TestOtherDatedThings:
    def test_a_journal_entry_is_limited_too(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-01")
        assert _add_journal(client, "2026-07-01").status_code == 402

    def test_declaring_a_longer_trip_is_refused(self, env, monkeypatch):
        client, _ = env
        _enforcing(monkeypatch)
        res = client.put("/api/projects/Trip 1", json={
            "trip_start": "2026-06-01", "trip_end": "2026-08-01",
        })
        assert res.status_code == 402

    def test_declaring_a_trip_within_the_limit_is_fine(self, env, monkeypatch):
        client, _ = env
        _enforcing(monkeypatch)
        res = client.put("/api/projects/Trip 1", json={
            "trip_start": "2026-06-01", "trip_end": "2026-06-10",
        })
        assert res.status_code == 200

    def test_clearing_the_dates_is_always_allowed(self, env, monkeypatch):
        """Clearing can only shrink the span, so it must never be refused —
        including on an account that is already over its limit."""
        client, engine = env
        _seed_memory(engine, "2026-06-01")
        _seed_memory(engine, "2026-12-01")
        _enforcing(monkeypatch)
        res = client.put("/api/projects/Trip 1",
                         json={"trip_start": None, "trip_end": None})
        assert res.status_code == 200


class TestStravaImport:
    """Both import paths — the manual pick-and-add screen and the
    fetch-everything sync — must be checked identically. #192: the sync
    endpoint fetched and added every Strava activity without ever calling
    ensure_trip_days_quota, so a free-plan account could blow past the
    trip-days limit through it even with enforcement on."""

    def test_manual_import_is_limited_too(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-01")
        res = client.post("/api/projects/Trip 1/activities", json={
            "activities": [_raw_strava_activity(1, "2026-07-01T10:00:00Z")],
        })
        assert res.status_code == 402
        assert res.json()["resource"] == "trip_days"

    def test_full_sync_is_limited_too(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_memory(engine, "2026-06-01")
        _seed_strava_token(engine)
        monkeypatch.setattr(strava_module, "_strava_client_for_token",
                            lambda token_row: _FakeStravaClient())
        monkeypatch.setattr(
            strava_module, "_fetch_all_strava",
            lambda client, after=None, before=None: [
                _raw_strava_activity(1, "2026-07-01T10:00:00Z"),
            ],
        )
        res = client.post("/api/projects/Trip 1/strava/sync")
        assert res.status_code == 402
        assert res.json()["resource"] == "trip_days"

    def test_full_sync_within_the_limit_still_works(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _seed_strava_token(engine)
        monkeypatch.setattr(strava_module, "_strava_client_for_token",
                            lambda token_row: _FakeStravaClient())
        monkeypatch.setattr(
            strava_module, "_fetch_all_strava",
            lambda client, after=None, before=None: [
                _raw_strava_activity(1, "2026-06-01T10:00:00Z"),
            ],
        )
        res = client.post("/api/projects/Trip 1/strava/sync")
        assert res.status_code == 200
        assert res.json()["added"] == 1


class TestAlreadyTooLong:
    """A trip that was already longer than the limit when enforcement was
    switched on must stay editable — it just cannot grow."""

    def test_editing_inside_the_existing_span_still_works(self, env, monkeypatch):
        client, engine = env
        _seed_memory(engine, "2026-06-01")
        _seed_memory(engine, "2026-12-01")   # 184 days, way past Free's 10
        _enforcing(monkeypatch)
        assert _add_memory(client, "2026-08-15").status_code == 201

    def test_but_it_cannot_get_any_longer(self, env, monkeypatch):
        client, engine = env
        _seed_memory(engine, "2026-06-01")
        _seed_memory(engine, "2026-12-01")
        _enforcing(monkeypatch)
        assert _add_memory(client, "2027-01-01").status_code == 402


class TestPlans:
    def test_a_paid_tier_allows_a_longer_trip(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        with Session(engine) as sess:
            sess.add(Subscription(user_info_id=1, plan=TIER_1, status="active",
                                  current_period_end=9e9))
            sess.commit()
        _seed_memory(engine, "2026-06-01")
        assert _add_memory(client, "2026-08-01").status_code == 201

    def test_the_top_tier_has_no_limit_at_all(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        with Session(engine) as sess:
            sess.add(Subscription(user_info_id=1, plan=TIER_3, status="active",
                                  current_period_end=9e9))
            sess.commit()
        _seed_memory(engine, "2020-01-01")
        assert _add_memory(client, "2030-01-01").status_code == 201

    def test_the_limit_is_configurable(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_TRIP_DAYS", "3")
        _seed_memory(engine, "2026-06-01")
        assert _add_memory(client, "2026-06-05").status_code == 402

    def test_measuring_without_enforcing_does_not_block(self, env, monkeypatch):
        client, engine = env
        monkeypatch.setenv("BILLING_ENABLED", "1")  # enforcement left off
        _seed_memory(engine, "2026-06-01")
        assert _add_memory(client, "2026-12-01").status_code == 201

    def test_self_hosted_has_no_day_limit(self, env, engine=None):
        client, engine = env
        _seed_memory(engine, "2020-01-01")
        assert _add_memory(client, "2030-01-01").status_code == 201


class TestSharedTrips:
    def test_a_companion_spends_the_owners_allowance(self, env, monkeypatch):
        """The trip is the owner's; a companion editing it cannot make it longer
        than the owner's plan allows, whatever plan the companion is on."""
        client, engine = env
        _enforcing(monkeypatch)
        with Session(engine) as sess:
            sess.add(DBProjectMember(project_id=1, user_info_id=2,
                                     role="editor", invited_by=1))
            sess.add(Subscription(user_info_id=2, plan=TIER_3, status="active",
                                  current_period_end=9e9))
            sess.commit()
        _seed_memory(engine, "2026-06-01")
        app.dependency_overrides[get_current_user] = lambda: {
            "sub": "2", "email": "mate@example.com"
        }
        res = client.post("/api/memories/?owner=1", json={
            "project_name": "Trip 1", "date": "2026-09-01",
            "geo_mode": "custom", "name": "Late", "lat": 1.0, "lon": 2.0,
        })
        assert res.status_code == 402

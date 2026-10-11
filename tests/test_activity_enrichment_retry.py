"""Rate-limit-aware retry for background activity enrichment (mutation-
propagation audit fix).

``_enrich_activities_background`` is the live path `add_activities` schedules
after every import. Before this fix it had no rate-limit awareness at all —
each ``get_activity_streams`` call blocked on the shared limiter for up to
``MAX_WAIT_SECONDS`` before raising, one activity at a time. These tests pin:

  1. a ``RateLimitError`` mid-batch defers the rest of the batch instead of
     dropping it, and the deferred activities are retried (and this time
     succeed) once the window is said to have reset;
  2. a quota that's already nearly exhausted is noticed *before* the next
     Strava call (no per-activity blocking wait).
"""
from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.activities as activities_module
import models.db as db_module
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.exceptions.errors import RateLimitError


def _strava_id(row_id: int) -> int:
    """The Strava id of seeded row *row_id*: a row id is not a Strava id
    (#592), and Strava is asked about the latter."""
    return 1000 + row_id


@pytest.fixture
def db(monkeypatch):
    """User 1's trip "Trip" holding rows 1-4, added through user 1's Strava
    connection."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        sess.add(UserInfo(id=1, email="a@e.com", display_name="A"))
        sess.add(DBProject(id=1, user_info_id=1, name="Trip"))
        for i in range(1, 5):
            sess.add(DBActivity(
                id=i, user_info_id=1, name=f"Act {i}", type="Ride",
                strava_id=_strava_id(i), strava_user_id=1,
                distance=1000.0, moving_time=100, elapsed_time=120,
                start_date="2024-06-01T10:00:00Z",
                start_date_local="2024-06-01T12:00:00Z",
            ))
            sess.add(DBProjectItem(project_id=1, position=i - 1,
                                   item_type="activity", activity_id=i))
        sess.commit()
    return engine


@pytest.fixture(autouse=True)
def _no_cache_side_effects(monkeypatch):
    # These need a real project/geo cache that isn't set up here — irrelevant
    # to the retry/logging behaviour under test.
    monkeypatch.setattr(activities_module, "bust_geo_cache", lambda *a, **k: None)
    monkeypatch.setattr(activities_module, "warm_geo_cache", lambda *a, **k: None)
    monkeypatch.setattr(activities_module, "warm_meta_cache", lambda *a, **k: None)


def test_rate_limit_mid_batch_reschedules_remainder(db, monkeypatch):
    """A RateLimitError partway through a batch must defer -- not drop -- the
    rest of the batch, and retry (successfully) once the window resets."""
    calls = []
    raised_for_2 = {"done": False}

    class _Client:
        remaining_requests = 50

        def get_activity_streams(self, activity_id):
            calls.append(activity_id)
            if activity_id == _strava_id(2) and not raised_for_2["done"]:
                raised_for_2["done"] = True
                raise RateLimitError("window full")
            return {"latlng": {"data": [[1.0, 2.0], [1.1, 2.1]]}}

    monkeypatch.setattr(activities_module, "_strava_client_for_user", lambda uid: _Client())

    sleeps = []
    monkeypatch.setattr(activities_module.time, "sleep", lambda s: sleeps.append(s))

    activities_module._enrich_activities_background([1, 2, 3, 4], 1, 1, "Trip")

    # Pass 1: activity 1 succeeds, activity 2 raises and stops the batch --
    # 3 and 4 are deferred, not attempted yet. Pass 2 (after the mocked
    # sleep): 2, 3, 4 are retried and all succeed.
    assert calls == [_strava_id(i) for i in (1, 2, 2, 3, 4)]
    assert sleeps == [activities_module.RateLimiter.WINDOW_SECONDS + 5]

    with Session(db) as sess:
        for i in (1, 2, 3, 4):
            assert sess.get(DBActivity, i).summary_polyline is not None


def test_low_quota_defers_before_calling_strava(db, monkeypatch):
    """A near-exhausted quota is noticed proactively -- no call (and no
    blocking wait on the limiter) is made until the window has reset."""
    calls = []
    state = {"remaining": 2}  # <= 2 triggers the proactive defer

    class _Client:
        @property
        def remaining_requests(self):
            return state["remaining"]

        def get_activity_streams(self, activity_id):
            calls.append(activity_id)
            return {"latlng": {"data": [[1.0, 2.0], [1.1, 2.1]]}}

    monkeypatch.setattr(activities_module, "_strava_client_for_user", lambda uid: _Client())

    def _fake_sleep(seconds):
        state["remaining"] = 50  # simulate the window having reset

    monkeypatch.setattr(activities_module.time, "sleep", _fake_sleep)

    activities_module._enrich_activities_background([1, 2], 1, 1, "Trip")

    # Deferred whole (no Strava call) on the first pass, then both succeed
    # once the simulated reset raises the quota back up.
    assert calls == [_strava_id(1), _strava_id(2)]

"""Background activity enrichment must log its failures (mutation-propagation
audit fix).

``_enrich_activities_background``'s ``except Exception: pass`` used to
swallow a revoked Strava token or a network failure with zero trace in the
logs -- an activity silently kept no track forever, with nothing to diagnose
why. This test pins that a failure now produces a log line naming the
activity and the exception.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.activities as activities_module
import models.db as db_module
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo


@pytest.fixture
def db(monkeypatch):
    """User 1's trip "Trip" holding row 1, Strava activity 1001 added through
    user 1's connection (#592: enrichment needs both)."""
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
        sess.add(DBActivity(
            id=1, user_info_id=1, name="Act 1", type="Ride",
            strava_id=1001, strava_user_id=1,
            distance=1000.0, moving_time=100, elapsed_time=120,
            start_date="2024-06-01T10:00:00Z",
            start_date_local="2024-06-01T12:00:00Z",
        ))
        sess.add(DBProjectItem(project_id=1, position=0, item_type="activity",
                               activity_id=1))
        sess.commit()
    return engine


def test_auth_or_network_failure_is_logged_not_silently_swallowed(db, monkeypatch, caplog):
    """A revoked token / network failure used to vanish into a bare
    `except: pass`. It must now produce a log line naming the activity and
    the cause."""
    class _Client:
        remaining_requests = 50

        def get_activity_streams(self, activity_id):
            raise PermissionError("Strava access token is invalid or expired")

    monkeypatch.setattr(activities_module, "_strava_client_for_user", lambda uid: _Client())
    monkeypatch.setattr(activities_module, "bust_geo_cache", lambda *a, **k: None)
    monkeypatch.setattr(activities_module, "warm_geo_cache", lambda *a, **k: None)
    monkeypatch.setattr(activities_module, "warm_meta_cache", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING, logger="api.activities"):
        activities_module._enrich_activities_background([1], 1, 1, "Trip")

    assert any(
        "1" in r.getMessage() and "PermissionError" in r.getMessage()
        for r in caplog.records
    )

"""Tests for GET /api/projects/{name}/content-days (issue #372).

The endpoint answers the one question a client cannot answer for itself:
which days does this trip hold content on, for *any* member? Journal entries
are per-user, so the item list each member receives hides everyone else's —
and the settings screen used to read a day whose only content was another
member's journal as empty, then delete that day's *shared* day-meta when the
trip end date moved back past it.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.projects import router as projects_router
from models.project_db import (
    DBActivity,
    DBEncounter,
    DBMemory,
    DBProject,
    DBProjectItem,
    DBProjectMember,
)
from models.user import UserInfo


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)

    import api.journal as journal_mod
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))

    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        users = {
            who: UserInfo(display_name=who.capitalize(), email=f"{who}@e.com")
            for who in ("owner", "editor", "viewer", "stranger")
        }
        for u in users.values():
            sess.add(u)
        sess.commit()
        for u in users.values():
            sess.refresh(u)
        ids = {who: u.id for who, u in users.items()}

        proj = DBProject(user_info_id=ids["owner"], name="Trip")
        sess.add(proj)
        sess.commit()
        sess.refresh(proj)
        ids["project"] = proj.id
        for who in ("editor", "viewer"):
            sess.add(DBProjectMember(
                project_id=proj.id, user_info_id=ids[who], role=who,
                invited_by=ids["owner"]))
        sess.commit()

    current = {"uid": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(projects_router)
    app.include_router(journal_router)

    client = TestClient(app)

    def act_as(who: str):
        current["uid"] = ids[who]

    return client, engine, ids, act_as


def _days(client, owner_id: int | None = None, name: str = "Trip"):
    q = f"?owner={owner_id}" if owner_id is not None else ""
    r = client.get(f"/api/projects/{name}/content-days{q}")
    assert r.status_code == 200, r.text
    return r.json()["days"]


# ── The defect this endpoint exists for ───────────────────────────────────────

def test_a_day_held_only_by_another_members_journal_is_reported(env):
    """The whole point: the editor's private journal is invisible to the owner
    in every item list, but the day it sits on must still come back here."""
    client, _, ids, act_as = env

    act_as("editor")
    r = client.post(f"/api/journal/?owner={ids['owner']}", json={
        "project_name": "Trip", "date": "2026-07-04",
        "geo_mode": "custom", "lat": 1.0, "lon": 2.0,
        "description": "editor's private note",
    })
    assert r.status_code == 201, r.text

    # Premise: the owner's own view of the trip shows no journal at all, so
    # 2026-07-04 looks like an empty day to them.
    act_as("owner")
    items = client.get("/api/projects/Trip").json()["items"]
    assert [i for i in items if i["item_type"] == "journal"] == []

    assert _days(client) == ["2026-07-04"]


# ── What counts as content ────────────────────────────────────────────────────

def test_reports_every_kind_of_content_once_and_sorted(env):
    client, engine, ids, _ = env
    with Session(engine) as sess:
        pid = ids["project"]
        sess.add(DBActivity(id=111, user_info_id=ids["owner"], name="Ride",
                            start_date="2026-07-03T06:00:00Z",
                            start_date_local="2026-07-03T08:00:00"))
        sess.add(DBProjectItem(project_id=pid, position=0,
                               item_type="activity", activity_id=111))
        sess.add(DBMemory(project_id=pid, date="2026-07-01", name="Sunrise"))
        # A second memory on an already-covered day: one key, not two.
        sess.add(DBMemory(project_id=pid, date="2026-07-01", name="Sunset"))
        sess.add(DBEncounter(project_id=pid, date="2026-07-02"))
        sess.add(DBProjectItem(project_id=pid, position=1, item_type="segment",
                               segment_json=json.dumps({"id": "s1",
                                                        "date": "2026-07-05"})))
        # An undated segment is placed by its neighbours, not by a day of its
        # own — it must not contribute a key.
        sess.add(DBProjectItem(project_id=pid, position=2, item_type="segment",
                               segment_json=json.dumps({"id": "s2",
                                                        "date": None})))
        sess.commit()

    assert _days(client) == ["2026-07-01", "2026-07-02", "2026-07-03",
                            "2026-07-05"]


def test_a_trip_with_no_content_reports_nothing(env):
    client, _, _, _ = env
    assert _days(client) == []


def test_days_do_not_leak_between_projects(env):
    client, engine, ids, _ = env
    with Session(engine) as sess:
        other = DBProject(user_info_id=ids["owner"], name="Other")
        sess.add(other)
        sess.commit()
        sess.refresh(other)
        sess.add(DBMemory(project_id=ids["project"], date="2026-07-01"))
        sess.add(DBMemory(project_id=other.id, date="2026-08-01"))
        sess.commit()

    assert _days(client) == ["2026-07-01"]
    assert _days(client, name="Other") == ["2026-08-01"]


# ── Access ────────────────────────────────────────────────────────────────────

def test_editor_and_owner_may_read_it(env):
    """Editor tier, matching the only write that consumes this answer."""
    client, engine, ids, act_as = env
    with Session(engine) as sess:
        sess.add(DBMemory(project_id=ids["project"], date="2026-07-01"))
        sess.commit()

    for who in ("owner", "editor"):
        act_as(who)
        assert _days(client, owner_id=ids["owner"]) == ["2026-07-01"], who


def test_a_viewer_may_not_read_it(env):
    """A journal-only day is exactly the day a viewer does NOT see in their own
    trip view, so this endpoint would disclose that somebody wrote something
    that day. A viewer can't prune days anyway — PATCH /day-meta is editor+."""
    client, engine, ids, act_as = env
    with Session(engine) as sess:
        sess.add(DBMemory(project_id=ids["project"], date="2026-07-01"))
        sess.commit()

    act_as("viewer")
    r = client.get(f"/api/projects/Trip/content-days?owner={ids['owner']}")
    assert r.status_code == 403


def test_a_malformed_segment_blob_does_not_break_the_check(env):
    """A single bad row must not 500 the endpoint: the client reads a failure
    as "cannot prune at all", which would be permanent for that trip."""
    client, engine, ids, _ = env
    with Session(engine) as sess:
        sess.add(DBMemory(project_id=ids["project"], date="2026-07-01"))
        sess.add(DBProjectItem(
            project_id=ids["project"], item_type="segment", position=0,
            segment_json="null"))
        sess.add(DBProjectItem(
            project_id=ids["project"], item_type="segment", position=1,
            segment_json="not json at all"))
        sess.commit()

    assert _days(client) == ["2026-07-01"]


def test_a_non_member_gets_the_same_404_as_a_missing_project(env):
    """resolve_project's own 404, not the router's — a stranger must not be
    able to tell a trip they can't touch from one that doesn't exist."""
    client, _, ids, act_as = env
    act_as("stranger")
    r = client.get(f"/api/projects/Trip/content-days?owner={ids['owner']}")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_an_unknown_project_gets_404(env):
    client, _, _, _ = env
    r = client.get("/api/projects/Nope/content-days")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"

"""API tests for travel-companion membership and invites (issue #106).

Covers the invite lifecycle, the owner/editor/stranger access matrix on
representative routes, member management, the shared-projects list, and the
E2EE mutual-exclusion guards.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.encryption import router as encryption_router
from api.members import router as members_router, invites_router
from api.people import router as people_router
from api.projects import router as projects_router
from models.project_db import DBProject, DBProjectInvite, DBProjectMember
from models.user import UserInfo


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        editor = UserInfo(display_name="Editor", email="editor@e.com")
        stranger = UserInfo(display_name="Stranger", email="stranger@e.com")
        sess.add(owner); sess.add(editor); sess.add(stranger); sess.commit()
        sess.refresh(owner); sess.refresh(editor); sess.refresh(stranger)
        proj = DBProject(user_info_id=owner.id, name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        ids = {"owner": owner.id, "editor": editor.id, "stranger": stranger.id,
               "project": proj.id}

    # Mutable holder so each test can act as different users.
    current = {"uid": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(members_router)
    app.include_router(invites_router)
    app.include_router(projects_router)
    app.include_router(people_router)
    app.include_router(encryption_router)

    client = TestClient(app)

    def act_as(who: str):
        current["uid"] = ids[who]

    return client, engine, ids, act_as


def _join(client, act_as) -> str:
    """Owner creates an invite, editor accepts it. Returns the token."""
    act_as("owner")
    token = client.post("/api/projects/Trip/members/invite").json()["token"]
    act_as("editor")
    r = client.post(f"/api/invites/{token}/accept")
    assert r.status_code == 200, r.text
    return token


# ── Invite lifecycle ──────────────────────────────────────────────────────────

def test_invite_create_is_idempotent(env):
    client, _, _, act_as = env
    act_as("owner")
    t1 = client.post("/api/projects/Trip/members/invite").json()["token"]
    t2 = client.post("/api/projects/Trip/members/invite").json()["token"]
    assert t1 == t2


def test_invite_preview_shows_project_and_owner(env):
    client, _, _, act_as = env
    act_as("owner")
    token = client.post("/api/projects/Trip/members/invite").json()["token"]
    act_as("stranger")
    r = client.get(f"/api/invites/{token}")
    assert r.status_code == 200
    # invited_email is null for the shared link — it isn't addressed to anyone
    # in particular (issue #110).
    assert r.json() == {"project_name": "Trip", "owner_name": "Owner",
                        "role": "editor", "invited_email": None}


def test_accept_creates_membership_and_is_idempotent(env):
    client, engine, ids, act_as = env
    token = _join(client, act_as)
    r = client.post(f"/api/invites/{token}/accept")  # second accept
    assert r.status_code == 200
    assert r.json() == {"name": "Trip", "owner_id": ids["owner"]}
    with Session(engine) as sess:
        rows = sess.exec(select(DBProjectMember)).all()
        assert len(rows) == 1
        assert rows[0].user_info_id == ids["editor"]
        assert rows[0].role == "editor"


def test_owner_accepting_own_invite_conflicts(env):
    client, _, _, act_as = env
    act_as("owner")
    token = client.post("/api/projects/Trip/members/invite").json()["token"]
    r = client.post(f"/api/invites/{token}/accept")
    assert r.status_code == 409


def test_revoked_invite_is_gone_but_members_remain(env):
    client, engine, _, act_as = env
    token = _join(client, act_as)
    act_as("owner")
    assert client.delete("/api/projects/Trip/members/invite").status_code == 204
    act_as("stranger")
    assert client.get(f"/api/invites/{token}").status_code == 404
    assert client.post(f"/api/invites/{token}/accept").status_code == 404
    with Session(engine) as sess:
        assert len(sess.exec(select(DBProjectMember)).all()) == 1
        assert sess.exec(select(DBProjectInvite)).first() is None


def test_unknown_invite_404s(env):
    client, _, _, act_as = env
    act_as("stranger")
    assert client.get("/api/invites/deadbeef").status_code == 404


def test_editor_cannot_create_or_revoke_invite(env):
    client, _, ids, act_as = env
    _join(client, act_as)
    act_as("editor")
    r = client.post(f"/api/projects/Trip/members/invite?owner={ids['owner']}")
    assert r.status_code == 403
    r = client.delete(f"/api/projects/Trip/members/invite?owner={ids['owner']}")
    assert r.status_code == 403


# ── Access matrix on project routes ───────────────────────────────────────────

def test_stranger_gets_404_with_owner_param(env):
    client, _, ids, act_as = env
    act_as("stranger")
    r = client.patch(f"/api/projects/Trip/day-meta?owner={ids['owner']}",
                     json={"days": {}})
    assert r.status_code == 404


def test_editor_can_write_day_meta(env):
    client, _, ids, act_as = env
    _join(client, act_as)
    act_as("editor")
    r = client.patch(f"/api/projects/Trip/day-meta?owner={ids['owner']}",
                     json={"days": {}})
    assert r.status_code == 200


def test_editor_can_update_trip_dates_but_not_rename(env):
    client, _, ids, act_as = env
    _join(client, act_as)
    act_as("editor")
    r = client.put(f"/api/projects/Trip?owner={ids['owner']}",
                   json={"trip_start": "2026-01-01"})
    assert r.status_code == 200
    r = client.put(f"/api/projects/Trip?owner={ids['owner']}",
                   json={"new_name": "Hijacked"})
    assert r.status_code == 403


def test_editor_cannot_delete_project(env):
    client, _, ids, act_as = env
    _join(client, act_as)
    act_as("editor")
    r = client.delete(f"/api/projects/Trip?owner={ids['owner']}")
    assert r.status_code == 403


def test_editor_without_owner_param_sees_own_namespace(env):
    client, _, _, act_as = env
    _join(client, act_as)
    act_as("editor")
    # No ?owner= → resolves against the editor's own (empty) project list.
    r = client.patch("/api/projects/Trip/day-meta", json={"days": {}})
    assert r.status_code == 404


def test_editor_can_create_person_in_shared_project(env):
    client, engine, ids, act_as = env
    _join(client, act_as)
    act_as("editor")
    r = client.post(f"/api/people/?owner={ids['owner']}",
                    json={"project_name": "Trip", "name": "Ana"})
    assert r.status_code == 201, r.text
    person_id = r.json()["id"]
    # …and the owner can edit that person right back.
    act_as("owner")
    r = client.get(f"/api/people/{person_id}")
    assert r.status_code == 200


# ── Member management ─────────────────────────────────────────────────────────

def test_members_list_owner_first_then_editors(env):
    client, _, ids, act_as = env
    _join(client, act_as)
    act_as("owner")
    r = client.get("/api/projects/Trip/members")
    assert r.status_code == 200
    members = r.json()["members"]
    assert [m["role"] for m in members] == ["owner", "editor"]
    assert members[0]["user_id"] == ids["owner"]
    assert members[1]["user_id"] == ids["editor"]
    assert members[1]["display_name"] == "Editor"


def test_editor_can_list_members_with_owner_param(env):
    client, _, ids, act_as = env
    _join(client, act_as)
    act_as("editor")
    r = client.get(f"/api/projects/Trip/members?owner={ids['owner']}")
    assert r.status_code == 200
    assert len(r.json()["members"]) == 2


def test_owner_can_remove_member(env):
    client, engine, ids, act_as = env
    _join(client, act_as)
    act_as("owner")
    r = client.delete(f"/api/projects/Trip/members/{ids['editor']}")
    assert r.status_code == 204
    with Session(engine) as sess:
        assert sess.exec(select(DBProjectMember)).first() is None


def test_editor_can_leave_but_not_remove_others(env):
    client, engine, ids, act_as = env
    _join(client, act_as)
    act_as("editor")
    # Removing someone else (the owner id isn't even a member — but the guard
    # fires before the lookup) is owner-only.
    r = client.delete(f"/api/projects/Trip/members/{ids['stranger']}?owner={ids['owner']}")
    assert r.status_code == 403
    # Leaving (removing yourself) is allowed.
    r = client.delete(f"/api/projects/Trip/members/{ids['editor']}?owner={ids['owner']}")
    assert r.status_code == 204
    with Session(engine) as sess:
        assert sess.exec(select(DBProjectMember)).first() is None


def test_remove_unknown_member_404s(env):
    client, _, ids, act_as = env
    act_as("owner")
    r = client.delete(f"/api/projects/Trip/members/{ids['stranger']}")
    assert r.status_code == 404


# ── Project list ──────────────────────────────────────────────────────────────

def test_list_includes_shared_project_with_role_and_owner(env):
    client, _, ids, act_as = env
    _join(client, act_as)
    act_as("editor")
    entries = client.get("/api/projects/").json()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "Trip"
    assert entry["role"] == "editor"
    assert entry["owner_id"] == ids["owner"]
    assert entry["owner_name"] == "Owner"

    act_as("owner")
    entries = client.get("/api/projects/").json()
    assert len(entries) == 1
    assert entries[0]["role"] == "owner"
    assert entries[0]["owner_id"] == ids["owner"]


def _shared_projects_query_count(n_memberships: int, monkeypatch) -> tuple[int, list]:
    """Set up one member shared into *n_memberships* projects (each with its own
    owner), call GET /api/projects/ as that member, and return
    (SELECT query count, response entries).
    """
    from sqlalchemy import event

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)

    owner_ids: list[int] = []
    with Session(engine) as sess:
        member = UserInfo(display_name="Member", email="member@e.com")
        sess.add(member); sess.commit(); sess.refresh(member)
        member_id = member.id

        for i in range(n_memberships):
            owner = UserInfo(display_name=f"Owner{i}", email=f"owner{i}@e.com")
            sess.add(owner); sess.commit(); sess.refresh(owner)
            owner_ids.append(owner.id)
            proj = DBProject(user_info_id=owner.id, name=f"Trip{i}")
            sess.add(proj); sess.commit(); sess.refresh(proj)
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=member_id,
                                      role="editor", invited_by=owner.id))
        sess.commit()

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(member_id)}
    app.include_router(projects_router)
    client = TestClient(app)

    selects: list[str] = []

    def _count(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.strip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        resp = client.get("/api/projects/")
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == n_memberships
    for i, oid in enumerate(owner_ids):
        entry = next(e for e in entries if e["name"] == f"Trip{i}")
        assert entry["owner_id"] == oid
        assert entry["owner_name"] == f"Owner{i}"
        assert entry["role"] == "editor"
    return len(selects), entries


def test_shared_project_list_query_count_does_not_scale_with_membership_count(monkeypatch):
    """GET /api/projects/ must not issue two extra queries per membership.

    Was ``sess.get(DBProject, ...)`` + ``sess.get(UserInfo, ...)`` inside the
    ``for m in memberships:`` loop — O(2N) queries for N shared projects.
    Batched into one query for the projects and one for their owners, so the
    query count is flat as membership count grows.
    """
    small, _ = _shared_projects_query_count(2, monkeypatch)
    large, _ = _shared_projects_query_count(6, monkeypatch)
    assert small == large, (
        f"query count scales with membership count: {small} (2 memberships) "
        f"vs {large} (6 memberships)"
    )


# ── E2EE mutual exclusion ─────────────────────────────────────────────────────

def _enable_encryption_body() -> dict:
    return {
        "device": {"public_key": "pk", "label": "test",
                   "wrapped_cmk": "cmk", "ephemeral_public_key": "epk"},
        "recovery": {"method": "recovery_key", "wrapped_cmk": "cmk", "salt": "s"},
    }


def test_invite_blocked_for_encrypted_owner(env):
    client, engine, ids, act_as = env
    with Session(engine) as sess:
        owner = sess.get(UserInfo, ids["owner"])
        owner.encryption_enabled = True
        sess.add(owner); sess.commit()
    act_as("owner")
    r = client.post("/api/projects/Trip/members/invite")
    assert r.status_code == 409


def test_enable_encryption_blocked_with_members(env):
    client, _, _, act_as = env
    _join(client, act_as)
    act_as("owner")
    r = client.post("/api/encryption/enable", json=_enable_encryption_body())
    assert r.status_code == 409
    assert "companion" in r.json()["detail"]


def test_enable_encryption_ok_without_members(env):
    client, _, _, act_as = env
    act_as("owner")
    r = client.post("/api/encryption/enable", json=_enable_encryption_body())
    assert r.status_code == 201

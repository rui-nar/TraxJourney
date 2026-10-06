"""End-to-end travel-companion journey (issue #106, U6).

Exercises the whole feature through the real routers, starting from nothing:
the owner creates a trip and an invite link, the companion joins, contributes
content (counters, a memory, a private journal entry), both sides see exactly
what they should, and leaving revokes access.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.members import router as members_router, invites_router
from api.memories import router as memories_router
from api.projects import router as projects_router
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
    import api.memories as mem_mod
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))

    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        companion = UserInfo(display_name="Companion", email="comp@e.com")
        sess.add(owner); sess.add(companion); sess.commit()
        sess.refresh(owner); sess.refresh(companion)
        ids = {"owner": owner.id, "companion": companion.id}

    current = {"uid": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(projects_router)
    app.include_router(members_router)
    app.include_router(invites_router)
    app.include_router(memories_router)
    app.include_router(journal_router)

    client = TestClient(app)

    def act_as(who: str):
        current["uid"] = ids[who]

    return client, ids, act_as


def test_full_companion_journey(env):
    client, ids, act_as = env
    owner_q = f"?owner={ids['owner']}"

    # Owner creates the trip and an invite link.
    act_as("owner")
    assert client.post("/api/projects/", json={"name": "Japan 2026"}).status_code == 201
    token = client.post("/api/projects/Japan 2026/members/invite").json()["token"]

    # Companion previews and accepts the invite.
    act_as("companion")
    preview = client.get(f"/api/invites/{token}").json()
    assert preview == {"project_name": "Japan 2026", "owner_name": "Owner",
                       "role": "editor", "invited_email": None}
    accepted = client.post(f"/api/invites/{token}/accept").json()
    assert accepted == {"name": "Japan 2026", "owner_id": ids["owner"]}

    # The shared trip shows up in the companion's project list as editor.
    entries = client.get("/api/projects/").json()
    assert [(e["name"], e["role"]) for e in entries] == [("Japan 2026", "editor")]
    assert entries[0]["owner_name"] == "Owner"

    # Companion adds a counter with a per-day value...
    r = client.patch(f"/api/projects/Japan 2026/day-meta{owner_q}", json={
        "days": {"2026-07-01": {"counters": {"km": 42}}},
        "counters": [{"name": "km", "start": 0}],
    })
    assert r.status_code == 200

    # ...a memory (shared trip content)...
    r = client.post(f"/api/memories/{owner_q}", json={
        "project_name": "Japan 2026", "name": "Fuji sunrise", "date": "2026-07-01",
        "geo_mode": "custom", "lat": 35.36, "lon": 138.73,
    })
    assert r.status_code == 201, r.text

    # ...and a private journal entry.
    r = client.post(f"/api/journal/{owner_q}", json={
        "project_name": "Japan 2026", "date": "2026-07-01",
        "geo_mode": "custom", "lat": 35.36, "lon": 138.73,
        "description": "companion private note",
    })
    assert r.status_code == 201, r.text

    # Owner sees the memory and the counter, but NOT the companion's journal.
    act_as("owner")
    details = client.get("/api/projects/Japan 2026").json()
    item_types = [i["item_type"] for i in details["items"]]
    assert "memory" in item_types
    assert "journal" not in item_types
    assert details["counters"] == [{"name": "km", "start": 0.0}]

    # The companion still sees their own journal entry in the shared trip.
    act_as("companion")
    details = client.get(f"/api/projects/Japan 2026{owner_q}").json()
    item_types = [i["item_type"] for i in details["items"]]
    assert "memory" in item_types and "journal" in item_types

    # Members list shows both, owner first.
    roles = [m["role"] for m in
             client.get(f"/api/projects/Japan 2026/members{owner_q}").json()["members"]]
    assert roles == ["owner", "editor"]

    # Companion leaves: access is gone and the list is empty again.
    r = client.delete(
        f"/api/projects/Japan 2026/members/{ids['companion']}{owner_q}")
    assert r.status_code == 204
    assert client.get(f"/api/projects/Japan 2026{owner_q}").status_code == 404
    assert client.get("/api/projects/").json() == []

    # The owner keeps the companion's contributed memory.
    act_as("owner")
    item_types = [i["item_type"] for i in
                  client.get("/api/projects/Japan 2026").json()["items"]]
    assert "memory" in item_types

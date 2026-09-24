"""API tests for project sharing endpoints — split out of api/projects.py into
api/project_shares.py. Covers the previously-untested happy paths: share-link
create/revoke (both full and no-memories variants), share-info, and visitors."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.project_shares import router as project_shares_router
from models.project_db import DBProject, DBShareVisit
from models.user import UserInfo


def _seed(engine) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        proj = DBProject(user_info_id=u.id, name="My Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        return u.id


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    uid = _seed(engine)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    app.include_router(project_shares_router)
    return TestClient(app), engine, uid


def test_create_share_link_is_idempotent(env):
    client, *_ = env
    r1 = client.post("/api/projects/My Trip/share")
    assert r1.status_code == 200, r1.text
    token1 = r1.json()["share_token"]
    assert token1

    r2 = client.post("/api/projects/My Trip/share")
    assert r2.json()["share_token"] == token1  # same token returned, not regenerated


def test_create_share_link_project_not_found(env):
    client, *_ = env
    resp = client.post("/api/projects/No Such Trip/share")
    assert resp.status_code == 404


def test_share_info_reflects_created_tokens(env):
    client, *_ = env
    resp = client.get("/api/projects/My Trip/share-info")
    assert resp.status_code == 200
    assert resp.json() == {"share_token": None, "share_token_no_memories": None}

    token = client.post("/api/projects/My Trip/share").json()["share_token"]
    resp = client.get("/api/projects/My Trip/share-info")
    assert resp.json()["share_token"] == token
    assert resp.json()["share_token_no_memories"] is None


def test_revoke_share_link_clears_token(env):
    client, *_ = env
    client.post("/api/projects/My Trip/share")
    resp = client.delete("/api/projects/My Trip/share")
    assert resp.status_code == 204

    info = client.get("/api/projects/My Trip/share-info").json()
    assert info["share_token"] is None


def test_revoke_share_link_without_existing_token_is_noop(env):
    client, *_ = env
    resp = client.delete("/api/projects/My Trip/share")
    assert resp.status_code == 204


def test_create_no_memories_link_is_idempotent(env):
    client, *_ = env
    r1 = client.post("/api/projects/My Trip/share/no-memories")
    assert r1.status_code == 200, r1.text
    token1 = r1.json()["share_token_no_memories"]
    assert token1

    r2 = client.post("/api/projects/My Trip/share/no-memories")
    assert r2.json()["share_token_no_memories"] == token1


def test_revoke_no_memories_link_clears_token(env):
    client, *_ = env
    client.post("/api/projects/My Trip/share/no-memories")
    resp = client.delete("/api/projects/My Trip/share/no-memories")
    assert resp.status_code == 204

    info = client.get("/api/projects/My Trip/share-info").json()
    assert info["share_token_no_memories"] is None


def test_full_and_no_memories_tokens_are_independent(env):
    client, *_ = env
    full = client.post("/api/projects/My Trip/share").json()["share_token"]
    no_mem = client.post("/api/projects/My Trip/share/no-memories").json()["share_token_no_memories"]
    assert full != no_mem

    client.delete("/api/projects/My Trip/share")
    info = client.get("/api/projects/My Trip/share-info").json()
    assert info["share_token"] is None
    assert info["share_token_no_memories"] == no_mem  # untouched by revoking the other


def test_share_visitors_empty_when_no_visits(env):
    client, *_ = env
    resp = client.get("/api/projects/My Trip/share/visitors")
    assert resp.status_code == 200
    assert resp.json() == {
        "full": {"anonymous_count": 0, "registered": []},
        "no_memories": {"anonymous_count": 0, "registered": []},
    }


def test_share_visitors_counts_anonymous_and_registered(env):
    client, engine, uid = env
    with Session(engine) as sess:
        proj = sess.exec(
            select(DBProject).where(DBProject.name == "My Trip")
        ).first()
        visitor = UserInfo(display_name="Visitor", email="v@e.com",
                           avatar_url="https://img/v.png")
        sess.add(visitor); sess.commit(); sess.refresh(visitor)
        sess.add(DBShareVisit(
            project_id=proj.id, token_type="full", visitor_type="anonymous",
            anonymous_id="anon-1", last_seen_at=100.0,
        ))
        sess.add(DBShareVisit(
            project_id=proj.id, token_type="full", visitor_type="registered",
            user_info_id=visitor.id, last_seen_at=200.0,
        ))
        sess.add(DBShareVisit(
            project_id=proj.id, token_type="no_memories", visitor_type="anonymous",
            anonymous_id="anon-2", last_seen_at=150.0,
        ))
        sess.commit()

    resp = client.get("/api/projects/My Trip/share/visitors")
    assert resp.status_code == 200
    body = resp.json()
    assert body["full"]["anonymous_count"] == 1
    [entry] = body["full"]["registered"]
    assert entry["display_name"] == "Visitor"
    assert entry["avatar_url"] == "https://img/v.png"
    assert entry["last_seen_at"] == 200.0
    assert body["no_memories"]["anonymous_count"] == 1
    assert body["no_memories"]["registered"] == []


def _add_registered_visit(engine, *, project_name: str, token_type: str,
                          display_name: str, email: str) -> int:
    with Session(engine) as sess:
        proj = sess.exec(
            select(DBProject).where(DBProject.name == project_name)
        ).first()
        visitor = UserInfo(display_name=display_name, email=email)
        sess.add(visitor); sess.commit(); sess.refresh(visitor)
        sess.add(DBShareVisit(
            project_id=proj.id, token_type=token_type, visitor_type="registered",
            user_info_id=visitor.id, last_seen_at=300.0,
        ))
        sess.commit()
        return visitor.id


def test_share_visitors_never_reveal_email(env):
    """Issue #431: a signed-in visitor's address must not reach the trip owner,
    whichever link they opened and even when they have no display name (the
    old client fell back to the email in that case)."""
    client, engine, _ = env
    _add_registered_visit(engine, project_name="My Trip", token_type="full",
                          display_name="Named", email="named@e.com")
    _add_registered_visit(engine, project_name="My Trip", token_type="no_memories",
                          display_name="", email="nameless@e.com")

    resp = client.get("/api/projects/My Trip/share/visitors")
    assert resp.status_code == 200
    body = resp.json()
    for bucket in ("full", "no_memories"):
        for entry in body[bucket]["registered"]:
            assert set(entry) == {"visitor_key", "display_name", "avatar_url", "last_seen_at"}
    assert "@e.com" not in resp.text
    assert "email" not in resp.text


def test_share_visitor_key_is_stable_pseudonymous_and_owner_scoped(env):
    """The key the owner sees is the same on every call, differs between
    visitors, is not the raw user id, and the same visitor gets a different
    key on another owner's trip — so two owners cannot correlate visitors."""
    client, engine, uid = env
    v1 = _add_registered_visit(engine, project_name="My Trip", token_type="full",
                               display_name="One", email="one@e.com")
    v2 = _add_registered_visit(engine, project_name="My Trip", token_type="full",
                               display_name="Two", email="two@e.com")

    first = client.get("/api/projects/My Trip/share/visitors").json()
    second = client.get("/api/projects/My Trip/share/visitors").json()
    keys = {e["display_name"]: e["visitor_key"] for e in first["full"]["registered"]}
    assert keys == {e["display_name"]: e["visitor_key"] for e in second["full"]["registered"]}
    assert keys["One"] != keys["Two"]
    assert keys["One"] not in (str(v1), v1) and keys["Two"] not in (str(v2), v2)
    assert str(v1) not in keys["One"]

    # Same visitor (v1) on a trip owned by someone else -> different key.
    with Session(engine) as sess:
        other = UserInfo(display_name="B", email="b@e.com")
        sess.add(other); sess.commit(); sess.refresh(other)
        proj = DBProject(user_info_id=other.id, name="Other Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        sess.add(DBShareVisit(
            project_id=proj.id, token_type="full", visitor_type="registered",
            user_info_id=v1, last_seen_at=400.0,
        ))
        sess.commit()
        other_id = other.id
    client.app.dependency_overrides[get_current_user] = (
        lambda: {"sub": str(other_id), "email": "b@e.com"})
    other_view = client.get("/api/projects/Other Trip/share/visitors").json()
    [entry] = other_view["full"]["registered"]
    assert entry["display_name"] == "One"
    assert entry["visitor_key"] != keys["One"]


def test_share_visitors_project_not_found(env):
    client, *_ = env
    resp = client.get("/api/projects/No Such Trip/share/visitors")
    assert resp.status_code == 404

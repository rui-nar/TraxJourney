"""No response shows another user's address as their name (issue #507).

Covers the surfaces outside the members API: the public share's owner_name,
comment and like author names (owner-side and share-side), and the signed-in
visitors list. Each is checked for a blank name, a name equal to
``UserInfo.email`` (other case) and a name equal to the linked
``LocalUser.username`` with no email — what the pre-#507 login auto-create
stored.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user, get_optional_current_user
from api.memories import router as memories_router
from api.project_shares import router as project_shares_router
from api.share import invalidate_share_cache, router as share_router
from models.project_db import (
    DBMemory, DBProject, DBProjectItem, DBProjectMember, DBShareVisit,
)
from models.user import LocalUser, UserInfo

KINDS = ["blank", "email", "username"]


def _make_user(sess, kind: str, address: str) -> int:
    if kind == "blank":
        u = UserInfo(display_name="", email=address)
    elif kind == "email":
        u = UserInfo(display_name=address.upper(), email=address)
    else:
        local = LocalUser(username=address)
        sess.add(local); sess.commit(); sess.refresh(local)
        u = UserInfo(display_name=address, email="", local_auth_id=local.id)
    sess.add(u); sess.commit(); sess.refresh(u)
    return u.id


@pytest.fixture
def make_env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)

    def _make(kind: str):
        with Session(engine) as sess:
            owner_id = _make_user(sess, kind, "owner@e.com")
            fan_id = _make_user(sess, kind, "fan@e.com")
            proj = DBProject(user_info_id=owner_id, name="Trip",
                             share_token="tok_full")
            sess.add(proj); sess.commit(); sess.refresh(proj)
            mem = DBMemory(project_id=proj.id, public_id="pub1",
                           date="2024-06-01", name="Lake")
            sess.add(mem); sess.commit(); sess.refresh(mem)
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                   item_type="memory", memory_id=mem.id))
            # The fan is also a companion, for the owner-side memory routes.
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=fan_id,
                                     role="editor", invited_by=owner_id))
            sess.commit()
            ids = {"owner": owner_id, "fan": fan_id, "memory": mem.id}
        invalidate_share_cache("tok_full")

        current = {"uid": None}

        def _user():
            return None if current["uid"] is None else {"sub": str(current["uid"])}

        app = FastAPI()
        app.dependency_overrides[get_current_user] = _user
        app.dependency_overrides[get_optional_current_user] = _user
        app.include_router(share_router)
        app.include_router(memories_router)
        app.include_router(project_shares_router)

        def act_as(who):
            current["uid"] = None if who is None else ids[who]

        return TestClient(app), ids, act_as

    return _make


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("path", ["/api/share/tok_full", "/api/share/tok_full/meta"])
def test_public_share_owner_name(make_env, kind, path):
    client, _, act_as = make_env(kind)
    act_as(None)
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert r.json()["owner_name"] == "Traveller"
    assert "@" not in r.text


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("base", ["/api/memories/{m}", "/api/share/tok_full/memories/{m}"])
def test_comment_author_name(make_env, kind, base):
    client, ids, act_as = make_env(kind)
    url = base.format(m=ids["memory"])
    act_as("fan")
    r = client.post(f"{url}/comments", json={"text": "Lovely"})
    assert r.status_code == 201, r.text
    act_as("owner")
    r = client.get(f"{url}/comments")
    assert r.status_code == 200, r.text
    assert [c["commenter_name"] for c in r.json()] == ["Traveller"]
    assert "@" not in r.text


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("base", ["/api/memories/{m}", "/api/share/tok_full/memories/{m}"])
def test_like_author_name(make_env, kind, base):
    client, ids, act_as = make_env(kind)
    url = base.format(m=ids["memory"])
    act_as("fan")
    assert client.post(f"{url}/like").status_code == 204
    act_as("owner")
    r = client.get(f"{url}/likes")
    assert r.status_code == 200, r.text
    assert [lk["name"] for lk in r.json()["likers"]] == ["Traveller"]
    assert "@" not in r.text


@pytest.mark.parametrize("kind", KINDS)
def test_signed_in_visitor_name(make_env, kind):
    client, _, act_as = make_env(kind)
    act_as("fan")
    assert client.get("/api/share/tok_full/meta").status_code == 200
    act_as("owner")
    r = client.get("/api/projects/Trip/share/visitors")
    assert r.status_code == 200, r.text
    assert [v["display_name"] for v in r.json()["full"]["registered"]] == ["Traveller"]
    assert "@" not in r.text


def _visitors_query_count(n: int, monkeypatch) -> int:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(owner); sess.commit(); sess.refresh(owner)
        owner_id = owner.id
        proj = DBProject(user_info_id=owner_id, name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        for i in range(n):
            vid = _make_user(sess, "username", f"v{i}@e.com")
            sess.add(DBShareVisit(project_id=proj.id, token_type="full",
                                  visitor_type="registered", user_info_id=vid,
                                  last_seen_at=1.0))
        sess.commit()
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    app.include_router(project_shares_router)
    client = TestClient(app)

    selects: list[str] = []

    def _count(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.strip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        r = client.get("/api/projects/Trip/share/visitors")
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert r.status_code == 200
    assert len(r.json()["full"]["registered"]) == n
    return len(selects)


def test_visitor_names_are_looked_up_in_one_batch(monkeypatch):
    """The sign-in check must not add a query per visitor."""
    assert _visitors_query_count(2, monkeypatch) == _visitors_query_count(6, monkeypatch)

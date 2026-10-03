"""PUT /api/auth/me refuses a blank display name (issue #507).

A blank name shows to companions as "Traveller", so clearing it is refused
with 422 and the stored name is kept.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from api.auth import router as auth_router
from api.deps import get_current_user
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
        user = UserInfo(display_name="Ada Lovelace", email="ada@e.com")
        sess.add(user); sess.commit(); sess.refresh(user)
        uid = user.id
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(auth_router)
    return TestClient(app), engine, uid


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_display_name_is_refused_and_kept(env, blank):
    client, engine, uid = env
    r = client.put("/api/auth/me", json={"display_name": blank})
    assert r.status_code == 422, r.text
    with Session(engine) as sess:
        assert sess.get(UserInfo, uid).display_name == "Ada Lovelace"


def test_display_name_is_trimmed_and_saved(env):
    client, engine, uid = env
    r = client.put("/api/auth/me", json={"display_name": "  Ada King  "})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["display_name"] == "Ada King"
    with Session(engine) as sess:
        assert sess.get(UserInfo, uid).display_name == "Ada King"

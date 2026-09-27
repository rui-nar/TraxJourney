"""Creating or renaming a trip into a name taken at the same instant (issue #467).

Trip names are unique per owner in the database (#452). Create and rename
check the name first, but a concurrent request can take it between the check
and the write; the unique index then refuses the write. That used to surface
as a 500. It is now the same 409 a plain duplicate gets, and nothing of the
losing request is written.

The race is forced by committing the competing trip from another connection
right after the endpoint's own name check has passed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBProject
from models.user import UserInfo


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app, one signed-in user, a file-backed DB (two connections)."""
    import api.router as router

    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(project_shared_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id

    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app, raise_server_exceptions=False), engine, uid
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _names(engine, uid) -> set[str]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBProject.name).where(DBProject.user_info_id == uid)))


def _row(engine, uid, name) -> DBProject:
    with Session(engine) as sess:
        return sess.exec(select(DBProject).where(
            DBProject.user_info_id == uid, DBProject.name == name)).one()


def _steal_name_after_check(monkeypatch, engine, uid, stolen: str):
    """Once, right after the endpoint has checked that *stolen* is free, commit
    a trip under it from another connection: a concurrent request winning the
    race between the check and the write."""
    repo_cls = type(project_shared_mod._repo)
    real = repo_cls.project_exists
    fired = {"done": False}

    def _patched(self, sess, user_info_id, name):
        exists = real(self, sess, user_info_id, name)
        if name == stolen and not fired["done"]:
            fired["done"] = True
            with Session(engine) as other:
                other.add(DBProject(user_info_id=uid, name=stolen))
                other.commit()
        return exists

    monkeypatch.setattr(repo_cls, "project_exists", _patched)
    return fired


def test_create_that_loses_the_race_is_a_409_not_a_500(env, monkeypatch):
    client, engine, uid = env
    fired = _steal_name_after_check(monkeypatch, engine, uid, "Alps")

    r = client.post("/api/projects/", json={"name": "Alps"})

    assert fired["done"]
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Project 'Alps' already exists"
    # Only the competitor's trip exists.
    assert _names(engine, uid) == {"Alps"}


def test_create_race_answers_as_a_plain_duplicate_does(env, monkeypatch):
    client, engine, uid = env
    assert client.post("/api/projects/", json={"name": "Alps"}).status_code == 201
    plain = client.post("/api/projects/", json={"name": "Alps"})

    _steal_name_after_check(monkeypatch, engine, uid, "Lakes")
    raced = client.post("/api/projects/", json={"name": "Lakes"})

    assert plain.status_code == raced.status_code == 409
    assert set(plain.json()) == set(raced.json())
    assert raced.json()["detail"] == plain.json()["detail"].replace("Alps", "Lakes")


def test_rename_that_loses_the_race_is_a_409_not_a_500(env, monkeypatch):
    client, engine, uid = env
    assert client.post("/api/projects/", json={"name": "Old"}).status_code == 201
    before = _row(engine, uid, "Old")
    fired = _steal_name_after_check(monkeypatch, engine, uid, "New")

    r = client.put("/api/projects/Old",
                   json={"new_name": "New", "trip_start": "2024-06-01"})

    assert fired["done"]
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Project 'New' already exists"
    # The losing request wrote nothing: not the name, not the date, and the
    # lock counter did not move (issue #397).
    assert _names(engine, uid) == {"Old", "New"}
    after = _row(engine, uid, "Old")
    assert after.trip_start == before.trip_start
    assert after.lock_version == before.lock_version


def test_rename_race_answers_as_a_plain_duplicate_does(env, monkeypatch):
    client, engine, uid = env
    for name in ("Old", "Taken"):
        assert client.post("/api/projects/", json={"name": name}).status_code == 201
    plain = client.put("/api/projects/Old", json={"new_name": "Taken"})

    _steal_name_after_check(monkeypatch, engine, uid, "New")
    raced = client.put("/api/projects/Old", json={"new_name": "New"})

    assert plain.status_code == raced.status_code == 409
    assert set(plain.json()) == set(raced.json())
    assert raced.json()["detail"] == plain.json()["detail"].replace("Taken", "New")


def test_a_rename_that_wins_still_bumps_the_lock(env):
    """The early flush must not change what a successful rename writes."""
    client, engine, uid = env
    assert client.post("/api/projects/", json={"name": "Old"}).status_code == 201
    before = _row(engine, uid, "Old").lock_version

    r = client.put("/api/projects/Old", json={"new_name": "New", "trip_start": "2024-06-01"})

    assert r.status_code == 200, r.text
    assert _names(engine, uid) == {"New"}
    after = _row(engine, uid, "New")
    assert after.trip_start == "2024-06-01"
    assert after.lock_version == before + 1

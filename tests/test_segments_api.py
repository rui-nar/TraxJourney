"""API tests for plain Segment CRUD (create/update/delete) — split out of
api/projects.py into api/segments.py. Async route resolution has its own
coverage in tests/test_resolve_route_async.py; this file covers the basic
create/update/delete happy paths and 404s that previously had no test."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from api.deps import get_current_user
from api.segments import router as segments_router
from models.project_db import DBProject
from models.user import UserInfo


def _seed(engine) -> tuple[int, int]:
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        proj = DBProject(user_info_id=u.id, name="My Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        return u.id, proj.id


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    uid, _pid = _seed(engine)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    app.include_router(segments_router)
    return TestClient(app), engine, uid


def _segment_body(**overrides) -> dict:
    body = {
        "segment_type": "flight",
        "label": "SFO -> JFK",
        "start_lat": 37.6,
        "start_lon": -122.4,
        "end_lat": 40.6,
        "end_lon": -73.8,
    }
    body.update(overrides)
    return body


def _load_project(engine, uid, name="My Trip"):
    import api.segments as segments_mod
    with Session(engine) as sess:
        return segments_mod._repo.get_project(sess, uid, name)


def test_create_segment_returns_id_and_is_added(env):
    client, engine, uid = env
    resp = client.post("/api/projects/My Trip/segments", json=_segment_body())
    assert resp.status_code == 201, resp.text
    seg_id = resp.json()["id"]
    assert seg_id

    project = _load_project(engine, uid)
    segs = [i.segment for i in project.items if i.item_type == "segment"]
    assert len(segs) == 1
    assert segs[0].id == seg_id
    assert segs[0].label == "SFO -> JFK"


def test_create_segment_project_not_found(env):
    client, *_ = env
    resp = client.post("/api/projects/No Such Trip/segments", json=_segment_body())
    assert resp.status_code == 404


def test_update_segment_changes_fields(env):
    client, engine, uid = env
    seg_id = client.post("/api/projects/My Trip/segments", json=_segment_body()).json()["id"]

    resp = client.put(
        f"/api/projects/My Trip/segments/{seg_id}",
        json=_segment_body(label="Renamed", segment_type="train"),
    )
    assert resp.status_code == 204, resp.text

    project = _load_project(engine, uid)
    seg = next(i.segment for i in project.items if i.item_type == "segment")
    assert seg.label == "Renamed"
    assert seg.segment_type == "train"


def test_update_segment_not_found(env):
    client, *_ = env
    resp = client.put("/api/projects/My Trip/segments/nope", json=_segment_body())
    assert resp.status_code == 404


def test_delete_segment_removes_it(env):
    client, engine, uid = env
    seg_id = client.post("/api/projects/My Trip/segments", json=_segment_body()).json()["id"]

    resp = client.delete(f"/api/projects/My Trip/segments/{seg_id}")
    assert resp.status_code == 204, resp.text

    project = _load_project(engine, uid)
    assert not any(i.item_type == "segment" for i in project.items)


def test_delete_segment_not_found(env):
    client, *_ = env
    resp = client.delete("/api/projects/My Trip/segments/nope")
    assert resp.status_code == 404


def test_segment_saves_never_touch_another_trips_row(env):
    """A segment write saves the whole trip, and with it only the trip's own
    activity rows (#592): another trip's row of the same Strava activity is
    neither read nor written, and that trip is not bumped."""
    from models.project_db import DBActivity, DBProjectItem
    from sqlmodel import select

    client, engine, uid = env
    with Session(engine) as sess:
        mine = sess.exec(select(DBProject).where(DBProject.name == "My Trip")).one()
        other = DBProject(user_info_id=uid, name="Other")
        sess.add(other)
        sess.commit()
        sess.add(DBActivity(id=10, user_info_id=uid, name="Mine", type="Ride",
                            strava_id=7, start_date="2024-06-01T10:00:00Z",
                            start_date_local="2024-06-01T10:00:00Z"))
        sess.add(DBActivity(id=7, user_info_id=uid, name="Theirs", type="Ride",
                            strava_id=7, start_date="2024-06-01T10:00:00Z",
                            start_date_local="2024-06-01T10:00:00Z"))
        sess.add(DBProjectItem(project_id=mine.id, position=0,
                               item_type="activity", activity_id=10))
        sess.add(DBProjectItem(project_id=other.id, position=0,
                               item_type="activity", activity_id=7))
        sess.commit()
        other_id, other_version = other.id, other.lock_version

    seg_id = client.post("/api/projects/My Trip/segments", json=_segment_body()).json()["id"]
    resp = client.put(f"/api/projects/My Trip/segments/{seg_id}",
                      json=_segment_body(label="SFO -> EWR"))
    assert resp.status_code == 204, resp.text

    with Session(engine) as sess:
        assert {r.id: r.name for r in sess.exec(select(DBActivity)).all()} == {
            10: "Mine", 7: "Theirs"}
        assert sess.exec(select(DBProjectItem.activity_id).where(
            DBProjectItem.project_id == other_id)).all() == [7]
        assert sess.get(DBProject, other_id).lock_version == other_version

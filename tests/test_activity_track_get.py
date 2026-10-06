"""``GET …/activities/{id}/track``: a single-row read with edit snapshots (E2EE remnants U2).

* It loads only the requested activity's row — no query reads another
  activity's polyline or profile (R4-7).
* For an edited row it returns ``original_polyline`` and
  ``original_elevation_profile_json`` as stored, to the owner and to members
  with the editor role or above — the role reset requires — and not to a
  viewer (R3-4). An unedited row carries none.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.activities import router as activities_router
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem, DBProjectMember
from models.user import UserInfo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]
_POLY = polyline_lib.encode(_TRACK)
_EP = json.dumps({"distances_km": [0.0, 1.0, 2.0], "elevations_m": [100.0, 110.0, 105.0]})
_ENC_ORIG_POLY = "v1.d2VsY29tZQ==.b3JpZ3BvbHk="
_ENC_ORIG_EP = "v1.d2VsY29tZQ==.b3JpZ2Vw"


@pytest.fixture
def env(monkeypatch):
    """Owner's trip "Trip" (lock 9) holds edited activity 111, unedited 222
    and edited 333. Ed is an editor member, Vi a viewer, Co a co-owner."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com")
                 for n in ("owner", "ed", "vi", "co")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        for u in users.values():
            sess.refresh(u)
        ids = {n: u.id for n, u in users.items()}
        proj = DBProject(user_info_id=ids["owner"], name="Trip", lock_version=9)
        sess.add(proj); sess.commit(); sess.refresh(proj)
        for name, role in (("ed", "editor"), ("vi", "viewer"), ("co", "co-owner")):
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=ids[name],
                                     role=role, invited_by=ids["owner"]))
        sess.add(DBActivity(
            id=111, user_info_id=ids["owner"], name="Edited", type="Ride",
            summary_polyline=_POLY, elevation_profile_json=_EP, is_edited=True,
            original_polyline=_ENC_ORIG_POLY,
            original_elevation_profile_json=_ENC_ORIG_EP,
        ))
        sess.add(DBActivity(
            id=222, user_info_id=ids["owner"], name="Plain", type="Ride",
            summary_polyline=_POLY, elevation_profile_json=_EP,
        ))
        sess.add(DBActivity(
            id=333, user_info_id=ids["owner"], name="Other", type="Ride",
            summary_polyline=_POLY, elevation_profile_json=_EP, is_edited=True,
            original_polyline=_POLY, original_elevation_profile_json=_EP,
        ))
        for pos, aid in enumerate((111, 222, 333)):
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()

    caller = {"id": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(caller["id"]), "email": "x@e.com"}
    app.include_router(activities_router)
    return TestClient(app), engine, ids, caller


def _get(client, ids, activity_id, *, as_member=False):
    url = f"/api/projects/Trip/activities/{activity_id}/track"
    if as_member:
        url += f"?owner={ids['owner']}"
    return client.get(url)


@pytest.mark.parametrize("who", ["owner", "ed", "co"])
def test_edited_row_returns_originals_to_editors_and_above(env, who):
    client, _, ids, caller = env
    caller["id"] = ids[who]
    resp = _get(client, ids, 111, as_member=who != "owner")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["original_polyline"] == _ENC_ORIG_POLY
    assert body["original_elevation_profile_json"] == _ENC_ORIG_EP
    assert body["lock_version"] == 9
    assert body["map"]["summary_polyline"] == _POLY
    assert body["elevation_profile"][0] == [0.0, 100.0]


def test_viewer_gets_the_track_without_originals(env):
    client, _, ids, caller = env
    caller["id"] = ids["vi"]
    resp = _get(client, ids, 111, as_member=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["map"]["summary_polyline"] == _POLY
    assert "original_polyline" not in body
    assert "original_elevation_profile_json" not in body


def test_unedited_row_carries_no_originals(env):
    client, _, ids, _ = env
    resp = _get(client, ids, 222)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "original_polyline" not in body
    assert "original_elevation_profile_json" not in body


def test_activity_not_in_the_trip_is_404(env):
    client, engine, ids, _ = env
    with Session(engine) as sess:
        sess.add(DBActivity(id=444, user_info_id=ids["owner"], name="Away",
                            type="Ride", summary_polyline=_POLY))
        sess.commit()
    assert _get(client, ids, 444).status_code == 404


def test_reads_no_other_activitys_heavy_columns(env):
    """Only the requested row's geometry is read: no statement that selects
    a polyline or profile column is bound to another activity's id."""
    client, engine, ids, _ = env
    seen = []

    def _capture(_conn, _cursor, statement, parameters, _context, _many):
        seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = _get(client, ids, 111)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    assert resp.status_code == 200, resp.text

    heavy = [(s, p) for s, p in seen
             if "summary_polyline" in s or "elevation_profile_json" in s]
    assert heavy, "expected the requested row's geometry to be read"
    for statement, params in heavy:
        flat = list(params) if isinstance(params, (list, tuple)) else list(params.values())
        assert 222 not in flat and 333 not in flat, statement


def test_originals_withheld_once_the_rows_owner_has_left_the_trip(env):
    """A companion's edited ride stays in the trip after he leaves, but the
    trip may no longer rewrite it (reset is refused), so its snapshots are
    not handed out either."""
    client, engine, ids, caller = env
    with Session(engine) as sess:
        proj = sess.exec(select(DBProject)).first()
        sess.add(DBActivity(
            id=555, user_info_id=ids["ed"], name="Eds ride", type="Ride",
            summary_polyline=_POLY, elevation_profile_json=_EP, is_edited=True,
            original_polyline=_POLY, original_elevation_profile_json=_EP,
        ))
        sess.add(DBProjectItem(project_id=proj.id, position=3,
                               item_type="activity", activity_id=555))
        sess.commit()

    for who in ("owner", "co"):
        caller["id"] = ids[who]
        body = _get(client, ids, 555, as_member=who != "owner").json()
        assert body["original_polyline"] == _POLY, who

    with Session(engine) as sess:
        m = sess.exec(select(DBProjectMember).where(
            DBProjectMember.user_info_id == ids["ed"])).one()
        sess.delete(m)
        sess.commit()

    for who in ("owner", "co"):
        caller["id"] = ids[who]
        resp = _get(client, ids, 555, as_member=who != "owner")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["map"]["summary_polyline"] == _POLY
        assert "original_polyline" not in body, who
        assert "original_elevation_profile_json" not in body, who

"""Removing an activity from a trip deletes its row (#592, plan Decision 9).

A row belongs to the one trip holding it, so once its item is removed nothing
else reaches it: it goes with its prepared geometry, whoever removed it. That
includes a companion removing the owner's activity (owner decision EQ1-1),
which used to only unlink it. Another trip's row of the same Strava activity
is that trip's, and stays.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import api.project_shared as project_shared_mod
import api.project_transfer as transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity, DBActivityGeoPrepared, DBProject, DBProjectItem
from models.user import UserInfo
from src.project.project_io import ProjectIO

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03), (48.0, 2.04)]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The owner's trip "Alps" holding Strava activities 1 and 2, the same
    file kept as a second trip "Alps (2)", and a companion of "Alps"."""
    import api.router as router

    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, transfer_mod, storage_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB", "FREE_MAX_TRIP_DAYS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com") for n in ("owner", "companion")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}
    current = {"uid": ids["owner"]}
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}

    def act_as(who: str) -> None:
        current["uid"] = ids[who]

    client = TestClient(router.app)
    content = json.dumps({
        "version": 1, "name": "x",
        "items": [{"item_type": "activity", "activity_id": aid} for aid in (1, 2)],
        "activities": [{
            "id": aid, "name": f"Ride {aid}", "type": "Ride",
            "start_date": f"2024-06-0{aid}T10:00:00Z",
            "start_date_local": f"2024-06-0{aid}T10:00:00Z",
            "map": {"summary_polyline": polyline_lib.encode(_TRACK)},
        } for aid in (1, 2)],
    }).encode("utf-8")
    for on_conflict in (None, "copy"):
        r = client.post(
            "/api/projects/import",
            params={"on_conflict": on_conflict} if on_conflict else {},
            files={"file": (f"Alps{ProjectIO.EXTENSION}", content, "application/json")})
        assert r.status_code == 201, r.text
    token = client.post("/api/projects/Alps/members/invite").json()["token"]
    act_as("companion")
    assert client.post(f"/api/invites/{token}/accept").status_code == 200
    act_as("owner")
    try:
        yield client, engine, ids, act_as
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _trip_rows(engine, owner: int, name: str) -> list[int]:
    with Session(engine) as sess:
        project_id = sess.exec(select(DBProject.id).where(
            DBProject.user_info_id == owner, DBProject.name == name)).one()
        return list(sess.exec(select(DBProjectItem.activity_id).where(
            DBProjectItem.project_id == project_id,
            DBProjectItem.item_type == "activity",
        ).order_by(DBProjectItem.position)).all())


def _exists(engine, activity_id: int) -> tuple[bool, bool]:
    """Whether the row, and its prepared geometry, exist."""
    with Session(engine) as sess:
        return (sess.get(DBActivity, activity_id) is not None,
                sess.get(DBActivityGeoPrepared, activity_id) is not None)


@pytest.mark.parametrize("who", ["owner", "companion"])
def test_removing_an_activity_deletes_its_row_and_only_its_row(env, who):
    client, engine, ids, act_as = env
    first, second = _trip_rows(engine, ids["owner"], "Alps")
    copies = _trip_rows(engine, ids["owner"], "Alps (2)")
    assert _exists(engine, first) == (True, True)

    act_as(who)
    r = client.delete(f"/api/projects/Alps/items/0?owner={ids['owner']}")

    assert r.status_code == 204, r.text
    assert _trip_rows(engine, ids["owner"], "Alps") == [second]
    assert _exists(engine, first) == (False, False)
    # The other trip's rows of the same Strava activities are its own.
    assert _trip_rows(engine, ids["owner"], "Alps (2)") == copies
    assert all(_exists(engine, aid) == (True, True) for aid in copies + [second])


def test_removing_a_split_piece_deletes_it_and_renumbers_the_rest(env):
    client, engine, ids, act_as = env
    first, _second = _trip_rows(engine, ids["owner"], "Alps")
    r = client.post(f"/api/projects/Alps/activities/{first}/split", json={"split_index": 2})
    assert r.status_code == 200, r.text
    head, tail, _ = _trip_rows(engine, ids["owner"], "Alps")
    with Session(engine) as sess:
        assert sess.get(DBActivity, tail).split_root_id == head

    act_as("companion")
    r = client.delete(f"/api/projects/Alps/items/1?owner={ids['owner']}")

    assert r.status_code == 204, r.text
    assert _exists(engine, tail) == (False, False)
    with Session(engine) as sess:
        assert sess.get(DBActivity, head).name == "Ride 1"


def test_a_departed_companions_encrypted_row_goes_with_its_item(env):
    """An encrypted row keeps the companion whose key wrote it as owner (plan
    Decision 4). Once that companion has left the trip, the owner removing its
    item still deletes the row and its prepared geometry: removal deletes the
    trip's row whoever owns it (Decision 9, EQ1-1)."""
    from models.project_db import DBProjectMember

    client, engine, ids, _act_as = env
    sealed = "v1." + "A" * 96 + "." + "B" * 64
    with Session(engine) as sess:
        project = sess.exec(select(DBProject).where(
            DBProject.user_info_id == ids["owner"], DBProject.name == "Alps")).one()
        sess.add(DBActivity(id=-77, user_info_id=ids["companion"], name=sealed, type="Ride",
                            start_date="2024-06-09T10:00:00Z",
                            start_date_local="2024-06-09T10:00:00Z"))
        sess.add(DBActivityGeoPrepared(activity_id=-77, version=1, blob=b"geo"))
        sess.add(DBProjectItem(project_id=project.id, position=2,
                               item_type="activity", activity_id=-77))
        for member in sess.exec(select(DBProjectMember).where(
                DBProjectMember.project_id == project.id)).all():
            sess.delete(member)
        sess.commit()
    assert _trip_rows(engine, ids["owner"], "Alps")[-1] == -77

    r = client.delete("/api/projects/Alps/items/2")

    assert r.status_code == 204, r.text
    assert -77 not in _trip_rows(engine, ids["owner"], "Alps")
    assert _exists(engine, -77) == (False, False)

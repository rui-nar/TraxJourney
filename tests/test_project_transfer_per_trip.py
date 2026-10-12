"""A .traxj export and its import keep a trip's activities its own (#592).

An import gives every activity the file carries a new row of the trip it
writes (plan Decision 8), so a copy of your own trip ("Keep both") is
independent of it. The export carries what the import restores: the Strava
id, the split family and the edit snapshot, so a copied split is still a
split, and resetting it in the copy restores the untouched track there and
nowhere else.
"""
from __future__ import annotations

import base64
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
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.project.project_io import ProjectIO

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03), (48.0, 2.04)]
_POLYLINE = polyline_lib.encode(_TRACK)


@pytest.fixture
def env(monkeypatch, tmp_path):
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
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(ids["owner"])}
    try:
        yield TestClient(router.app), engine, ids
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _file(activities) -> bytes:
    return json.dumps({
        "version": 1, "name": "x",
        "items": [{"item_type": "activity", "activity_id": a["id"]} for a in activities],
        "activities": list(activities),
    }).encode("utf-8")


def _ride(act_id: int, name: str = "Ride", **extra) -> dict:
    return {
        "id": act_id, "name": name, "type": "Ride",
        "distance": 3000.0, "moving_time": 1000, "elapsed_time": 1200,
        "total_elevation_gain": 40.0,
        "start_date": "2024-06-01T10:00:00Z", "start_date_local": "2024-06-01T12:00:00Z",
        "start_latlng": [48.0, 2.0], "end_latlng": [48.0, 2.04],
        "map": {"summary_polyline": _POLYLINE},
        "elevation_profile": {"distances_km": [0.0, 0.75, 1.5, 2.25, 3.0],
                              "elevations_m": [100.0, 120.0, 110.0, 140.0, 130.0]},
        **extra,
    }


def _import(client, name: str, content: bytes, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    r = client.post(
        "/api/projects/import", params=params,
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})
    assert r.status_code == 201, r.text
    return r.json()["name"]


def _trip_rows(engine, owner: int, name: str) -> list[DBActivity]:
    """The trip's activity rows, in timeline order."""
    with Session(engine) as sess:
        project_id = sess.exec(select(DBProject.id).where(
            DBProject.user_info_id == owner, DBProject.name == name)).one()
        ids = sess.exec(select(DBProjectItem.activity_id).where(
            DBProjectItem.project_id == project_id,
            DBProjectItem.item_type == "activity",
        ).order_by(DBProjectItem.position)).all()
        rows = [sess.get(DBActivity, aid) for aid in ids]
        for row in rows:
            sess.expunge(row)
        return rows


def test_keep_both_of_a_split_and_edited_trip_gives_an_independent_family(env):
    client, engine, ids = env
    _import(client, "Alps", _file([_ride(111)]))
    (head,) = _trip_rows(engine, ids["owner"], "Alps")
    r = client.post(f"/api/projects/Alps/activities/{head.id}/split", json={"split_index": 2})
    assert r.status_code == 200, r.text
    original = _trip_rows(engine, ids["owner"], "Alps")
    assert [a.name for a in original] == ["Ride (1/2)", "Ride (2/2)"]

    exported = client.get("/api/projects/Alps/export-traxj").content
    acts = {a["id"]: a for a in json.loads(exported)["activities"]}
    assert acts[head.id]["strava_id"] == 111
    assert acts[head.id]["original_polyline"] == _POLYLINE
    assert acts[original[1].id]["split_root_id"] == head.id

    copy = _import(client, "Alps", exported, on_conflict="copy")

    assert copy == "Alps (2)"
    head2, tail2 = _trip_rows(engine, ids["owner"], copy)
    # New rows, under the sign convention (plan Decision 3).
    assert not {head2.id, tail2.id} & {a.id for a in original}
    assert head2.id > 0 and tail2.id < 0
    assert (head2.strava_id, tail2.strava_id) == (111, None)
    # The family is the copy's own, linked through its new rows.
    assert (head2.name, head2.split_base_name, head2.split_root_id) == ("Ride (1/2)", "Ride", None)
    assert (tail2.name, tail2.split_root_id, tail2.split_parent_id) == (
        "Ride (2/2)", head2.id, head2.id)
    # The edit snapshot came along: the untouched track and its figures.
    assert head2.is_edited and tail2.is_edited
    assert head2.original_polyline == _POLYLINE
    assert head2.original_distance == 3000.0
    assert json.loads(head2.original_elevation_profile_json)["elevations_m"] == [
        100.0, 120.0, 110.0, 140.0, 130.0]
    assert head2.summary_polyline == original[0].summary_polyline != _POLYLINE

    # Resetting the copy's split restores the whole track there, and only there.
    r = client.post(f"/api/projects/{copy}/activities/{head2.id}/reset")
    assert r.status_code == 200, r.text
    (restored,) = _trip_rows(engine, ids["owner"], copy)
    assert (restored.id, restored.name, restored.summary_polyline, restored.is_edited) == (
        head2.id, "Ride", _POLYLINE, False)
    assert [(a.id, a.name, a.split_root_id) for a in _trip_rows(engine, ids["owner"], "Alps")] == [
        (a.id, a.name, a.split_root_id) for a in original]


def test_a_piece_whose_family_the_file_lacks_becomes_a_plain_activity(env):
    """A link names an id of the same file. A piece whose parent the file does
    not carry, or whose parents loop, hangs off the family's first piece; one
    whose first piece the file does not carry as one has no family left."""
    client, engine, ids = env
    _import(client, "Alps", _file([
        _ride(111, "Ride (1/5)", split_base_name="Ride", is_edited=True),
        _ride(-6, "Ride (2/5)", strava_id=None, split_root_id=111, split_parent_id=-5),
        _ride(-8, "Ride (3/5)", strava_id=None, split_root_id=111, split_parent_id=-9),
        _ride(-9, "Ride (4/5)", strava_id=None, split_root_id=111, split_parent_id=-8),
        _ride(-10, "Ride (5/5)", strava_id=None, split_root_id=111, split_parent_id=-6),
        _ride(-7, "Other (2/2)", strava_id=None, split_root_id=222, split_parent_id=222),
        _ride(-11, "Odd (2/2)", strava_id=None, split_root_id=-6, split_parent_id=-6),
    ]))

    head, cut_off, loop_a, loop_b, kept, orphan, odd = _trip_rows(engine, ids["owner"], "Alps")
    for piece in (cut_off, loop_a, loop_b):
        assert (piece.split_root_id, piece.split_parent_id) == (head.id, head.id)
    # A parent that leads back to the first piece is kept.
    assert (kept.split_root_id, kept.split_parent_id) == (head.id, cut_off.id)
    for plain in (orphan, odd):
        assert (plain.split_root_id, plain.split_parent_id, plain.split_base_name) == (
            None, None, None)
    assert orphan.name == "Other (2/2)"


def _envelope() -> str:
    b64 = lambda n: base64.urlsafe_b64encode(bytes(n)).decode().rstrip("=")  # noqa: E731
    return f"v1.{b64(72)}.{b64(48)}"


def test_replace_keeps_an_encrypted_rows_owner_and_a_copy_takes_the_importer(env):
    """A row holding an envelope keeps the owner whose key wrote it (plan
    Decision 4): Replacing the trip with the file naming that row gives the
    new row the same owner, else nobody could read it. A copy is a new trip,
    whose rows are the importer's."""
    client, engine, ids = env
    sealed = _envelope()
    _import(client, "Alps", _file([_ride(50)]))
    (row,) = _trip_rows(engine, ids["owner"], "Alps")
    with Session(engine) as sess:
        stored = sess.get(DBActivity, row.id)
        stored.name = sealed
        stored.user_info_id = ids["companion"]
        stored.strava_user_id = ids["companion"]
        sess.add(stored)
        sess.commit()
    content = _file([_ride(row.id, sealed, strava_id=50)])

    _import(client, "Alps", content, on_conflict="replace")

    (new,) = _trip_rows(engine, ids["owner"], "Alps")
    assert new.id != row.id
    assert (new.name, new.user_info_id, new.strava_user_id) == (
        sealed, ids["companion"], ids["companion"])

    copy = _import(client, "Alps", content, on_conflict="copy")
    (copied,) = _trip_rows(engine, ids["owner"], copy)
    assert (copied.name, copied.user_info_id, copied.strava_user_id) == (
        sealed, ids["owner"], ids["owner"])

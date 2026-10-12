"""A Replace import deletes the replaced trip's activity rows (issues #509, #592).

Regression (integrated review I-R1-2): ``on_conflict=replace`` deleted every
item of the trip and wrote the file's, but left the Strava activity rows the
old timeline held and the file did not — name, track, prepared geometry —
in the database for good, as deleting the trip used to. Both routes are
covered: ``/import`` (.traxj) and ``/import-zip``.

Since #592 a row belongs to the one trip holding it, and an import gives every
file activity a new row (plan Decisions 8 and 9): a Replace deletes all of the
trip's rows, split families whole, in the same transaction as it writes the
file's, and never touches another trip's row of the same activity.
"""
from __future__ import annotations

import io
import json
import zipfile

import polyline as polyline_lib
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import api.memories as memories_mod
import api.project_shared as project_shared_mod
import api.project_transfer as transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity, DBActivityGeoPrepared, DBProject, DBProjectItem
from models.user import UserInfo
from src.project.project_io import ProjectIO

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]


@pytest.fixture
def env(monkeypatch, tmp_path):
    import api.router as router

    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, transfer_mod, storage_mod, journal_mod, memories_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB", "FREE_MAX_TRIP_DAYS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        owner = UserInfo(display_name="owner", email="owner@e.com")
        sess.add(owner)
        sess.commit()
        uid = owner.id
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app), engine, uid
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


# ── Seeding and files ──────────────────────────────────────────────────────────

def _activity(engine, act_id: int, owner: int, name: str = "Ride", **kw) -> None:
    with Session(engine) as sess:
        sess.add(DBActivity(id=act_id, user_info_id=owner, name=name, type="Ride",
                            start_date=kw.pop("start_date", "2026-06-10T09:00:00Z"),
                            start_date_local="2026-06-10T09:00:00Z", **kw))
        sess.add(DBActivityGeoPrepared(activity_id=act_id, version=1, blob=b"geo"))
        sess.commit()


def _split_family(engine, owner: int) -> None:
    """Root 111 split into three: -5 cut out of the root, -6 out of -5."""
    _activity(engine, 111, owner, name="Ride (1/3)", split_base_name="Ride", is_edited=True)
    _activity(engine, -5, owner, name="Ride (2/3)", split_root_id=111, split_parent_id=111,
              start_date="2026-06-10T10:00:00Z")
    _activity(engine, -6, owner, name="Ride (3/3)", split_root_id=111, split_parent_id=-5,
              start_date="2026-06-10T11:00:00Z")


def _trip(engine, owner: int, name: str, activity_ids) -> int:
    with Session(engine) as sess:
        proj = DBProject(user_info_id=owner, name=name)
        sess.add(proj)
        sess.commit()
        for pos, aid in enumerate(activity_ids):
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()
        return proj.id


def _raw_strava_activity(act_id: int) -> dict:
    return {
        "id": act_id, "name": "Ride", "type": "Ride",
        "distance": 1000.0, "moving_time": 100, "elapsed_time": 120,
        "total_elevation_gain": 0.0,
        "start_date": "2026-06-10T09:00:00Z", "start_date_local": "2026-06-10T09:00:00Z",
        "map": {"summary_polyline": polyline_lib.encode(_TRACK)},
    }


def _traxj(activity_ids) -> bytes:
    """A trip file whose timeline holds *activity_ids*, in that order."""
    return json.dumps({
        "version": 1, "name": "x",
        "items": [{"item_type": "activity", "activity_id": aid} for aid in activity_ids],
        "activities": [_raw_strava_activity(aid) for aid in activity_ids],
    }).encode("utf-8")


def _replace(client, route: str, name: str, activity_ids) -> None:
    content = _traxj(activity_ids)
    if route == "traxj":
        resp = client.post(
            "/api/projects/import", params={"on_conflict": "replace"},
            files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{name}{ProjectIO.EXTENSION}", content)
        resp = client.post(
            "/api/projects/import-zip", params={"on_conflict": "replace"},
            files={"file": (f"{name}.zip", buf.getvalue(), "application/zip")})
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["outcome"] == "replaced"


def _rows(engine) -> set[int]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBActivity.id)).all())


def _prepared(engine) -> set[int]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBActivityGeoPrepared.activity_id)).all())


# ── Tests ──────────────────────────────────────────────────────────────────────

ROUTES = ["traxj", "zip"]


def _trip_rows(engine, project_id: int) -> set[int]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBProjectItem.activity_id).where(
            DBProjectItem.project_id == project_id,
            DBProjectItem.item_type == "activity")).all())


@pytest.mark.parametrize("route", ROUTES)
def test_replace_deletes_the_trips_rows_and_gives_the_files_new_ones(env, route):
    client, engine, uid = env
    _activity(engine, 1, uid, strava_id=1)
    _activity(engine, 2, uid, strava_id=2)
    trip = _trip(engine, uid, "Alps", [1, 2])

    _replace(client, route, "Alps", [2])

    (new,) = _trip_rows(engine, trip)
    assert _rows(engine) == {new} and new not in (1, 2)
    with Session(engine) as sess:
        assert sess.get(DBActivity, new).strava_id == 2
    # The new row's geometry is prepared from the file's track; the old
    # rows' prepared geometry went with them.
    assert _prepared(engine) == {new}


@pytest.mark.parametrize("route", ROUTES)
def test_replace_never_touches_another_trips_row_of_the_same_activity(env, route):
    client, engine, uid = env
    _activity(engine, 4, uid, strava_id=3)
    _activity(engine, 3, uid, strava_id=3)
    _trip(engine, uid, "Alps", [4])
    other = _trip(engine, uid, "Other", [3])
    with Session(engine) as sess:
        version_before = sess.get(DBProject, other).lock_version

    _replace(client, route, "Alps", [])

    assert _rows(engine) == {3}
    assert _prepared(engine) == {3}
    with Session(engine) as sess:
        assert sess.get(DBProject, other).lock_version == version_before


@pytest.mark.parametrize("route", ROUTES)
def test_replace_frees_a_split_family_the_file_dropped(env, route):
    client, engine, uid = env
    _split_family(engine, uid)
    _trip(engine, uid, "Alps", [111, -5, -6])

    _replace(client, route, "Alps", [])

    assert _rows(engine) == set()
    assert _prepared(engine) == set()


@pytest.mark.parametrize("route", ROUTES)
def test_replace_keeps_a_root_another_trip_holds_and_renumbers_it(env, route):
    """A split link across trips is what the migration removes (plan
    Decision 10); seeded here directly, the Replace still never deletes a row
    another trip holds."""
    client, engine, uid = env
    _split_family(engine, uid)
    _trip(engine, uid, "Alps", [111, -5, -6])
    other = _trip(engine, uid, "Other", [111])
    with Session(engine) as sess:
        version_before = sess.get(DBProject, other).lock_version

    _replace(client, route, "Alps", [])

    assert _rows(engine) == {111}
    with Session(engine) as sess:
        assert sess.get(DBActivity, 111).name == "Ride"
        assert sess.get(DBProject, other).lock_version > version_before


@pytest.mark.parametrize("route", ROUTES)
def test_a_failed_delete_rolls_the_whole_replace_back(env, route, monkeypatch):
    """The old rows go in the Replace's own transaction: when deleting them
    fails, nothing of the Replace is committed, so the trip never names rows
    that are gone nor keeps rows nothing names."""
    client, engine, uid = env
    _activity(engine, 1, uid, strava_id=1)
    trip = _trip(engine, uid, "Alps", [1])

    def _fail(*_args, **_kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(transfer_mod._repo, "delete_unreferenced_activities", _fail)

    with pytest.raises(RuntimeError, match="database is locked"):
        _replace(client, route, "Alps", [2])

    assert _trip_rows(engine, trip) == {1}
    assert _rows(engine) == {1}
    assert _prepared(engine) == {1}

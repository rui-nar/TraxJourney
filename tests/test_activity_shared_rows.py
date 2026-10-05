"""Rows another user's trip holds stay readable (E2EE remnants U16, decision 15).

An activity row is shared by every trip that holds it. When a trip owned by
someone other than the caller holds it, that trip's owner cannot read an
envelope, so the four writes that store one — ``PUT /api/activities/{id}``,
the encrypted track and split routes and the elevation-gain route — answer
409 ``shared_with_other_trip`` and write nothing, lock versions included, even
for the row's own owner. Plaintext is still taken, so the owner can decrypt
such a row back. Each activity of a trip payload says ``shared_with_others``,
computed in the load query; a ``.traxj`` export never carries it.
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, delete, select

import api.activities as activities_mod
import models.db as db_module
from api.activities import activity_fields_router, router as activities_router
from api.deps import get_current_user
from api.project_shared import _repo, build_details_payload, build_meta_payload
from api.project_transfer import _traxj_document
from models.project_db import DBActivity, DBProject, DBProjectItem, DBProjectMember
from models.user import UserInfo


def _env(seed: int) -> str:
    """An envelope as ``EncryptedField.encode`` writes one (see
    test_activity_encrypted_edit.py)."""
    wrapped = b"\xfb\xff\xbf" * 24
    ciphertext = bytes([seed % 256]) * 41
    return (f"v1.{base64.b64encode(wrapped).decode()}"
            f".{base64.b64encode(ciphertext).decode()}")


_ENCRYPTED = dict(
    summary_polyline=_env(1), elevation_profile_json=_env(2),
    elevation_profile_low_res_json=_env(2), start_latlng_json=_env(3),
    end_latlng_json=_env(4), distance=4000.0, moving_time=1000, elapsed_time=1200,
    average_speed=4.0, total_elevation_gain=60.0, elev_high=140.0, elev_low=100.0,
    start_date="2026-05-01T08:00:00Z", start_date_local="2026-05-01T10:00:00Z",
    timezone="Europe/Paris", source="gpx",
)


def _piece(seed: int):
    return dict(
        summary_polyline=_env(seed), elevation_profile_json=_env(seed + 1),
        start_latlng_json=_env(seed + 2), end_latlng_json=_env(seed + 3),
        distance=1000.0 + seed, moving_time=300 + seed, elapsed_time=400 + seed,
        average_speed=3.0 + seed / 100, total_elevation_gain=10.0 + seed,
        elev_high=130.0 + seed, elev_low=100.0 + seed,
    )


@pytest.fixture
def env(monkeypatch):
    """Owner's trip "Trip" (lock 7) holds three of the owner's rows:

    * 111 — encrypted, also held by the friend's trip "Theirs" (lock 5);
    * 112 — encrypted, held by "Trip" alone;
    * 113 — plaintext, also held by "Theirs".
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com") for n in ("owner", "friend")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}
        trips = {
            "Trip": DBProject(user_info_id=ids["owner"], name="Trip", lock_version=7),
            "Theirs": DBProject(user_info_id=ids["friend"], name="Theirs", lock_version=5),
        }
        for t in trips.values():
            sess.add(t)
        sess.commit()
        pids = {n: t.id for n, t in trips.items()}
        sess.add(DBActivity(id=111, user_info_id=ids["owner"], name=_env(5), type="Ride",
                            **_ENCRYPTED))
        sess.add(DBActivity(id=112, user_info_id=ids["owner"], name=_env(6), type="Ride",
                            **_ENCRYPTED))
        sess.add(DBActivity(id=113, user_info_id=ids["owner"], name="Plain", type="Ride",
                            summary_polyline="_p~iF~ps|U_ulLnnqC", distance=10.0,
                            start_date="2026-05-03T08:00:00Z"))
        for pos, aid in enumerate((111, 112, 113)):
            sess.add(DBProjectItem(project_id=pids["Trip"], position=pos,
                                   item_type="activity", activity_id=aid))
        for pos, aid in enumerate((111, 113)):
            sess.add(DBProjectItem(project_id=pids["Theirs"], position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()

    for fn in ("bust_geo_cache", "queue_stats_refresh", "queue_share_tiles_refresh"):
        monkeypatch.setattr(activities_mod, fn, lambda *a: None)

    caller = {"id": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(caller["id"])}
    app.include_router(activities_router)
    app.include_router(activity_fields_router)
    return dict(client=TestClient(app), engine=engine, ids=ids, caller=caller)


def _row(engine, aid):
    with Session(engine) as sess:
        a = sess.get(DBActivity, aid)
        return {c: getattr(a, c) for c in DBActivity.model_fields}


def _rows(engine):
    with Session(engine) as sess:
        return sorted(a.id for a in sess.exec(select(DBActivity)).all())


def _locks(engine):
    with Session(engine) as sess:
        return {p.name: p.lock_version for p in sess.exec(select(DBProject)).all()}


# The four writes, each storing an envelope (the gain route's value is the
# device's measure of an enveloped profile).
def _put_fields(env, aid):
    return env["client"].put(f"/api/activities/{aid}", json={"name": _env(9)})


def _put_fields_cas(env, aid):
    return env["client"].put(f"/api/activities/{aid}",
                             json={"name": _env(9), "project": "Trip", "lock_version": 7})


def _put_track(env, aid):
    return env["client"].put(f"/api/projects/Trip/activities/{aid}/track/encrypted",
                             json={**_piece(20), "lock_version": 7})


def _post_split(env, aid):
    return env["client"].post(f"/api/projects/Trip/activities/{aid}/split/encrypted",
                              json={"head": _piece(30), "tail": _piece(40),
                                    "tail_name": _env(50), "lock_version": 7})


def _put_gain(env, aid):
    return env["client"].put(f"/api/activities/{aid}/elevation-gain",
                             json={"total_elevation_gain": 77.0})


_WRITES = [_put_fields, _put_fields_cas, _put_track, _post_split, _put_gain]
_IDS = ["fields", "fields-cas", "track", "split", "gain"]


@pytest.mark.parametrize("write", _WRITES, ids=_IDS)
def test_an_envelope_on_a_row_another_users_trip_holds_is_refused(env, write):
    before, locks, rows = _row(env["engine"], 111), _locks(env["engine"]), _rows(env["engine"])
    resp = write(env, 111)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "shared_with_other_trip"
    assert resp.json()["detail"]["activity_id"] == 111
    assert _row(env["engine"], 111) == before
    assert _locks(env["engine"]) == locks
    assert _rows(env["engine"]) == rows


@pytest.mark.parametrize("write", _WRITES, ids=_IDS)
def test_the_same_write_on_a_row_only_the_owners_trips_hold_is_taken(env, write):
    assert write(env, 112).status_code == 200


def test_an_envelope_on_a_shared_plaintext_row_is_refused(env):
    before, locks = _row(env["engine"], 113), _locks(env["engine"])
    resp = env["client"].put("/api/activities/113",
                             json={"name": "Renamed", "summary_polyline": _env(9)})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "shared_with_other_trip"
    assert _row(env["engine"], 113) == before
    assert _locks(env["engine"]) == locks


def test_the_owner_may_write_plaintext_back_to_a_shared_enveloped_row(env):
    resp = env["client"].put("/api/activities/111", json={
        "name": "Morning ride", "start_latlng_json": "[43.6, 1.44]",
        "project": "Trip", "lock_version": 7,
    })
    assert resp.status_code == 200
    assert resp.json()["lock_version"] == 8
    row = _row(env["engine"], 111)
    assert row["name"] == "Morning ride"
    assert row["start_latlng_json"] == "[43.6, 1.44]"
    assert _locks(env["engine"]) == {"Trip": 8, "Theirs": 6}


def test_text_that_only_looks_like_an_envelope_is_plaintext(env):
    """The strict check: a name a user typed, such as "v1.2.3", is no envelope."""
    resp = env["client"].put("/api/activities/113", json={"name": "v1.2.3"})
    assert resp.status_code == 200
    assert _row(env["engine"], 113)["name"] == "v1.2.3"


def test_a_stranger_still_gets_404_not_the_409(env):
    """The refusal comes after the write permission check, so it tells no one
    without access whether the row exists or who holds it."""
    with Session(env["engine"]) as sess:
        u = UserInfo(display_name="stranger", email="stranger@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        env["caller"]["id"] = u.id
    assert _put_fields(env, 111).status_code == 404
    assert _put_gain(env, 111).status_code == 404


@pytest.mark.parametrize("path, method", [("track/encrypted", "put"),
                                          ("split/encrypted", "post")],
                         ids=["track", "split"])
def test_the_trip_routes_judge_by_the_trips_owner(env, path, method):
    """On the trip-scoped routes the trip's owner is the one who must read the
    row: an editor member is refused on a row a third trip holds, and not on
    one only the owner's trips hold."""
    with Session(env["engine"]) as sess:
        trip = sess.exec(select(DBProject.id).where(DBProject.name == "Trip")).one()
        sess.add(DBProjectMember(project_id=trip, user_info_id=env["ids"]["friend"],
                                 role="editor", invited_by=env["ids"]["owner"]))
        sess.commit()
    env["caller"]["id"] = env["ids"]["friend"]
    body = ({**_piece(20), "lock_version": 7} if path == "track/encrypted" else
            {"head": _piece(30), "tail": _piece(40), "tail_name": _env(50),
             "lock_version": 7})

    def _call(aid):
        url = f"/api/projects/Trip/activities/{aid}/{path}?owner={env['ids']['owner']}"
        return getattr(env["client"], method)(url, json=body)

    resp = _call(111)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "shared_with_other_trip"
    assert _call(112).status_code == 200


@pytest.mark.parametrize("write", [_put_fields, _put_fields_cas, _put_gain],
                         ids=["fields", "fields-cas", "gain"])
def test_a_trip_taking_the_row_before_the_write_lock_is_seen(env, write, monkeypatch):
    """The check runs again once the lock-version bump holds the write lock,
    and undoes the bump: here the row becomes shared between the two."""
    answers = iter([False, True])
    monkeypatch.setattr(_repo, "activity_shared_with_others",
                        lambda sess, aid, uid: next(answers))
    before, locks = _row(env["engine"], 112), _locks(env["engine"])
    resp = write(env, 112)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "shared_with_other_trip"
    assert _row(env["engine"], 112) == before
    assert _locks(env["engine"]) == locks


# ── shared_with_others on the payload ────────────────────────────────────────

_EXPECTED = {111: True, 112: False, 113: True}


def _by_id(payload):
    return {a["id"]: a for a in payload["activities"]}


def _build(env, builder, name="Trip", who="owner"):
    with Session(env["engine"]) as sess:
        row = sess.exec(select(DBProject).where(DBProject.name == name)).first()
        return builder(sess, row, name, env["ids"][who])


@pytest.mark.parametrize("builder", [build_meta_payload, build_details_payload],
                         ids=["meta", "full"])
def test_shared_with_others_says_which_rows_another_users_trip_holds(env, builder):
    acts = _by_id(_build(env, builder))
    assert {aid: a["shared_with_others"] for aid, a in acts.items()} == _EXPECTED


def test_shared_with_others_is_relative_to_the_trip_owner(env):
    """In the friend's trip, the rows the owner's trip also holds are shared."""
    acts = _by_id(_build(env, build_meta_payload, name="Theirs", who="friend"))
    assert {aid: a["shared_with_others"] for aid, a in acts.items()} == {111: True, 113: True}


def test_the_light_path_issues_no_extra_query(env):
    """Building /meta's payload runs the same statements whether or not a row
    is shared: the flag comes from the load query's own row."""
    def _statements():
        seen = []

        def _capture(conn, cursor, statement, params, context, executemany):
            seen.append(statement)

        event.listen(env["engine"], "after_cursor_execute", _capture)
        try:
            payload = _build(env, build_meta_payload)
        finally:
            event.remove(env["engine"], "after_cursor_execute", _capture)
        return payload, seen

    payload, shared = _statements()
    assert _by_id(payload)[111]["shared_with_others"] is True
    with Session(env["engine"]) as sess:
        theirs = sess.exec(select(DBProject.id).where(DBProject.name == "Theirs")).one()
        sess.exec(delete(DBProjectItem).where(DBProjectItem.project_id == theirs))
        sess.commit()
    payload, unshared = _statements()
    assert {aid: a["shared_with_others"] for aid, a in _by_id(payload).items()} == {
        111: False, 112: False, 113: False}
    assert shared == unshared
    act_loads = [s for s in shared if "FROM activity" in s]
    assert len(act_loads) == 1


def test_a_traxj_export_carries_no_shared_with_others(env):
    with Session(env["engine"]) as sess:
        project = _repo.get_project(sess, env["ids"]["owner"], "Trip")
    assert {a.id: a.shared_with_others for a in project.activities} == _EXPECTED
    doc = _traxj_document(project)
    assert doc["activities"]
    assert "shared_with_others" not in json.dumps(doc)

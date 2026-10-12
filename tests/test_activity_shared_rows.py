"""No activity row is shared between trips any more (#592).

Until #592 an activity row was shared by every trip holding it, so an envelope
was refused on a row another user's trip held (E2EE remnants U16, decision
15): that trip's owner could not have read it. Now every trip holds its own
copy, tied to the others by ``strava_id`` alone, so the four writes that store
an envelope — ``PUT /api/activities/{id}``, the encrypted track and split
routes and the elevation-gain route — are taken on a row of the caller's trip
whoever else holds the same activity, and touch neither the other trip's copy
nor its lock version. No payload says ``shared_with_others``.
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

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

_PLAIN = dict(summary_polyline="_p~iF~ps|U_ulLnnqC", distance=10.0,
              start_date="2026-05-03T08:00:00Z")


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
    """Owner's trip "Trip" (lock 7) holds the owner's rows 111 (encrypted) and
    113 (plaintext). The friend's trip "Theirs" (lock 5) holds the friend's own
    copies of the same two activities: 211 and 213."""
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
        sess.add(DBActivity(id=113, user_info_id=ids["owner"], name="Plain", type="Ride",
                            **_PLAIN))
        sess.add(DBActivity(id=211, user_info_id=ids["friend"], name=_env(6), type="Ride",
                            **_ENCRYPTED))
        sess.add(DBActivity(id=213, user_info_id=ids["friend"], name="Plain", type="Ride",
                            **_PLAIN))
        for pos, aid in enumerate((111, 113)):
            sess.add(DBProjectItem(project_id=pids["Trip"], position=pos,
                                   item_type="activity", activity_id=aid))
        for pos, aid in enumerate((211, 213)):
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
def test_an_envelope_is_taken_whoever_else_holds_the_same_activity(env, write):
    theirs, before = _row(env["engine"], 211), _locks(env["engine"])

    resp = write(env, 111)

    assert resp.status_code == 200, resp.text
    assert _row(env["engine"], 211) == theirs
    assert _locks(env["engine"])["Theirs"] == before["Theirs"]


def test_an_envelope_on_a_plaintext_row_is_taken_and_the_other_copy_stays_plaintext(env):
    resp = env["client"].put("/api/activities/113",
                             json={"name": _env(9), "summary_polyline": _env(10)})
    assert resp.status_code == 200, resp.text
    assert _row(env["engine"], 113)["summary_polyline"] == _env(10)
    theirs = _row(env["engine"], 213)
    assert (theirs["name"], theirs["summary_polyline"]) == ("Plain", _PLAIN["summary_polyline"])


def test_the_owner_may_write_plaintext_back_to_an_enveloped_row(env):
    resp = env["client"].put("/api/activities/111", json={
        "name": "Morning ride", "start_latlng_json": "[43.6, 1.44]",
        "project": "Trip", "lock_version": 7,
    })
    assert resp.status_code == 200
    assert resp.json()["lock_version"] == 8
    row = _row(env["engine"], 111)
    assert row["name"] == "Morning ride"
    assert row["start_latlng_json"] == "[43.6, 1.44]"
    assert _locks(env["engine"]) == {"Trip": 8, "Theirs": 5}


def test_text_that_only_looks_like_an_envelope_is_plaintext(env):
    """The strict check: a name a user typed, such as "v1.2.3", is no envelope."""
    resp = env["client"].put("/api/activities/113", json={"name": "v1.2.3"})
    assert resp.status_code == 200
    assert _row(env["engine"], 113)["name"] == "v1.2.3"


def test_another_trips_copy_is_not_the_callers_to_write(env):
    """The owner holds the same activity, but row 211 is the friend's."""
    before = _row(env["engine"], 211)
    assert _put_fields(env, 211).status_code == 404
    assert _put_gain(env, 211).status_code == 404
    assert _row(env["engine"], 211) == before


def test_a_stranger_gets_404(env):
    with Session(env["engine"]) as sess:
        u = UserInfo(display_name="stranger", email="stranger@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        env["caller"]["id"] = u.id
    assert _put_fields(env, 111).status_code == 404
    assert _put_gain(env, 111).status_code == 404


@pytest.mark.parametrize("path, method", [("track/encrypted", "put"),
                                          ("split/encrypted", "post")],
                         ids=["track", "split"])
def test_the_trip_routes_take_an_envelope_from_an_editor_member(env, path, method):
    """No third trip holds the row, so nothing stands between an editor and
    the trip's own encrypted row."""
    with Session(env["engine"]) as sess:
        trip = sess.exec(select(DBProject.id).where(DBProject.name == "Trip")).one()
        sess.add(DBProjectMember(project_id=trip, user_info_id=env["ids"]["friend"],
                                 role="editor", invited_by=env["ids"]["owner"]))
        sess.commit()
    env["caller"]["id"] = env["ids"]["friend"]
    body = ({**_piece(20), "lock_version": 7} if path == "track/encrypted" else
            {"head": _piece(30), "tail": _piece(40), "tail_name": _env(50),
             "lock_version": 7})

    url = f"/api/projects/Trip/activities/111/{path}?owner={env['ids']['owner']}"
    resp = getattr(env["client"], method)(url, json=body)

    assert resp.status_code == 200, resp.text


# ── No shared_with_others on the payload ─────────────────────────────────────

def _build(env, builder, name="Trip", who="owner"):
    with Session(env["engine"]) as sess:
        row = sess.exec(select(DBProject).where(DBProject.name == name)).first()
        return builder(sess, row, name, env["ids"][who])


@pytest.mark.parametrize("builder", [build_meta_payload, build_details_payload],
                         ids=["meta", "full"])
def test_no_payload_says_shared_with_others(env, builder):
    payload = _build(env, builder)
    assert {a["id"] for a in payload["activities"]} == {111, 113}
    assert all("shared_with_others" not in a for a in payload["activities"])


def test_the_light_path_loads_the_activities_in_one_statement(env):
    seen = []

    def _capture(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(env["engine"], "after_cursor_execute", _capture)
    try:
        _build(env, build_meta_payload)
    finally:
        event.remove(env["engine"], "after_cursor_execute", _capture)
    assert len([s for s in seen if "FROM activity" in s]) == 1


def test_a_traxj_export_carries_no_shared_with_others(env):
    with Session(env["engine"]) as sess:
        project = _repo.get_project(sess, env["ids"]["owner"], "Trip")
    assert {a.shared_with_others for a in project.activities} == {None}
    doc = _traxj_document(project)
    assert doc["activities"]
    assert "shared_with_others" not in json.dumps(doc)

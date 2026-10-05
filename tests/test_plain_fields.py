"""``plain_fields`` on trip payloads (E2EE remnants U12, decision 6).

Each activity of a trip payload lists its E2EE columns that still hold
plaintext — non-null, non-empty, not an envelope — so the encryption catch-up
knows which rows to fetch and encrypt. The four geometry snapshots count. The
envelope test is case-sensitive, like the Python and Dart ones: a name such as
"V1.0.1 ride" is plaintext (R5-5).

It is computed by the project-load query, so the light (``/meta``) path still
brings no deferred heavy column into Python (R4-6). It is sent to the client
but never written to a ``.traxj`` export. ``GET …/track`` hands the catch-up
all four geometry snapshots, to editors only (R5-1).
"""
from __future__ import annotations

import base64
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
from api.project_shared import _repo, build_details_payload, build_meta_payload
from api.project_transfer import _traxj_document
from models.project_db import DBActivity, DBProject, DBProjectItem, DBProjectMember
from models.user import UserInfo

_ALL = ["name", "summary_polyline", "start_latlng_json", "end_latlng_json",
        "elevation_profile_json", "elevation_profile_low_res_json",
        "original_polyline", "original_elevation_profile_json",
        "original_start_latlng_json", "original_end_latlng_json"]

_POLY = polyline_lib.encode([(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)])
_EP = json.dumps({"distances_km": [0.0, 1.0, 2.0], "elevations_m": [100.0, 110.0, 105.0]})
_START = json.dumps([48.0, 2.0])
_END = json.dumps([48.0, 2.02])


def _env(seed: int) -> str:
    """An envelope as ``EncryptedField.encode`` writes one (see
    test_activity_encrypted_edit.py)."""
    wrapped = b"\xfb\xff\xbf" * 24
    ciphertext = bytes([seed % 256]) * 41
    return (f"v1.{base64.b64encode(wrapped).decode()}"
            f".{base64.b64encode(ciphertext).decode()}")


_PLAIN_ROW = dict(
    name="Morning ride", summary_polyline=_POLY, start_latlng_json=_START,
    end_latlng_json=_END, elevation_profile_json=_EP, elevation_profile_low_res_json=_EP,
    is_edited=True, original_polyline=_POLY, original_elevation_profile_json=_EP,
    original_start_latlng_json=_START, original_end_latlng_json=_END,
)
_ENC_ROW = {f: (_env(i) if f != "is_edited" else True)
            for i, f in enumerate(_PLAIN_ROW)}


@pytest.fixture
def env(monkeypatch):
    """Owner's trip "Trip" holds:

    * 101 — every E2EE column plaintext, the four snapshots included;
    * 102 — every E2EE column an envelope;
    * 103 — a "V1.0.1 ride" name (an envelope only to a case-insensitive
      test) beside an enveloped track, and one plaintext snapshot;
    * 104 — unedited, with an empty name and every other E2EE column null;
    * 105 — values shaped like envelopes but not: "v1.a.b.c" and "v1.ab".
    Ed is an editor, Vi a viewer.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com") for n in ("owner", "ed", "vi")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}
        proj = DBProject(user_info_id=ids["owner"], name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        for name, role in (("ed", "editor"), ("vi", "viewer")):
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=ids[name],
                                     role=role, invited_by=ids["owner"]))
        rows = {
            101: _PLAIN_ROW,
            102: _ENC_ROW,
            103: dict(_ENC_ROW, name="V1.0.1 ride", original_end_latlng_json=_END),
            104: dict(name="", summary_polyline=None, elevation_profile_json=None),
            105: dict(name="v1.a.b.c", summary_polyline="v1.ab"),
        }
        for pos, (aid, fields) in enumerate(rows.items()):
            sess.add(DBActivity(id=aid, user_info_id=ids["owner"], type="Ride",
                                start_date=f"2026-05-0{pos + 1}T08:00:00Z", **fields))
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()

    caller = {"id": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(caller["id"])}
    app.include_router(activities_router)
    return dict(client=TestClient(app), engine=engine, ids=ids, caller=caller)


_EXPECTED = {
    101: _ALL,
    102: [],
    103: ["name", "original_end_latlng_json"],
    104: [],
    105: ["name", "summary_polyline"],
}


def _by_id(payload):
    return {a["id"]: a for a in payload["activities"]}


@pytest.mark.parametrize("builder", [build_meta_payload, build_details_payload],
                         ids=["meta", "full"])
def test_plain_fields_lists_exactly_the_plaintext_e2ee_columns(env, builder):
    with Session(env["engine"]) as sess:
        row = sess.exec(select(DBProject)).first()
        payload = builder(sess, row, "Trip", env["ids"]["owner"])
    acts = _by_id(payload)
    assert {aid: a["plain_fields"] for aid, a in acts.items()} == _EXPECTED


def test_the_light_path_brings_no_deferred_column_into_python(env):
    """Every column each statement returns, while /meta's payload is built:
    neither heavy column may be among them — not selected by the load query,
    not lazy-loaded afterwards by anything reading it off the row."""
    returned = []

    def _capture(conn, cursor, statement, params, context, executemany):
        returned.extend(d[0] for d in cursor.description or ())

    event.listen(env["engine"], "after_cursor_execute", _capture)
    try:
        with Session(env["engine"]) as sess:
            row = sess.exec(select(DBProject)).first()
            payload = build_meta_payload(sess, row, "Trip", env["ids"]["owner"])
    finally:
        event.remove(env["engine"], "after_cursor_execute", _capture)
    assert _by_id(payload)[101]["plain_fields"] == _ALL     # computed all the same
    assert "plain_fields_mask" in returned
    # A lazy load labels its column "activity_<name>".
    names = {n.removeprefix("activity_") for n in returned}
    assert "summary_polyline" not in names
    assert "elevation_profile_json" not in names


def test_a_traxj_export_carries_no_plain_fields(env):
    with Session(env["engine"]) as sess:
        project = _repo.get_project(sess, env["ids"]["owner"], "Trip")
    assert all(a.plain_fields is not None for a in project.activities)
    doc = _traxj_document(project)
    assert doc["activities"]
    assert all("plain_fields" not in a for a in doc["activities"])
    assert "plain_fields" not in json.dumps(doc)


def _track(env, aid):
    url = f"/api/projects/Trip/activities/{aid}/track"
    if env["caller"]["id"] != env["ids"]["owner"]:
        url += f"?owner={env['ids']['owner']}"
    resp = env["client"].get(url)
    assert resp.status_code == 200, resp.text
    return resp.json()


_ORIGINALS = ("original_polyline", "original_elevation_profile_json",
              "original_start_latlng_json", "original_end_latlng_json")


@pytest.mark.parametrize("who", ["owner", "ed"])
def test_get_track_returns_all_four_geometry_originals_to_an_editor(env, who):
    env["caller"]["id"] = env["ids"][who]
    for aid, row in ((101, _PLAIN_ROW), (102, _ENC_ROW)):
        body = _track(env, aid)
        for field in _ORIGINALS:
            assert body[field] == row[field], (aid, field)


def test_get_track_returns_no_originals_to_a_viewer(env):
    env["caller"]["id"] = env["ids"]["vi"]
    body = _track(env, 101)
    assert not any(field in body for field in _ORIGINALS)


def test_the_latlng_originals_can_be_written_back_encrypted(env):
    """The catch-up encrypts the two latlng snapshots through
    PUT /api/activities/{id}, under the rules of the other two snapshots."""
    from api.activities import activity_fields_router

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(env["ids"]["owner"])}
    app.include_router(activity_fields_router)
    client = TestClient(app)
    resp = client.put("/api/activities/101", json={
        "original_start_latlng_json": _env(70), "original_end_latlng_json": _env(71)})
    assert resp.status_code == 200, resp.text
    with Session(env["engine"]) as sess:
        row = sess.get(DBActivity, 101)
        assert row.original_start_latlng_json == _env(70)
        assert row.original_end_latlng_json == _env(71)
        payload = build_meta_payload(sess, sess.exec(select(DBProject)).first(),
                                     "Trip", env["ids"]["owner"])
    assert "original_start_latlng_json" not in _by_id(payload)[101]["plain_fields"]

    # Plaintext there must be a start or end the trip-file import takes (#462).
    resp = client.put("/api/activities/101", json={"original_start_latlng_json": "[999, 2]"})
    assert resp.status_code == 422, resp.text
    resp = client.put("/api/activities/101", json={"original_end_latlng_json": _END})
    assert resp.status_code == 200, resp.text

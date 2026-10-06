"""``has_gain_snapshot`` on trip payloads (E2EE remnants U15, decision 12).

Each activity of a trip payload says whether its row holds a gain snapshot
(``original_total_elevation_gain`` not null), so the encryption catch-up can
tell an edit made before #386 (no snapshot: its gain is the old raw sum) from
a later one. The column is not deferred, so the light (``/meta``) path reads
it from the load query with no extra statement. It is sent to the client but
never written to a ``.traxj`` export.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.project_shared import _repo, build_details_payload, build_meta_payload
from api.project_transfer import _traxj_document
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo


@pytest.fixture
def env(monkeypatch):
    """Owner's trip "Trip" holds:

    * 201 — edited, with a gain snapshot;
    * 202 — edited, without one (an edit made before #386);
    * 203 — unedited.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        owner = UserInfo(display_name="owner", email="owner@e.com")
        sess.add(owner); sess.commit(); sess.refresh(owner)
        proj = DBProject(user_info_id=owner.id, name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        rows = {
            201: dict(is_edited=True, total_elevation_gain=80.0,
                      original_total_elevation_gain=120.0),
            202: dict(is_edited=True, total_elevation_gain=95.0),
            203: dict(total_elevation_gain=40.0),
        }
        for pos, (aid, fields) in enumerate(rows.items()):
            sess.add(DBActivity(id=aid, user_info_id=owner.id, type="Ride", name="Ride",
                                start_date=f"2026-05-0{pos + 1}T08:00:00Z", **fields))
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()
        owner_id = owner.id
    return dict(engine=engine, owner=owner_id)


_EXPECTED = {201: True, 202: False, 203: False}


def _by_id(payload):
    return {a["id"]: a for a in payload["activities"]}


def _build(env, builder):
    with Session(env["engine"]) as sess:
        row = sess.exec(select(DBProject)).first()
        return builder(sess, row, "Trip", env["owner"])


@pytest.mark.parametrize("builder", [build_meta_payload, build_details_payload],
                         ids=["meta", "full"])
def test_has_gain_snapshot_follows_the_snapshot_column(env, builder):
    acts = _by_id(_build(env, builder))
    assert {aid: a["has_gain_snapshot"] for aid, a in acts.items()} == _EXPECTED


def test_the_light_path_issues_no_extra_query(env):
    """Building /meta's payload runs the same statements with or without a
    gain snapshot: the flag comes from the load query's own row."""
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

    payload, with_snapshot = _statements()
    assert _by_id(payload)[201]["has_gain_snapshot"] is True
    with Session(env["engine"]) as sess:
        sess.get(DBActivity, 201).original_total_elevation_gain = None
        sess.commit()
    payload, without_snapshot = _statements()
    assert _by_id(payload)[201]["has_gain_snapshot"] is False
    assert with_snapshot == without_snapshot
    # The activity load is one statement, which selects the snapshot column.
    act_loads = [s for s in with_snapshot if "FROM activity" in s]
    assert len(act_loads) == 1
    assert "original_total_elevation_gain" in act_loads[0]


def test_a_traxj_export_carries_no_has_gain_snapshot(env):
    with Session(env["engine"]) as sess:
        project = _repo.get_project(sess, env["owner"], "Trip")
    assert {a.id: a.has_gain_snapshot for a in project.activities} == _EXPECTED
    doc = _traxj_document(project)
    assert doc["activities"]
    assert all("has_gain_snapshot" not in a for a in doc["activities"])
    assert "has_gain_snapshot" not in json.dumps(doc)

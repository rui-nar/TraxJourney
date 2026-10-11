"""An added activity is matched to the trip by ``strava_id``, never by row id
(#592, plan Decisions 2 and 7).

A trip's rows have opaque ids, so a Strava id that happens to equal one of
them says nothing: the activity is still new to the trip. And a Strava
activity the trip holds under any row id is recognised by its ``strava_id``.
A local activity is still matched by its id, which its caller allocated as
its row's.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.models.activity import Activity
from src.models.project import Project, ProjectItem
from src.project.project_repo import ProjectRepo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]
_POLY = polyline_lib.encode(_TRACK)
_HELD_ROW = 5_000_000_123      # an opaque row id the trip holds
_HELD_STRAVA_ID = 777
_OTHER_STRAVA_ID = 9001

repo = ProjectRepo()


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _user(engine) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name="owner", email="owner@e.com")
        sess.add(u)
        sess.commit()
        return u.id


def _trip(engine, owner: int, activity_ids) -> int:
    with Session(engine) as sess:
        proj = DBProject(user_info_id=owner, name="A")
        sess.add(proj)
        sess.commit()
        for pos, aid in enumerate(activity_ids):
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()
        return proj.id


def _row(engine, aid: int, owner: int, **kw) -> None:
    fields = dict(
        user_info_id=owner, name="Ride", type="Ride", distance=2000.0,
        moving_time=600, elapsed_time=700, total_elevation_gain=10.0,
        summary_polyline=_POLY,
        start_latlng_json=json.dumps([48.0, 2.0]),
        end_latlng_json=json.dumps([48.0, 2.02]),
        start_date="2024-06-01T10:00:00Z", start_date_local="2024-06-01T12:00:00Z",
    )
    fields.update(kw)
    with Session(engine) as sess:
        sess.add(DBActivity(id=aid, **fields))
        sess.commit()


def _get(engine, aid: int) -> dict | None:
    with Session(engine) as sess:
        a = sess.get(DBActivity, aid)
        return None if a is None else {c: getattr(a, c) for c in DBActivity.model_fields}


def _items(engine, project_id: int) -> list[int]:
    with Session(engine) as sess:
        return [i.activity_id for i in sess.exec(
            select(DBProjectItem).where(DBProjectItem.project_id == project_id,
                                        DBProjectItem.item_type == "activity")
            .order_by(DBProjectItem.position))]


def _strava_activity(sid: int, name: str = "Morning ride") -> Activity:
    """As a Strava sync or the add route hands it over: its id IS the Strava id."""
    return Activity.from_strava_api({
        "id": sid, "name": name, "type": "Ride",
        "start_date": "2024-06-02T08:00:00Z", "start_date_local": "2024-06-02T08:00:00Z",
        "map": {"summary_polyline": _POLY},
    })


def _add(owner: int, activities) -> tuple[Project, list[int]]:
    counts = []

    def _mutate(project):
        counts.append(project.add_activities(activities))
    return repo.save_project_with_retry(owner, "A", _mutate), counts


def _trip_holding_a_strava_row(engine, **kw):
    uid = _user(engine)
    _row(engine, _HELD_ROW, uid, strava_id=_HELD_STRAVA_ID, strava_user_id=uid, **kw)
    return uid, _trip(engine, uid, [_HELD_ROW])


# ── A Strava id equal to an unrelated row id ───────────────────────────────────

def test_a_strava_id_equal_to_a_held_row_id_is_still_added(engine):
    uid, trip = _trip_holding_a_strava_row(engine)
    before = _get(engine, _HELD_ROW)

    _project, counts = _add(uid, [_strava_activity(_HELD_ROW, name="New ride")])

    assert counts == [1]
    held, new = _items(engine, trip)
    assert held == _HELD_ROW
    assert new not in (_HELD_ROW, None)
    assert (_get(engine, new)["strava_id"], _get(engine, new)["name"]) == (
        _HELD_ROW, "New ride")
    assert _get(engine, _HELD_ROW) == before


def test_the_activity_added_under_a_taken_id_is_not_changed_for_a_retry(engine):
    """A retried save runs the mutation again on the same Activity objects."""
    uid, _trip_id = _trip_holding_a_strava_row(engine)
    act = _strava_activity(_HELD_ROW)

    _add(uid, [act])

    assert (act.id, act.strava_id) == (_HELD_ROW, _HELD_ROW)


def test_a_pre_592_activity_equal_to_a_held_row_id_is_still_added(engine):
    """No ``strava_id``: its positive Strava-sourced id is its Strava id
    (plan Decision 8), which is still not the row's."""
    uid, trip = _trip_holding_a_strava_row(engine)
    act = _strava_activity(_HELD_ROW)
    act.strava_id = None

    _add(uid, [act])

    (new,) = set(_items(engine, trip)) - {_HELD_ROW}
    assert _get(engine, new)["strava_id"] == _HELD_ROW


def test_in_memory_the_added_activity_takes_an_id_of_its_own():
    held = _strava_activity(_HELD_STRAVA_ID, name="Ride")
    held.id = _HELD_ROW
    project = Project(name="A", activities=[held],
                      items=[ProjectItem(item_type="activity", activity_id=_HELD_ROW)])
    project.rebuild_map()
    act = _strava_activity(_HELD_ROW)

    assert project.add_activities([act]) == 1

    added = next(a for a in project.activities if a is not held)
    assert added.id not in (_HELD_ROW, None) and added.id > 0
    assert added.strava_id == _HELD_ROW
    assert project.activity_by_id(_HELD_ROW) is held
    assert [it.activity_id for it in project.items] == [_HELD_ROW, added.id]


# ── A Strava activity the trip holds under another row id ──────────────────────

def test_a_strava_activity_held_under_another_row_id_is_not_added_again(engine):
    uid, trip = _trip_holding_a_strava_row(engine, name="Ride (1/2)",
                                           split_base_name="Ride")
    before = _get(engine, _HELD_ROW)

    _project, counts = _add(uid, [_strava_activity(_HELD_STRAVA_ID,
                                                   name="Renamed on Strava")])

    assert counts == [0]
    assert _items(engine, trip) == [_HELD_ROW]
    assert _get(engine, _HELD_ROW) == before


def test_a_pre_592_activity_held_under_another_row_id_is_recognised(engine):
    uid, trip = _trip_holding_a_strava_row(engine)
    act = _strava_activity(_HELD_STRAVA_ID, name="Renamed on Strava")
    act.strava_id = None

    _project, counts = _add(uid, [act])

    assert counts == [0]
    assert _items(engine, trip) == [_HELD_ROW]
    assert _get(engine, _HELD_ROW)["name"] == "Ride"


def test_a_save_never_takes_another_strava_activity_for_a_held_row(engine):
    """An activity named by a held row's id but carrying another Strava id is
    not that row: the row is left as it was."""
    uid, trip = _trip_holding_a_strava_row(engine)
    before = _get(engine, _HELD_ROW)
    with Session(engine) as sess:
        project = repo.get_project(sess, uid, "A")
        project.activities = [_strava_activity(_HELD_ROW, name="Impostor")]
        project.rebuild_map()
        repo.save_project(sess, uid, project)

    assert [a.name for a in project.activities] == []
    assert _items(engine, trip) == [_HELD_ROW]
    assert _get(engine, _HELD_ROW) == before
    with Session(engine) as sess:
        assert sess.exec(select(DBActivity).where(
            DBActivity.strava_id == _HELD_ROW)).first() is None


# ── Local activities are matched by id, as before ──────────────────────────────

def _local(aid: int, name: str = "Walk") -> Activity:
    act = _strava_activity(1, name=name)
    act.id, act.source, act.strava_id = aid, "gpx", None
    return act


def test_a_local_activity_the_trip_holds_is_not_added_again(engine):
    uid = _user(engine)
    _row(engine, -8, uid, name="Walk", source="gpx")
    trip = _trip(engine, uid, [-8])
    before = _get(engine, -8)

    _project, counts = _add(uid, [_local(-8, name="Walk, again")])

    assert counts == [0]
    assert _items(engine, trip) == [-8]
    assert _get(engine, -8) == before


def test_a_new_local_activity_keeps_its_id_beside_a_strava_row(engine):
    uid, trip = _trip_holding_a_strava_row(engine)

    _project, counts = _add(uid, [_local(-9)])

    assert counts == [1]
    assert _items(engine, trip) == [_HELD_ROW, -9]
    assert (_get(engine, -9)["strava_id"], _get(engine, -9)["source"]) == (None, "gpx")

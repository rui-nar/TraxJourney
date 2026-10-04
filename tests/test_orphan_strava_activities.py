"""A Strava activity row no trip references is deleted (issue #509).

Regression: removing an activity from its last trip, or deleting the trip,
unlinked the timeline item and left the Strava row (name, track, prepared
geometry) in the database for good, reachable from nowhere. Split families
were worse: the trip's tails stayed and kept naming the root.

The rows go only when nothing references them: no project item, and no split
piece naming them as its root or parent. A file-backed SQLite is used
throughout, so the race test at the bottom shows two real transactions
contending (the in-memory StaticPool shares one connection).
"""
from __future__ import annotations

import logging
import threading

import polyline as polyline_lib
import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, select

import api.activities as activities_module
import api.project_items as project_items_module
import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.project_db import (
    DBActivity,
    DBActivityGeoPrepared,
    DBProject,
    DBProjectItem,
    DBProjectMember,
)
from models.user import UserInfo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]


@pytest.fixture
def engine(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'orphans.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_TRIP_DAYS", "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ── Seeding ────────────────────────────────────────────────────────────────────

def _user(engine, name: str) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name=name, email=f"{name}@e.com")
        sess.add(u)
        sess.commit()
        return u.id


def _activity(engine, act_id: int, owner: int, name: str = "Ride", **kw) -> None:
    """An activity row and its prepared geometry."""
    with Session(engine) as sess:
        sess.add(DBActivity(id=act_id, user_info_id=owner, name=name, type="Ride",
                            start_date=kw.pop("start_date", "2026-06-10T09:00:00Z"),
                            **kw))
        sess.add(DBActivityGeoPrepared(activity_id=act_id, version=1, blob=b"geo"))
        sess.commit()


def _split_family(engine, owner: int, root: int = 111) -> tuple[int, int]:
    """Root *root* split into three: tail -5 cut out of the root, tail -6 cut
    out of tail -5. Named as split_activity names them."""
    _activity(engine, root, owner, name="Ride (1/3)", split_base_name="Ride",
              is_edited=True, start_date="2026-06-10T09:00:00Z")
    _activity(engine, -5, owner, name="Ride (2/3)", split_root_id=root,
              split_parent_id=root, start_date="2026-06-10T10:00:00Z")
    _activity(engine, -6, owner, name="Ride (3/3)", split_root_id=root,
              split_parent_id=-5, start_date="2026-06-10T11:00:00Z")
    return -5, -6


def _trip(engine, owner: int, name: str, activity_ids, members=()) -> int:
    with Session(engine) as sess:
        proj = DBProject(user_info_id=owner, name=name)
        sess.add(proj)
        sess.commit()
        for pos, aid in enumerate(activity_ids):
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        for member in members:
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=member,
                                     role="editor", invited_by=owner))
        sess.commit()
        return proj.id


def _leave(engine, project_id: int, member: int) -> None:
    with Session(engine) as sess:
        for row in sess.exec(select(DBProjectMember).where(
                DBProjectMember.project_id == project_id,
                DBProjectMember.user_info_id == member)).all():
            sess.delete(row)
        sess.commit()


def _as(uid: int) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "x@e.com"}
    return TestClient(app)


def _remove(uid: int, trip: str, index: int, owner: int | None = None) -> None:
    url = f"/api/projects/{trip}/items/{index}"
    resp = _as(uid).delete(url, params={"owner": owner} if owner else None)
    assert resp.status_code == 204, resp.text


def _delete_trip(uid: int, trip: str) -> None:
    resp = _as(uid).delete(f"/api/projects/{trip}")
    assert resp.status_code == 204, resp.text


def _rows(engine) -> set[int]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBActivity.id)).all())


def _prepared(engine) -> set[int]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBActivityGeoPrepared.activity_id)).all())


# ── Removing an item ───────────────────────────────────────────────────────────

def test_removing_an_activity_from_its_last_trip_deletes_it(engine):
    uid = _user(engine, "owner")
    _activity(engine, 1, uid)
    _activity(engine, 2, uid)
    _trip(engine, uid, "Trip", [1, 2])

    _remove(uid, "Trip", 0)

    assert _rows(engine) == {2}
    assert _prepared(engine) == {2}


def test_removing_an_activity_from_one_of_two_trips_keeps_it(engine):
    uid = _user(engine, "owner")
    _activity(engine, 1, uid)
    _trip(engine, uid, "Trip", [1])
    _trip(engine, uid, "Other", [1])

    _remove(uid, "Trip", 0)

    assert _rows(engine) == {1}
    assert _prepared(engine) == {1}


def test_owner_removing_a_companions_activity_deletes_it(engine):
    """The row is the companion's: the cleanup considers the removed id
    whoever owns it, not only the caller's rows (plan R1-3)."""
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _activity(engine, 7, companion)
    _trip(engine, owner, "Trip", [7], members=[companion])

    _remove(owner, "Trip", 0)

    assert _rows(engine) == set()
    assert _prepared(engine) == set()


def test_an_editor_removing_the_owners_activity_only_unlinks_it(engine):
    """Owner decision 2026-10-03: a companion frees only their own rows. The
    owner's activity leaves the timeline and stays, for the owner's own
    removal or disconnect to free."""
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _activity(engine, 1, owner)
    _trip(engine, owner, "Trip", [1], members=[companion])

    _remove(companion, "Trip", 0, owner=owner)

    assert _rows(engine) == {1}
    assert _prepared(engine) == {1}
    with Session(engine) as sess:
        assert sess.exec(select(DBProjectItem).where(DBProjectItem.activity_id == 1)).first() is None


def test_an_editor_removing_their_own_activity_deletes_it(engine):
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _activity(engine, 7, companion)
    _trip(engine, owner, "Trip", [7], members=[companion])

    _remove(companion, "Trip", 0, owner=owner)

    assert _rows(engine) == set()
    assert _prepared(engine) == set()


def test_an_editor_removing_a_tail_of_the_owners_split_only_unlinks_it(engine):
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _activity(engine, 111, owner, name="Ride (1/2)", split_base_name="Ride", is_edited=True)
    _activity(engine, -5, owner, name="Ride (2/2)", split_root_id=111, split_parent_id=111,
              start_date="2026-06-10T10:00:00Z")
    _trip(engine, owner, "Trip", [111, -5], members=[companion])

    _remove(companion, "Trip", 1, owner=owner)

    assert _rows(engine) == {111, -5}
    with Session(engine) as sess:
        assert sess.exec(select(DBProjectItem).where(DBProjectItem.activity_id == -5)).first() is None


def test_removing_the_head_then_the_last_tail_deletes_the_root(engine):
    """The root is kept while its tail is in the trip, and reconsidered when
    the tail goes (plan R1-6)."""
    uid = _user(engine, "owner")
    _activity(engine, 111, uid, name="Ride (1/2)", split_base_name="Ride", is_edited=True)
    _activity(engine, -5, uid, name="Ride (2/2)", split_root_id=111, split_parent_id=111,
              start_date="2026-06-10T10:00:00Z")
    _trip(engine, uid, "Trip", [111, -5])

    _remove(uid, "Trip", 0)
    assert _rows(engine) == {111, -5}

    _remove(uid, "Trip", 0)
    assert _rows(engine) == set()
    assert _prepared(engine) == set()


def test_owner_removes_a_departed_companions_split_tail_then_its_head(engine):
    """A companion imported and split the activity, then left the trip. The
    tail is freed all the same — the removed item proves it was this trip's —
    and once the head goes too, so does the root (plan R4-1)."""
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _activity(engine, 111, companion, name="Ride (1/2)", split_base_name="Ride",
              is_edited=True)
    _activity(engine, -5, companion, name="Ride (2/2)", split_root_id=111,
              split_parent_id=111, start_date="2026-06-10T10:00:00Z")
    trip = _trip(engine, owner, "Trip", [111, -5], members=[companion])
    _leave(engine, trip, companion)

    _remove(owner, "Trip", 1)
    assert _rows(engine) == {111}
    with Session(engine) as sess:
        assert sess.get(DBActivity, 111).name == "Ride"

    _remove(owner, "Trip", 0)
    assert _rows(engine) == set()
    assert _prepared(engine) == set()


# ── Deleting a split tail directly ────────────────────────────────────────────
#
# The Flutter panel deletes every negative-id item through
# DELETE .../activities/{id}/local, not the timeline route, so that route must
# reconsider the root too, under the same rule (review I-R1-1).

def _delete_local(uid: int, trip: str, act_id: int, owner: int | None = None) -> None:
    url = f"/api/projects/{trip}/activities/{act_id}/local"
    resp = _as(uid).delete(url, params={"owner": owner} if owner else None)
    assert resp.status_code == 204, resp.text


def test_removing_the_head_then_deleting_the_last_tail_locally_deletes_the_root(engine):
    uid = _user(engine, "owner")
    _activity(engine, 111, uid, name="Ride (1/2)", split_base_name="Ride", is_edited=True)
    _activity(engine, -5, uid, name="Ride (2/2)", split_root_id=111, split_parent_id=111,
              start_date="2026-06-10T10:00:00Z")
    _trip(engine, uid, "Trip", [111, -5])

    _remove(uid, "Trip", 0)
    assert _rows(engine) == {111, -5}

    _delete_local(uid, "Trip", -5)
    assert _rows(engine) == set()
    assert _prepared(engine) == set()


def test_owner_deleting_a_departed_companions_tail_locally_frees_it_and_its_root(engine):
    """The trip-rewrite gate would refuse a departed companion's row; a
    Strava tail the trip holds is the owner's to free all the same, and its
    root goes with it once nothing else holds it (F1-R1-1)."""
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _activity(engine, 111, companion, name="Ride (1/2)", split_base_name="Ride",
              is_edited=True)
    _activity(engine, -5, companion, name="Ride (2/2)", split_root_id=111,
              split_parent_id=111, start_date="2026-06-10T10:00:00Z")
    trip = _trip(engine, owner, "Trip", [111, -5], members=[companion])
    _leave(engine, trip, companion)

    _remove(owner, "Trip", 0)
    assert _rows(engine) == {111, -5}

    _delete_local(owner, "Trip", -5)
    assert _rows(engine) == set()
    assert _prepared(engine) == set()


def test_an_editor_deleting_a_tail_of_the_owners_split_locally_is_refused(engine):
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _activity(engine, 111, owner, name="Ride (1/2)", split_base_name="Ride", is_edited=True)
    _activity(engine, -5, owner, name="Ride (2/2)", split_root_id=111, split_parent_id=111,
              start_date="2026-06-10T10:00:00Z")
    _trip(engine, owner, "Trip", [111, -5], members=[companion])

    resp = _as(companion).delete("/api/projects/Trip/activities/-5/local",
                                 params={"owner": owner})

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == (
        "Only the activity's owner or the trip owner can delete this piece")
    assert _rows(engine) == {111, -5}
    assert _prepared(engine) == {111, -5}
    with Session(engine) as sess:
        assert sess.exec(select(DBProjectItem).where(DBProjectItem.activity_id == -5)).first()


def test_an_editor_deleting_a_tail_of_their_own_split_locally_frees_it_and_its_root(engine):
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _activity(engine, 111, companion, name="Ride (1/2)", split_base_name="Ride",
              is_edited=True)
    _activity(engine, -5, companion, name="Ride (2/2)", split_root_id=111,
              split_parent_id=111, start_date="2026-06-10T10:00:00Z")
    _trip(engine, owner, "Trip", [111, -5], members=[companion])

    _remove(companion, "Trip", 0, owner=owner)
    assert _rows(engine) == {111, -5}

    _delete_local(companion, "Trip", -5, owner=owner)
    assert _rows(engine) == set()
    assert _prepared(engine) == set()


# ── Deleting a trip ────────────────────────────────────────────────────────────

def test_deleting_a_trip_deletes_rows_no_other_trip_holds(engine):
    uid = _user(engine, "owner")
    for aid in (1, 2, 3):
        _activity(engine, aid, uid)
    _trip(engine, uid, "Trip", [1, 2])
    _trip(engine, uid, "Other", [2, 3])

    _delete_trip(uid, "Trip")

    assert _rows(engine) == {2, 3}
    assert _prepared(engine) == {2, 3}


def test_deleting_a_trip_deletes_its_whole_split_family(engine):
    uid = _user(engine, "owner")
    _split_family(engine, uid)
    _trip(engine, uid, "Trip", [111, -5, -6])

    _delete_trip(uid, "Trip")

    assert _rows(engine) == set()
    assert _prepared(engine) == set()


def test_deleting_a_trip_keeps_a_root_another_trip_holds_and_renumbers_it(engine):
    """The tails go, the root stays for the other trip, which now shows it
    under its plain name rather than "Ride (1/3)" (plan R3-2)."""
    uid = _user(engine, "owner")
    _split_family(engine, uid)
    _trip(engine, uid, "Trip", [111, -5, -6])
    other = _trip(engine, uid, "Other", [111])
    with Session(engine) as sess:
        version_before = sess.get(DBProject, other).lock_version

    _delete_trip(uid, "Trip")

    assert _rows(engine) == {111}
    assert _prepared(engine) == {111}
    with Session(engine) as sess:
        assert sess.get(DBActivity, 111).name == "Ride"
        # The other trip's cached copy must notice the new name.
        assert sess.get(DBProject, other).lock_version > version_before


def test_deleting_a_trip_frees_a_departed_companions_split_family(engine):
    """Imported and split by a companion who has since left: the trip's
    tails are its own to delete, whoever cut them (plan R3-4)."""
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    _split_family(engine, companion)
    trip = _trip(engine, owner, "Trip", [111, -5, -6], members=[companion])
    _leave(engine, trip, companion)

    _delete_trip(owner, "Trip")

    assert _rows(engine) == set()
    assert _prepared(engine) == set()


def test_a_root_kept_only_by_unreferenced_tails_is_logged(engine, caplog):
    """Tail -9 names the root but sits in no trip — as a split committed while
    the trip was being deleted would (plan R3-3). The root stays, and says so."""
    uid = _user(engine, "owner")
    _split_family(engine, uid)
    _activity(engine, -9, uid, name="Ride (4/4)", split_root_id=111,
              split_parent_id=111, start_date="2026-06-10T12:00:00Z")
    _trip(engine, uid, "Trip", [111, -5, -6])

    with caplog.at_level(logging.WARNING, logger="src.project.repo_activities"):
        _delete_trip(uid, "Trip")

    assert _rows(engine) == {111, -9}
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "src.project.repo_activities" and r.levelno == logging.WARNING]
    assert warnings == ["activity 111 kept only by split tails no trip holds: [-9]"]


def test_deleting_a_trip_keeps_gpx_rows(engine):
    uid = _user(engine, "owner")
    _activity(engine, -40, uid, source="gpx")
    _trip(engine, uid, "Trip", [-40])

    _delete_trip(uid, "Trip")

    assert _rows(engine) == {-40}


def test_a_refresh_that_outlives_its_row_warns_as_it_reinserts(engine, caplog):
    """Guard U6-R1-1: the background re-fetch finds the row gone — the
    activity left its last trip while Strava was answering — and says so
    as it recreates it."""
    from api.project_shared import _repo
    from src.models.activity import Activity

    uid = _user(engine, "owner")
    act = Activity.from_strava_api(_raw_strava_activity(900))

    with caplog.at_level(logging.WARNING, logger="src.project.repo_activities"):
        with Session(engine) as sess:
            _repo.force_update_activity(sess, uid, act)

    assert (f"refresh re-inserting activity=900 user={uid}: row deleted while the "
            "re-fetch was in flight (#509 orphan)") in caplog.text


# ── The add that races a removal (Review envelope, U6 concurrency) ─────────────

def _raw_strava_activity(act_id: int) -> dict:
    return {
        "id": act_id, "name": "Ride", "type": "Ride",
        "distance": 1000.0, "moving_time": 100, "elapsed_time": 120,
        "total_elevation_gain": 0.0,
        "start_date": "2026-06-10T09:00:00Z", "start_date_local": "2026-06-10T09:00:00Z",
        "map": {"summary_polyline": polyline_lib.encode(_TRACK)},
    }


def test_an_add_to_another_trip_racing_the_removal_keeps_the_row(engine):
    """Removing activity 500 from its only trip while the same user adds it to
    a second trip. The add lands right after the removal checked that nothing
    references the row: it must wait for the delete and recreate the row, not
    commit its reference in between and be left pointing at nothing."""
    uid = _user(engine, "owner")
    _activity(engine, 500, uid)
    _trip(engine, uid, "Trip", [500])
    other = _trip(engine, uid, "Other", [])
    user = {"sub": str(uid), "email": "x@e.com"}

    def _add():
        activities_module.add_activities(
            "Other", activities_module.AddActivitiesRequest(
                activities=[_raw_strava_activity(500)]),
            user, BackgroundTasks(), None)

    add = threading.Thread(target=_add)
    remover = {}
    seen = {}

    def _after_execute(_conn, _cursor, statement, _params, _context, _many):
        # The removal's "is anything still referencing it" check has just run.
        if (threading.current_thread() is remover.get("thread") and not seen
                and "FROM projectitem" in statement
                and "projectitem.activity_id = activity.id" in statement):
            add.start()
            add.join(1.0)
            seen["add_blocked"] = add.is_alive()

    def _removal():
        project_items_module.delete_item("Trip", 0, user, BackgroundTasks(), None)

    event.listen(engine, "after_cursor_execute", _after_execute)
    try:
        remover["thread"] = threading.Thread(target=_removal)
        remover["thread"].start()
        remover["thread"].join(35.0)
        add.join(35.0)
    finally:
        event.remove(engine, "after_cursor_execute", _after_execute)

    assert not remover["thread"].is_alive() and not add.is_alive()
    with Session(engine) as sess:
        assert sess.get(DBActivity, 500) is not None
        assert sess.exec(select(DBProjectItem).where(
            DBProjectItem.project_id == other,
            DBProjectItem.activity_id == 500)).first() is not None
    # And it was the write lock that ordered them, not luck.
    assert seen["add_blocked"] is True

"""DELETE /{name}/activities/{id}/local must be scoped to the project (#405).

``ActivityMixin.delete_local_activity`` validated that the row was local and
existed, but never that the row belonged to the project the call named. The route's permission check proves you may edit *that* project — so an
editor of trip B could delete trip A's local activity, bump trip B's
lock_version, and leave trip A with an item pointing at a missing row.

The fix tests "no OTHER project references this row" rather than "this project
references it", which deliberately lets an ORPHANED local row through. Two
tests here exist to make that asymmetry load-bearing, so the inverted,
"simpler" check fails instead of quietly breaking things:

  * ``test_orphaned_local_row_is_deletable`` — the state migration
    ``d5b1c0a2e3f4`` documents must stay reachable from the API.
  * ``test_deleting_the_timeline_item_still_removes_the_local_row`` — the
    timeline path commits the item removal BEFORE calling the repo, so the row
    it asks us to delete is always unreferenced.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.activities import router as activities_router
from api.project_items import router as project_items_router
from api.projects import router as projects_router
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03), (48.0, 2.04)]
_ELEV = [100.0, 120.0, 110.0, 140.0, 130.0]

_GPX = (
    '<?xml version="1.0"?>'
    '<gpx version="1.1" creator="test" '
    'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
    '<trkpt lat="45.0" lon="6.0"><ele>500.0</ele></trkpt>'
    '<trkpt lat="45.001" lon="6.001"><ele>520.0</ele></trkpt>'
    '<trkpt lat="45.002" lon="6.002"><ele>515.0</ele></trkpt>'
    '</trkseg></trk></gpx>'
).encode("utf-8")


def _strava_activity(activity_id: int, user_info_id: int) -> DBActivity:
    return DBActivity(
        id=activity_id, user_info_id=user_info_id, name=f"Ride {activity_id}",
        type="Ride", distance=4000.0, moving_time=1000, elapsed_time=1200,
        total_elevation_gain=60.0,
        summary_polyline=polyline_lib.encode(_TRACK),
        elevation_profile_json=json.dumps({
            "distances_km": [i * 1.0 for i in range(len(_TRACK))],
            "elevations_m": _ELEV,
        }),
        start_latlng_json=json.dumps([48.0, 2.0]),
        end_latlng_json=json.dumps([48.0, 2.04]),
        start_date="2024-06-01T10:00:00Z",
        start_date_local="2024-06-01T12:00:00Z",
    )


@pytest.fixture
def env(monkeypatch):
    """One user, two of their own trips — so the route's editor check passes for
    both and the only thing standing between them is the scope check."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        for name, act_id in (("Trip A", 111), ("Trip B", 222)):
            proj = DBProject(user_info_id=u.id, name=name)
            sess.add(proj); sess.commit(); sess.refresh(proj)
            sess.add(_strava_activity(act_id, u.id))
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                   item_type="activity", activity_id=act_id))
        sess.commit()
        uid = u.id

    app = FastAPI()
    app.dependency_overrides[get_current_user] = \
        lambda: {"sub": str(uid), "email": "a@e.com"}
    app.include_router(projects_router)
    app.include_router(activities_router)
    app.include_router(project_items_router)
    return TestClient(app), engine


def _project(sess, name):
    return sess.exec(select(DBProject).where(DBProject.name == name)).one()


def _lock_version(engine, name):
    with Session(engine) as sess:
        return _project(sess, name).lock_version


def _split_tail(client, project="Trip A", activity_id=111):
    """Split `activity_id` in `project` and return the new local tail's id."""
    resp = client.post(
        f"/api/projects/{project}/activities/{activity_id}/split",
        json={"split_index": 2})
    assert resp.status_code == 200, resp.text
    return next(i for i in (a["id"] for a in resp.json()["activities"]) if i < 0)


def _import_gpx(client, project="Trip A"):
    resp = client.post(
        f"/api/projects/{project}/activities/import-gpx",
        files={"file": ("track.gpx", _GPX, "application/gpx+xml")},
        data={"date": "2024-06-01", "start_time": "09:00",
              "end_time": "10:00", "activity_type": "Hike"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["activity_id"]


def _activity_ids(client, project):
    body = client.get(f"/api/projects/{project}").json()
    return [it.get("activity_id") for it in body["items"]
            if it["item_type"] == "activity"]


# ── Criterion 1: another trip cannot reach it ────────────────────────────────

def test_cross_trip_delete_of_split_tail_is_404_and_deletes_nothing(env):
    client, engine = env
    tail_id = _split_tail(client)

    resp = client.delete(f"/api/projects/Trip B/activities/{tail_id}/local")
    assert resp.status_code == 404, resp.text

    with Session(engine) as sess:
        assert sess.get(DBActivity, tail_id) is not None   # row survives
    assert tail_id in _activity_ids(client, "Trip A")      # item survives


def test_cross_trip_delete_of_gpx_import_is_404_and_deletes_nothing(env):
    """The path #404 found: the import snackbar's Undo outlived the screen, so
    it fired while the notifier pointed at a different trip."""
    client, engine = env
    imported = _import_gpx(client)
    assert imported < 0                                    # a local row

    resp = client.delete(f"/api/projects/Trip B/activities/{imported}/local")
    assert resp.status_code == 404, resp.text

    with Session(engine) as sess:
        assert sess.get(DBActivity, imported) is not None
    assert imported in _activity_ids(client, "Trip A")


def test_cross_trip_delete_does_not_bump_the_wrong_lock_version(env):
    """The refused call must leave trip B's cache counter alone — a bump would
    make every native client re-download trip B's payloads for nothing."""
    client, engine = env
    tail_id = _split_tail(client)
    before = _lock_version(engine, "Trip B")

    assert client.delete(
        f"/api/projects/Trip B/activities/{tail_id}/local").status_code == 404
    assert _lock_version(engine, "Trip B") == before


# ── Criterion 3: its own trip still works ────────────────────────────────────

def test_split_tail_is_deletable_from_its_own_trip(env):
    client, engine = env
    tail_id = _split_tail(client)

    resp = client.delete(f"/api/projects/Trip A/activities/{tail_id}/local")
    assert resp.status_code == 204, resp.text
    with Session(engine) as sess:
        assert sess.get(DBActivity, tail_id) is None
    assert _activity_ids(client, "Trip A") == [111]


def test_gpx_import_is_deletable_from_its_own_trip(env):
    client, engine = env
    imported = _import_gpx(client)

    resp = client.delete(f"/api/projects/Trip A/activities/{imported}/local")
    assert resp.status_code == 204, resp.text
    with Session(engine) as sess:
        assert sess.get(DBActivity, imported) is None
    assert _activity_ids(client, "Trip A") == [111]


def test_a_trips_own_delete_survives_a_second_trip_holding_a_local_row(env):
    """Two trips each with their own local activity: deleting one must not be
    influenced by the other's existence."""
    client, engine = env
    tail_a = _split_tail(client, "Trip A", 111)
    tail_b = _split_tail(client, "Trip B", 222)

    assert client.delete(
        f"/api/projects/Trip A/activities/{tail_a}/local").status_code == 204
    with Session(engine) as sess:
        assert sess.get(DBActivity, tail_a) is None
        assert sess.get(DBActivity, tail_b) is not None
    assert tail_b in _activity_ids(client, "Trip B")


# ── Criterion 2: the orphan decision, made load-bearing ──────────────────────

def test_orphaned_local_row_is_deletable(env):
    """A local row referenced by ZERO timeline items stays deletable (#405).

    Migration ``d5b1c0a2e3f4`` documents this state as real, and deletes such
    rows wholesale. Refusing it here would strand them behind a migration while
    granting no protection: an unreferenced row is in nobody's timeline.

    This is the test that fails if the scope check is later "simplified" to
    "the project must reference this activity".
    """
    client, engine = env
    with Session(engine) as sess:
        owner_id = _project(sess, "Trip A").user_info_id
        sess.add(_strava_activity(-99, owner_id))
        sess.commit()

    resp = client.delete("/api/projects/Trip B/activities/-99/local")
    assert resp.status_code == 204, resp.text
    with Session(engine) as sess:
        assert sess.get(DBActivity, -99) is None


def test_deleting_the_timeline_item_still_removes_the_local_row(env):
    """The timeline path removes and COMMITS the item before calling the repo,
    so the row it hands us is unreferenced by then. The inverted check would
    silently refuse here and re-orphan every deleted split tail — reviving the
    id-reuse UNIQUE violation that PR #44 fixed.
    """
    client, engine = env
    tail_id = _split_tail(client)

    items = client.get("/api/projects/Trip A").json()["items"]
    tail_index = next(i for i, it in enumerate(items)
                      if it["item_type"] == "activity"
                      and (it.get("activity_id") or 0) < 0)
    assert client.delete(
        f"/api/projects/Trip A/items/{tail_index}").status_code == 204

    with Session(engine) as sess:
        assert sess.get(DBActivity, tail_id) is None       # not merely unlinked


def test_row_referenced_by_two_projects_is_refused_not_destroyed(env):
    """Corruption, not a normal state — every row belongs to one trip (#592),
    and the migration leaves none held by two. A row another trip holds is
    that trip's, so neither may delete it: deleting it would break the other
    project's timeline.
    """
    client, engine = env
    tail_id = _split_tail(client)
    with Session(engine) as sess:
        trip_b = _project(sess, "Trip B")
        sess.add(DBProjectItem(project_id=trip_b.id, position=1,
                               item_type="activity", activity_id=tail_id))
        sess.commit()

    for project in ("Trip A", "Trip B"):
        resp = client.delete(
            f"/api/projects/{project}/activities/{tail_id}/local")
        assert resp.status_code == 404, f"{project}: {resp.text}"
    with Session(engine) as sess:
        assert sess.get(DBActivity, tail_id) is not None


# ── Any editor of the trip, whoever owns the row (U4R1-1) ────────────────────

_ENVELOPE = "v1.d2VsY29tZQ==.Y2lwaGVydGV4dA=="


@pytest.fixture
def team(monkeypatch):
    """Owner's trip "Trip" holds -40, a GPX import of a companion who
    encrypted it, so it stayed theirs (plan Decision 4), and who then left
    the trip. The owner's "Other" holds -41, a local row of the owner's."""
    from models.project_db import DBActivityGeoPrepared, DBProjectMember

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="o@e.com")
        companion = UserInfo(display_name="Companion", email="c@e.com")
        sess.add(owner); sess.add(companion); sess.commit()
        sess.refresh(owner); sess.refresh(companion)
        trip = DBProject(user_info_id=owner.id, name="Trip")
        other = DBProject(user_info_id=owner.id, name="Other")
        sess.add(trip); sess.add(other); sess.commit()
        sess.refresh(trip); sess.refresh(other)
        sess.add(DBActivity(id=-40, user_info_id=companion.id, name=_ENVELOPE,
                            summary_polyline=_ENVELOPE, type="hike", source="gpx",
                            start_date="2026-06-01T10:00:00Z"))
        sess.add(DBActivity(id=-41, user_info_id=owner.id, name="Walk", type="hike",
                            source="gpx", start_date="2026-06-01T10:00:00Z"))
        for aid in (-40, -41):
            sess.add(DBActivityGeoPrepared(activity_id=aid, version=1, blob=b"geo"))
        sess.add(DBProjectItem(project_id=trip.id, position=0,
                               item_type="activity", activity_id=-40))
        sess.add(DBProjectItem(project_id=other.id, position=0,
                               item_type="activity", activity_id=-41))
        member = DBProjectMember(project_id=trip.id, user_info_id=companion.id,
                                 role="editor", invited_by=owner.id)
        sess.add(member); sess.commit()
        sess.delete(member); sess.commit()   # the companion left
        owner_id = owner.id

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner_id)}
    app.include_router(activities_router)
    app.include_router(projects_router)
    return TestClient(app), engine


def test_the_owner_deletes_a_departed_companions_encrypted_local_row(team):
    """Was a 404 "Activity not in project": the trip-rewrite gate refused a
    row whose owner is no longer part of the trip (review U4R1-1)."""
    from models.project_db import DBActivityGeoPrepared
    client, engine = team

    resp = client.delete("/api/projects/Trip/activities/-40/local")

    assert resp.status_code == 204, resp.text
    with Session(engine) as sess:
        assert sess.get(DBActivity, -40) is None
        assert sess.get(DBActivityGeoPrepared, -40) is None
        assert not sess.exec(select(DBProjectItem)
                             .where(DBProjectItem.activity_id == -40)).first()


def test_a_row_another_trip_holds_is_still_refused(team):
    from models.project_db import DBActivityGeoPrepared
    client, engine = team

    resp = client.delete("/api/projects/Trip/activities/-41/local")

    assert resp.status_code == 404, resp.text
    with Session(engine) as sess:
        assert sess.get(DBActivity, -41) is not None
        assert sess.get(DBActivityGeoPrepared, -41) is not None

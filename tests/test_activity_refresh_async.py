"""Async Strava re-fetch of a single activity (issue #148).

The re-fetch used to run inside the request, where two Strava calls (each able
to spend 60 s waiting on the rate limiter plus >=60 s backing off a 429) blew
past the client's 30 s HTTP timeout — the user saw "re-fetch failed: timeout"
for work the server usually completed. It now returns 202 with the activity
marked ``pending`` and does the fetching in a background task.

Covers:
  - the trigger returns 202 and marks the row pending before any Strava call;
  - checks answerable without Strava (409 edited / 409 not connected) still
    fail synchronously, so the button press is still an error;
  - the job writes ``resolved`` and the fresh data on success;
  - the job writes ``failed`` + a message when the Strava fetch raises;
  - the job writes ``failed`` rather than leaving a stuck spinner when it
    crashes outside the fetch;
  - a re-fetch still persists metadata when only the streams call fails;
  - refresh bookkeeping survives force_update_activity;
  - the refresh state reaches the client through /meta, which is what the client
    polls.

Since #592 a row id is not a Strava id: the seeded row 111 stands for Strava
activity 9111, and Strava is asked for 9111 with the token of the account it
came through (``strava_user_id``), only while that account is part of the trip.
"""
from __future__ import annotations

import json
import time

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.activities as activities_module
import models.db as db_module
from api.activities import _refresh_activity_job, router as activities_router
from api.deps import get_current_user
from api.projects import router as projects_router
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import StravaToken, UserInfo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]
_ELEV = [100.0, 120.0, 110.0]
#: The Strava id of the seeded row 111.
_STRAVA_ID = 9111


def _seed(engine):
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        proj = DBProject(user_info_id=u.id, name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        sess.add(StravaToken(
            user_info_id=u.id, access_token="tok", refresh_token="ref",
            expires_at=time.time() + 3600,
        ))
        act = DBActivity(
            id=111, user_info_id=u.id, name="Old name", type="Ride",
            strava_id=_STRAVA_ID, strava_user_id=u.id,
            distance=4000.0, moving_time=1000, elapsed_time=1200,
            total_elevation_gain=60.0,
            summary_polyline=polyline_lib.encode(_TRACK),
            elevation_profile_json=json.dumps(
                {"distances_km": [0.0, 1.0, 2.0], "elevations_m": _ELEV}),
        )
        sess.add(act)
        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="activity", activity_id=111))
        sess.commit()
        return u.id


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    uid = _seed(engine)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    app.include_router(activities_router)
    app.include_router(projects_router)
    return TestClient(app), engine, uid


def _raw(aid=_STRAVA_ID, name="Fresh name", distance=9999.0):
    """A minimal Strava activity-detail payload."""
    return {
        "id": aid, "name": name, "type": "Ride", "distance": distance,
        "moving_time": 1000, "elapsed_time": 1200, "total_elevation_gain": 60.0,
        "start_date": "2026-01-01T10:00:00Z",
        "start_date_local": "2026-01-01T11:00:00Z",
        "timezone": "Europe/Paris",
    }


class _FakeClient:
    """Stands in for StravaAPI. `remaining_requests` gates the streams call."""

    def __init__(self, raw=None, streams=None, fail_activity=None,
                 fail_streams=False, remaining=100):
        self._raw = raw if raw is not None else _raw()
        self._streams = streams
        self._fail_activity = fail_activity
        self._fail_streams = fail_streams
        self.remaining_requests = remaining
        self.streams_calls = 0
        self.asked = []

    def get_activity(self, aid):
        self.asked.append(aid)
        if self._fail_activity is not None:
            raise self._fail_activity
        return self._raw

    def get_activity_streams(self, aid):
        self.asked.append(aid)
        self.streams_calls += 1
        if self._fail_streams:
            raise RuntimeError("streams boom")
        return self._streams or {}


def _use_client(monkeypatch, fake):
    monkeypatch.setattr(
        activities_module, "_strava_client_for_user", lambda _uid: fake)


def _row(engine, aid=111):
    with Session(engine) as sess:
        return sess.get(DBActivity, aid)


# ── Trigger ───────────────────────────────────────────────────────────────────

def test_trigger_returns_202_and_marks_pending(env, monkeypatch):
    """The request returns immediately; the row is pending before any fetching.

    A no-op background runner keeps the job from running, which is exactly the
    window the client polls in.
    """
    client, engine, _ = env
    _use_client(monkeypatch, _FakeClient())
    scheduled = []
    monkeypatch.setattr(
        "fastapi.BackgroundTasks.add_task",
        lambda self, fn, *a, **kw: scheduled.append((fn, a)),
    )

    r = client.post("/api/projects/Trip/activities/111/refresh")

    assert r.status_code == 202, r.text
    assert r.json()["refresh_status"] == "pending"
    row = _row(engine)
    assert row.refresh_status == "pending"
    assert row.refresh_started_at  # ISO timestamp, used to spot orphaned jobs
    assert row.refresh_error is None
    # Still the pre-refresh data — nothing was fetched on the request path.
    assert row.name == "Old name"
    assert [fn.__name__ for fn, _ in scheduled] == ["_refresh_activity_job"]


def test_trigger_rejects_when_strava_not_connected(env, monkeypatch):
    """Knowable without a network call, so it stays synchronous: a 409, as
    every refresh the activity's Strava connection cannot serve (#592)."""
    client, engine, _ = env
    _use_client(monkeypatch, None)
    r = client.post("/api/projects/Trip/activities/111/refresh")
    assert r.status_code == 409
    assert "Strava not connected" in r.json()["detail"]
    # No job was scheduled, so nothing must be left pending.
    assert _row(engine).refresh_status is None


def test_trigger_rejects_edited_activity_synchronously(env, monkeypatch):
    client, engine, _ = env
    _use_client(monkeypatch, _FakeClient())
    with Session(engine) as sess:
        row = sess.get(DBActivity, 111)
        row.is_edited = True
        sess.add(row); sess.commit()

    r = client.post("/api/projects/Trip/activities/111/refresh")
    assert r.status_code == 409
    assert "locally edited" in r.json()["detail"]
    assert _row(engine).refresh_status is None


# ── Job ───────────────────────────────────────────────────────────────────────

def test_job_writes_resolved_and_fresh_data(env, monkeypatch):
    client, engine, uid = env
    fake = _FakeClient(
        raw=_raw(name="Fresh name", distance=9999.0),
        streams={
            "latlng": {"data": [[49.0, 3.0], [49.1, 3.1]]},
            "altitude": {"data": [200.0, 250.0]},
            "distance": {"data": [0.0, 1000.0]},
        },
    )
    _use_client(monkeypatch, fake)

    # TestClient runs background tasks before returning, so this exercises the
    # full trigger -> job path.
    r = client.post("/api/projects/Trip/activities/111/refresh")
    assert r.status_code == 202, r.text

    row = _row(engine)
    assert row.refresh_status == "resolved"
    assert row.refresh_error is None
    assert row.name == "Fresh name"
    assert row.distance == 9999.0
    # Streams were applied: the polyline is the fetched one, not the seeded one.
    assert polyline_lib.decode(row.summary_polyline)[0] == pytest.approx(
        (49.0, 3.0), abs=1e-4)
    ep = json.loads(row.elevation_profile_json)
    assert ep["elevations_m"] == [200.0, 250.0]
    assert ep["distances_km"] == [0.0, 1.0]
    # Strava was asked about the row's Strava id, never its row id.
    assert fake.asked == [_STRAVA_ID, _STRAVA_ID]
    assert row.strava_id == _STRAVA_ID


def test_job_writes_failed_when_strava_fetch_raises(env, monkeypatch):
    client, engine, _ = env
    _use_client(monkeypatch, _FakeClient(fail_activity=RuntimeError("502 upstream")))

    r = client.post("/api/projects/Trip/activities/111/refresh")
    assert r.status_code == 202, r.text

    row = _row(engine)
    assert row.refresh_status == "failed"
    assert "502 upstream" in row.refresh_error
    assert row.name == "Old name"  # untouched


def test_job_marks_failed_when_it_crashes_outside_the_fetch(env, monkeypatch):
    """A crash must still write a verdict, or the tile spins forever."""
    client, engine, _ = env
    _use_client(monkeypatch, _FakeClient())
    monkeypatch.setattr(
        activities_module._repo, "force_update_activity",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db exploded")),
    )

    r = client.post("/api/projects/Trip/activities/111/refresh")
    assert r.status_code == 202, r.text

    row = _row(engine)
    assert row.refresh_status == "failed"
    assert "db exploded" in row.refresh_error


def test_job_keeps_metadata_when_only_streams_fail(env, monkeypatch):
    """Streams are best-effort — a failure there still resolves the re-fetch."""
    client, engine, _ = env
    fake = _FakeClient(raw=_raw(name="Fresh name"), fail_streams=True)
    _use_client(monkeypatch, fake)

    r = client.post("/api/projects/Trip/activities/111/refresh")
    assert r.status_code == 202, r.text

    row = _row(engine)
    assert row.refresh_status == "resolved"
    assert row.name == "Fresh name"
    assert fake.streams_calls == 1


def test_job_skips_streams_when_quota_is_nearly_spent(env, monkeypatch):
    client, engine, _ = env
    fake = _FakeClient(raw=_raw(name="Fresh name"), remaining=1)
    _use_client(monkeypatch, fake)

    r = client.post("/api/projects/Trip/activities/111/refresh")
    assert r.status_code == 202, r.text

    assert fake.streams_calls == 0
    assert _row(engine).refresh_status == "resolved"
    assert _row(engine).name == "Fresh name"


def test_job_on_missing_row_does_not_raise(env, monkeypatch):
    """A row deleted mid-refresh must not blow up the background task."""
    _, engine, uid = env
    _use_client(monkeypatch, _FakeClient())
    with Session(engine) as sess:
        sess.delete(sess.get(DBActivity, 111)); sess.commit()

    _refresh_activity_job(uid, uid, "Trip", 111)  # must not raise


# ── Contract with the client's poll ───────────────────────────────────────────

def test_refresh_state_is_visible_through_meta(env, monkeypatch):
    """/meta is what the client polls, so the verdict has to travel on it."""
    client, engine, _ = env
    _use_client(monkeypatch, _FakeClient(raw=_raw(name="Fresh name")))

    before = client.get("/api/projects/Trip/meta").json()
    act = next(a for a in before["activities"] if a["id"] == 111)
    assert act["refresh_status"] is None

    client.post("/api/projects/Trip/activities/111/refresh")

    after = client.get("/api/projects/Trip/meta").json()
    act = next(a for a in after["activities"] if a["id"] == 111)
    assert act["refresh_status"] == "resolved"
    assert act["refresh_error"] is None


def _lock_version(engine, name="Trip") -> int:
    with Session(engine) as sess:
        return sess.exec(
            select(DBProject).where(DBProject.name == name)
        ).one().lock_version


def test_job_bumps_lock_version_on_success(env, monkeypatch):
    """A native client's on-disk cache only ever checks lock_version, so a
    re-fetched polyline/elevation must advance it (issue #173) — the same
    signal edit_activity_track/split_activity/etc. already bump on every
    other activity-mutating path."""
    client, engine, _ = env
    before = _lock_version(engine)
    fake = _FakeClient(
        raw=_raw(name="Fresh name"),
        streams={
            "latlng": {"data": [[49.0, 3.0], [49.1, 3.1]]},
            "altitude": {"data": [200.0, 250.0]},
            "distance": {"data": [0.0, 1000.0]},
        },
    )
    _use_client(monkeypatch, fake)

    r = client.post("/api/projects/Trip/activities/111/refresh")
    assert r.status_code == 202, r.text

    assert _row(engine).refresh_status == "resolved"
    assert _lock_version(engine) == before + 1


def test_force_update_does_not_clobber_refresh_bookkeeping(env, monkeypatch):
    """The job writes the activity and its status separately; the data write
    must not reset the status it is about to set."""
    from src.models.activity import Activity

    _, engine, uid = env
    with Session(engine) as sess:
        row = sess.get(DBActivity, 111)
        row.refresh_status = "pending"
        row.refresh_started_at = "2026-07-29T10:00:00+00:00"
        sess.add(row); sess.commit()

    with Session(engine) as sess:
        activities_module._repo.force_update_activity(
            sess, uid, Activity.from_strava_api(_raw(name="Fresh name")), row_id=111)

    row = _row(engine)
    assert row.name == "Fresh name"          # data was written
    assert row.refresh_status == "pending"   # bookkeeping untouched
    assert row.refresh_started_at == "2026-07-29T10:00:00+00:00"


# ── Whose token, and only while they are part of the trip (#592) ─────────────
#
# The trip "Trip" belongs to the owner. The companion, an editor, added Strava
# activity 9222 to it: row 222, owned by the trip's owner (plan Decision 4),
# with the companion as strava_user_id (Decision 5). The owner's own trip
# "Other" holds a copy of the same Strava activity, row 333.

_COMPANION_STRAVA_ID = 9222


@pytest.fixture
def team(monkeypatch):
    from api.members import router as members_router
    from models.project_db import DBProjectMember

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        companion = UserInfo(display_name="Companion", email="companion@e.com")
        sess.add(owner); sess.add(companion); sess.commit()
        sess.refresh(owner); sess.refresh(companion)
        trip = DBProject(user_info_id=owner.id, name="Trip")
        other = DBProject(user_info_id=owner.id, name="Other")
        sess.add(trip); sess.add(other); sess.commit()
        sess.refresh(trip); sess.refresh(other)
        sess.add(DBProjectMember(project_id=trip.id, user_info_id=companion.id,
                                 role="editor", invited_by=owner.id,
                                 created_at=time.time()))
        for uid in (owner.id, companion.id):
            sess.add(StravaToken(user_info_id=uid, access_token="tok",
                                 refresh_token="ref", expires_at=time.time() + 3600))
        for row_id, project in ((222, trip), (333, other)):
            sess.add(DBActivity(
                id=row_id, user_info_id=owner.id, name="Old name", type="Ride",
                strava_id=_COMPANION_STRAVA_ID, strava_user_id=companion.id,
                distance=4000.0, moving_time=1000, elapsed_time=1200,
                summary_polyline=polyline_lib.encode(_TRACK)))
            sess.add(DBProjectItem(project_id=project.id, position=0,
                                   item_type="activity", activity_id=row_id))
        sess.commit()
        ids = {"owner": owner.id, "companion": companion.id}

    current = {"uid": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(activities_router)
    app.include_router(projects_router)
    app.include_router(members_router)

    tokens_used = []
    fake = _FakeClient(raw=_raw(aid=_COMPANION_STRAVA_ID, name="Fresh name"))

    def _factory(uid):
        tokens_used.append(uid)
        return fake

    monkeypatch.setattr(activities_module, "_strava_client_for_user", _factory)

    def act_as(who):
        current["uid"] = ids[who]

    return TestClient(app), engine, ids, act_as, fake, tokens_used


def test_owner_refreshes_a_companions_activity_with_the_companions_token(team):
    client, engine, ids, act_as, fake, tokens_used = team
    act_as("owner")

    r = client.post("/api/projects/Trip/activities/222/refresh")

    assert r.status_code == 202, r.text
    assert tokens_used and set(tokens_used) == {ids["companion"]}
    assert fake.asked == [_COMPANION_STRAVA_ID, _COMPANION_STRAVA_ID]
    row = _row(engine, 222)
    assert row.refresh_status == "resolved"
    assert row.name == "Fresh name"
    # Refreshing keeps the row the trip owner's, its Strava link the companion's.
    assert (row.user_info_id, row.strava_user_id) == (ids["owner"], ids["companion"])


def test_a_refresh_writes_and_bumps_only_the_trip_that_asked(team):
    """The owner's other trip holds its own copy of the same Strava activity:
    it keeps what it has, and its lock_version does not move (Decision 6)."""
    client, engine, _, act_as, _, _ = team
    act_as("owner")
    before_other = _lock_version(engine, "Other")
    before_trip = _lock_version(engine, "Trip")

    assert client.post("/api/projects/Trip/activities/222/refresh").status_code == 202

    assert _row(engine, 222).name == "Fresh name"
    other_copy = _row(engine, 333)
    assert other_copy.name == "Old name"
    assert other_copy.refresh_status is None
    assert _lock_version(engine, "Other") == before_other
    assert _lock_version(engine, "Trip") == before_trip + 1


def test_refresh_after_the_importer_was_deleted_is_a_409(team):
    """Account deletion clears strava_user_id (Decision 5): no token is left
    to ask Strava with."""
    client, engine, _, act_as, fake, tokens_used = team
    with Session(engine) as sess:
        row = sess.get(DBActivity, 222)
        row.strava_user_id = None
        sess.add(row); sess.commit()
    act_as("owner")

    r = client.post("/api/projects/Trip/activities/222/refresh")

    assert r.status_code == 409
    assert "no longer exists" in r.json()["detail"]
    assert tokens_used == [] and fake.asked == []
    assert _row(engine, 222).refresh_status is None


@pytest.mark.parametrize("how", ["left", "removed"])
def test_refresh_after_the_importer_left_the_trip_is_a_409_with_no_strava_call(team, how):
    """A companion who leaves, or is removed, takes their Strava access with
    them (review R1-5)."""
    client, engine, ids, act_as, fake, tokens_used = team
    act_as("companion" if how == "left" else "owner")
    r = client.delete(f"/api/projects/Trip/members/{ids['companion']}",
                      params={"owner": ids["owner"]} if how == "left" else None)
    assert r.status_code == 204, r.text
    act_as("owner")

    r = client.post("/api/projects/Trip/activities/222/refresh")

    assert r.status_code == 409
    assert "no longer part of this trip" in r.json()["detail"]
    assert tokens_used == [] and fake.asked == []
    row = _row(engine, 222)
    assert row.refresh_status is None
    assert row.name == "Old name"


def test_the_job_makes_no_strava_call_once_the_importer_has_left(team):
    """Checked again in the job: the trigger's answer is a moment old."""
    _, engine, ids, _, fake, tokens_used = team
    from models.project_db import DBProjectMember
    with Session(engine) as sess:
        for member in sess.exec(select(DBProjectMember)).all():
            sess.delete(member)
        sess.commit()

    _refresh_activity_job(ids["companion"], ids["owner"], "Trip", 222)

    assert tokens_used == [] and fake.asked == []
    row = _row(engine, 222)
    assert row.refresh_status == "failed"
    assert row.name == "Old name"


def test_an_old_picker_refreshes_by_strava_id(team):
    """An installed picker posts the Strava id (Decision 11): the trip's row
    with that strava_id is refreshed, and no other trip's copy."""
    client, engine, _, act_as, fake, _ = team
    act_as("owner")

    r = client.post(f"/api/projects/Trip/activities/{_COMPANION_STRAVA_ID}/refresh")

    assert r.status_code == 202, r.text
    assert fake.asked == [_COMPANION_STRAVA_ID, _COMPANION_STRAVA_ID]
    assert _row(engine, 222).refresh_status == "resolved"
    assert _row(engine, 222).name == "Fresh name"
    assert _row(engine, 333).name == "Old name"


def test_a_row_the_trip_does_not_hold_is_a_404(team):
    """Row 333 is the other trip's copy: neither its id nor any Strava id
    names a row of this trip."""
    client, engine, _, act_as, fake, tokens_used = team
    act_as("owner")

    r = client.post("/api/projects/Trip/activities/333/refresh")

    assert r.status_code == 404
    assert tokens_used == [] and fake.asked == []
    assert _row(engine, 333).refresh_status is None


def test_a_row_not_from_strava_is_a_409(team):
    client, engine, _, act_as, fake, tokens_used = team
    with Session(engine) as sess:
        row = sess.get(DBActivity, 222)
        row.strava_id = None
        sess.add(row); sess.commit()
    act_as("owner")

    r = client.post("/api/projects/Trip/activities/222/refresh")

    assert r.status_code == 409
    assert "did not come from Strava" in r.json()["detail"]
    assert tokens_used == [] and fake.asked == []


# ── A refresh force_update_activity declines is logged (review U4R1-2) ───────

def test_a_refresh_dropped_for_another_strava_user_is_logged(env, caplog):
    """The importer changed under the job (account deleted, so NULL): the
    row is left as it was, and the log says why."""
    import logging
    from src.models.activity import Activity
    _, engine, uid = env
    with Session(engine) as sess:
        row = sess.get(DBActivity, 111)
        row.strava_user_id = None
        sess.add(row); sess.commit()

    with caplog.at_level(logging.WARNING), Session(engine) as sess:
        activities_module._repo.force_update_activity(
            sess, uid, Activity.from_strava_api(_raw(name="Fresh name")), row_id=111)

    assert _row(engine).name == "Old name"
    assert any(r.levelno == logging.WARNING
               and "strava_user_id no longer matches" in r.getMessage()
               for r in caplog.records)


def test_a_refresh_dropped_for_a_trip_no_longer_holding_the_row_is_logged(env, caplog):
    import logging
    from src.models.activity import Activity
    _, engine, uid = env
    with Session(engine) as sess:
        project_id = sess.exec(select(DBProject.id)).one()
        for item in sess.exec(select(DBProjectItem)).all():
            sess.delete(item)
        sess.commit()

    with caplog.at_level(logging.WARNING), Session(engine) as sess:
        activities_module._repo.force_update_activity(
            sess, uid, Activity.from_strava_api(_raw(name="Fresh name")),
            project_id, row_id=111)

    assert _row(engine).name == "Old name"
    assert any(r.levelno == logging.WARNING
               and f"trip {project_id} no longer holds the row" in r.getMessage()
               for r in caplog.records)

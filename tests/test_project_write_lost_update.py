"""A load-mutate-save that blind-overwrites loses a concurrent writer's work.

`save_project` with the default `check_version=False` rewrites *every* field of
the project row from the in-memory snapshot the caller loaded — day-meta,
sleeping options, counters, track style, trip dates. So any write that commits
between that load and the save is silently reverted to its pre-request value,
with no error on either side.

Two importers still did this: the GPX import and the Strava sync. The Strava
one is the worst window in the app, since a network round trip to Strava sits
inside it. `save_project_with_retry` reloads and re-applies the mutation under
the optimistic lock instead — the policy every other structural mutation
already used (issues #172/#173).

These tests drive the real endpoints and land a real concurrent write inside
the window, by hooking the repo call the endpoint makes mid-request.
"""
from __future__ import annotations

import json
import time
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.activities import router as activities_router
from api.deps import get_current_user
from api.projects import router as projects_router
from api.strava import router as strava_router
from models.project_db import DBProject
from models.user import UserInfo


def _gpx_bytes():
    pts = [(48.0, 2.0, 100.0), (48.001, 2.001, 110.0), (48.002, 2.002, 105.0)]
    body = "".join(
        f'<trkpt lat="{la}" lon="{lo}"><ele>{el}</ele></trkpt>' for la, lo, el in pts
    )
    return (
        '<?xml version="1.0"?>'
        '<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><trkseg>{body}</trkseg></trk></gpx>"
    ).encode("utf-8")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A file-backed database, per test.

    Not the usual in-memory StaticPool engine: this module is about two writers
    racing, and StaticPool hands every Session the same connection, so the
    "concurrent" write would share the caller's transaction and the isolation
    under test would be fake.

    Only `db_module.engine` is patched, never `get_session`. Patching the
    factory was what made an earlier version of this file mis-route writes
    between tests: the routers are imported lazily below, so the first import
    ran `from models.db import get_session` while the patch was live and bound
    *that* test's lambda — and with it that test's engine — permanently into
    the router module. The second test's PUT then wrote to the first test's
    database and the race quietly stopped happening. The real `get_session`
    reads the `engine` global at call time, so patching the engine alone is
    both sufficient and stable. (Nothing in api/ binds a factory on its own;
    this was the harness doing it to itself.)
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(owner); sess.commit(); sess.refresh(owner)
        proj = DBProject(user_info_id=owner.id, name="Trip",
                         day_meta_json=json.dumps({"2024-06-01": {"note": "before"}}))
        sess.add(proj); sess.commit(); sess.refresh(proj)
        ids = {"owner": owner.id, "project": proj.id, "name": proj.name}

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(ids["owner"])}
    app.include_router(activities_router)
    app.include_router(projects_router)
    app.include_router(strava_router)
    return TestClient(app), engine, ids


def _concurrent_day_meta_write(client, name: str, payload: dict):
    """Another request's PATCH /day-meta committing right now — the REAL
    endpoint, not a stand-in.

    An earlier version of this helper wrote the row by hand and bumped
    lock_version itself. That faked the one thing the endpoint did not do, so
    the tests certified a compare-and-swap that could never fire in
    production. Drive the real route or prove nothing.
    """
    r = client.patch(f"/api/projects/{name}/day-meta", json={"days": payload})
    assert r.status_code == 200, r.text


def _stored_day_meta(engine, name):
    with Session(engine) as sess:
        row = sess.exec(select(DBProject).where(DBProject.name == name)).one()
        return json.loads(row.day_meta_json) if row.day_meta_json else {}


def test_gpx_import_does_not_revert_a_day_meta_write_made_while_it_ran(env, monkeypatch):
    client, engine, ids = env
    import api.activities as activities_mod
    monkeypatch.setattr(activities_mod, "_schedule_gpx_enrichment", lambda *a, **k: None,
                        raising=False)

    # Fire the concurrent write from the quota check: it runs exactly once per
    # save attempt, inside the mutate callback, i.e. squarely between the load
    # and the save. (Project.add_activities is NOT a safe hook — the loader
    # calls it too, which would land the write before the window opens.)
    fired = {"done": False}
    real_quota = activities_mod.ensure_trip_days_quota

    def _quota_then_race(*a, **k):
        result = real_quota(*a, **k)
        if not fired["done"]:
            fired["done"] = True
            _concurrent_day_meta_write(client, ids["name"], {"2024-06-02": {"note": "written during"}})
        return result

    monkeypatch.setattr(activities_mod, "ensure_trip_days_quota", _quota_then_race)

    r = client.post(
        f"/api/projects/{ids['name']}/activities/import-gpx",
        files={"file": ("t.gpx", _gpx_bytes(), "application/gpx+xml")},
        data={"date": "2024-06-01", "start_time": "09:00", "end_time": "10:00",
              "activity_type": "Hike"},
    )
    assert r.status_code == 200, r.text
    assert fired["done"], "the concurrent write never ran — test is not exercising the window"

    # The concurrent write must survive. A blind save rewrites day_meta_json
    # from the snapshot loaded before it, restoring {"2024-06-01": ...}.
    assert "2024-06-02" in _stored_day_meta(engine, ids["name"])


class _FakeStravaClient:
    """Stands in for StravaAPI; never refreshes, so no token is persisted."""
    token_data = {"access_token": "tok", "refresh_token": "ref", "expires_at": 9e9}


def _raw_strava_activity(act_id, start):
    return {
        "id": act_id, "name": "Ride", "type": "Ride",
        "distance": 1000.0, "moving_time": 100, "elapsed_time": 120,
        "total_elevation_gain": 0.0,
        "start_date": start, "start_date_local": start,
    }


def test_strava_sync_does_not_revert_a_day_meta_write_made_during_the_fetch(
        env, monkeypatch):
    """The Strava fetch is a network round trip that used to sit inside the
    load-save window — the widest one in the app. A write committing there was
    reverted to its pre-request value.

    Note what this does and does not pin. The fix moved the fetch *before* the
    load, so this test passes on the narrowed window alone and would still pass
    with the optimistic lock broken; the CAS itself is pinned by the two GPX
    tests. Keep that in mind before trusting it as a lock regression test —
    an earlier commit message wrongly claimed it was one."""
    client, engine, ids = env
    from models.user import StravaToken
    import api.strava as strava_mod

    with Session(engine) as sess:
        sess.add(StravaToken(user_info_id=ids["owner"], access_token="tok",
                             refresh_token="ref", expires_at=9e9))
        sess.commit()

    fired = {"done": False}

    def _fetch(_client, after=None, before=None):
        # Land the concurrent write exactly where the real fetch blocks.
        fired["done"] = True
        _concurrent_day_meta_write(client, ids["name"], {"2024-06-02": {"note": "written during"}})
        return [_raw_strava_activity(1, "2024-06-03T10:00:00Z")]

    monkeypatch.setattr(strava_mod, "_strava_client_for_token",
                        lambda token_row: _FakeStravaClient())
    monkeypatch.setattr(strava_mod, "_fetch_all_strava", _fetch)

    r = client.post(f"/api/projects/{ids['name']}/strava/sync")
    assert r.status_code == 200, r.text
    assert fired["done"], "the fetch never ran — test is not exercising the window"

    assert "2024-06-02" in _stored_day_meta(engine, ids["name"])


# ── The mechanism, not just the outcome ──────────────────────────────────────

def test_the_conflict_is_actually_detected_and_the_mutation_re_runs(env, monkeypatch):
    """The outcome tests above would also pass if the save simply happened to
    read fresh state. This pins the mechanism: the compare-and-swap must see
    the concurrent write, and the mutation must be re-applied to a freshly
    loaded project rather than to the snapshot the CAS just rejected."""
    import src.project.repo_core as repo_core
    client, engine, ids = env
    import api.activities as activities_mod
    monkeypatch.setattr(activities_mod, "_schedule_gpx_enrichment",
                        lambda *a, **k: None, raising=False)

    calls = {"mutate": 0}
    conflicts = {"n": 0}
    real_quota = activities_mod.ensure_trip_days_quota

    def _quota_then_race(*a, **k):
        calls["mutate"] += 1
        result = real_quota(*a, **k)
        if calls["mutate"] == 1:
            _concurrent_day_meta_write(client, ids["name"], {"2024-06-02": {"note": "during"}})
        return result

    monkeypatch.setattr(activities_mod, "ensure_trip_days_quota", _quota_then_race)

    real_save = repo_core.ProjectCoreMixin.save_project

    def _counting_save(self, sess, uid, project, **kw):
        try:
            return real_save(self, sess, uid, project, **kw)
        except repo_core.StaleWriteError:
            conflicts["n"] += 1
            raise

    monkeypatch.setattr(repo_core.ProjectCoreMixin, "save_project", _counting_save)

    r = client.post(
        f"/api/projects/{ids['name']}/activities/import-gpx",
        files={"file": ("t.gpx", _gpx_bytes(), "application/gpx+xml")},
        data={"date": "2024-06-01", "start_time": "09:00", "end_time": "10:00",
              "activity_type": "Hike"},
    )
    assert r.status_code == 200, r.text
    assert conflicts["n"] >= 1, "the CAS never detected the concurrent write"
    assert calls["mutate"] >= 2, "the mutation did not re-run on fresh state"
    assert "2024-06-02" in _stored_day_meta(engine, ids["name"])


def test_a_quota_refusal_surfaces_as_402_instead_of_being_retried(env, monkeypatch):
    """ensure_trip_days_quota now runs inside the retry callback. Its 402 must
    escape the loop on the first attempt — retrying it would burn all five
    attempts and surface the wrong error (a 409) for a billing limit."""
    from fastapi import HTTPException
    client, engine, ids = env
    import api.activities as activities_mod
    monkeypatch.setattr(activities_mod, "_schedule_gpx_enrichment",
                        lambda *a, **k: None, raising=False)

    calls = {"n": 0}

    def _refuse(*a, **k):
        calls["n"] += 1
        raise HTTPException(status_code=402, detail={"resource": "trip_days"})

    monkeypatch.setattr(activities_mod, "ensure_trip_days_quota", _refuse)

    r = client.post(
        f"/api/projects/{ids['name']}/activities/import-gpx",
        files={"file": ("t.gpx", _gpx_bytes(), "application/gpx+xml")},
        data={"date": "2024-06-01", "start_time": "09:00", "end_time": "10:00",
              "activity_type": "Hike"},
    )
    assert r.status_code == 402, r.text
    assert calls["n"] == 1, "the quota refusal was retried instead of surfacing"


def test_every_direct_project_write_advances_the_lock_past_a_concurrent_bump(
        env, monkeypatch):
    """The counter must be advanced in SQL, and this asserts the *effect*.

    An inline `row.lock_version = loaded + 1` computes the new value from what
    this request's ORM loaded. Land another writer's bump between that load and
    this one's write and the two resolve to the same N+1: the counter stands
    still, and the compare-and-swap behind save_project_with_retry is blind to
    one of them.

    An earlier version of this test asserted only that bump_lock_version was
    *called*, on the stated grounds that SQLite serialises writers so no
    sequential test could tell the two forms apart. That was wrong — injecting
    the concurrent bump at exactly this point distinguishes them — and the
    call-only test passed with the lock fully disabled.
    """
    import api.projects as projects_mod

    client, engine, ids = env
    name = ids["name"]

    def lock_version():
        with Session(engine) as sess:
            return sess.exec(
                select(DBProject).where(DBProject.name == name)
            ).one().lock_version

    real = projects_mod.bump_lock_version

    def _bump_after_someone_else(sess, project_id):
        # Another writer commits between this request's load and its bump.
        with Session(engine) as other:
            real(other, project_id)
            other.commit()
        real(sess, project_id)

    monkeypatch.setattr(projects_mod, "bump_lock_version", _bump_after_someone_else)

    # PATCH /day-meta advances the counter by compare-and-set, not a blind
    # bump: land the other writer's bump in front of its first one. It loses
    # that attempt and retries on a fresh read.
    real_cas = projects_mod.check_and_bump_lock_version
    cas_raced = {"done": False}

    def _cas_after_someone_else(sess, project_id, expected_version):
        if not cas_raced["done"]:
            cas_raced["done"] = True
            with Session(engine) as other:
                real(other, project_id)
                other.commit()
        real_cas(sess, project_id, expected_version)

    monkeypatch.setattr(projects_mod, "check_and_bump_lock_version",
                        _cas_after_someone_else)

    writes = {
        "day-meta": ("PATCH", f"/api/projects/{name}/day-meta",
                     {"days": {"2024-06-05": {"note": "a"}}}),
        "trip dates": ("PUT", f"/api/projects/{name}", {"trip_start": "2024-06-01"}),
        "track style": ("PUT", f"/api/projects/{name}/track-style",
                        {"track_color": "#ff0000"}),
        "languages": ("PUT", f"/api/projects/{name}/languages", {"languages": ["fr"]}),
    }
    for label, (method, url, body) in writes.items():
        before = lock_version()
        r = client.request(method, url, json=body)
        assert r.status_code in (200, 204), f"{label}: {r.text}"
        assert lock_version() == before + 2, (
            f"{label}: counter went {before} -> {lock_version()}; one of the two "
            "writes was invisible to the optimistic lock"
        )


def test_a_corrupt_day_entry_is_never_treated_as_content_to_protect(env):
    """_day_meta_has_content must say False for a non-dict entry: protecting an
    unreadable blob would pin that day in place permanently, and there is
    nothing in it a user could miss."""
    from api.projects import _day_meta_has_content

    assert _day_meta_has_content("rubble") is False
    assert _day_meta_has_content(["rubble"]) is False
    assert _day_meta_has_content(None) is False
    assert _day_meta_has_content({}) is False
    assert _day_meta_has_content({"note": None, "counters": []}) is False
    assert _day_meta_has_content({"note": "real"}) is True

"""PATCH /day-meta merges per day, and the whole-map PUT is retired (issue #397).

The PUT took the client's whole map and wrote it back. A device holding a map
from before another device's edit wrote that edit away, with no error on
either side. The PATCH names the days it sets and the days it deletes, and
leaves every other day as stored. Its read-merge-write runs under the
``lock_version`` compare-and-set and retries on a fresh read, so a write that
commits in between is merged with, not over.

Races are landed for real: a hook on the compare-and-set runs the other
writer's real request between this one's read and its write. The database is
a file, as in production: an in-memory StaticPool engine shares one
connection between sessions and cannot show two writers contending.
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.projects import router as projects_router
from models.project_db import DBProject, DBProjectMember
from models.user import UserInfo
from src.models.project import DayMeta
from src.project.repo_core import StaleWriteError

_URL = "/api/projects/Trip/day-meta"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'trip.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    import api.journal as journal_mod
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        users = {who: UserInfo(display_name=who.capitalize(), email=f"{who}@e.com")
                 for who in ("owner", "editor", "viewer")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {who: u.id for who, u in users.items()}
        proj = DBProject(user_info_id=ids["owner"], name="Trip",
                         day_meta_json=json.dumps({"2024-06-01": {"journal": "before"}}))
        sess.add(proj)
        sess.commit()
        sess.refresh(proj)
        ids["project"] = proj.id
        for who in ("editor", "viewer"):
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=ids[who],
                                     role=who, invited_by=ids["owner"]))
        sess.commit()

    current = {"uid": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(projects_router)
    app.include_router(journal_router)
    yield TestClient(app), engine, ids, lambda who: current.update(uid=ids[who])
    engine.dispose()


def _row(engine):
    with Session(engine) as sess:
        return sess.exec(select(DBProject).where(DBProject.name == "Trip")).one()


def _stored(engine) -> dict:
    return json.loads(_row(engine).day_meta_json or "{}")


def _patch(client, url=_URL, **body):
    r = client.patch(url, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _race_before_first_cas(monkeypatch, other_write):
    """Run *other_write* once, between the PATCH's read and its compare-and-set.
    Returns the list of compare-and-set calls, one per attempt."""
    import api.projects as projects_mod
    real_cas = projects_mod.check_and_bump_lock_version
    calls = []

    def _cas(sess, project_id, expected_version):
        calls.append(expected_version)
        if len(calls) == 1:
            other_write()
        real_cas(sess, project_id, expected_version)

    monkeypatch.setattr(projects_mod, "check_and_bump_lock_version", _cas)
    return calls


# ── Concurrent writers ──────────────────────────────────────────────────────

def test_two_patches_to_different_days_both_persist(env, monkeypatch):
    client, engine, *_ = env
    calls = _race_before_first_cas(monkeypatch, lambda: _patch(
        client, days={"2024-06-03": {"journal": "from the other device"}}))

    _patch(client, days={"2024-06-02": {"journal": "from this device"}})

    assert _stored(engine) == {
        "2024-06-01": {"journal": "before"},
        "2024-06-02": {"journal": "from this device"},
        "2024-06-03": {"journal": "from the other device"},
    }
    # Two calls from the outer PATCH (lost, then won) and one from the inner.
    assert len(calls) == 3, "the compare-and-set never saw the other write"


def test_patches_from_parallel_threads_all_land(env):
    """No injected hook: real threads, each setting its own day."""
    client, engine, *_ = env
    days = [f"2024-06-1{i}" for i in range(4)]
    barrier = threading.Barrier(len(days))
    results = {}

    def _write(day):
        barrier.wait()
        results[day] = client.patch(_URL, json={"days": {day: {"journal": day}}}).status_code

    threads = [threading.Thread(target=_write, args=(d,)) for d in days]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {d: 200 for d in days}
    stored = _stored(engine)
    assert {d: stored.get(d) for d in days} == {d: {"journal": d} for d in days}
    assert stored["2024-06-01"] == {"journal": "before"}


def test_a_patch_inside_a_structural_save_survives_it(env):
    """save_project rewrites day_meta_json from the project it loaded. A PATCH
    committing inside that window must make the save retry on a fresh read."""
    from api.project_shared import _repo
    client, engine, ids, _ = env
    runs = []

    def _mutate(project):
        runs.append(1)
        if len(runs) == 1:
            _patch(client, days={"2024-06-02": {"journal": "patched"}})
        project.trip_start = "2024-06-01"
        project.day_meta["2024-06-09"] = DayMeta(journal="structural")

    _repo.save_project_with_retry(ids["owner"], "Trip", _mutate)

    assert len(runs) == 2, "the structural save never saw the PATCH"
    stored = _stored(engine)
    assert stored["2024-06-02"]["journal"] == "patched"
    assert stored["2024-06-09"]["journal"] == "structural"
    assert _row(engine).trip_start == "2024-06-01"


def test_a_structural_save_inside_a_patch_survives_it(env, monkeypatch):
    from api.project_shared import _repo
    client, engine, ids, _ = env

    def _structural():
        def _mutate(project):
            project.trip_start = "2024-06-01"
            project.day_meta["2024-06-09"] = DayMeta(journal="structural")
        _repo.save_project_with_retry(ids["owner"], "Trip", _mutate)

    calls = _race_before_first_cas(monkeypatch, _structural)

    _patch(client, days={"2024-06-02": {"journal": "patched"}})

    assert len(calls) == 2, "the PATCH never saw the structural save"
    stored = _stored(engine)
    assert stored["2024-06-02"] == {"journal": "patched"}
    assert stored["2024-06-09"]["journal"] == "structural"
    assert _row(engine).trip_start == "2024-06-01"


def test_a_retry_keeps_the_counters_stored_by_then(env, monkeypatch):
    """The counter merge copies stored counters into the day being set. A
    retry must start from the request as sent, or it would write the lost
    attempt's counters over the ones the other writer just stored."""
    client, engine, *_ = env
    _patch(client, days={"2024-06-02": {"journal": "x",
                                        "counters": [{"name": "km", "value": 1}]}})
    _race_before_first_cas(monkeypatch, lambda: _patch(
        client, days={"2024-06-02": {"journal": "x",
                                     "counters": [{"name": "km", "value": 9}]}}))

    _patch(client, days={"2024-06-02": {"journal": "y"}})

    assert _stored(engine)["2024-06-02"] == {
        "journal": "y", "counters": [{"name": "km", "value": 9}]}


def test_it_gives_up_after_five_lost_attempts_and_writes_nothing(env, monkeypatch):
    """The app maps StaleWriteError to 409 (api.router)."""
    import api.projects as projects_mod
    client, engine, ids, _ = env
    real_cas = projects_mod.check_and_bump_lock_version
    calls = []

    def _always_raced(sess, project_id, expected_version):
        calls.append(expected_version)
        with Session(engine) as other:
            projects_mod.bump_lock_version(other, project_id)
            other.commit()
        real_cas(sess, project_id, expected_version)

    monkeypatch.setattr(projects_mod, "check_and_bump_lock_version", _always_raced)

    with pytest.raises(StaleWriteError):
        client.patch(_URL, json={"days": {"2024-06-02": {"journal": "lost"}}})

    assert len(calls) == 5
    assert "2024-06-02" not in _stored(engine)


# ── The merge ───────────────────────────────────────────────────────────────

def test_set_and_delete_touch_only_the_named_days(env):
    client, engine, *_ = env
    _patch(client, days={"2024-06-02": {"journal": "two"},
                         "2024-06-03": {"journal": "three"}})

    _patch(client, days={"2024-06-02": {"tags": ["alps"]}},
           delete=["2024-06-03", "2024-06-30"])  # an absent day is a no-op

    assert _stored(engine) == {
        "2024-06-01": {"journal": "before"},
        "2024-06-02": {"tags": ["alps"]},  # the entry is replaced whole
    }


def test_omitting_counters_keeps_them_and_an_empty_list_clears_them(env):
    client, engine, *_ = env
    _patch(client, days={"2024-06-02": {"journal": "a",
                                        "counters": [{"name": "km", "value": 3}]}})

    _patch(client, days={"2024-06-02": {"journal": "b"}})
    assert _stored(engine)["2024-06-02"] == {
        "journal": "b", "counters": [{"name": "km", "value": 3}]}

    _patch(client, days={"2024-06-02": {"journal": "b", "counters": []}})
    assert _stored(engine)["2024-06-02"] == {"journal": "b", "counters": []}


def test_delete_respects_the_387_guard(env):
    """A day only another member's journal keeps on screen is not the
    caller's to delete; an ordinary day in the same request goes."""
    client, engine, ids, act_as = env
    _patch(client, days={"2024-06-04": {"journal": "shared notes"},
                         "2024-06-05": {"journal": "ordinary"}})
    act_as("editor")
    r = client.post(f"/api/journal/?owner={ids['owner']}", json={
        "project_name": "Trip", "date": "2024-06-04",
        "geo_mode": "custom", "lat": 1.0, "lon": 2.0,
        "description": "editor's private note",
    })
    assert r.status_code == 201, r.text
    act_as("owner")

    body = _patch(client, delete=["2024-06-04", "2024-06-05"])

    assert _stored(engine) == {
        "2024-06-01": {"journal": "before"},
        "2024-06-04": {"journal": "shared notes"},
    }
    assert "2024-06-04" in body["day_meta"]


def test_sleeping_options_and_counters_keep_the_put_semantics(env):
    client, engine, *_ = env
    _patch(client, sleeping_options=["Hut"], sleeping_option_groups={"Hut": "Indoors"},
           counters=[{"name": "km", "start": 5}])
    _patch(client, sleeping_options=[])  # an empty list never wipes them

    row = _row(engine)
    assert json.loads(row.sleeping_options_json) == [{"name": "Hut", "group": "Indoors"}]
    assert json.loads(row.counters_json) == [{"name": "km", "start": 5.0}]


# ── Refusals ────────────────────────────────────────────────────────────────

def test_a_viewer_gets_403(env):
    client, engine, ids, act_as = env
    act_as("viewer")
    r = client.patch(f"{_URL}?owner={ids['owner']}",
                     json={"days": {"2024-06-02": {"journal": "no"}}})
    assert r.status_code == 403
    assert "2024-06-02" not in _stored(engine)


def test_a_day_both_set_and_deleted_is_400(env):
    client, engine, *_ = env
    before = _row(engine).lock_version
    r = client.patch(_URL, json={"days": {"2024-06-01": {"journal": "x"}},
                                 "delete": ["2024-06-01"]})
    assert r.status_code == 400
    assert "2024-06-01" in r.json()["detail"]
    assert _stored(engine) == {"2024-06-01": {"journal": "before"}}
    assert _row(engine).lock_version == before


# ── The response ────────────────────────────────────────────────────────────

def test_the_response_is_what_meta_serves_next(env):
    """Nulls dropped, counters normalised, an unreadable stored entry gone:
    whatever /meta does to the stored map, the response did too."""
    client, engine, *_ = env
    with Session(engine) as sess:
        row = sess.get(DBProject, _row(engine).id)
        row.day_meta_json = json.dumps({
            "2024-06-01": {"journal": "before", "weather": None},
            "2024-06-06": "rubble",
            "2024-06-07": {"counters": {"km": 4}},  # legacy map form
        })
        sess.add(row)
        sess.commit()

    body = _patch(client, days={
        "2024-06-02": {"journal": "x", "sleeping": None, "tags": [],
                       "counters": [{"name": "km", "value": 2}]},
        "2024-06-03": {"tags": None, "difficulty": "hard"},
    })

    meta = client.get("/api/projects/Trip/meta")
    assert meta.status_code == 200, meta.text
    assert body["day_meta"] == meta.json()["day_meta"]
    assert body["day_meta"]["2024-06-07"] == {"counters": [{"name": "km", "value": 4.0}]}
    assert "2024-06-06" not in body["day_meta"]


# ── The retired PUT ─────────────────────────────────────────────────────────

def test_the_put_is_retired_with_426_and_writes_nothing(env):
    client, engine, *_ = env
    before = _row(engine)

    r = client.put(_URL, json={"day_meta": {"2024-06-02": {"journal": "stale map"}}})

    assert r.status_code == 426
    assert r.json()["detail"] == (
        "This version of the app can no longer save day notes. Please update the app.")
    after = _row(engine)
    assert after.day_meta_json == before.day_meta_json
    assert after.lock_version == before.lock_version

"""The /meta payload cache (issue #178).

``GET /api/projects/{name}/meta`` is what the activity panel blocks on at load
and what the client polls every few seconds while a re-fetch or a route resolve
is pending; on a large project a cold load measured 11-13 s, most of the
client's 30 s budget. It now rides the same per-project byte cache as the
full-res geo payload, which brings two things that must be proven here: it is
keyed per *caller* (journal entries and caller_role differ per member), and it
is invalidated by writes rather than only by its TTL.

Cache mechanics shared with geo (generation guard, TTL, cross-process busts)
are covered by test_geo_cache_staleness.py / test_geo_cache_cross_process.py.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.project_shared as shared_mod
import models.db as db_module
from api.deps import get_current_user
from api.geo import _geo_cache, _geo_gen
from api.journal import router as journal_router
from api.members import router as members_router
from api.project_shared import warm_meta_cache
from api.projects import router as projects_router
from models.project_db import (
    DBJournalEntry,
    DBProject,
    DBProjectItem,
    DBProjectMember,
)
from models.user import UserInfo


@pytest.fixture
def env(monkeypatch):
    """An owner and a companion (editor) on one project, each with a journal entry."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    _geo_cache.clear()
    _geo_gen.clear()

    with Session(engine) as sess:
        owner = UserInfo(display_name="Alice", email="a@b.c")
        mate = UserInfo(display_name="Bob", email="b@b.c")
        sess.add(owner)
        sess.add(mate)
        sess.commit()
        sess.refresh(owner)
        sess.refresh(mate)
        owner_id, mate_id = owner.id, mate.id

        project = DBProject(user_info_id=owner_id, name="Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        pid = project.id

        sess.add(DBProjectMember(project_id=pid, user_info_id=mate_id, role="editor",
                                 invited_by=owner_id))
        for uid, text in ((owner_id, "alice's private entry"), (mate_id, "bob's private entry")):
            entry = DBJournalEntry(project_id=pid, user_info_id=uid, date="2026-06-01",
                                   description=text, photos_json="[]")
            sess.add(entry)
            sess.flush()
            sess.add(DBProjectItem(project_id=pid, position=0, item_type="journal",
                                   journal_id=entry.id))
        sess.commit()

    app = FastAPI()
    app.include_router(projects_router)
    app.include_router(members_router)
    app.include_router(journal_router)
    caller = {"id": owner_id}
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(caller["id"])}

    def as_user(user_id: int) -> None:
        caller["id"] = user_id

    yield TestClient(app), owner_id, mate_id, as_user, engine


def _meta(client, *, owner: int | None = None):
    url = "/api/projects/Trip/meta" + (f"?owner={owner}" if owner else "")
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    return resp


def _descriptions(resp) -> list[str]:
    return [
        i["journal"]["description"]
        for i in resp.json()["items"]
        if i.get("item_type") == "journal"
    ]


def test_a_second_read_is_served_from_the_cache(env):
    client, *_ = env
    assert _meta(client).headers["x-cache"] == "MISS"
    hit = _meta(client)
    assert hit.headers["x-cache"] == "HIT"
    # A HIT is byte-identical to what the MISS computed, not a truncated or
    # double-compressed body: httpx decodes Content-Encoding for us.
    assert hit.json() == _meta(client).json()
    assert hit.json()["name"] == "Trip"


def test_a_write_invalidates_the_cached_payload(env):
    client, *_ = env
    assert _meta(client).headers["x-cache"] == "MISS"
    assert _meta(client).headers["x-cache"] == "HIT"

    resp = client.patch("/api/projects/Trip/day-meta",
                        json={"days": {"2026-06-01": {"sleeping": "Hotel"}}})
    assert resp.status_code == 200, resp.text

    fresh = _meta(client)
    assert fresh.headers["x-cache"] == "MISS", "a day-meta write left a stale payload cached"
    assert fresh.json()["day_meta"]["2026-06-01"]["sleeping"] == "Hotel"


def test_a_content_write_invalidates_it_too(env):
    """Journal/memory/people routes used to bust nothing at all — they had no
    cache to invalidate. Now they do, and the panel must show the new entry."""
    client, owner_id, *_ = env
    assert _meta(client).headers["x-cache"] == "MISS"
    assert _meta(client).headers["x-cache"] == "HIT"

    resp = client.post("/api/journal/", json={
        "project_name": "Trip",
        "date": "2026-06-02",
        "geo_mode": "custom",
        "description": "a new page",
    })
    assert resp.status_code == 201, resp.text

    fresh = _meta(client)
    assert fresh.headers["x-cache"] == "MISS"
    assert "a new page" in _descriptions(fresh)


def test_lock_version_is_exposed_and_bumps_on_content_write(env):
    """The client cache (flutter_client's ProjectDataCache) uses lock_version
    to decide whether a previously downloaded geo/elevation payload is still
    valid, so it must be present and must actually change when project
    content changes."""
    client, *_ = env
    before = _meta(client).json()
    assert isinstance(before["lock_version"], int)

    resp = client.post("/api/journal/", json={
        "project_name": "Trip",
        "date": "2026-06-02",
        "geo_mode": "custom",
        "description": "a new page",
    })
    assert resp.status_code == 201, resp.text

    after = _meta(client).json()
    assert after["lock_version"] != before["lock_version"], \
        "a content write must bump lock_version so cached clients invalidate"


def test_each_caller_gets_their_own_journal_and_role(env):
    """The cache key includes the caller: journals are per-user (#106) and
    caller_role is baked into the body (#109)."""
    client, owner_id, mate_id, as_user, _engine = env

    as_user(owner_id)
    alice = _meta(client)
    assert alice.headers["x-cache"] == "MISS"
    assert _descriptions(alice) == ["alice's private entry"]
    assert alice.json()["caller_role"] == "owner"

    as_user(mate_id)
    bob = _meta(client, owner=owner_id)
    assert bob.headers["x-cache"] == "MISS", "Bob was served Alice's cached payload"
    assert _descriptions(bob) == ["bob's private entry"]
    assert bob.json()["caller_role"] == "editor"

    # Each caller's entry is cached independently, and still their own.
    assert _meta(client, owner=owner_id).headers["x-cache"] == "HIT"
    as_user(owner_id)
    assert _descriptions(_meta(client)) == ["alice's private entry"]


def test_losing_membership_invalidates_the_cached_role(env):
    client, owner_id, mate_id, as_user, _engine = env

    as_user(mate_id)
    assert _meta(client, owner=owner_id).json()["caller_role"] == "editor"
    assert _meta(client, owner=owner_id).headers["x-cache"] == "HIT"

    as_user(owner_id)
    assert client.delete(f"/api/projects/Trip/members/{mate_id}").status_code == 204

    as_user(mate_id)
    # Access is gone entirely now — the point is that the cached body cannot be
    # what answers, since it was computed while Bob still had a role.
    assert client.get(f"/api/projects/Trip/meta?owner={owner_id}").status_code in (403, 404)


def test_warming_makes_the_owners_next_load_a_hit(env):
    """warm_meta_cache runs after a mutation's background work, so reopening a
    just-edited trip is a HIT instead of the cold load that timed out."""
    client, owner_id, *_ = env
    warm_meta_cache(owner_id, "Trip")
    assert _meta(client).headers["x-cache"] == "HIT"


def test_a_read_racing_a_mutation_does_not_refill_the_cache(env, monkeypatch):
    """Same generation guard as geo (#132): a payload built from a pre-mutation
    read must not be persisted after the mutation evicted the entry."""
    client, owner_id, _mate_id, _as_user, engine = env
    orig_gzip = shared_mod.gzip_json

    def _mutate_then_gzip(payload):
        monkeypatch.setattr(shared_mod, "gzip_json", orig_gzip)  # interleave once
        with Session(engine) as sess:
            row = sess.get(DBProject, 1)
            row.day_meta_json = json.dumps({"2026-06-01": {"sleeping": "Tent"}})
            sess.add(row)
            sess.commit()
        from api.geo import bust_geo_cache

        bust_geo_cache(owner_id, "Trip")
        return orig_gzip(payload)

    monkeypatch.setattr(shared_mod, "gzip_json", _mutate_then_gzip)
    # api.projects imported the symbol directly, so patch it there too.
    import api.projects as projects_mod

    monkeypatch.setattr(projects_mod, "gzip_json", lambda p: shared_mod.gzip_json(p))

    assert _meta(client).headers["x-cache"] == "MISS"
    assert [k for k in _geo_cache if k[0] == owner_id] == [], (
        "a payload superseded before it was stored got cached anyway")
    assert _meta(client).headers["x-cache"] == "MISS"

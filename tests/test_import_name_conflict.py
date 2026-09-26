"""Importing a trip under a name the user already has (issues #452, #463).

The import used to answer 201 and do nothing when the name was taken: the user
believed the trip had been restored and kept looking at the old one. Now:

* without ``on_conflict`` the import is refused with 409, naming the trip, and
  nothing changes;
* ``on_conflict=copy`` ("Keep both") imports under the first free
  ``"<name> (n)"``, a new trip that counts against the plan's trip limit.

A copy of a trip that holds memories used to fail with 500 (#463): memories
kept their exported ``public_id``, which is unique across all trips. An
exported ``public_id`` is now kept only while it is free.

Project names are unique per owner in the database, so two imports racing for
the same free name cannot both win; the loser takes the next one.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity, DBMemory, DBProject, DBProjectItem
from models.user import UserInfo
from src.project.project_io import ProjectIO


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app, one signed-in user, a file-backed DB (threads share it)."""
    import api.router as router

    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(project_shared_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id

    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app, raise_server_exceptions=False), engine, uid
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _trip(*, memories=("Lake", "Summit"), activity_id=1, name="Ride") -> bytes:
    items = [{"item_type": "activity", "activity_id": activity_id}]
    items += [
        {"item_type": "memory",
         "memory": {"name": m, "date": "2024-06-01", "public_id": f"pub-{m.lower()}"}}
        for m in memories
    ]
    return json.dumps({
        "version": 1, "name": "ignored", "trip_start": None,
        "filter_state": {"start_date": None, "end_date": None, "activity_types": None},
        "items": items,
        "activities": [{"id": activity_id, "name": name, "type": "Ride",
                        "start_date": "2024-06-01T08:00:00Z"}],
    }).encode("utf-8")


def _import(client, name: str, content: bytes, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    return client.post(
        "/api/projects/import", params=params,
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")},
    )


def _names(engine, uid) -> set[str]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBProject.name).where(DBProject.user_info_id == uid)))


def _row(engine, uid, name) -> DBProject:
    with Session(engine) as sess:
        return sess.exec(select(DBProject).where(
            DBProject.user_info_id == uid, DBProject.name == name)).one()


def _memories(engine, uid, name) -> dict[str, str]:
    """name -> public_id of the trip's memories."""
    project_id = _row(engine, uid, name).id
    with Session(engine) as sess:
        rows = sess.exec(select(DBMemory).where(DBMemory.project_id == project_id)).all()
    return {m.name: m.public_id for m in rows}


def _item_count(engine, uid, name) -> int:
    project_id = _row(engine, uid, name).id
    with Session(engine) as sess:
        return len(sess.exec(select(DBProjectItem).where(
            DBProjectItem.project_id == project_id)).all())


def _seed(client, name: str, content: bytes | None = None) -> None:
    r = _import(client, name, content or _trip())
    assert r.status_code == 201, r.text


# ── Refused without a choice ─────────────────────────────────────────────────

def test_a_taken_name_is_refused_with_409_naming_the_trip(env):
    client, engine, uid = env
    _seed(client, "Alps")
    before = _row(engine, uid, "Alps")

    r = _import(client, "Alps", _trip(memories=("Other",), activity_id=2, name="New"))

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "name_conflict"
    assert body["name"] == "Alps"
    # The name travels in its own field: a file name may hold a double
    # quote, which would cut the client's reading of the detail short.
    assert body["detail"] and '"' not in body["detail"]
    # Nothing changed: same trip, same content, and the file's activity was
    # not ingested either.
    assert _names(engine, uid) == {"Alps"}
    after = _row(engine, uid, "Alps")
    assert (after.id, after.lock_version) == (before.id, before.lock_version)
    assert set(_memories(engine, uid, "Alps")) == {"Lake", "Summit"}
    with Session(engine) as sess:
        assert sess.get(DBActivity, 2) is None


def test_a_free_name_imports_as_before_and_says_so(env):
    client, engine, uid = env

    r = _import(client, "Alps", _trip())

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps", "outcome": "created"}


def test_an_unknown_choice_is_refused(env):
    client, engine, uid = env

    r = _import(client, "Alps", _trip(), on_conflict="merge")

    assert r.status_code == 422
    assert _names(engine, uid) == set()


def test_a_malformed_file_is_a_400_even_under_a_taken_name(env):
    """Asking the user to choose is pointless if the file can't be read."""
    client, engine, uid = env
    _seed(client, "Alps")

    r = _import(client, "Alps", b"{not json")

    assert r.status_code == 400, r.text


# ── Keep both ────────────────────────────────────────────────────────────────

def test_keep_both_imports_a_copy_under_the_next_free_name(env):
    client, engine, uid = env
    _seed(client, "Alps")
    original = _row(engine, uid, "Alps")

    r = _import(client, "Alps", _trip(memories=("Glacier",)), on_conflict="copy")

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps (2)", "outcome": "copied"}
    assert _names(engine, uid) == {"Alps", "Alps (2)"}
    # The original is untouched.
    kept = _row(engine, uid, "Alps")
    assert (kept.id, kept.lock_version) == (original.id, original.lock_version)
    assert set(_memories(engine, uid, "Alps")) == {"Lake", "Summit"}
    assert set(_memories(engine, uid, "Alps (2)")) == {"Glacier"}


def test_keep_both_on_a_free_name_just_imports_it(env):
    client, engine, uid = env

    r = _import(client, "Alps", _trip(), on_conflict="copy")

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps", "outcome": "created"}


@pytest.mark.parametrize("existing, imported, expected", [
    ({"X", "X (2)"}, "X", "X (3)"),
    ({"X", "X (3)"}, "X", "X (2)"),
    # A name that already carries a counter counts on from it.
    ({"X", "X (2)"}, "X (2)", "X (3)"),
    # Only a trailing " (n)" is a counter.
    ({"X(2)"}, "X(2)", "X(2) (2)"),
    ({"Day (one)"}, "Day (one)", "Day (one) (2)"),
])
def test_keep_both_picks_the_lowest_free_counter(env, existing, imported, expected):
    client, engine, uid = env
    for name in existing:
        _seed(client, name)

    r = _import(client, imported, _trip(), on_conflict="copy")

    assert r.status_code == 201, r.text
    assert r.json()["name"] == expected
    assert _names(engine, uid) == existing | {expected}


def test_another_users_trip_does_not_take_the_name(env):
    client, engine, uid = env
    with Session(engine) as sess:
        other = UserInfo(display_name="Other", email="other@e.com")
        sess.add(other)
        sess.commit()
        sess.add(DBProject(user_info_id=other.id, name="Alps"))
        sess.commit()

    r = _import(client, "Alps", _trip())

    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Alps"


def test_keep_both_counts_against_the_trip_limit(env, monkeypatch):
    client, engine, uid = env
    _seed(client, "Alps")
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_PROJECTS", "1")

    r = _import(client, "Alps", _trip(), on_conflict="copy")

    assert r.status_code == 402, r.text
    assert _names(engine, uid) == {"Alps"}


def test_a_conflict_at_the_trip_limit_is_still_a_409(env, monkeypatch):
    """The user must get to choose; replacing a trip needs no new slot."""
    client, engine, uid = env
    _seed(client, "Alps")
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_PROJECTS", "1")

    r = _import(client, "Alps", _trip())

    assert r.status_code == 409, r.text


# ── Memory public ids (#463) ────────────────────────────────────────────────

def test_an_exported_trip_with_memories_imports_as_a_copy(env):
    """The #463 scenario: export a trip, import the file while it still exists."""
    client, engine, uid = env
    _seed(client, "Alps")
    exported = client.get("/api/projects/Alps/export-traxj")
    assert exported.status_code == 200, exported.text
    original = _memories(engine, uid, "Alps")
    items_before = _item_count(engine, uid, "Alps")

    r = _import(client, "Alps", exported.content, on_conflict="copy")

    assert r.status_code == 201, r.text
    copy = _memories(engine, uid, "Alps (2)")
    # Both trips intact, each with its own memories.
    assert _memories(engine, uid, "Alps") == original
    assert _item_count(engine, uid, "Alps") == items_before
    assert set(copy) == set(original)
    assert not set(copy.values()) & set(original.values())


def test_the_same_file_under_a_new_name_imports_too(env):
    client, engine, uid = env
    _seed(client, "Alps")

    r = _import(client, "Alps backup", _trip())

    assert r.status_code == 201, r.text
    assert set(_memories(engine, uid, "Alps backup")) == {"Lake", "Summit"}
    assert not (set(_memories(engine, uid, "Alps backup").values())
                & set(_memories(engine, uid, "Alps").values()))


def test_a_free_public_id_is_kept(env):
    """Share deep links address a memory by public_id (#15): re-importing a
    trip that is gone must bring its links back."""
    client, engine, uid = env

    r = _import(client, "Alps", _trip())

    assert r.status_code == 201, r.text
    assert _memories(engine, uid, "Alps") == {"Lake": "pub-lake", "Summit": "pub-summit"}


def test_a_public_id_repeated_inside_one_file_is_minted_afresh(env):
    client, engine, uid = env
    doc = json.loads(_trip())
    for item in doc["items"]:
        if item["item_type"] == "memory":
            item["memory"]["public_id"] = "same"

    r = _import(client, "Alps", json.dumps(doc).encode())

    assert r.status_code == 201, r.text
    ids = list(_memories(engine, uid, "Alps").values())
    assert len(ids) == 2 and len(set(ids)) == 2 and "same" in ids


# ── Races ────────────────────────────────────────────────────────────────────

def _steal_name_before_insert(monkeypatch, engine, uid, stolen: str):
    """Once, right after the import has looked at the taken names, commit a
    trip called *stolen* from another connection: a concurrent import winning
    the race between the read and the insert."""
    repo_cls = type(project_shared_mod._repo)
    real = repo_cls._taken_names
    fired = {"done": False}

    def _patched(self, sess, user_info_id):
        taken = real(self, sess, user_info_id)
        if not fired["done"]:
            fired["done"] = True
            with Session(engine) as other:
                other.add(DBProject(user_info_id=uid, name=stolen))
                other.commit()
        return taken

    monkeypatch.setattr(repo_cls, "_taken_names", _patched)


def test_keep_both_takes_the_next_name_when_a_concurrent_import_wins(env, monkeypatch):
    client, engine, uid = env
    _seed(client, "Alps")
    _steal_name_before_insert(monkeypatch, engine, uid, "Alps (2)")

    r = _import(client, "Alps", _trip(), on_conflict="copy")

    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Alps (3)"
    assert _names(engine, uid) == {"Alps", "Alps (2)", "Alps (3)"}


def test_a_plain_import_that_loses_the_race_is_a_409_not_a_500(env, monkeypatch):
    client, engine, uid = env
    _steal_name_before_insert(monkeypatch, engine, uid, "Alps")

    r = _import(client, "Alps", _trip())

    assert r.status_code == 409, r.text
    assert r.json()["name"] == "Alps"
    # Only the competitor's (empty) trip exists; nothing of the file landed.
    assert _names(engine, uid) == {"Alps"}
    assert _memories(engine, uid, "Alps") == {}


def test_concurrent_copies_each_get_their_own_name(env):
    client, engine, uid = env
    _seed(client, "Alps")
    results: list = []

    def _worker():
        results.append(_import(client, "Alps", _trip(), on_conflict="copy"))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [r.status_code for r in results] == [201] * 4, [r.text for r in results]
    names = {r.json()["name"] for r in results}
    assert names == {"Alps (2)", "Alps (3)", "Alps (4)", "Alps (5)"}
    assert _names(engine, uid) == {"Alps"} | names


def test_the_database_refuses_a_duplicate_trip_name(env):
    """The backstop every check-then-insert above relies on."""
    from sqlalchemy.exc import IntegrityError

    _client, engine, uid = env
    with Session(engine) as sess:
        sess.add(DBProject(user_info_id=uid, name="Alps"))
        sess.commit()
        sess.add(DBProject(user_info_id=uid, name="Alps"))
        with pytest.raises(IntegrityError):
            sess.commit()

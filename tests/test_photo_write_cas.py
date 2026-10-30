"""Photo-list writes are compare-and-set (D13 of
docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md).

``photo_lock`` serialises the writers of one process only. A writer in
another process (the photo-upgrade worker) can commit between a writer's read
of ``photos_json``/``photo_order_json`` and its write. Every write is
therefore an ``UPDATE`` matching both columns as read
(``api/photo_write.py``), re-read and recomputed on a conflict.

Each race here is staged with a second session that writes the row between
the read and the write — what another process would do, holding no
``photo_lock`` — on a file-backed SQLite:

  * a delete racing a placement: both photos end correct;
  * a replace racing a delete of the same photo: the delete wins, the new
    files go, the route answers 404;
  * five straight conflicts answer 409 and write nothing;
  * a compare-and-set leaves the row's other columns as they were;
  * ``_drop_unplaced`` racing a replace of another photo of the row keeps the
    replacement and drops only the unplaced name;
  * a re-import's clear racing a placement clears that photo too, and writes
    the refreshed fields with it.
"""
from __future__ import annotations

import json
import logging
import uuid as uuid_lib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import api.memories as mem_mod
import api.photo_write as photo_write
import api.project_transfer as transfer_mod
import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.memories import router as memories_router
from api.photo_order import dump_state, load_state, place
from api.photo_order import remove as remove_from_order, replace as replace_in_order
from models.billing import UserUsage
from models.project_db import DBJournalEntry, DBMemory, DBProject
from models.user import UserInfo

_BASELINE_USAGE = 1_000_000
_HASH = "a" * 64


@pytest.fixture
def env(monkeypatch, tmp_path):
    # File-backed: the racing writer is a second session on its own
    # connection, which an in-memory StaticPool engine cannot give.
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'cas.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        project = DBProject(user_info_id=user.id, name="My Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        memory = DBMemory(project_id=project.id, date="2025-06-01", geo_mode="custom",
                          description="before")
        journal = DBJournalEntry(project_id=project.id, date="2025-06-01", geo_mode="custom")
        sess.add(memory)
        sess.add(journal)
        sess.add(UserUsage(user_info_id=user.id, storage_bytes=_BASELINE_USAGE))
        sess.commit()
        ids = {"user": user.id, "memory": memory.id, "journal": journal.id}

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: _user(ids["user"])
    app.include_router(memories_router)
    app.include_router(journal_router)
    yield TestClient(app), engine, ids
    engine.dispose()


# ── Helpers ─────────────────────────────────────────────────────────────────

_MODELS = {"memory": DBMemory, "journal": DBJournalEntry}
_FOLDERS = {"memory": "memories", "journal": "journal"}


def _user(user_id) -> dict:
    return {"sub": str(user_id), "email": "alice@example.com"}


def _jpeg_bytes(shade: int = 10) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (shade, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _row(engine, kind, row_id):
    with Session(engine) as sess:
        return sess.get(_MODELS[kind], row_id)


def _photos(engine, kind, row_id):
    row = _row(engine, kind, row_id)
    return None if row is None else json.loads(row.photos_json)


def _files(user_id, kind, row_id) -> list:
    folder = Path(mem_mod._DATA_DIR) / "users" / str(user_id) / _FOLDERS[kind] / str(row_id)
    return sorted(p.name for p in folder.glob("*")) if folder.exists() else []


def _usage(engine, user_id) -> int:
    with Session(engine) as sess:
        return sess.exec(select(UserUsage).where(UserUsage.user_info_id == user_id)).one().storage_bytes


def _save(kind, user_id, row_id, uuid_str) -> None:
    mod = mem_mod if kind == "memory" else journal_mod
    mod._save_photo_files(str(user_id), row_id, uuid_str, _jpeg_bytes())


def _land(kind, user_id, row_id, order=None) -> str:
    """Write a photo's files and place it, as an upload does."""
    uuid_str = str(uuid_lib.uuid4())
    _save(kind, user_id, row_id, uuid_str)
    if kind == "memory":
        mem_mod._write_memory_photo(row_id, uuid_str, order, owner_dir=str(user_id))
    else:
        journal_mod._write_journal_photo(row_id, uuid_str, order, owner_dir=str(user_id))
    return uuid_str


def _other_process_writes(engine, kind, row_id, change) -> None:
    """Change the row's photo columns from a second session, holding no
    photo_lock: what a writer in another process does."""
    with Session(engine) as sess:
        row = sess.get(_MODELS[kind], row_id)
        photos, state = change(json.loads(row.photos_json or "[]"), load_state(row.photo_order_json))
        row.photos_json = json.dumps(photos)
        row.photo_order_json = dump_state(state)
        sess.add(row)
        sess.commit()


def _between_read_and_write(monkeypatch, action, times=1) -> list:
    """Run *action* just before each of the next *times* compare-and-sets,
    after the row was read. Returns the list of calls made."""
    real = photo_write.cas_photo_columns
    calls = []

    def racing(*args, **kwargs):
        calls.append(1)
        if len(calls) <= times:
            action()
        return real(*args, **kwargs)

    monkeypatch.setattr(photo_write, "cas_photo_columns", racing)
    return calls


# ── A delete racing a placement ─────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["memory", "journal"])
def test_a_delete_racing_a_placement_keeps_the_placed_photo(env, monkeypatch, kind):
    client, engine, ids = env
    user_id, row_id = ids["user"], ids[kind]
    gone = _land(kind, user_id, row_id, order=0)
    placed = str(uuid_lib.uuid4())
    _save(kind, user_id, row_id, placed)

    _between_read_and_write(monkeypatch, lambda: _other_process_writes(
        engine, kind, row_id, lambda p, s: place(p, s, placed, 1)))
    resp = client.delete(f"/api/{'memories' if kind == 'memory' else 'journal'}/{row_id}/photos/{gone}")

    assert resp.status_code == 204
    assert _photos(engine, kind, row_id) == [placed]
    assert load_state(_row(engine, kind, row_id).photo_order_json)["ranks"] == {placed: 1}
    assert _files(user_id, kind, row_id) == sorted([f"{placed}.jpg", f"{placed}_thumb.jpg"])


# ── A replace racing a delete of the same photo ─────────────────────────────

@pytest.mark.parametrize("kind", ["memory", "journal"])
def test_a_replace_racing_a_delete_of_its_photo_answers_404_and_leaves_no_files(
        env, monkeypatch, kind):
    client, engine, ids = env
    user_id, row_id = ids["user"], ids[kind]
    old = _land(kind, user_id, row_id, order=0)
    mod = mem_mod if kind == "memory" else journal_mod

    def delete_old():
        _other_process_writes(engine, kind, row_id, lambda p, s: remove_from_order(p, s, old))
        mod._delete_photo_files(str(user_id), row_id, [old])

    _between_read_and_write(monkeypatch, delete_old)
    resp = client.put(f"/api/{'memories' if kind == 'memory' else 'journal'}/{row_id}/photos/{old}/replace",
                      files={"file": ("n.jpg", _jpeg_bytes(shade=99), "image/jpeg")})

    assert resp.status_code == 404
    assert _photos(engine, kind, row_id) == []
    assert _files(user_id, kind, row_id) == []
    assert _usage(engine, user_id) == _BASELINE_USAGE


# ── Five straight conflicts ─────────────────────────────────────────────────

def _keep_changing(engine, kind, row_id):
    """Each call lists one more photo, so every compare-and-set misses."""
    counter = iter(range(1000))
    return lambda: _other_process_writes(
        engine, kind, row_id, lambda p, s: (p + [f"other-{next(counter)}"], s))


@pytest.mark.parametrize("kind", ["memory", "journal"])
def test_five_straight_conflicts_on_a_delete_answer_409_and_delete_nothing(env, monkeypatch, kind):
    client, engine, ids = env
    user_id, row_id = ids["user"], ids[kind]
    photo = _land(kind, user_id, row_id)

    calls = _between_read_and_write(monkeypatch, _keep_changing(engine, kind, row_id), times=5)
    resp = client.delete(f"/api/{'memories' if kind == 'memory' else 'journal'}/{row_id}/photos/{photo}")

    assert resp.status_code == 409
    assert len(calls) == 5
    assert _photos(engine, kind, row_id)[0] == photo
    assert _files(user_id, kind, row_id) == sorted([f"{photo}.jpg", f"{photo}_thumb.jpg"])


@pytest.mark.parametrize("kind", ["memory", "journal"])
def test_five_straight_conflicts_on_a_replace_answer_409_and_remove_the_new_files(
        env, monkeypatch, kind):
    client, engine, ids = env
    user_id, row_id = ids["user"], ids[kind]
    old = _land(kind, user_id, row_id)
    usage_before = _usage(engine, user_id)

    _between_read_and_write(monkeypatch, _keep_changing(engine, kind, row_id), times=5)
    resp = client.put(f"/api/{'memories' if kind == 'memory' else 'journal'}/{row_id}/photos/{old}/replace",
                      files={"file": ("n.jpg", _jpeg_bytes(shade=99), "image/jpeg")})

    assert resp.status_code == 409
    assert _photos(engine, kind, row_id)[0] == old
    assert _files(user_id, kind, row_id) == sorted([f"{old}.jpg", f"{old}_thumb.jpg"])
    assert _usage(engine, user_id) == usage_before


@pytest.mark.parametrize("kind", ["memory", "journal"])
def test_five_straight_conflicts_on_an_upload_answer_409_and_remove_its_files(env, monkeypatch, kind):
    client, engine, ids = env
    user_id, row_id = ids["user"], ids[kind]

    _between_read_and_write(monkeypatch, _keep_changing(engine, kind, row_id), times=5)
    resp = client.post(f"/api/{'memories' if kind == 'memory' else 'journal'}/{row_id}/photos",
                       files={"file": ("n.jpg", _jpeg_bytes(), "image/jpeg")})

    assert resp.status_code == 409
    assert all(p.startswith("other-") for p in _photos(engine, kind, row_id))
    assert _files(user_id, kind, row_id) == []
    assert _usage(engine, user_id) == _BASELINE_USAGE


def test_a_background_download_that_keeps_conflicting_logs_and_removes_its_files(
        env, monkeypatch, caplog):
    client, engine, ids = env
    user_id, row_id = ids["user"], ids["memory"]
    monkeypatch.setattr(mem_mod, "fetch_bytes", lambda url, **kwargs: _jpeg_bytes())
    _between_read_and_write(monkeypatch, _keep_changing(engine, "memory", row_id), times=5)

    with caplog.at_level(logging.ERROR, logger=mem_mod.__name__):
        resp = client.post(f"/api/memories/{row_id}/photos/from-url", json={"url": "http://x/0.jpg"})

    assert resp.status_code == 202
    assert any(str(row_id) in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    assert _files(user_id, "memory", row_id) == []
    assert _usage(engine, user_id) == _BASELINE_USAGE


def test_mutate_photos_retries_up_to_the_attempts_then_raises(env, monkeypatch):
    _, engine, ids = env
    calls = _between_read_and_write(monkeypatch, _keep_changing(engine, "memory", ids["memory"]), times=99)

    with pytest.raises(photo_write.PhotoWriteConflict):
        photo_write.mutate_photos("memory", ids["memory"], lambda p, s, row: (p + ["mine"], s))

    assert len(calls) == 5
    assert "mine" not in _photos(engine, "memory", ids["memory"])


def test_mutate_photos_succeeds_once_the_race_is_over(env, monkeypatch):
    _, engine, ids = env
    _between_read_and_write(monkeypatch, _keep_changing(engine, "memory", ids["memory"]), times=4)

    written = photo_write.mutate_photos("memory", ids["memory"], lambda p, s, row: (p + ["mine"], s))

    assert written[0] == ["other-0", "other-1", "other-2", "other-3", "mine"]
    assert _photos(engine, "memory", ids["memory"]) == written[0]


# ── The compare-and-set itself ──────────────────────────────────────────────

def test_a_cas_update_leaves_the_rows_other_columns_as_they_were(env):
    _, engine, ids = env
    with Session(engine) as sess:
        row = sess.get(DBMemory, ids["memory"])
        read_photos, read_order = row.photos_json, row.photo_order_json
        assert read_order is None  # matched with IS NULL

        # Another writer changes a column the photo write does not own.
        with Session(engine) as other:
            other_row = other.get(DBMemory, ids["memory"])
            other_row.description = "edited meanwhile"
            other.add(other_row)
            other.commit()

        state = {"epoch": 0, "ranks": {"a": 0}, "hashes": {"a": _HASH}}
        assert photo_write.cas_photo_columns(
            sess, DBMemory, ids["memory"], read_photos_json=read_photos,
            read_order_json=read_order, photos=["a"], state=state)
        sess.commit()

    row = _row(engine, "memory", ids["memory"])
    assert row.description == "edited meanwhile"
    assert row.date == "2025-06-01"
    assert json.loads(row.photos_json) == ["a"]
    assert load_state(row.photo_order_json) == state


def test_a_cas_update_on_a_changed_list_writes_nothing(env):
    _, engine, ids = env
    with Session(engine) as sess:
        row = sess.get(DBMemory, ids["memory"])
        read_photos, read_order = row.photos_json, row.photo_order_json
        _other_process_writes(engine, "memory", ids["memory"], lambda p, s: (["theirs"], s))

        assert not photo_write.cas_photo_columns(
            sess, DBMemory, ids["memory"], read_photos_json=read_photos,
            read_order_json=read_order, photos=["mine"], state=load_state(None))
        sess.commit()

    assert _photos(engine, "memory", ids["memory"]) == ["theirs"]


# ── _drop_unplaced racing a replace of another photo ────────────────────────

@pytest.mark.parametrize("kind,transfer_kind", [("memory", "memories"), ("journal", "journal")])
def test_drop_unplaced_racing_a_replace_keeps_the_replacement(env, monkeypatch, kind, transfer_kind):
    _, engine, ids = env
    row_id = ids[kind]
    _other_process_writes(engine, kind, row_id, lambda p, s: (
        ["keep", "drop"],
        {"epoch": 0, "ranks": {"keep": 0, "drop": 1}, "hashes": {"keep": _HASH, "drop": "b" * 64}}))

    _between_read_and_write(monkeypatch, lambda: _other_process_writes(
        engine, kind, row_id, lambda p, s: replace_in_order(p, s, "keep", "upgraded")))
    transfer_mod._drop_unplaced(transfer_kind, row_id, "drop")

    assert _photos(engine, kind, row_id) == ["upgraded"]
    assert load_state(_row(engine, kind, row_id).photo_order_json) == {
        "epoch": 0, "ranks": {"upgraded": 0}, "hashes": {"upgraded": _HASH}}


def test_drop_unplaced_that_keeps_conflicting_logs_and_leaves_the_name(env, monkeypatch, caplog):
    _, engine, ids = env
    row_id = ids["memory"]
    _other_process_writes(engine, "memory", row_id, lambda p, s: (["keep", "drop"], s))

    _between_read_and_write(monkeypatch, _keep_changing(engine, "memory", row_id), times=5)
    with caplog.at_level(logging.ERROR, logger=transfer_mod.__name__):
        transfer_mod._drop_unplaced("memories", row_id, "drop")

    assert "drop" in _photos(engine, "memory", row_id)
    (rec,) = [r for r in caplog.records
              if r.levelno == logging.ERROR and r.name == transfer_mod.__name__]
    assert "drop" in rec.getMessage() and str(row_id) in rec.getMessage()


# ── A re-import's clear racing a placement ──────────────────────────────────

def _reimport(user_id) -> int:
    """A Polarsteps re-import of the fixture memory's step (same name+date):
    adopts the memory and clears its photos."""
    body = mem_mod.MemoryBody(project_name="My Trip", date="2025-06-01", geo_mode="custom",
                              description="from the step", polarsteps_step_id=42)
    return mem_mod.create_memory(body, _user(user_id))["id"]


def test_a_reimport_clear_racing_a_placement_clears_it_too(env, monkeypatch):
    _, engine, ids = env
    user_id, row_id = ids["user"], ids["memory"]
    first = _land("memory", user_id, row_id, order=0)
    racer = str(uuid_lib.uuid4())
    _save("memory", user_id, row_id, racer)

    _between_read_and_write(monkeypatch, lambda: _other_process_writes(
        engine, "memory", row_id, lambda p, s: place(p, s, racer, 1)))
    assert _reimport(user_id) == row_id

    row = _row(engine, "memory", row_id)
    assert json.loads(row.photos_json) == []
    assert load_state(row.photo_order_json)["epoch"] == 1
    assert row.description == "from the step" and row.polarsteps_step_id == 42
    assert _files(user_id, "memory", row_id) == []
    assert _usage(engine, user_id) == _BASELINE_USAGE
    assert first != racer


def test_a_reimport_clear_that_keeps_conflicting_answers_409_and_writes_nothing(env, monkeypatch):
    client, engine, ids = env
    user_id, row_id = ids["user"], ids["memory"]
    photo = _land("memory", user_id, row_id, order=0)

    _between_read_and_write(monkeypatch, _keep_changing(engine, "memory", row_id), times=5)
    resp = client.post("/api/memories/", json={
        "project_name": "My Trip", "date": "2025-06-01", "geo_mode": "custom",
        "description": "from the step", "polarsteps_step_id": 42})

    assert resp.status_code == 409
    row = _row(engine, "memory", row_id)
    assert json.loads(row.photos_json)[0] == photo
    assert load_state(row.photo_order_json)["epoch"] == 0
    assert row.description == "before" and row.polarsteps_step_id is None
    assert _files(user_id, "memory", row_id) == sorted([f"{photo}.jpg", f"{photo}_thumb.jpg"])

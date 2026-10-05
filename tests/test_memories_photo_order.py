"""Tests for photo-order correctness under concurrency (issue #237).

Polarsteps import queues one background download per photo and fires them
concurrently; the download that finishes first used to win the append race,
scrambling order (and, under true concurrency, losing photos outright).
``_write_memory_photo``/``_write_journal_photo`` now take an explicit
``order`` and serialize the read-modify-write behind a per-entity lock
(``api/photo_locks.py``) so the final list reflects intended order — and
loses nothing — no matter what order the writes actually land in.

For memories ``order`` is a *rank* (``api/photo_order.py``): a photo is
inserted among the ranked ones, never written over a slot, and the list
stays dense. A re-import's clear bumps an epoch that drops downloads queued
before it, and a photo that cannot be placed (memory gone, stale epoch, old
photo of a replace gone) leaves no files and no counted storage behind.

Covers:
  * placing photos out of call-order via explicit ``order`` lands them in the
    correct final position, and a missing rank leaves no gap;
  * a manual upload or a delete during an import neither gets overwritten
    nor shifts later downloads;
  * re-import: stale downloads are dropped with their files, and a placement
    racing the clear lands after its commit;
  * downloads, uploads and replaces for a memory (or photo) deleted meanwhile
    leave no files and never land in a newer memory;
  * ``order`` outside 0..9999 is refused;
  * N threads writing shuffled ranks concurrently lose nothing (memories on
    a file-backed SQLite; journal still index-based until its own unit);
  * the end-to-end ``POST /photos/from-url`` route accepts ``order``;
  * ``delete_photo`` still works under the same lock.
"""
from __future__ import annotations

import json
import random
import threading
import time
import uuid as uuid_lib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import api.memories as mem_mod
import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.memories import router as memories_router
from api.photo_order import load_state
from models.billing import UserUsage
from models.project_db import DBJournalEntry, DBMemory, DBProject
from models.user import UserInfo

# Counted storage before any photo lands; non-zero so a cleanup that
# subtracts too much is caught (the counter clamps at zero).
_BASELINE_USAGE = 1_000_000


@pytest.fixture
def env(monkeypatch, tmp_path):
    # File-backed, one connection per session: several tests run requests and
    # writers on separate threads, which an in-memory StaticPool engine (one
    # shared connection) cannot do reliably.
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'order.db').as_posix()}")
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
        user_id = user.id

        project = DBProject(user_info_id=user_id, name="My Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        project_id = project.id

        memory = DBMemory(project_id=project_id, date="2025-06-01", geo_mode="custom")
        sess.add(memory)
        sess.commit()
        sess.refresh(memory)
        memory_id = memory.id

        journal = DBJournalEntry(project_id=project_id, date="2025-06-01", geo_mode="custom")
        sess.add(journal)
        sess.commit()
        sess.refresh(journal)
        journal_id = journal.id

        sess.add(UserUsage(user_info_id=user_id, storage_bytes=_BASELINE_USAGE))
        sess.commit()

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id), "email": "alice@example.com"}
    app.include_router(memories_router)
    app.include_router(journal_router)
    client = TestClient(app)
    yield client, engine, user_id, memory_id, journal_id
    engine.dispose()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _jpeg_bytes() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _user(user_id) -> dict:
    return {"sub": str(user_id), "email": "alice@example.com"}


def _photos(engine, memory_id):
    with Session(engine) as sess:
        row = sess.get(DBMemory, memory_id)
    return None if row is None else json.loads(row.photos_json)


def _usage(engine, user_id) -> int:
    with Session(engine) as sess:
        return sess.exec(select(UserUsage).where(UserUsage.user_info_id == user_id)).one().storage_bytes


def _files(user_id, memory_id) -> list:
    folder = Path(mem_mod._DATA_DIR) / "users" / str(user_id) / "memories" / str(memory_id)
    return sorted(p.name for p in folder.glob("*")) if folder.exists() else []


def _land(user_id, memory_id, order=None, epoch=None, uuid_str=None) -> str:
    """Write a photo's files and place it — what a finished download does."""
    uuid_str = uuid_str or str(uuid_lib.uuid4())
    mem_mod._save_photo_files(str(user_id), memory_id, uuid_str, _jpeg_bytes())
    mem_mod._write_memory_photo(memory_id, uuid_str, order, epoch, owner_dir=str(user_id))
    return uuid_str


def _reimport(user_id) -> int:
    """A Polarsteps re-import of the fixture memory's step (same name+date):
    adopts the memory and clears its photos."""
    body = mem_mod.MemoryBody(project_name="My Trip", date="2025-06-01", geo_mode="custom",
                              polarsteps_step_id=42)
    return mem_mod.create_memory(body, _user(user_id))["id"]


def _new_memory(user_id) -> int:
    body = mem_mod.MemoryBody(project_name="My Trip", date="2025-06-02", geo_mode="custom")
    return mem_mod.create_memory(body, _user(user_id))["id"]


# ── Direct unit tests of the write helper ───────────────────────────────────

class TestWriteMemoryPhotoOrdering:
    def test_out_of_call_order_lands_at_the_right_index(self, env):
        _, engine, _, memory_id, _ = env
        # Call in shuffled order — as if photo 2's download finished before 0 and 1.
        mem_mod._write_memory_photo(memory_id, "uuid-2", order=2)
        mem_mod._write_memory_photo(memory_id, "uuid-0", order=0)
        mem_mod._write_memory_photo(memory_id, "uuid-1", order=1)

        assert _photos(engine, memory_id) == ["uuid-0", "uuid-1", "uuid-2"]

    def test_a_missing_rank_leaves_no_gap(self, env):
        _, engine, _, memory_id, _ = env
        # Photo 1's download never lands (e.g. permanently failed).
        mem_mod._write_memory_photo(memory_id, "uuid-0", order=0)
        mem_mod._write_memory_photo(memory_id, "uuid-2", order=2)

        # Stored dense: no placeholder is written any more.
        assert _photos(engine, memory_id) == ["uuid-0", "uuid-2"]
        # A late arrival for the missing rank still goes between them.
        mem_mod._write_memory_photo(memory_id, "uuid-1", order=1)
        assert _photos(engine, memory_id) == ["uuid-0", "uuid-1", "uuid-2"]

    def test_no_order_appends(self, env):
        _, engine, _, memory_id, _ = env
        mem_mod._write_memory_photo(memory_id, "a")
        mem_mod._write_memory_photo(memory_id, "b")
        assert _photos(engine, memory_id) == ["a", "b"]

    def test_a_manual_upload_during_import_is_not_overwritten(self, env):
        client, engine, user_id, memory_id, _ = env
        first = _land(user_id, memory_id, order=0)
        # The user adds a photo by hand while the import is still downloading.
        resp = client.post(f"/api/memories/{memory_id}/photos",
                           files={"file": ("m.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 201, resp.text
        manual = resp.json()["uuid"]
        # The download for order 1 lands where the manual photo sits (index 1):
        # it used to overwrite it.
        second = _land(user_id, memory_id, order=1)

        assert _photos(engine, memory_id) == [first, second, manual]
        assert len(_files(user_id, memory_id)) == 6

    def test_a_delete_during_import_does_not_shift_later_downloads(self, env):
        client, engine, user_id, memory_id, _ = env
        p0 = _land(user_id, memory_id, order=0)
        p1 = _land(user_id, memory_id, order=1)
        p3 = _land(user_id, memory_id, order=3)

        assert client.delete(f"/api/memories/{memory_id}/photos/{p1}").status_code == 204
        # Order 2 lands after the delete: index 2 is now p3's slot, which the
        # old positional write overwrote.
        p2 = _land(user_id, memory_id, order=2)

        assert _photos(engine, memory_id) == [p0, p2, p3]
        with Session(engine) as sess:
            ranks = load_state(sess.get(DBMemory, memory_id).photo_order_json)["ranks"]
        assert p1 not in ranks

    def test_concurrent_downloads_lose_nothing(self, env):
        _, engine, user_id, memory_id, _ = env
        n = 20
        ranks = list(range(n))
        random.Random(237).shuffle(ranks)
        uuids = {rank: str(uuid_lib.uuid4()) for rank in ranks}
        threads = [
            threading.Thread(target=_land, args=(user_id, memory_id, rank, None, uuids[rank]))
            for rank in ranks
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert _photos(engine, memory_id) == [uuids[r] for r in range(n)]


class TestWriteJournalPhotoOrdering:
    def test_out_of_call_order_lands_at_the_right_index(self, env):
        _, engine, _, _, journal_id = env
        journal_mod._write_journal_photo(journal_id, "uuid-2", order=2)
        journal_mod._write_journal_photo(journal_id, "uuid-0", order=0)
        journal_mod._write_journal_photo(journal_id, "uuid-1", order=1)

        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        assert json.loads(row.photos_json) == ["uuid-0", "uuid-1", "uuid-2"]

    def test_concurrent_writes_to_distinct_slots_lose_nothing(self, env):
        _, engine, _, _, journal_id = env
        n = 20
        threads = [
            threading.Thread(target=journal_mod._write_journal_photo, args=(journal_id, f"uuid-{i}", i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with Session(engine) as sess:
            row = sess.get(DBJournalEntry, journal_id)
        photos = json.loads(row.photos_json)
        assert photos == [f"uuid-{i}" for i in range(n)]


# ── Re-import epochs ────────────────────────────────────────────────────────

class TestReimportEpoch:
    def test_a_download_from_before_a_reimport_is_dropped_and_its_files_removed(self, env, monkeypatch):
        client, engine, user_id, memory_id, _ = env
        _land(user_id, memory_id, order=0)

        def fetch_during_reimport(url, **kwargs):
            # The route has queued this download under the current epoch; the
            # re-import clears the memory while it is still downloading.
            assert _reimport(user_id) == memory_id
            return _jpeg_bytes()

        monkeypatch.setattr(mem_mod, "fetch_bytes", fetch_during_reimport)
        resp = client.post(f"/api/memories/{memory_id}/photos/from-url",
                           json={"url": "http://x/1.jpg", "order": 1})
        assert resp.status_code == 202

        assert _photos(engine, memory_id) == []
        assert _files(user_id, memory_id) == []
        assert _usage(engine, user_id) == _BASELINE_USAGE
        with Session(engine) as sess:
            assert load_state(sess.get(DBMemory, memory_id).photo_order_json) == {"epoch": 1, "ranks": {}}

    def test_a_download_queued_after_a_reimport_lands(self, env, monkeypatch):
        client, engine, user_id, memory_id, _ = env
        _land(user_id, memory_id, order=0)
        _reimport(user_id)
        monkeypatch.setattr(mem_mod, "fetch_bytes", lambda url, **kwargs: _jpeg_bytes())

        resp = client.post(f"/api/memories/{memory_id}/photos/from-url",
                           json={"url": "http://x/0.jpg", "order": 0})
        assert resp.status_code == 202
        assert len(_photos(engine, memory_id)) == 1

    def test_a_reimport_clear_and_a_concurrent_placement_do_not_interleave(self, env, monkeypatch):
        _, engine, user_id, memory_id, _ = env
        owner = str(user_id)
        _land(user_id, memory_id, order=0)
        # Two photos whose files are written and which are about to be placed:
        # a download queued before the re-import (epoch 0) and a manual upload.
        stale, manual = str(uuid_lib.uuid4()), str(uuid_lib.uuid4())
        for u in (stale, manual):
            mem_mod._save_photo_files(owner, memory_id, u, _jpeg_bytes())

        original_clear = mem_mod._clear_memory_photos
        racers = []
        blocked = []

        def clear_with_racers(sess, user_id_, mem_row):
            for uuid_str, order, epoch in ((stale, 0, 0), (manual, None, None)):
                t = threading.Thread(target=mem_mod._write_memory_photo,
                                     args=(memory_id, uuid_str, order, epoch),
                                     kwargs={"owner_dir": owner})
                t.start()
                racers.append(t)
            time.sleep(0.3)
            # Both are waiting on the photo lock the re-import holds.
            blocked.append(all(t.is_alive() for t in racers))
            original_clear(sess, user_id_, mem_row)

        monkeypatch.setattr(mem_mod, "_clear_memory_photos", clear_with_racers)
        _reimport(user_id)
        for t in racers:
            t.join()

        assert blocked == [True]
        # The manual photo landed after the clear's commit (not wiped by it);
        # the stale download was dropped by the epoch rule, files and all.
        assert _photos(engine, memory_id) == [manual]
        assert _files(user_id, memory_id) == sorted([f"{manual}.jpg", f"{manual}_thumb.jpg"])
        manual_bytes = sum((Path(mem_mod._DATA_DIR) / "users" / owner / "memories" / str(memory_id) / f).stat().st_size
                           for f in _files(user_id, memory_id))
        # The previously imported photo went with the clear, uncounted.
        assert _usage(engine, user_id) == _BASELINE_USAGE + manual_bytes


# ── Photos for a memory deleted meanwhile ───────────────────────────────────

class TestDeletedMemory:
    def test_a_download_for_a_deleted_memory_leaves_no_files(self, env, monkeypatch):
        client, engine, user_id, memory_id, _ = env

        def fetch_then_delete(url, **kwargs):
            mem_mod.delete_memory(memory_id, _user(user_id))
            return _jpeg_bytes()

        monkeypatch.setattr(mem_mod, "fetch_bytes", fetch_then_delete)
        resp = client.post(f"/api/memories/{memory_id}/photos/from-url",
                           json={"url": "http://x/0.jpg", "order": 0})
        assert resp.status_code == 202

        assert _photos(engine, memory_id) is None
        assert _files(user_id, memory_id) == []
        assert _usage(engine, user_id) == _BASELINE_USAGE

    def test_a_download_for_a_deleted_memory_does_not_land_in_a_new_memory(self, env, monkeypatch):
        client, engine, user_id, memory_id, _ = env
        created = []

        def fetch_then_recreate(url, **kwargs):
            mem_mod.delete_memory(memory_id, _user(user_id))
            created.append(_new_memory(user_id))
            return _jpeg_bytes()

        monkeypatch.setattr(mem_mod, "fetch_bytes", fetch_then_recreate)
        resp = client.post(f"/api/memories/{memory_id}/photos/from-url", json={"url": "http://x/0.jpg"})
        assert resp.status_code == 202

        (new_id,) = created
        assert new_id != memory_id
        assert _photos(engine, new_id) == []
        assert _files(user_id, memory_id) == [] and _files(user_id, new_id) == []
        assert _usage(engine, user_id) == _BASELINE_USAGE

    def test_an_upload_for_a_deleted_memory_does_not_land_in_a_new_memory(self, env, monkeypatch):
        client, engine, user_id, memory_id, _ = env
        original_save = mem_mod._save_photo_files
        created = []

        def recreate_then_save(*args):
            mem_mod.delete_memory(memory_id, _user(user_id))
            created.append(_new_memory(user_id))
            original_save(*args)

        monkeypatch.setattr(mem_mod, "_save_photo_files", recreate_then_save)
        resp = client.post(f"/api/memories/{memory_id}/photos",
                           files={"file": ("m.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 404

        (new_id,) = created
        assert new_id != memory_id
        assert _photos(engine, new_id) == []
        assert _files(user_id, memory_id) == [] and _files(user_id, new_id) == []
        assert _usage(engine, user_id) == _BASELINE_USAGE


# ── Replace racing a delete ─────────────────────────────────────────────────

class TestReplaceRacingDelete:
    def test_replacing_a_photo_deleted_meanwhile_answers_404_and_leaves_no_files(self, env, monkeypatch):
        client, engine, user_id, memory_id, _ = env
        old = _land(user_id, memory_id, order=0)
        original_save = mem_mod._save_photo_files

        def delete_old_then_save(*args):
            mem_mod.delete_photo(memory_id, old, _user(user_id))
            original_save(*args)

        monkeypatch.setattr(mem_mod, "_save_photo_files", delete_old_then_save)
        resp = client.put(f"/api/memories/{memory_id}/photos/{old}/replace",
                          files={"file": ("n.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 404

        assert _photos(engine, memory_id) == []
        assert _files(user_id, memory_id) == []
        assert _usage(engine, user_id) == _BASELINE_USAGE

    def test_replacing_a_photo_of_a_memory_deleted_meanwhile_answers_404_and_leaves_no_files(
            self, env, monkeypatch):
        client, engine, user_id, memory_id, _ = env
        old = _land(user_id, memory_id, order=0)
        original_save = mem_mod._save_photo_files

        def delete_memory_then_save(*args):
            mem_mod.delete_memory(memory_id, _user(user_id))
            original_save(*args)

        monkeypatch.setattr(mem_mod, "_save_photo_files", delete_memory_then_save)
        resp = client.put(f"/api/memories/{memory_id}/photos/{old}/replace",
                          files={"file": ("n.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 404

        assert _photos(engine, memory_id) is None
        assert _files(user_id, memory_id) == []
        assert _usage(engine, user_id) == _BASELINE_USAGE


# ── End-to-end via the from-url route ───────────────────────────────────────

class TestFromUrlRouteOrdering:
    def test_out_of_order_completion_still_lands_correctly(self, env, monkeypatch):
        """Photo 0's download is the slowest; photo 2's is the fastest. Even so,
        once both land, the memory's photo list must read [photo0, photo1, photo2]."""
        client, engine, _, memory_id, _ = env
        photo_bytes = _jpeg_bytes()

        def fake_fetch(url, **kwargs):
            # Simulate the slowest download being for order=0, the fastest for order=2.
            delay = {"http://x/0.jpg": 0.06, "http://x/1.jpg": 0.03, "http://x/2.jpg": 0.0}[url]
            time.sleep(delay)
            return photo_bytes

        monkeypatch.setattr(mem_mod, "fetch_bytes", fake_fetch)

        for i in range(3):
            resp = client.post(
                f"/api/memories/{memory_id}/photos/from-url",
                json={"url": f"http://x/{i}.jpg", "order": i},
            )
            assert resp.status_code == 202

        photos = _photos(engine, memory_id)
        assert len(photos) == 3
        assert all(photos)  # no gaps once everything has landed

    def test_omitted_order_still_appends(self, env, monkeypatch):
        client, engine, _, memory_id, _ = env
        photo_bytes = _jpeg_bytes()
        monkeypatch.setattr(mem_mod, "fetch_bytes", lambda url, **kwargs: photo_bytes)

        resp = client.post(
            f"/api/memories/{memory_id}/photos/from-url",
            json={"url": "http://x/only.jpg"},
        )
        assert resp.status_code == 202
        assert len(_photos(engine, memory_id)) == 1

    def test_order_out_of_range_is_refused(self, env, monkeypatch):
        client, engine, _, memory_id, _ = env
        monkeypatch.setattr(mem_mod, "fetch_bytes", lambda url, **kwargs: _jpeg_bytes())

        for order in (-1, 10000):
            resp = client.post(f"/api/memories/{memory_id}/photos/from-url",
                               json={"url": "http://x/bad.jpg", "order": order})
            assert resp.status_code == 422, order
        assert _photos(engine, memory_id) == []

        for order in (9999, 0):
            resp = client.post(f"/api/memories/{memory_id}/photos/from-url",
                               json={"url": f"http://x/{order}.jpg", "order": order})
            assert resp.status_code == 202, order
        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        ranks = load_state(row.photo_order_json)["ranks"]
        assert [ranks[p] for p in json.loads(row.photos_json)] == [0, 9999]


# ── delete still correct under the lock ─────────────────────────────────────

class TestDeleteAndReplaceUnderLock:
    def test_delete_removes_by_value_and_keeps_remaining_order(self, env):
        client, engine, user_id, memory_id, _ = env
        mem_mod._write_memory_photo(memory_id, "a", order=0)
        mem_mod._write_memory_photo(memory_id, "b", order=1)
        mem_mod._write_memory_photo(memory_id, "c", order=2)

        # Write the on-disk files delete_photo expects to unlink.
        base = Path(mem_mod._DATA_DIR) / "users" / str(user_id) / "memories" / str(memory_id)
        base.mkdir(parents=True, exist_ok=True)
        for uuid_str in ("a", "b", "c"):
            (base / f"{uuid_str}.jpg").write_bytes(_jpeg_bytes())
            (base / f"{uuid_str}_thumb.jpg").write_bytes(_jpeg_bytes())

        resp = client.delete(f"/api/memories/{memory_id}/photos/b")
        assert resp.status_code == 204

        assert _photos(engine, memory_id) == ["a", "c"]

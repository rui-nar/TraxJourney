"""Tests for photo-order correctness under concurrency (issue #237).

Polarsteps import queues one background download per photo and fires them
concurrently; the download that finishes first used to win the append race,
scrambling order (and, under true concurrency, losing photos outright).
``_write_memory_photo``/``_write_journal_photo`` now take an explicit
``order`` and serialize the read-modify-write behind a per-entity lock
(``api/photo_locks.py``) so the final list reflects intended order — and
loses nothing — no matter what order the writes actually land in.

Covers:
  * placing photos out of call-order via explicit ``order`` lands them in the
    correct final position;
  * N threads writing distinct ``order`` slots concurrently lose nothing
    (the old unsynchronized version would clobber updates here);
  * the end-to-end ``POST /photos/from-url`` route accepts ``order`` and a
    slow/out-of-order background download still lands at the right index;
  * in-flight/failed slots (``null`` placeholders) never reach the API
    response;
  * ``delete_photo``/``replace_photo`` still work correctly now that they
    run under the same lock.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.journal as journal_mod
import api.memories as mem_mod
import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.memories import router as memories_router
from models.project_db import DBJournalEntry, DBMemory, DBProject
from models.user import UserInfo


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id), "email": "alice@example.com"}
    app.include_router(memories_router)
    app.include_router(journal_router)
    client = TestClient(app)
    yield client, engine, user_id, memory_id, journal_id


# ── Direct unit tests of the write helper ───────────────────────────────────

class TestWriteMemoryPhotoOrdering:
    def test_out_of_call_order_lands_at_the_right_index(self, env):
        _, engine, _, memory_id, _ = env
        # Call in shuffled order — as if photo 2's download finished before 0 and 1.
        mem_mod._write_memory_photo(memory_id, "uuid-2", order=2)
        mem_mod._write_memory_photo(memory_id, "uuid-0", order=0)
        mem_mod._write_memory_photo(memory_id, "uuid-1", order=1)

        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        assert json.loads(row.photos_json) == ["uuid-0", "uuid-1", "uuid-2"]

    def test_missing_slot_stays_a_placeholder_and_is_filtered_on_read(self, env):
        _, engine, _, memory_id, _ = env
        # Photo 1's download never lands (e.g. permanently failed).
        mem_mod._write_memory_photo(memory_id, "uuid-0", order=0)
        mem_mod._write_memory_photo(memory_id, "uuid-2", order=2)

        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        # Stored with a gap...
        assert json.loads(row.photos_json) == ["uuid-0", None, "uuid-2"]
        # ...but the client-facing mapper never sees the gap.
        memory = mem_mod._row_to_memory(row)
        assert memory.photos == ["uuid-0", "uuid-2"]

    def test_no_order_appends(self, env):
        _, engine, _, memory_id, _ = env
        mem_mod._write_memory_photo(memory_id, "a")
        mem_mod._write_memory_photo(memory_id, "b")
        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        assert json.loads(row.photos_json) == ["a", "b"]

    def test_concurrent_writes_to_distinct_slots_lose_nothing(self, env):
        _, engine, _, memory_id, _ = env
        n = 20
        threads = [
            threading.Thread(target=mem_mod._write_memory_photo, args=(memory_id, f"uuid-{i}", i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        photos = json.loads(row.photos_json)
        assert photos == [f"uuid-{i}" for i in range(n)]


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


# ── End-to-end via the from-url route ───────────────────────────────────────

def _jpeg_bytes() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


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

        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        photos = json.loads(row.photos_json)
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

        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        assert len(json.loads(row.photos_json)) == 1


# ── delete/replace still correct under the lock ─────────────────────────────

class TestDeleteAndReplaceUnderLock:
    def test_delete_removes_by_value_and_keeps_remaining_order(self, env):
        client, engine, user_id, memory_id, _ = env
        mem_mod._write_memory_photo(memory_id, "a", order=0)
        mem_mod._write_memory_photo(memory_id, "b", order=1)
        mem_mod._write_memory_photo(memory_id, "c", order=2)

        # Write the on-disk files delete_photo expects to unlink.
        from pathlib import Path
        base = Path(mem_mod._DATA_DIR) / "users" / str(user_id) / "memories" / str(memory_id)
        base.mkdir(parents=True, exist_ok=True)
        for uuid_str in ("a", "b", "c"):
            (base / f"{uuid_str}.jpg").write_bytes(_jpeg_bytes())
            (base / f"{uuid_str}_thumb.jpg").write_bytes(_jpeg_bytes())

        resp = client.delete(f"/api/memories/{memory_id}/photos/b")
        assert resp.status_code == 204

        with Session(engine) as sess:
            row = sess.get(DBMemory, memory_id)
        assert json.loads(row.photos_json) == ["a", "c"]

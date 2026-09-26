"""Regression tests for Polarsteps import deduplication.

The bug: the read path (`/trips/{id}/steps`) recognised already-imported steps by
`polarsteps_step_id` OR `(name, date)`, but the write path (`POST /api/memories/`)
deduped only by `polarsteps_step_id`. So re-importing a memory created before the
step-id column existed produced a duplicate, and the matching was whitespace- and
empty-name-fragile. These tests pin the unified behaviour:

  * shared `step_key` normalization (trailing space, empty-vs-NULL name);
  * write path adopts a pre-step-id memory by name+date (no duplicate);
  * adopt = full refresh (scalars overwritten, step id backfilled, photos cleared);
  * exact step-id re-import is idempotent (no photo wipe);
  * the partial unique index forbids duplicate step ids per project;
  * the repair script merges existing duplicate pairs.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.memories import router as memories_router
from models.project_db import DBMemory, DBProject, DBProjectItem
from models.user import UserInfo
from src.project.memory_match import normalize_name, step_key


# ── Pure unit: normalization ──────────────────────────────────────────────────

class TestStepKey:
    def test_trailing_space_is_trimmed(self):
        assert normalize_name("Beuron ") == "Beuron"
        assert step_key("Beuron ", "2026-03-04") == step_key("Beuron", "2026-03-04")

    def test_empty_string_equals_none(self):
        assert normalize_name("") is None
        assert normalize_name("   ") is None
        assert step_key("", "2026-03-04") == step_key(None, "2026-03-04")

    def test_case_is_preserved(self):
        # Folding case could merge genuinely distinct places — keep it.
        assert step_key("Kalmar", "d") != step_key("kalmar", "d")

    def test_distinct_names_differ(self):
        assert step_key("Beuron", "d") != step_key("Tuttlingen", "d")


# ── Integration harness ───────────────────────────────────────────────────────

@pytest.fixture
def env(monkeypatch, tmp_path):
    """In-memory DB + memories TestClient for one user/project.

    Yields (client, user_id, project_id, engine, data_dir).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)

    import api.memories as mem_mod
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))

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

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id), "email": "alice@example.com"}
    app.include_router(memories_router)
    client = TestClient(app)
    yield client, user_id, project_id, engine, tmp_path


def _insert_memory(engine, project_id, **kw) -> int:
    """Insert a memory row + its project item directly, return memory id."""
    with Session(engine) as sess:
        row = DBMemory(project_id=project_id, **kw)
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        n = len(sess.exec(select(DBProjectItem).where(DBProjectItem.project_id == project_id)).all())
        sess.add(DBProjectItem(project_id=project_id, position=n, item_type="memory", memory_id=row.id))
        sess.commit()
        return row.id


def _counts(engine, project_id):
    with Session(engine) as sess:
        mems = len(sess.exec(select(DBMemory).where(DBMemory.project_id == project_id)).all())
        items = len(sess.exec(select(DBProjectItem).where(DBProjectItem.project_id == project_id)).all())
        return mems, items


# ── Write path ────────────────────────────────────────────────────────────────

class TestCreateMemoryDedup:
    def _body(self, **over):
        b = {"project_name": "My Trip", "date": "2026-03-04", "geo_mode": "custom",
             "name": "Beuron", "description": "hi", "lat": 48.0, "lon": 8.9,
             "polarsteps_step_id": 220373126}
        b.update(over)
        return b

    def test_new_step_inserts_one_memory_and_item(self, env):
        client, _, project_id, engine, _ = env
        r = client.post("/api/memories/", json=self._body())
        assert r.status_code == 201
        assert _counts(engine, project_id) == (1, 1)

    def test_exact_step_id_reimport_is_idempotent(self, env):
        client, _, project_id, engine, _ = env
        r1 = client.post("/api/memories/", json=self._body())
        mid = r1.json()["id"]
        r2 = client.post("/api/memories/", json=self._body(description="changed"))
        assert r2.json()["id"] == mid          # same row
        assert _counts(engine, project_id) == (1, 1)  # no duplicate

    def test_exact_step_id_reimport_keeps_photos(self, env):
        # A retry of an already-imported step must NOT wipe uploaded photos.
        client, user_id, project_id, engine, data = env
        mid = _insert_memory(engine, project_id, name="Beuron", date="2026-03-04",
                             polarsteps_step_id=220373126, photos_json=json.dumps(["abc"]))
        pdir = data / "users" / str(user_id) / "memories" / str(mid)
        pdir.mkdir(parents=True)
        (pdir / "abc.jpg").write_bytes(b"x")
        client.post("/api/memories/", json=self._body())
        with Session(engine) as sess:
            assert json.loads(sess.get(DBMemory, mid).photos_json) == ["abc"]
        assert (pdir / "abc.jpg").exists()

    def test_namedate_match_adopts_pre_step_id_memory(self, env):
        # The split-brain regression: a NULL-step memory with the same name+date
        # is adopted, not duplicated.
        client, _, project_id, engine, _ = env
        old = _insert_memory(engine, project_id, name="Beuron ", date="2026-03-04",
                             description="old", polarsteps_step_id=None)
        r = client.post("/api/memories/", json=self._body(name="Beuron"))
        assert r.json()["id"] == old           # adopted same row (trailing space normalized)
        assert _counts(engine, project_id) == (1, 1)  # no duplicate, no extra item
        with Session(engine) as sess:
            row = sess.get(DBMemory, old)
            assert row.polarsteps_step_id == 220373126   # backfilled
            assert row.description == "hi"               # refreshed from step

    def test_adopt_clears_existing_photos(self, env):
        client, user_id, project_id, engine, data = env
        old = _insert_memory(engine, project_id, name="Beuron", date="2026-03-04",
                             polarsteps_step_id=None, photos_json=json.dumps(["00000000-0000-4000-8000-000000000011", "00000000-0000-4000-8000-000000000012"]))
        pdir = data / "users" / str(user_id) / "memories" / str(old)
        pdir.mkdir(parents=True)
        for u in ("00000000-0000-4000-8000-000000000011", "00000000-0000-4000-8000-000000000012"):
            (pdir / f"{u}.jpg").write_bytes(b"x")
            (pdir / f"{u}_thumb.jpg").write_bytes(b"x")
        client.post("/api/memories/", json=self._body())
        with Session(engine) as sess:
            assert json.loads(sess.get(DBMemory, old).photos_json) == []
        assert not (pdir / "00000000-0000-4000-8000-000000000011.jpg").exists()
        assert not (pdir / "00000000-0000-4000-8000-000000000012_thumb.jpg").exists()

    def test_nameless_step_matches_null_name_memory(self, env):
        # format_step yields "" for a nameless step; client sends null → stored
        # NULL. The empty-vs-NULL normalization must still match on re-import.
        client, _, project_id, engine, _ = env
        old = _insert_memory(engine, project_id, name=None, date="2026-03-04", polarsteps_step_id=None)
        r = client.post("/api/memories/", json=self._body(name=None))
        assert r.json()["id"] == old
        assert _counts(engine, project_id) == (1, 1)


# ── Defense-in-depth: partial unique index ────────────────────────────────────

class TestUniqueStepIdIndex:
    def test_duplicate_step_id_in_project_rejected(self, env):
        _, _, project_id, engine, _ = env
        with Session(engine) as sess:
            sess.add(DBMemory(project_id=project_id, date="d", polarsteps_step_id=999))
            sess.commit()
            sess.add(DBMemory(project_id=project_id, date="d2", polarsteps_step_id=999))
            with pytest.raises(IntegrityError):
                sess.commit()

    def test_null_step_ids_are_exempt(self, env):
        _, _, project_id, engine, _ = env
        with Session(engine) as sess:
            sess.add(DBMemory(project_id=project_id, date="d", polarsteps_step_id=None))
            sess.add(DBMemory(project_id=project_id, date="d", polarsteps_step_id=None))
            sess.commit()  # must not raise


# ── TOCTOU race: two concurrent imports of the same step ──────────────────────

class TestConcurrentStepIdRace:
    """`_find_by_step_id` (the app-level dedup check) and the commit that
    follows it are not atomic. If a second request for the same step commits
    in between, the first request's own commit now loses to the unique index
    and must recover the same "already imported" response instead of a raw
    500 (issue: uncaught IntegrityError on create_memory's commit)."""

    def _body(self, **over):
        b = {"project_name": "My Trip", "date": "2026-03-04", "geo_mode": "custom",
             "name": "Beuron", "description": "hi", "lat": 48.0, "lon": 8.9,
             "polarsteps_step_id": 220373126}
        b.update(over)
        return b

    def test_loser_of_the_race_returns_the_winners_id_not_500(self, env, monkeypatch):
        client, _, project_id, engine, _ = env
        import api.memories as mem_mod

        # The row a *different* request already committed for this step,
        # landing after our own `_find_by_step_id` check ran but before our
        # commit — the exact TOCTOU window the bug lives in.
        winner_id = _insert_memory(engine, project_id, name="Beuron",
                                    date="2026-03-04", polarsteps_step_id=220373126)

        orig_find = mem_mod._find_by_step_id
        calls = {"n": 0}

        def flaky_find(sess, pid, step_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # the pre-commit check ran before the race landed
            return orig_find(sess, pid, step_id)

        monkeypatch.setattr(mem_mod, "_find_by_step_id", flaky_find)

        resp = client.post("/api/memories/", json=self._body())

        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == winner_id
        assert _counts(engine, project_id) == (1, 1)  # no duplicate row or item

    def test_unrecoverable_integrity_error_still_propagates(self, env, monkeypatch):
        """If recovery can't find a winning row after all, the failure must
        still surface — never silently swallowed."""
        client, _, project_id, engine, _ = env
        import api.memories as mem_mod

        # Both lookups miss, so the handler has no row to fall back to.
        monkeypatch.setattr(mem_mod, "_find_by_step_id", lambda sess, pid, step_id: None)
        # Force the commit to fail as if the unique index caught a race that,
        # in this contrived case, no query can see afterwards.
        real_commit = Session.commit
        state = {"n": 0}

        def flaky_commit(self):
            state["n"] += 1
            if state["n"] == 1:
                raise IntegrityError(
                    "INSERT INTO memory ...", {},
                    Exception("UNIQUE constraint failed: memory.project_id, memory.polarsteps_step_id"),
                )
            return real_commit(self)

        monkeypatch.setattr(Session, "commit", flaky_commit)

        no_raise_client = TestClient(client.app, raise_server_exceptions=False)
        resp = no_raise_client.post("/api/memories/", json=self._body())

        assert resp.status_code == 500
        assert _counts(engine, project_id) == (0, 0)


# ── Read path: /trips/{id}/steps already_imported ─────────────────────────────

class TestStepsAlreadyImported:
    def test_flags_via_id_and_normalized_name_date(self, monkeypatch, tmp_path):
        from fastapi import FastAPI
        import api.polarsteps as ps
        from api.polarsteps import router as ps_router
        from models.user import PolarstepsToken

        engine = create_engine("sqlite:///:memory:",
                               connect_args={"check_same_thread": False}, poolclass=StaticPool)
        monkeypatch.setattr(db_module, "engine", engine)
        SQLModel.metadata.create_all(engine)
        with Session(engine) as sess:
            u = UserInfo(display_name="A", email="a@e.com"); sess.add(u); sess.commit(); sess.refresh(u)
            uid = u.id
            p = DBProject(user_info_id=uid, name="My Trip"); sess.add(p); sess.commit(); sess.refresh(p)
            sess.add(PolarstepsToken(user_info_id=uid, remember_token="t", polarsteps_user_id=1))
            # Pre-step-id memory with a trailing-space name (matches step 111),
            # and a step-id memory (matches step 222 by id).
            sess.add(DBMemory(project_id=p.id, name="Beuron ", date="2026-03-04", polarsteps_step_id=None))
            sess.add(DBMemory(project_id=p.id, name="Kalmar", date="2026-03-05", polarsteps_step_id=222))
            sess.commit()

        raw = [
            {"id": 111, "name": "Beuron", "start_time": "2026-03-04T10:00:00", "location": {"lat": 1, "lon": 2}},
            {"id": 222, "name": "Kalmar", "start_time": "2026-03-05T10:00:00"},
            {"id": 333, "name": "NewPlace", "start_time": "2026-03-06T10:00:00"},
        ]

        class _FakeClient:
            def __init__(self, *a, **k): pass
            def get_trip_steps(self, trip_id): return raw

        monkeypatch.setattr(ps, "PolarstepsClient", _FakeClient)

        app = FastAPI()
        app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
        app.include_router(ps_router)
        client = TestClient(app)

        resp = client.get("/api/polarsteps/trips/9/steps", params={"project_name": "My Trip"})
        assert resp.status_code == 200
        flags = {s["id"]: s["already_imported"] for s in resp.json()}
        assert flags == {111: True, 222: True, 333: False}


# ── Repair script ─────────────────────────────────────────────────────────────

class TestRepairScript:
    def _seed_db(self, path: Path) -> None:
        engine = create_engine(f"sqlite:///{path}")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as sess:
            u = UserInfo(display_name="A", email="a@e.com"); sess.add(u); sess.commit(); sess.refresh(u)
            p = DBProject(user_info_id=u.id, name="T"); sess.add(p); sess.commit(); sess.refresh(p)
            # Duplicate pair: old NULL-step (richer: 2 photos) + new step-id (1 photo).
            old = DBMemory(project_id=p.id, name="Beuron ", date="2026-03-04",
                           description="d", photos_json=json.dumps(["a", "b"]), polarsteps_step_id=None)
            new = DBMemory(project_id=p.id, name="Beuron", date="2026-03-04",
                           description="d", photos_json=json.dumps(["c"]), polarsteps_step_id=220373126)
            sess.add(old); sess.add(new); sess.commit(); sess.refresh(old); sess.refresh(new)
            sess.add(DBProjectItem(project_id=p.id, position=0, item_type="memory", memory_id=old.id))
            sess.add(DBProjectItem(project_id=p.id, position=1, item_type="memory", memory_id=new.id))
            sess.commit()
        engine.dispose()

    def test_dry_run_changes_nothing(self, tmp_path, monkeypatch):
        import scripts.dedupe_polarsteps_memories as repair
        db = tmp_path / "r.db"
        self._seed_db(db)
        monkeypatch.setattr("sys.argv", ["x", "--db", str(db)])
        assert repair.main() == 0
        con = sqlite3.connect(str(db))
        assert con.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 2  # untouched
        con.close()

    def test_apply_merges_to_single_survivor(self, tmp_path, monkeypatch):
        import scripts.dedupe_polarsteps_memories as repair
        db = tmp_path / "r.db"
        self._seed_db(db)
        monkeypatch.setattr("sys.argv", ["x", "--db", str(db), "--apply"])
        assert repair.main() == 0
        con = sqlite3.connect(str(db)); con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, name, photos_json, polarsteps_step_id FROM memory").fetchall()
        assert len(rows) == 1
        survivor = rows[0]
        assert json.loads(survivor["photos_json"]) == ["a", "b"]   # richer copy kept
        assert survivor["polarsteps_step_id"] == 220373126          # step id backfilled
        items = con.execute("SELECT memory_id, position FROM projectitem ORDER BY position").fetchall()
        assert len(items) == 1 and items[0]["position"] == 0        # orphan item gone, compacted
        con.close()

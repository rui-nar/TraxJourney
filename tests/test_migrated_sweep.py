"""Startup sweep of leftover ``*.migrated`` project files (issue #434).

Every import before #434 left a ``<name>.traxj.migrated`` copy under
``data/users/<id>/projects/``, counted against the user's storage. The sweep
deletes those once on API startup and re-measures the affected users. It is
temporary: once every deployed instance has booted a release carrying it, it
and these tests go.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import src.admin.storage as storage_mod
from models.billing import UserUsage
from models.user import UserInfo
from src.project.migrated_sweep import sweep_migrated_files


@pytest.fixture
def data(monkeypatch, tmp_path):
    """Two users with leftovers, one without; stale usage counters."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        uids = []
        for i in range(3):
            u = UserInfo(display_name=f"U{i}", email=f"u{i}@e.com")
            sess.add(u)
            sess.commit()
            sess.refresh(u)
            uids.append(u.id)
        for uid in uids:
            # Stale: includes bytes the sweep is about to free.
            sess.add(UserUsage(user_info_id=uid, storage_bytes=999_999))
        sess.commit()

    users = tmp_path / "users"
    u1, u2, u3 = (users / str(uid) for uid in uids)

    def put(path: Path, size: int = 1000) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    leftovers = [
        put(u1 / "projects" / "Trip.traxj.migrated"),
        put(u1 / "projects" / "Old.viewtrip.migrated"),
        put(u2 / "projects" / "Other.traxj.migrated"),
    ]
    kept = [
        put(u1 / "projects" / "Unmigrated.viewtrip"),
        put(u1 / "projects" / "notes.txt"),
        # Only the projects directory is swept.
        put(u1 / "memories" / "5" / "photo.migrated"),
        put(u2 / "memories" / "7" / "a.jpg"),
        put(u3 / "memories" / "1" / "b.jpg"),
    ]
    return engine, uids, leftovers, kept


def _counter(engine, uid: int) -> int:
    with Session(engine) as sess:
        return sess.exec(
            select(UserUsage).where(UserUsage.user_info_id == uid)
        ).one().storage_bytes


def test_sweep_deletes_leftovers_and_nothing_else(data):
    _engine, _uids, leftovers, kept = data

    removed = sweep_migrated_files()

    assert removed == 3
    assert [p for p in leftovers if p.exists()] == []
    assert [p for p in kept if not p.exists()] == []


def test_sweep_is_idempotent(data):
    _engine, _uids, _leftovers, kept = data
    sweep_migrated_files()

    assert sweep_migrated_files() == 0
    assert all(p.exists() for p in kept)


def test_sweep_reconciles_the_users_it_freed_space_for(data):
    engine, (uid1, uid2, uid3), _leftovers, _kept = data

    sweep_migrated_files()

    for uid in (uid1, uid2):
        assert _counter(engine, uid) == storage_mod.dir_size(storage_mod._user_dir(str(uid)))
    # Nothing was freed for the third user, so their counter is left to the
    # nightly reconcile rather than walked here.
    assert _counter(engine, uid3) == 999_999


def test_sweep_of_a_deleted_accounts_directory_creates_no_usage_row(data, tmp_path):
    engine, _uids, _leftovers, _kept = data
    ghost = tmp_path / "users" / "4242" / "projects" / "Gone.traxj.migrated"
    ghost.parent.mkdir(parents=True)
    ghost.write_bytes(b"x")

    sweep_migrated_files()

    assert not ghost.exists()
    with Session(engine) as sess:
        assert sess.exec(
            select(UserUsage).where(UserUsage.user_info_id == 4242)
        ).first() is None


def test_sweep_busts_the_dashboard_storage_cache(data, monkeypatch):
    _engine, (uid1, _uid2, _uid3), _leftovers, _kept = data
    monkeypatch.setattr(storage_mod, "_cache", {str(uid1): (999_999, 1e18)})

    sweep_migrated_files()

    assert str(uid1) not in storage_mod._cache


def test_a_file_that_cannot_be_deleted_does_not_stop_the_sweep(data, monkeypatch):
    _engine, _uids, leftovers, _kept = data
    locked = leftovers[0]
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)

    removed = sweep_migrated_files()

    assert removed == 2
    assert locked.exists()
    assert [p for p in leftovers[1:] if p.exists()] == []


def test_sweep_with_no_users_directory_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path / "absent"))

    assert sweep_migrated_files() == 0


# ── Wiring: run once, by the API process only ─────────────────────────────────

class _FakeScheduler:
    def add_job(self, *a, **kw): pass
    def add_listener(self, *a, **kw): pass
    def start(self): pass
    def shutdown(self, *a, **kw): pass


def _run_lifespan(monkeypatch, *, is_api: bool, sweep=None) -> list[str]:
    import api.router as router

    calls: list[str] = []

    def record():
        calls.append("sweep")
        return 0

    monkeypatch.setenv("JWT_SECRET", "test-secret-" + "x" * 40)
    monkeypatch.setattr(router, "_IS_API_PROCESS", is_api)
    monkeypatch.setattr(router.alembic_command, "upgrade", lambda *a, **kw: None)
    monkeypatch.setattr(router, "_check_schema_contract", lambda: None)
    monkeypatch.setattr(router, "seed_admin", lambda: None)
    monkeypatch.setattr(router, "sweep_orphaned_jobs", lambda: None)
    monkeypatch.setattr(router, "sweep_orphaned_poster_jobs", lambda: None)
    monkeypatch.setattr(router, "_scheduler", _FakeScheduler())
    monkeypatch.setattr(router, "sweep_migrated_files", sweep or record)

    async def run():
        async with router.lifespan(router.app):
            pass

    asyncio.run(run())
    return calls


def test_api_process_sweeps_once_at_startup(monkeypatch):
    assert _run_lifespan(monkeypatch, is_api=True) == ["sweep"]


def test_worker_process_does_not_sweep(monkeypatch):
    assert _run_lifespan(monkeypatch, is_api=False) == []


def test_a_failing_sweep_does_not_stop_startup(monkeypatch):
    def boom():
        raise OSError("disk gone")

    _run_lifespan(monkeypatch, is_api=True, sweep=boom)  # must not raise

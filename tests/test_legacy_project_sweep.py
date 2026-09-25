"""Startup sweep of the obsolete ``data/users/<id>/projects/`` directories (issue #434).

Nothing has read that directory since #420 put every trip in the database, and
since #434 nothing writes to it. What is left there is dead weight counted
against the user's storage: ``*.migrated`` copies from every earlier import,
plain ``.traxj`` uploads a failed import left behind, and pre-rename files that
were never ingested. The sweep deletes every regular file in it once on API
startup, removes the emptied directory, and re-measures the affected users. It
is temporary: once every deployed instance has booted a release carrying it, it
and these tests go.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import src.admin.storage as storage_mod
from models.billing import UserUsage
from models.user import UserInfo
from src.project import legacy_project_sweep
from src.project.legacy_project_sweep import sweep_legacy_project_files


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
        # A failed import's raw upload, and a pre-rename file never ingested.
        put(u1 / "projects" / "Failed.traxj"),
        put(u1 / "projects" / "Unmigrated.viewtrip"),
        put(u1 / "projects" / "sub" / "stray.bin"),
        put(u2 / "projects" / "Other.traxj.migrated"),
    ]
    kept = [
        # Only the projects directory is swept.
        put(u1 / "memories" / "5" / "photo.migrated"),
        put(u1 / "memories" / "5" / "photo.jpg"),
        put(u2 / "memories" / "7" / "a.jpg"),
        put(u3 / "memories" / "1" / "b.jpg"),
    ]
    return engine, uids, leftovers, kept


def _counter(engine, uid: int) -> int:
    with Session(engine) as sess:
        return sess.exec(
            select(UserUsage).where(UserUsage.user_info_id == uid)
        ).one().storage_bytes


def test_sweep_deletes_every_file_under_projects_and_nothing_else(data):
    _engine, _uids, leftovers, kept = data

    removed = sweep_legacy_project_files()

    assert removed == len(leftovers)
    assert [p for p in leftovers if p.exists()] == []
    assert [p for p in kept if not p.exists()] == []


def test_sweep_removes_the_emptied_projects_directories(data, tmp_path):
    _engine, uids, _leftovers, _kept = data

    sweep_legacy_project_files()

    assert not any((tmp_path / "users" / str(uid) / "projects").exists() for uid in uids)
    # The user directories themselves stay.
    assert all((tmp_path / "users" / str(uid)).is_dir() for uid in uids)


def test_an_already_empty_projects_directory_is_removed(data, tmp_path):
    _engine, (_uid1, _uid2, uid3), _leftovers, _kept = data
    empty = tmp_path / "users" / str(uid3) / "projects"
    empty.mkdir()

    sweep_legacy_project_files()

    assert not empty.exists()


def test_sweep_is_idempotent(data):
    _engine, _uids, _leftovers, kept = data
    sweep_legacy_project_files()

    assert sweep_legacy_project_files() == 0
    assert all(p.exists() for p in kept)


def test_sweep_reconciles_the_users_it_freed_space_for(data):
    engine, (uid1, uid2, uid3), _leftovers, _kept = data

    sweep_legacy_project_files()

    for uid in (uid1, uid2):
        assert _counter(engine, uid) == storage_mod.dir_size(storage_mod._user_dir(str(uid)))
    # Nothing was freed for the third user, so their counter is left to the
    # nightly reconcile rather than walked here.
    assert _counter(engine, uid3) == 999_999


def test_sweep_of_a_deleted_accounts_directory_creates_no_usage_row(data, tmp_path):
    engine, _uids, _leftovers, _kept = data
    ghost = tmp_path / "users" / "4242" / "projects" / "Gone.traxj"
    ghost.parent.mkdir(parents=True)
    ghost.write_bytes(b"x")

    sweep_legacy_project_files()

    assert not ghost.exists()
    with Session(engine) as sess:
        assert sess.exec(
            select(UserUsage).where(UserUsage.user_info_id == 4242)
        ).first() is None


def test_sweep_busts_the_dashboard_storage_cache(data, monkeypatch):
    _engine, (uid1, _uid2, _uid3), _leftovers, _kept = data
    monkeypatch.setattr(storage_mod, "_cache", {str(uid1): (999_999, 1e18)})

    sweep_legacy_project_files()

    assert str(uid1) not in storage_mod._cache


def test_a_file_that_cannot_be_deleted_does_not_stop_the_sweep(data, monkeypatch):
    _engine, _uids, leftovers, _kept = data
    locked = leftovers[0]
    projects = locked.parent
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)

    removed = sweep_legacy_project_files()

    assert removed == len(leftovers) - 1
    assert locked.exists()
    assert [p for p in leftovers[1:] if p.exists()] == []
    # Not empty, so it stays; the next boot tries again.
    assert projects.is_dir()


def test_sweep_with_no_users_directory_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path / "absent"))

    assert sweep_legacy_project_files() == 0


def test_sweep_does_not_follow_symlinked_directories(data, tmp_path):
    """A symlinked ``users/<id>`` or ``<id>/projects`` is not the user's data:
    following it would delete files wherever the link points."""
    _engine, (_uid1, _uid2, uid3), _leftovers, _kept = data
    outside = tmp_path / "outside"
    (outside / "projects").mkdir(parents=True)
    (outside / "deep").mkdir()
    victims = [outside / "a.traxj", outside / "projects" / "b.traxj",
               outside / "deep" / "c.traxj"]
    for v in victims:
        v.write_bytes(b"x")
    users = tmp_path / "users"
    try:
        # <id>/projects -> elsewhere, users/<id> -> elsewhere, and a link
        # inside a real projects directory -> elsewhere.
        os.symlink(outside, users / str(uid3) / "projects", target_is_directory=True)
        os.symlink(outside, users / "9999", target_is_directory=True)
        os.symlink(outside / "deep", users / str(_uid2) / "projects" / "link",
                   target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create directory symlinks here: {exc}")

    # Only the fixture's real leftovers.
    assert sweep_legacy_project_files() == len(_leftovers)
    assert all(v.exists() for v in victims)


# ── Wiring: API process only, off the startup path ────────────────────────────

class _FakeScheduler:
    def add_job(self, *a, **kw): pass
    def add_listener(self, *a, **kw): pass
    def start(self): pass
    def shutdown(self, *a, **kw): pass


def _run_lifespan(monkeypatch, tmp_path, *, is_api: bool, sweep, during=None) -> None:
    """Run the real lifespan with every other startup step stubbed out and
    ``sweep`` standing in for the sweep itself. ``during`` runs while the app
    is up, between startup and shutdown."""
    import api.router as router

    monkeypatch.setenv("JWT_SECRET", "test-secret-" + "x" * 40)
    # Belt and braces: never let a wiring test near the real data directory.
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(router, "_IS_API_PROCESS", is_api)
    monkeypatch.setattr(router.alembic_command, "upgrade", lambda *a, **kw: None)
    monkeypatch.setattr(router, "_check_schema_contract", lambda: None)
    monkeypatch.setattr(router, "seed_admin", lambda: None)
    monkeypatch.setattr(router, "sweep_orphaned_jobs", lambda: None)
    monkeypatch.setattr(router, "sweep_orphaned_poster_jobs", lambda: None)
    monkeypatch.setattr(router, "_scheduler", _FakeScheduler())
    monkeypatch.setattr(legacy_project_sweep, "sweep_legacy_project_files", sweep)

    async def run():
        async with router.lifespan(router.app):
            if during:
                during()

    asyncio.run(run())


def _sweep_thread() -> threading.Thread | None:
    return next((t for t in threading.enumerate() if t.name == "legacy-project-sweep"), None)


def test_api_process_sweeps_once(monkeypatch, tmp_path):
    calls = []
    done = threading.Event()

    def sweep():
        calls.append(1)
        done.set()
        return 0

    _run_lifespan(monkeypatch, tmp_path, is_api=True, sweep=sweep)

    assert done.wait(5)
    assert calls == [1]


def test_worker_process_does_not_sweep(monkeypatch, tmp_path):
    calls = []
    _run_lifespan(monkeypatch, tmp_path, is_api=False, sweep=lambda: calls.append(1))

    time.sleep(0.2)  # give a wrongly started thread the chance to show up
    assert calls == []


def test_startup_and_shutdown_do_not_wait_for_the_sweep(monkeypatch, tmp_path):
    """The sweep reconciles each affected user's whole tree; on a large
    instance that must not hold up the API listening, or its shutdown."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    seen_while_up = {}

    def slow_sweep():
        started.set()
        release.wait(10)
        finished.set()
        return 0

    def during():
        seen_while_up["finished"] = finished.is_set()

    try:
        _run_lifespan(monkeypatch, tmp_path, is_api=True, sweep=slow_sweep, during=during)
        # Startup and shutdown have both returned; the sweep is still going.
        assert started.wait(5), "the sweep never ran"
        assert seen_while_up == {"finished": False}
        assert not finished.is_set()
        # A daemon thread, so the process can exit even mid-sweep; the sweep
        # is idempotent, so the next boot finishes the job.
        thread = _sweep_thread()
        assert thread is not None and thread.daemon
    finally:
        release.set()
    assert finished.wait(5)


def test_a_failing_sweep_is_logged_and_does_not_stop_startup(monkeypatch, tmp_path):
    logged = []
    monkeypatch.setattr(legacy_project_sweep._log, "exception",
                        lambda msg, *a, **kw: logged.append(msg % a if a else msg))

    def boom():
        raise OSError("disk gone")

    _run_lifespan(monkeypatch, tmp_path, is_api=True, sweep=boom)  # must not raise

    thread = _sweep_thread()
    if thread is not None:
        thread.join(5)
    assert len(logged) == 1 and "sweep" in logged[0]

"""SQLite under two writing processes (issue #173, phase B2).

Until the worker container existed, one process owned every write. A queued job
runs in a *different* OS process against the same SQLite file, so two engines
now write concurrently — the situation `busy_timeout` and WAL exist for, but
which nothing previously exercised.

The existing suites cover adjacent ground: `test_sqlite_pragmas` checks the
PRAGMAs are applied per connection, `test_track_save_db_lock` covers contention
*within* one engine. Neither creates a second engine, which is what a second
process actually is.

Uses file-backed SQLite deliberately — `:memory:` gives each engine its own
private database, so a two-writer test against it would pass while proving
nothing.
"""
from __future__ import annotations

import threading

import pytest
from sqlmodel import Session, SQLModel, select, text

from models.db import _configure_sqlite, _make_engine
from models.project_db import DBProject
from models.user import UserInfo


@pytest.fixture
def db_file(tmp_path):
    return tmp_path / "two_writers.db"


def _engine_for(db_file):
    """An engine configured exactly as the app configures its own."""
    engine = _make_engine(f"sqlite:///{db_file.as_posix()}")
    _configure_sqlite(engine)
    return engine


class TestTwoEnginesOneFile:
    def test_both_engines_get_wal_and_a_busy_timeout(self, db_file):
        """WAL is in the file header; busy_timeout is per connection.

        A second process picks up WAL for free but must set its own
        busy_timeout — without it, the moment it contends it raises
        "database is locked" instead of waiting.
        """
        api, worker = _engine_for(db_file), _engine_for(db_file)
        SQLModel.metadata.create_all(api)

        for engine in (api, worker):
            with Session(engine) as sess:
                assert sess.exec(text("PRAGMA journal_mode")).one()[0].lower() == "wal"
                assert int(sess.exec(text("PRAGMA busy_timeout")).one()[0]) == 30000

    def test_concurrent_writes_from_two_engines_all_land(self, db_file):
        """The headline case: neither writer errors, and no write is lost."""
        api, worker = _engine_for(db_file), _engine_for(db_file)
        SQLModel.metadata.create_all(api)
        with Session(api) as sess:
            user = UserInfo(display_name="A", email="a@e.com")
            sess.add(user); sess.commit(); sess.refresh(user)
            user_id = user.id

        errors: list[Exception] = []
        start = threading.Barrier(2)

        def _write(engine, prefix):
            try:
                start.wait(timeout=5)
                for i in range(20):
                    with Session(engine) as sess:
                        sess.add(DBProject(user_info_id=user_id, name=f"{prefix}-{i}"))
                        sess.commit()
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)

        threads = [
            threading.Thread(target=_write, args=(api, "api")),
            threading.Thread(target=_write, args=(worker, "worker")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"concurrent writers errored: {errors!r}"
        with Session(api) as sess:
            names = {p.name for p in sess.exec(select(DBProject)).all()}
        assert len(names) == 40, f"expected 40 projects, got {len(names)}"

    def test_a_write_is_visible_to_the_other_engine(self, db_file):
        """A worker's committed job result must be readable by the API process."""
        api, worker = _engine_for(db_file), _engine_for(db_file)
        SQLModel.metadata.create_all(api)
        with Session(api) as sess:
            user = UserInfo(display_name="A", email="a@e.com")
            sess.add(user); sess.commit(); sess.refresh(user)
            user_id = user.id

        with Session(worker) as sess:
            sess.add(DBProject(user_info_id=user_id, name="written-by-worker"))
            sess.commit()

        with Session(api) as sess:
            found = sess.exec(
                select(DBProject).where(DBProject.name == "written-by-worker")).first()
        assert found is not None

    def test_the_api_checkpoint_folds_in_the_workers_writes(self, db_file):
        """Only the API process runs the periodic WAL checkpoint (TRAXJOURNEY_ROLE).

        That is correct rather than a gap — there is one WAL for one file, so
        the API's checkpoint covers frames the worker appended too. Worth a test
        because the alternative reading ("the worker never checkpoints, so its
        writes accumulate forever") is a plausible and wrong conclusion.
        """
        api, worker = _engine_for(db_file), _engine_for(db_file)
        SQLModel.metadata.create_all(api)
        with Session(api) as sess:
            user = UserInfo(display_name="A", email="a@e.com")
            sess.add(user); sess.commit(); sess.refresh(user)
            user_id = user.id

        with Session(worker) as sess:
            for i in range(50):
                sess.add(DBProject(user_info_id=user_id, name=f"w-{i}"))
            sess.commit()

        conn = api.raw_connection()
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
            busy, log_frames, checkpointed = cur.fetchone()
            cur.close()
        finally:
            conn.close()

        assert busy == 0, "checkpoint was blocked"
        assert checkpointed > 0, "the API checkpoint folded in no frames"

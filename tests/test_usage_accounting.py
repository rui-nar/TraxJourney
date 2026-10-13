"""Per-user storage counters used by quota checks (issue #121).

The counter is maintained incrementally on every write and delete, so the tests
that matter are the ones about it *drifting*: a double delete must not push it
negative, and the nightly reconcile must bring it back to what is on disk.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import src.admin.storage as storage_mod
import src.billing.usage as usage_mod
from models.billing import UserUsage
from models.user import UserInfo
from src.billing.usage import (
    bytes_of,
    reconcile_all_usage,
    reconcile_usage,
    record_delta,
    record_written,
    unlink_and_record,
)


@pytest.fixture
def engine(monkeypatch, tmp_path):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as sess:
        sess.add(UserInfo(id=1, email="a@example.com", display_name="A"))
        sess.add(UserInfo(id=2, email="b@example.com", display_name="B"))
        sess.commit()
    yield test_engine


def _usage(engine, user_id: int) -> int:
    with Session(engine) as sess:
        row = sess.exec(
            select(UserUsage).where(UserUsage.user_info_id == user_id)
        ).first()
        return row.storage_bytes if row else 0


def _user_file(tmp_path, user_id: int, name: str, size: int):
    directory = tmp_path / "users" / str(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * size)
    return path


class TestRecordDelta:
    def test_first_delta_creates_the_row(self, engine):
        record_delta(1, 500)
        assert _usage(engine, 1) == 500

    def test_deltas_accumulate(self, engine):
        record_delta(1, 500)
        record_delta(1, 250)
        assert _usage(engine, 1) == 750

    def test_negative_delta_subtracts(self, engine):
        record_delta(1, 500)
        record_delta(1, -200)
        assert _usage(engine, 1) == 300

    def test_counter_never_goes_negative(self, engine):
        """A double-counted delete must not hand the account infinite headroom."""
        record_delta(1, 100)
        record_delta(1, -500)
        assert _usage(engine, 1) == 0

    def test_zero_delta_writes_nothing(self, engine):
        record_delta(1, 0)
        assert _usage(engine, 1) == 0

    def test_users_are_independent(self, engine):
        record_delta(1, 100)
        record_delta(2, 700)
        assert (_usage(engine, 1), _usage(engine, 2)) == (100, 700)

    def test_accounting_failure_never_propagates(self, engine, monkeypatch):
        """An upload that already hit disk must not 500 because of bookkeeping."""
        def _boom():
            raise RuntimeError("db down")
        monkeypatch.setattr("src.billing.usage.get_session", _boom)
        record_delta(1, 100)  # must not raise


class TestFileHelpers:
    def test_bytes_of_sums_existing_files(self, tmp_path):
        a = tmp_path / "a.jpg"; a.write_bytes(b"x" * 10)
        b = tmp_path / "b.jpg"; b.write_bytes(b"x" * 5)
        assert bytes_of(a, b) == 15

    def test_bytes_of_skips_missing_files(self, tmp_path):
        a = tmp_path / "a.jpg"; a.write_bytes(b"x" * 10)
        assert bytes_of(a, tmp_path / "gone.jpg") == 10

    def test_record_written_counts_what_landed_on_disk(self, engine, tmp_path):
        full = _user_file(tmp_path, 1, "photo.jpg", 900)
        thumb = _user_file(tmp_path, 1, "photo_thumb.jpg", 100)
        record_written(1, full, thumb)
        assert _usage(engine, 1) == 1000

    def test_unlink_and_record_removes_files_and_subtracts(self, engine, tmp_path):
        full = _user_file(tmp_path, 1, "photo.jpg", 900)
        thumb = _user_file(tmp_path, 1, "photo_thumb.jpg", 100)
        record_written(1, full, thumb)
        unlink_and_record(1, [full, thumb])
        assert not full.exists() and not thumb.exists()
        assert _usage(engine, 1) == 0

    def test_deleting_an_already_missing_file_costs_nothing(self, engine, tmp_path):
        record_delta(1, 500)
        unlink_and_record(1, [tmp_path / "users" / "1" / "gone.jpg"])
        assert _usage(engine, 1) == 500


class TestReconcile:
    def test_corrects_a_drifted_counter(self, engine, tmp_path):
        _user_file(tmp_path, 1, "photo.jpg", 700)
        record_delta(1, 99999)  # drift: e.g. a delete that never got counted
        assert reconcile_usage(1) == 700
        assert _usage(engine, 1) == 700

    def test_counts_nested_directories(self, engine, tmp_path):
        nested = tmp_path / "users" / "1" / "memories" / "5"
        nested.mkdir(parents=True)
        (nested / "a.jpg").write_bytes(b"x" * 300)
        (nested / "a_thumb.jpg").write_bytes(b"x" * 50)
        assert reconcile_usage(1) == 350

    def test_a_user_with_no_files_reconciles_to_zero(self, engine):
        record_delta(1, 1234)
        assert reconcile_usage(1) == 0

    def test_reconcile_all_is_a_no_op_when_billing_is_off(self, engine, tmp_path,
                                                          monkeypatch):
        monkeypatch.delenv("BILLING_ENABLED", raising=False)
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        _user_file(tmp_path, 1, "photo.jpg", 700)
        reconcile_all_usage()
        assert _usage(engine, 1) == 0

    def test_reconcile_all_walks_every_user_when_billing_is_on(
        self, engine, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("BILLING_ENABLED", "1")
        _user_file(tmp_path, 1, "photo.jpg", 700)
        _user_file(tmp_path, 2, "photo.jpg", 200)
        reconcile_all_usage()
        assert (_usage(engine, 1), _usage(engine, 2)) == (700, 200)


@pytest.fixture
def file_engine(monkeypatch, tmp_path):
    """File-backed SQLite, one connection per session, configured like the
    app's: concurrent writers need real connections, which the shared
    StaticPool connection above cannot give them."""
    test_engine = db_module._make_engine(
        f"sqlite:///{(tmp_path / 'usage.db').as_posix()}")
    db_module._configure_sqlite(test_engine)
    monkeypatch.setattr(db_module, "engine", test_engine)
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as sess:
        sess.add(UserInfo(id=1, email="a@example.com", display_name="A"))
        sess.commit()
    yield test_engine
    test_engine.dispose()


def _hold_writes_until_all_arrive(engine, parties: int) -> None:
    """Make each thread's first write to the counter wait until every thread
    has reached its own: a value read beforehand is then stale for all but
    one of them (issue #612)."""
    arrived = threading.Barrier(parties, timeout=5)
    seen = threading.local()

    @event.listens_for(engine, "before_cursor_execute")
    def _wait(conn, cursor, statement, params, context, executemany):
        sql = statement.lstrip().upper()
        if ("USERUSAGE" in sql and sql.startswith(("UPDATE", "INSERT"))
                and not getattr(seen, "done", False)):
            seen.done = True
            arrived.wait()


def _run_concurrently(deltas: list[int]) -> None:
    threads = [threading.Thread(target=record_delta, args=(1, d)) for d in deltas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class TestConcurrentDeltas:
    def test_concurrent_deltas_on_an_existing_counter_are_all_counted(
        self, file_engine
    ):
        record_delta(1, 1_000_000)
        _hold_writes_until_all_arrive(file_engine, 4)
        _run_concurrently([1288, 1288, 500, -1288])
        assert _usage(file_engine, 1) == 1_000_000 + 1288 + 500

    def test_concurrent_first_deltas_are_all_counted(self, file_engine):
        _hold_writes_until_all_arrive(file_engine, 3)
        _run_concurrently([100, 200, 300])
        assert _usage(file_engine, 1) == 600

    def test_a_first_negative_delta_creates_a_zero_counter(self, file_engine):
        record_delta(1, -500)
        assert _usage(file_engine, 1) == 0

    def test_a_racing_first_insert_is_retried_as_an_update(
        self, file_engine, monkeypatch
    ):
        """Postgres lets two first uploads both find no row and both insert;
        the loser's insert hits the unique user_info_id and must still count."""
        real_add = usage_mod._add_in_db
        calls = []

        def add_after_a_rival_created_the_row(sess, uid, delta, now):
            calls.append(delta)
            if len(calls) == 1:
                # The rival's insert commits after this UPDATE matched nothing.
                with Session(file_engine) as rival:
                    rival.add(UserUsage(user_info_id=uid, storage_bytes=700))
                    rival.commit()
                return 0
            return real_add(sess, uid, delta, now)

        monkeypatch.setattr(usage_mod, "_add_in_db", add_after_a_rival_created_the_row)
        record_delta(1, 300)
        assert _usage(file_engine, 1) == 1000
        assert len(calls) == 2

    def test_the_clamp_holds_under_concurrent_deletes(self, file_engine):
        record_delta(1, 100)
        _hold_writes_until_all_arrive(file_engine, 2)
        _run_concurrently([-80, -80])
        assert _usage(file_engine, 1) == 0

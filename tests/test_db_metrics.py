"""Database metrics — query volume/latency, pool saturation, file growth and
error classification (issue #125).

Motivated by this app's two worst incidents: pool exhaustion that looked like a
total hang and needed a manual restart (#35), and a 6.5 s project reload nobody
could see from the outside (#45). Neither is visible in HTTP metrics alone.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import models.db as db_module
from models.db import _configure_sqlite, _make_engine, get_session
from src.utils.metrics import db_error_kind, install_db_metrics


@pytest.fixture
def file_engine(tmp_path):
    """A file-backed engine with metrics installed, matching production's
    QueuePool + WAL setup (in-memory uses a different pool class entirely, and
    without the PRAGMAs there is no -wal sidecar to measure)."""
    engine = _make_engine(f"sqlite:///{(tmp_path / 'metrics.db').as_posix()}")
    _configure_sqlite(engine)
    install_db_metrics(engine)
    return engine


class TestQueryMetrics:
    def test_statements_are_counted_by_operation(self, file_engine, metric):
        before = metric("traxjourney_db_queries_total", operation="SELECT")
        with file_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.execute(text("SELECT 2"))
        assert metric("traxjourney_db_queries_total", operation="SELECT") - before == 2

    def test_writes_are_counted_separately_from_reads(self, file_engine, metric):
        selects = metric("traxjourney_db_queries_total", operation="SELECT")
        inserts = metric("traxjourney_db_queries_total", operation="INSERT")

        with file_engine.begin() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
            conn.execute(text("INSERT INTO t VALUES (1)"))

        assert metric("traxjourney_db_queries_total", operation="INSERT") - inserts == 1
        assert metric("traxjourney_db_queries_total", operation="SELECT") == selects
        # CREATE TABLE isn't in the closed set — it lands in OTHER, not its own
        # label value.
        assert metric("traxjourney_db_queries_total", operation="CREATE") == 0

    def test_duration_is_observed(self, file_engine, metric):
        before = metric("traxjourney_db_query_duration_seconds_count", operation="SELECT")
        with file_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        assert metric("traxjourney_db_query_duration_seconds_count",
                      operation="SELECT") - before == 1

    def test_statement_text_never_becomes_a_label(self, file_engine, metric):
        """Statements carry user content (E2EE payloads included) and are
        unbounded in shape — only the leading keyword may be labelled."""
        with file_engine.connect() as conn:
            conn.execute(text("SELECT 'secret-value'"))

        samples = [
            sample
            for family in __import__("prometheus_client").REGISTRY.collect()
            if family.name == "traxjourney_db_queries"
            for sample in family.samples
        ]
        for sample in samples:
            assert "secret-value" not in str(sample.labels)

    def test_install_is_idempotent(self, file_engine, metric):
        """A second install would double-count every statement."""
        install_db_metrics(file_engine)
        before = metric("traxjourney_db_queries_total", operation="SELECT")
        with file_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        assert metric("traxjourney_db_queries_total", operation="SELECT") - before == 1


class TestPoolGauges:
    """These gauges are computed at scrape time via set_function, so they are
    read back through the registry — reading the child's stored value would
    bypass the callback entirely and always return 0."""

    def test_reports_checked_out_connections(self, file_engine, metric):
        """The #35 early-warning signal: in_use climbing towards capacity is
        what preceded the hang."""
        with file_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            assert metric("traxjourney_db_pool_connections", state="in_use") == 1

        assert metric("traxjourney_db_pool_connections", state="in_use") == 0
        assert metric("traxjourney_db_pool_connections", state="idle") == 1

    def test_reports_capacity(self, file_engine, metric):
        # pool_size=20 + max_overflow=40, the ceiling requests queue behind.
        assert metric("traxjourney_db_pool_capacity") == 60

    def test_overflow_is_reported(self, file_engine, metric):
        assert metric("traxjourney_db_pool_overflow") <= 0  # negative until it fills

    def test_in_memory_pool_does_not_break_a_scrape(self, metric):
        """The in-memory pool used by tests has no checkedout()/overflow(); a
        scrape must degrade to 0 rather than raise and take /metrics down."""
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        install_db_metrics(engine)

        assert metric("traxjourney_db_pool_overflow") == 0
        assert metric("traxjourney_db_pool_connections", state="in_use") == 0
        assert metric("traxjourney_db_pool_capacity") == 0


class TestFileSizeGauges:
    def test_reports_main_and_wal_sizes(self, file_engine, metric):
        """WAL growth is the only visible symptom of checkpoint_wal having
        stopped — wal_autocheckpoint=0 means nothing else folds it back."""
        with file_engine.begin() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
            conn.execute(text("INSERT INTO t VALUES (1)"))

        assert metric("traxjourney_db_file_size_bytes", file="main") > 0
        assert metric("traxjourney_db_file_size_bytes", file="wal") > 0

    def test_in_memory_database_reports_zero(self, metric):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        install_db_metrics(engine)
        assert metric("traxjourney_db_file_size_bytes", file="main") == 0


class TestErrorClassification:
    def test_pool_timeout_is_its_own_kind(self):
        assert db_error_kind(sqlalchemy.exc.TimeoutError("pool limit")) == "pool_timeout"

    def test_database_locked_is_its_own_kind(self):
        exc = sqlalchemy.exc.OperationalError(
            "INSERT", {}, Exception("database is locked")
        )
        assert db_error_kind(exc) == "locked"

    def test_other_operational_errors(self):
        exc = sqlalchemy.exc.OperationalError("SELECT", {}, Exception("no such table"))
        assert db_error_kind(exc) == "operational"

    def test_other_sqlalchemy_errors(self):
        assert db_error_kind(sqlalchemy.exc.InvalidRequestError("nope")) == "other"

    def test_application_exceptions_are_not_database_errors(self):
        """A failed login raises HTTPException from inside a get_session()
        block; counting those would drown the metric in normal traffic."""
        assert db_error_kind(ValueError("business rule")) is None


class TestGetSessionInstrumentation:
    @pytest.fixture
    def engine(self, monkeypatch):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        monkeypatch.setattr(db_module, "engine", engine)
        SQLModel.metadata.create_all(engine)
        return engine

    def test_session_duration_is_observed(self, engine, metric):
        before = metric("traxjourney_db_session_duration_seconds_count")
        with get_session():
            pass
        assert metric("traxjourney_db_session_duration_seconds_count") - before == 1

    def test_pool_timeout_is_counted_and_still_raises(self, engine, metric):
        """Pool exhaustion is what took production down in #35 — it has to be
        both counted and left to propagate exactly as before."""
        before = metric("traxjourney_db_errors_total", kind="pool_timeout")

        with patch.object(db_module, "Session",
                          side_effect=sqlalchemy.exc.TimeoutError("pool limit")):
            with pytest.raises(sqlalchemy.exc.TimeoutError):
                with get_session():
                    pass

        assert metric("traxjourney_db_errors_total", kind="pool_timeout") - before == 1

    def test_database_locked_is_counted(self, engine, metric):
        before = metric("traxjourney_db_errors_total", kind="locked")
        locked = sqlalchemy.exc.OperationalError("INSERT", {}, Exception("database is locked"))

        with pytest.raises(sqlalchemy.exc.OperationalError):
            with get_session():
                raise locked

        assert metric("traxjourney_db_errors_total", kind="locked") - before == 1

    def test_application_exception_is_not_counted(self, engine, metric):
        before = metric("traxjourney_db_errors_total", kind="other")

        with pytest.raises(ValueError):
            with get_session():
                raise ValueError("business rule")

        assert metric("traxjourney_db_errors_total", kind="other") == before

    def test_stale_writes_are_counted_without_changing_the_response(self, metric):
        """Optimistic-lock 409s spike exactly when writers collide, so they are
        a contention signal — but the client-visible response must not move."""
        import anyio

        import api.router as router
        from src.project.project_repo import StaleWriteError

        before = metric("traxjourney_stale_writes_total")
        resp = anyio.run(
            router._stale_write_handler, None, StaleWriteError("version mismatch")
        )

        assert resp.status_code == 409
        assert metric("traxjourney_stale_writes_total") - before == 1

    def test_original_exception_propagates_unchanged(self, engine):
        """Instrumentation must not alter a single response — the exception the
        caller sees is the one that was raised."""
        sentinel = ValueError("exactly this")

        with pytest.raises(ValueError) as caught:
            with get_session():
                raise sentinel

        assert caught.value is sentinel

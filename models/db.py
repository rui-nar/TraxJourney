"""Database engine and session factory — replaces rx.session()."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from models.db_url import resolve_database_url
from src.utils.metrics import track_db_session

_DB_URL = resolve_database_url()


def _make_engine(url: str):
    """Create the app engine with a connection pool sized for the client's
    parallel request fan-out.

    The Flutter client fires many requests in parallel when opening a project
    (meta, geo, stats, activities, photos …). SQLAlchemy's default QueuePool
    (size 5 + 10 overflow = 15 total) is far too small: a couple of concurrent
    users exhaust it, then every new request blocks for ``pool_timeout`` seconds
    waiting for a connection — so the app looks completely hung and needs a
    manual restart (issue #35). SQLite in WAL mode serves many concurrent readers
    fine, so we raise the ceiling well above realistic load and fail fast if it
    is ever hit. In-memory SQLite (tests) uses a non-queue pool that rejects
    these kwargs, so they are only applied to file-backed / networked DBs.
    """
    is_sqlite = url.startswith("sqlite")
    is_memory = is_sqlite and (":memory:" in url or url == "sqlite://")
    kwargs: dict = {}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    if not is_memory:
        kwargs.update(
            pool_size=20,        # persistent connections
            max_overflow=40,     # burst headroom → 60 total, vs the old 15
            pool_timeout=10,     # fail fast instead of a 30s hang cascade
            pool_pre_ping=True,  # drop dead connections rather than erroring
        )
    return create_engine(url, **kwargs)


engine = _make_engine(_DB_URL)


def _configure_sqlite(target_engine) -> None:
    """Tune SQLite for concurrent access.

    Without this, SQLite runs with journal_mode=DELETE (writers block all
    readers) and busy_timeout=0 — so the moment two operations contend (e.g. a
    Polarsteps import firing parallel create_memory writes plus background photo
    downloads), it raises "database is locked" instead of waiting. On every new
    connection we set:

      * busy_timeout=30000  — wait up to 30s for a lock rather than erroring
        immediately. The single biggest win; safe on every filesystem.
      * journal_mode=WAL    — readers no longer block on the writer. Persisted in
        the DB header; safe on local filesystems (incl. Btrfs).
      * synchronous=NORMAL  — the standard, durable-enough pairing with WAL.
      * wal_autocheckpoint=0 — disable SQLite's default *inline* checkpoint
        (which runs synchronously inside whichever commit happens to push the
        WAL past ~1000 pages). On slow storage that checkpoint's I/O can take
        seconds, and since it runs inside that connection's commit, it holds the
        single SQLite write lock for the whole thing — any other writer queued
        behind it burns its busy_timeout waiting on an unlucky commit instead of
        real contention. ``checkpoint_wal()`` below replaces it with a periodic
        PASSIVE checkpoint that never blocks a writer.
      * cache_size=-64000    — ~64 MB of page cache per connection (SQLite's
        default is ~2 MB). Rows with large TEXT columns (an activity's full
        summary_polyline/elevation_profile_json can run several hundred KB)
        spill into SQLite overflow pages; a cache too small to hold a
        project's working set forces repeated random-read disk I/O for pages
        that were just written in the SAME transaction, not only across
        requests (issue #45 follow-up: a project reload right after writing a
        large activity row measured 6.5s on this alone). cache_size is a
        per-connection PRAGMA (unlike journal_mode, not persisted in the DB
        header), so — like busy_timeout/synchronous — it must be reapplied on
        every new connection here.

    No-op for non-SQLite backends.
    """
    if target_engine.dialect.name != "sqlite":
        return

    @event.listens_for(target_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _conn_record):  # pragma: no cover - thin
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA wal_autocheckpoint=0")
            cursor.execute("PRAGMA cache_size=-64000")
        finally:
            cursor.close()


_configure_sqlite(engine)


def checkpoint_wal() -> None:
    """Fold the WAL back into the main DB file via a PASSIVE checkpoint.

    Meant to be called periodically from a background job (see api/router.py)
    now that automatic checkpointing is disabled. PASSIVE mode never waits for
    other connections' locks — it checkpoints as many frames as it can without
    disturbing concurrent readers/writers and returns immediately — so this
    keeps the WAL small without ever making an interactive request pay for the
    checkpoint's I/O cost. No-op for non-SQLite backends.
    """
    if engine.dialect.name != "sqlite":
        return
    conn = engine.raw_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            cursor.close()
    finally:
        conn.close()


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session():
    """Context manager that yields a SQLModel session — mirrors rx.session().

    Every DB access in the app goes through here, which makes it the one place
    that can time a unit of database work and classify the failures leaving it
    (pool exhaustion, "database is locked"). Application exceptions travelling
    through are left alone — see ``db_error_kind``.
    """
    with track_db_session():
        with Session(engine) as session:
            yield session

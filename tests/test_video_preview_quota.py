"""Trip-video previews and the quotas (docs/VIDEO_PREVIEW_PLAN.md D2, D3, D6).

A preview is a videojob row of ``kind='preview'``. It never counts toward the
monthly video quota; it counts toward an hourly rate limit instead, read from
the rows by :func:`previews_in_last_hour`. Migration c519a0b1d2e3 adds the
column and backfills every existing row as a full video.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from models.project_db import DBVideoJob
from models.user import UserInfo
from src.billing.entitlements import (
    PREVIEW_IN_FLIGHT_S,
    PREVIEW_WINDOW_S,
    ensure_video_quota,
    preview_slot_frees_at,
    previews_in_last_hour,
    videos_this_month,
)
from src.exceptions.errors import QuotaExceeded

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BEFORE = "a442d0e1f2b3"
_REVISION = "c519a0b1d2e3"

#: Mid-month reference instant.
_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_VIDEOS_PER_MONTH"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def engine(monkeypatch):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as sess:
        sess.add(UserInfo(id=1, email="a@example.com", display_name="A"))
        sess.add(UserInfo(id=2, email="b@example.com", display_name="B"))
        sess.commit()
    yield test_engine


def _job(engine, *, kind="preview", user=1, status="done", age=0.0,
         started=None, completed=None):
    """A job created ``age`` seconds before ``_NOW``; returns its id."""
    with Session(engine) as sess:
        job = DBVideoJob(project_id=1, user_info_id=user, kind=kind,
                         status=status, created_at=_NOW - age,
                         started_at=started, completed_at=completed)
        sess.add(job)
        sess.commit()
        return job.id


def _set(engine, job_id, **fields):
    with Session(engine) as sess:
        job = sess.get(DBVideoJob, job_id)
        for name, value in fields.items():
            setattr(job, name, value)
        sess.add(job)
        sess.commit()


def _videos(engine, user=1) -> int:
    with Session(engine) as sess:
        return videos_this_month(sess, user, _NOW)


def _previews(engine, user=1, now=_NOW) -> int:
    with Session(engine) as sess:
        return previews_in_last_hour(sess, user, now)


def _frees_at(engine, user=1, now=_NOW, limit=10):
    with Session(engine) as sess:
        return preview_slot_frees_at(sess, user, now, limit)


# ── Monthly video quota ───────────────────────────────────────────────────────

class TestMonthlyQuota:
    def test_kind_defaults_to_video(self, engine):
        with Session(engine) as sess:
            sess.add(DBVideoJob(project_id=1, user_info_id=1, created_at=_NOW))
            sess.commit()
        assert _videos(engine) == 1

    def test_previews_do_not_count(self, engine):
        for status in ("pending", "running", "done", "expired"):
            _job(engine, kind="preview", status=status)
        assert _videos(engine) == 0

    def test_videos_still_count(self, engine):
        _job(engine, kind="video", status="done")
        _job(engine, kind="video", status="running")
        _job(engine, kind="preview", status="done")
        _job(engine, kind="video", status="failed")
        assert _videos(engine) == 2

    def test_previews_never_exhaust_the_quota(self, engine, monkeypatch):
        monkeypatch.setenv("BILLING_ENABLED", "true")
        monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "true")
        monkeypatch.setenv("FREE_MAX_VIDEOS_PER_MONTH", "1")
        for _ in range(5):
            _job(engine, kind="preview")
        with Session(engine) as sess:
            ensure_video_quota(sess, 1, _NOW)  # does not raise
        _job(engine, kind="video")
        with Session(engine) as sess, pytest.raises(QuotaExceeded):
            ensure_video_quota(sess, 1, _NOW)


# ── Hourly preview count ──────────────────────────────────────────────────────

class TestPreviewsInLastHour:
    def test_done_previews_count(self, engine):
        _job(engine, status="done", started=_NOW - 60)
        _job(engine, status="done", age=3000, started=_NOW - 3000)
        assert _previews(engine) == 2

    def test_full_videos_do_not_count(self, engine):
        _job(engine, kind="video", status="done")
        _job(engine, kind="video", status="pending")
        assert _previews(engine) == 0

    def test_other_users_previews_do_not_count(self, engine):
        _job(engine, user=2)
        assert _previews(engine, user=1) == 0
        assert _previews(engine, user=2) == 1

    def test_one_hour_window(self, engine):
        _job(engine, status="done", age=3599)
        _job(engine, status="done", age=3600)  # exactly an hour: out
        _job(engine, status="done", age=7200)
        assert _previews(engine) == 1

    def test_window_rolls_forward(self, engine):
        _job(engine, status="done", age=0)
        assert _previews(engine, now=_NOW + 3599) == 1
        assert _previews(engine, now=_NOW + 3600) == 0

    def test_failed_after_starting_counts(self, engine):
        _job(engine, status="failed", age=100, started=_NOW - 90,
             completed=_NOW - 10)
        assert _previews(engine) == 1

    def test_timed_out_five_minutes_after_start_counts(self, engine):
        # The 300 s render timeout fires well inside the in-flight window.
        _job(engine, status="failed", age=400, started=_NOW - 390,
             completed=_NOW - 90)
        assert _previews(engine) == 1

    def test_failed_at_the_in_flight_bound_still_counts(self, engine):
        _job(engine, status="failed", age=1000, started=_NOW - 990,
             completed=_NOW - 1000 + PREVIEW_IN_FLIGHT_S)
        assert _previews(engine) == 1

    def test_failed_by_the_sweep_does_not_count(self, engine):
        # Orphaned in running, failed by the hourly sweep 40 minutes in.
        _job(engine, status="failed", age=2700, started=_NOW - 2690,
             completed=_NOW - 2700 + 2400)
        assert _previews(engine) == 0

    def test_failed_without_completed_at_does_not_count(self, engine):
        _job(engine, status="failed", age=100, started=_NOW - 90)
        assert _previews(engine) == 0

    def test_an_orphan_never_re_enters_the_count(self, engine):
        job = _job(engine, status="pending", age=0)
        seen = []
        for t in range(0, 3700, 60):
            if t == 30 * 60:
                _set(engine, job, status="running", started_at=_NOW + 10)
            if t == 40 * 60:  # the sweep fails the orphan
                _set(engine, job, status="failed",
                     completed_at=_NOW + 40 * 60)
            seen.append(_previews(engine, now=_NOW + t))
        # Counts, drops out at 15 minutes, and never comes back.
        assert seen[0] == 1
        assert all(later <= earlier for earlier, later in zip(seen, seen[1:]))
        assert seen[-1] == 0

    def test_failed_before_starting_does_not_count(self, engine):
        _job(engine, status="failed", age=100, started=None)
        assert _previews(engine) == 0

    def test_failed_after_starting_leaves_with_the_hour(self, engine):
        _job(engine, status="failed", age=3600, started=_NOW - 3500,
             completed=_NOW - 3400)
        assert _previews(engine) == 0

    def test_expired_does_not_count(self, engine):
        _job(engine, status="expired", age=100, started=_NOW - 90)
        assert _previews(engine) == 0

    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_in_flight_counts_only_while_younger_than_15_minutes(
            self, engine, status):
        started = _NOW - 800 if status == "running" else None
        _job(engine, status=status, age=899, started=started)
        assert _previews(engine) == 1
        assert _previews(engine, now=_NOW + 1) == 0  # now exactly 900 s old
        assert _previews(engine, now=_NOW + 600) == 0

    def test_lost_in_flight_previews_stop_counting(self, engine):
        _job(engine, status="pending", age=901)
        _job(engine, status="running", age=1800, started=_NOW - 1790)
        _job(engine, status="done", age=1800)
        assert _previews(engine) == 1


# ── When a slot frees up (Retry-After) ────────────────────────────────────────

class TestPreviewSlotFreesAt:
    def _assert_frees_exactly_at(self, engine, frees_at, limit):
        assert _previews(engine, now=frees_at) < limit
        assert _previews(engine, now=frees_at - 1) >= limit

    def test_none_under_the_limit(self, engine):
        for _ in range(9):
            _job(engine, status="done")
        assert _frees_at(engine) is None

    def test_none_with_no_rows(self, engine):
        assert _frees_at(engine) is None

    def test_at_the_limit_the_oldest_row_frees_it(self, engine):
        for age in range(100, 1100, 100):  # ten done previews
            _job(engine, status="done", age=age)
        frees_at = _frees_at(engine)
        assert frees_at == _NOW - 1000 + PREVIEW_WINDOW_S
        self._assert_frees_exactly_at(engine, frees_at, 10)

    def test_a_running_preview_is_forecast_to_count_for_the_hour(self, engine):
        # Ten previews, the tenth still running. It finishes in a minute and
        # then counts for the hour, so the slot frees when the oldest leaves.
        for age in range(1000, 100, -100):  # nine done previews
            _job(engine, status="done", age=age)
        running = _job(engine, status="running", age=30, started=_NOW - 20)
        frees_at = _frees_at(engine)
        assert frees_at == _NOW - 1000 + PREVIEW_WINDOW_S
        _set(engine, running, status="done", completed_at=_NOW + 40)
        self._assert_frees_exactly_at(engine, frees_at, 10)

    def test_mixed_rows(self, engine):
        _job(engine, status="done", age=3000)                  # leaves +600
        running = _job(engine, status="running", age=100,
                       started=_NOW - 90)                      # leaves +3500
        _job(engine, status="failed", age=2500, started=_NOW - 2490,
             completed=_NOW - 2300)                            # leaves +1100
        _job(engine, status="failed", age=10, started=None)    # never counts
        _job(engine, status="expired", age=10)                 # never counts
        assert _frees_at(engine, limit=3) == _NOW + 600
        assert _frees_at(engine, limit=2) == _NOW + 1100
        assert _frees_at(engine, limit=1) == _NOW + 3500
        _set(engine, running, status="done", completed_at=_NOW + 30)
        for limit, frees_at in ((3, _NOW + 600), (2, _NOW + 1100),
                                (1, _NOW + 3500)):
            self._assert_frees_exactly_at(engine, frees_at, limit)

    def test_over_the_limit_waits_for_enough_rows_to_leave(self, engine):
        # Twelve rows against a limit of ten: three must leave, not one.
        for age in range(100, 1300, 100):
            _job(engine, status="done", age=age)
        frees_at = _frees_at(engine)
        assert frees_at == _NOW - 1000 + PREVIEW_WINDOW_S
        assert frees_at != _NOW - 1200 + PREVIEW_WINDOW_S
        self._assert_frees_exactly_at(engine, frees_at, 10)

    def test_a_lost_preview_makes_the_forecast_late_never_early(self, engine):
        # Nine done previews and one pending that is never picked up: it drops
        # out at 15 minutes, well before the forecast. Retry-After is only
        # "not before", so arriving late is allowed; early would be refused.
        for age in range(1000, 100, -100):
            _job(engine, status="done", age=age)
        _job(engine, status="pending", age=50)
        drops_at = _NOW - 50 + PREVIEW_IN_FLIGHT_S
        assert _previews(engine, now=drops_at - 1) == 10
        assert _previews(engine, now=drops_at) == 9
        frees_at = _frees_at(engine)
        assert frees_at == _NOW - 1000 + PREVIEW_WINDOW_S
        assert frees_at >= drops_at
        assert _previews(engine, now=frees_at) < 10

    def test_ignores_other_users(self, engine):
        for _ in range(10):
            _job(engine, user=2, status="done")
        assert _frees_at(engine, user=1) is None
        assert _frees_at(engine, user=2) is not None


# ── Migration ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "video_kind.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["db_path"] = db_path
    return config


def _connect(cfg):
    return closing(sqlite3.connect(cfg.attributes["db_path"]))


def _columns(cfg) -> set[str]:
    with _connect(cfg) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(videojob)")}


def _seed_jobs(cfg) -> None:
    with _connect(cfg) as conn:
        for status in ("done", "failed", "pending"):
            conn.execute(
                "INSERT INTO videojob (project_id, user_info_id, status) "
                "VALUES (1, 1, ?)", (status,))
        conn.commit()


def test_migration_follows_the_heart_rate_drop():
    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    assert script.get_revision(_REVISION).down_revision == _BEFORE


def test_migration_backfills_existing_rows_as_video(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed_jobs(cfg)
    assert "kind" not in _columns(cfg)

    command.upgrade(cfg, _REVISION)

    with _connect(cfg) as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM videojob")]
        # A row inserted without a kind after the migration is a video too.
        conn.execute("INSERT INTO videojob (project_id, user_info_id) VALUES (1, 1)")
        conn.commit()
        latest = conn.execute(
            "SELECT kind FROM videojob ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert kinds == ["video", "video", "video"]
    assert latest == "video"


def test_migration_downgrade_drops_the_column_and_keeps_the_rows(cfg):
    command.upgrade(cfg, _BEFORE)
    _seed_jobs(cfg)
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _BEFORE)

    assert "kind" not in _columns(cfg)
    with _connect(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM videojob").fetchone()[0] == 3

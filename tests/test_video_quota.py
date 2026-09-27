"""Monthly trip-video quota and resolution limit (docs/TRIP_VIDEO_PLAN.md D3, D6, D10).

The quota is the videojob rows themselves: every job created in the current UTC
calendar month counts unless it failed. The resolution cap is a plan limit so
the pricing bullet is generated from it. Stored videos never count toward the
photo storage limit.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
import src.admin.storage as storage_mod
from models.billing import Subscription
from models.project_db import DBVideoJob
from models.user import UserInfo
from src.billing.entitlements import (
    ensure_video_quota,
    limits_for_user,
    videos_this_month,
)
from src.billing.plans import (
    FREE,
    PLAN_ORDER,
    TIER_1,
    TIER_2,
    TIER_3,
    features_for,
    limits_for,
)
from src.billing.usage import reconcile_usage
from src.exceptions.errors import QuotaExceeded


def _ts(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


#: Mid-month reference instant.
_NOW = _ts(2026, 9, 15, 12, 0, 0)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Shipped defaults and an unconfigured deployment unless a test says so."""
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    for prefix in ("FREE", "TIER_1", "TIER_2", "TIER_3"):
        for suffix in ("MAX_VIDEOS_PER_MONTH", "MAX_VIDEO_HEIGHT"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "true")


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


def _job(engine, *, user=1, status="done", created_at=_NOW):
    with Session(engine) as sess:
        sess.add(DBVideoJob(project_id=1, user_info_id=user, status=status,
                            created_at=created_at))
        sess.commit()


def _count(engine, user=1, now=_NOW) -> int:
    with Session(engine) as sess:
        return videos_this_month(sess, user, now)


# ── Counting ──────────────────────────────────────────────────────────────────

class TestVideosThisMonth:
    def test_every_status_but_failed_counts(self, engine):
        for status in ("pending", "running", "done", "expired"):
            _job(engine, status=status)
        _job(engine, status="failed")
        _job(engine, status="failed")
        assert _count(engine) == 4

    def test_other_users_jobs_do_not_count(self, engine):
        _job(engine, user=2)
        assert _count(engine, user=1) == 0
        assert _count(engine, user=2) == 1

    def test_month_starts_at_utc_midnight(self, engine):
        """A job one second before 1 October 00:00 UTC belongs to September."""
        _job(engine, created_at=_ts(2026, 9, 30, 23, 59, 59))
        _job(engine, created_at=_ts(2026, 10, 1, 0, 0, 0))
        assert _count(engine, now=_ts(2026, 9, 30, 23, 59, 59)) == 1
        assert _count(engine, now=_ts(2026, 10, 1, 0, 0, 0)) == 1
        assert _count(engine, now=_ts(2026, 10, 31, 23, 0, 0)) == 1
        assert _count(engine, now=_ts(2026, 11, 1, 0, 0, 0)) == 0

    def test_december_rolls_into_january(self, engine):
        _job(engine, created_at=_ts(2026, 12, 31, 23, 0, 0))
        _job(engine, created_at=_ts(2027, 1, 1, 0, 0, 0))
        assert _count(engine, now=_ts(2026, 12, 15)) == 1
        assert _count(engine, now=_ts(2027, 1, 15)) == 1


# ── Enforcement ───────────────────────────────────────────────────────────────

class TestEnsureVideoQuota:
    def test_free_first_video_is_allowed(self, engine, enforced):
        with Session(engine) as sess:
            ensure_video_quota(sess, 1, _NOW)

    def test_free_second_video_is_refused(self, engine, enforced):
        _job(engine)
        with Session(engine) as sess, pytest.raises(QuotaExceeded) as exc:
            ensure_video_quota(sess, 1, _NOW)
        err = exc.value
        assert (err.plan, err.limit, err.used, err.resource) == (FREE, 1, 1, "videos")

    def test_a_failed_video_does_not_use_up_the_quota(self, engine, enforced):
        _job(engine, status="failed")
        with Session(engine) as sess:
            ensure_video_quota(sess, 1, _NOW)

    def test_last_months_video_does_not_count(self, engine, enforced):
        _job(engine, created_at=_ts(2026, 8, 31, 23, 59, 59))
        with Session(engine) as sess:
            ensure_video_quota(sess, 1, _NOW)

    def test_paid_plan_uses_its_own_limit(self, engine, enforced):
        with Session(engine) as sess:
            sess.add(Subscription(user_info_id=1, plan=TIER_1, status="active"))
            sess.commit()
        for _ in range(9):
            _job(engine)
        with Session(engine) as sess:
            ensure_video_quota(sess, 1, _NOW)
        _job(engine)
        with Session(engine) as sess, pytest.raises(QuotaExceeded):
            ensure_video_quota(sess, 1, _NOW)

    def test_billing_off_is_unlimited(self, engine):
        for _ in range(50):
            _job(engine)
        with Session(engine) as sess:
            ensure_video_quota(sess, 1, _NOW)
            limits = limits_for_user(sess, 1)
        assert limits.max_videos_per_month is None
        assert limits.max_video_height == 1080

    def test_billing_on_but_not_enforced_only_counts(self, engine, monkeypatch):
        monkeypatch.setenv("BILLING_ENABLED", "true")
        _job(engine)
        _job(engine)
        with Session(engine) as sess:
            ensure_video_quota(sess, 1, _NOW)


# ── Limits and bullets ────────────────────────────────────────────────────────

class TestVideoLimits:
    def test_default_videos_per_month(self):
        assert [limits_for(p).max_videos_per_month for p in PLAN_ORDER] == [
            1, 10, 30, None]

    def test_video_height_is_720_on_free_and_1080_on_paid(self):
        assert limits_for(FREE).max_video_height == 720
        for plan in (TIER_1, TIER_2, TIER_3):
            assert limits_for(plan).max_video_height == 1080

    def test_bullets(self):
        assert "1 video per month · 720p" in features_for(FREE)
        assert "10 videos per month · 1080p" in features_for(TIER_1)
        assert "30 videos per month · 1080p" in features_for(TIER_2)
        assert "Unlimited videos · 1080p" in features_for(TIER_3)

    def test_env_overrides_limit_and_bullet(self, monkeypatch):
        monkeypatch.setenv("TIER_1_MAX_VIDEOS_PER_MONTH", "20")
        assert limits_for(TIER_1).max_videos_per_month == 20
        assert "20 videos per month · 1080p" in features_for(TIER_1)
        assert "10 videos per month · 1080p" not in features_for(TIER_1)
        assert limits_for(TIER_2).max_videos_per_month == 30

    def test_env_overrides_height(self, monkeypatch):
        monkeypatch.setenv("FREE_MAX_VIDEO_HEIGHT", "1080")
        assert limits_for(FREE).max_video_height == 1080
        assert "1 video per month · 1080p" in features_for(FREE)

    def test_env_can_make_videos_unlimited(self, monkeypatch):
        monkeypatch.setenv("FREE_MAX_VIDEOS_PER_MONTH", "unlimited")
        assert limits_for(FREE).max_videos_per_month is None
        assert "Unlimited videos · 720p" in features_for(FREE)


# ── Storage reconcile (D6, D13) ───────────────────────────────────────────────

class TestReconcileSkipsVideos:
    def test_videos_are_not_counted_but_memories_are(self, engine, tmp_path):
        user_dir = tmp_path / "users" / "1"
        (user_dir / "videos" / "7").mkdir(parents=True)
        (user_dir / "videos" / "7" / "video.mp4").write_bytes(b"x" * 5000)
        (user_dir / "memories" / "3").mkdir(parents=True)
        (user_dir / "memories" / "3" / "a.jpg").write_bytes(b"x" * 300)
        assert reconcile_usage(1) == 300

    def test_only_videos_reconciles_to_zero(self, engine, tmp_path):
        video = tmp_path / "users" / "1" / "videos" / "7"
        video.mkdir(parents=True)
        (video / "video.mp4").write_bytes(b"x" * 5000)
        assert reconcile_usage(1) == 0

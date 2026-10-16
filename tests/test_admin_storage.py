"""Tests for admin storage accounting and the encryption-tier mapping.

Covers:
  * dir_size — sums nested files, per-user isolation, missing dir → 0
  * cached_user_storage — TTL cache doesn't re-walk within the window; refresh busts it
  * tier_from — pure none/low/medium/high mapping
  * user_encryption_tier(s) — tier read from the E2EE rows
"""
from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import src.admin.storage as storage
from src.admin.storage import (
    cached_user_storage,
    dir_size,
    refresh_storage_cache,
)
from models.project_db import DBRecoveryWrap
from models.user import UserInfo
from src.admin.tiers import tier_from, user_encryption_tier, user_encryption_tiers


# ── dir_size ──────────────────────────────────────────────────────────────────

class TestDirSize:
    def test_missing_dir_returns_zero(self, tmp_path):
        assert dir_size(tmp_path / "does-not-exist") == 0

    def test_empty_dir_returns_zero(self, tmp_path):
        (tmp_path / "empty").mkdir()
        assert dir_size(tmp_path / "empty") == 0

    def test_sums_nested_files(self, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"x" * 10)
        sub = tmp_path / "sub" / "deep"
        sub.mkdir(parents=True)
        (sub / "b.bin").write_bytes(b"y" * 25)
        assert dir_size(tmp_path) == 35

    def test_per_user_isolation(self, tmp_path):
        u1 = tmp_path / "users" / "1"
        u2 = tmp_path / "users" / "2"
        u1.mkdir(parents=True)
        u2.mkdir(parents=True)
        (u1 / "f").write_bytes(b"a" * 100)
        (u2 / "f").write_bytes(b"a" * 7)
        assert dir_size(u1) == 100
        assert dir_size(u2) == 7


# ── cached_user_storage ───────────────────────────────────────────────────────

class TestStorageCache:
    @pytest.fixture(autouse=True)
    def _isolate_cache_and_data(self, tmp_path, monkeypatch):
        refresh_storage_cache()
        monkeypatch.setattr(storage, "_DATA_DIR", str(tmp_path))
        yield
        refresh_storage_cache()

    def _seed(self, tmp_path, user_id: str, size: int):
        d = tmp_path / "users" / str(user_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob").write_bytes(b"z" * size)

    def test_computes_from_filesystem(self, tmp_path):
        self._seed(tmp_path, "5", 42)
        assert cached_user_storage("5", now=1000.0) == 42

    def test_repeated_calls_within_ttl_do_not_rewalk(self, tmp_path, monkeypatch):
        self._seed(tmp_path, "5", 42)
        calls = {"n": 0}
        real_dir_size = storage.dir_size

        def spy(path):
            calls["n"] += 1
            return real_dir_size(path)

        monkeypatch.setattr(storage, "dir_size", spy)

        assert cached_user_storage("5", now=1000.0) == 42
        assert cached_user_storage("5", now=1000.0 + 10) == 42  # within TTL
        assert calls["n"] == 1  # walked exactly once

    def test_refresh_busts_cache(self, tmp_path, monkeypatch):
        self._seed(tmp_path, "5", 42)
        calls = {"n": 0}
        real_dir_size = storage.dir_size

        def spy(path):
            calls["n"] += 1
            return real_dir_size(path)

        monkeypatch.setattr(storage, "dir_size", spy)

        assert cached_user_storage("5", now=1000.0) == 42
        refresh_storage_cache()
        assert cached_user_storage("5", now=1000.0) == 42  # same window, but busted
        assert calls["n"] == 2

    def test_ttl_expiry_rewalks(self, tmp_path, monkeypatch):
        self._seed(tmp_path, "5", 42)
        calls = {"n": 0}
        real_dir_size = storage.dir_size

        def spy(path):
            calls["n"] += 1
            return real_dir_size(path)

        monkeypatch.setattr(storage, "dir_size", spy)

        assert cached_user_storage("5", now=1000.0) == 42
        # Advance well past the TTL.
        assert cached_user_storage("5", now=1000.0 + 10_000) == 42
        assert calls["n"] == 2


# ── tier_from (pure mapping) ──────────────────────────────────────────────────

class TestTierFrom:
    def test_disabled_is_none(self):
        assert tier_from(False, None) == "none"
        assert tier_from(False, "recovery_key") == "none"  # disabled wins

    def test_escrow_is_low(self):
        assert tier_from(True, "escrow") == "low"

    def test_qna_is_medium(self):
        assert tier_from(True, "qna") == "medium"

    def test_recovery_key_and_passphrase_are_high(self):
        assert tier_from(True, "recovery_key") == "high"
        assert tier_from(True, "passphrase") == "high"

    def test_unknown_method_is_none(self):
        assert tier_from(True, "banana") == "none"
        assert tier_from(True, None) == "none"


class TestUserEncryptionTier:
    @pytest.fixture
    def sess(self):
        engine = create_engine("sqlite://", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            yield s

    def _user(self, sess, *, enabled, methods=()):
        ui = UserInfo(display_name="u", encryption_enabled=enabled)
        sess.add(ui)
        sess.commit()
        for m in methods:
            sess.add(DBRecoveryWrap(user_info_id=ui.id, method=m,
                                    wrapped_cmk="w", salt="s"))
        sess.commit()
        return ui.id

    @pytest.mark.parametrize("method, tier", [
        ("escrow", "low"),
        ("qna", "medium"),
        ("recovery_key", "high"),
        ("passphrase", "high"),
    ])
    def test_tier_per_recovery_method(self, sess, method, tier):
        uid = self._user(sess, enabled=True, methods=[method])
        assert user_encryption_tier(sess, uid) == tier

    def test_not_enabled_is_none(self, sess):
        assert user_encryption_tier(sess, self._user(sess, enabled=False)) == "none"

    def test_wrap_left_on_disabled_account_is_ignored(self, sess):
        uid = self._user(sess, enabled=False, methods=["recovery_key"])
        assert user_encryption_tier(sess, uid) == "none"

    def test_strongest_of_several_wraps(self, sess):
        uid = self._user(sess, enabled=True, methods=["qna", "recovery_key", "escrow"])
        assert user_encryption_tier(sess, uid) == "high"

    def test_unknown_user_is_none(self, sess):
        assert user_encryption_tier(sess, 999) == "none"

    def test_bulk_maps_every_id(self, sess):
        a = self._user(sess, enabled=False)
        b = self._user(sess, enabled=True, methods=["qna"])
        assert user_encryption_tiers(sess, [a, b]) == {a: "none", b: "medium"}
        assert user_encryption_tiers(sess, []) == {}

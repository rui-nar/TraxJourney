"""Plan limits, end to end through the real app (issue #121).

What these pin down is mostly *when not to block*: a self-hosted instance has no
limits at all, and a hosted one measures usage for as long as the operator wants
before it starts refusing writes. The blocking cases assert the 402 payload the
client turns into an upgrade prompt.
"""
from __future__ import annotations

import io
import json
import random

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.journal as journal_mod
import api.memories as mem_mod
import api.people as people_mod
import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.billing import Subscription, UserUsage
from models.project_db import DBJournalEntry, DBMemory, DBPerson, DBProject, DBProjectMember
from models.user import UserInfo
from src.billing.plans import TIER_2
from src.utils.photo_store import prepare_photo

_MB = 1024 * 1024


def _jpeg_bytes(size=(40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """In-memory DB + the real app, authenticated as user 1 (owner of "Trip 1")."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        sess.add(UserInfo(id=1, email="owner@example.com", display_name="Owner"))
        sess.add(UserInfo(id=2, email="mate@example.com", display_name="Mate"))
        sess.add(DBProject(id=1, user_info_id=1, name="Trip 1"))
        sess.commit()

    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "1", "email": "owner@example.com"
    }
    try:
        yield TestClient(app), engine
    finally:
        app.dependency_overrides.clear()


def _enforcing(monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")


def _subscribe(engine, user_id=1, plan=TIER_2, status="active"):
    with Session(engine) as sess:
        sess.add(Subscription(user_info_id=user_id, plan=plan, status=status,
                              current_period_end=9e9))
        sess.commit()


def _seed_usage(engine, user_id, used_bytes):
    with Session(engine) as sess:
        sess.add(UserUsage(user_info_id=user_id, storage_bytes=used_bytes))
        sess.commit()


def _usage(engine, user_id) -> int:
    with Session(engine) as sess:
        row = sess.exec(
            select(UserUsage).where(UserUsage.user_info_id == user_id)
        ).first()
        return row.storage_bytes if row else 0


def _memory(engine, project_id=1) -> int:
    with Session(engine) as sess:
        row = DBMemory(project_id=project_id, date="2026-06-01",
                       geo_mode="custom", photos_json=json.dumps([]))
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return row.id


# ── Trip count ────────────────────────────────────────────────────────────────

class TestProjectQuota:
    def test_self_hosted_has_no_limit(self, env):
        """The landing page promises unlimited projects when you run it yourself."""
        client, _ = env
        for name in ("Trip 2", "Trip 3", "Trip 4"):
            assert client.post("/api/projects/", json={"name": name}).status_code == 201

    def test_measuring_without_enforcing_does_not_block(self, env, monkeypatch):
        client, _ = env
        monkeypatch.setenv("BILLING_ENABLED", "1")  # enforcement flag left off
        assert client.post("/api/projects/", json={"name": "Trip 2"}).status_code == 201

    def test_free_plan_blocks_the_second_trip(self, env, monkeypatch):
        client, _ = env
        _enforcing(monkeypatch)
        res = client.post("/api/projects/", json={"name": "Trip 2"})
        assert res.status_code == 402

    def test_the_402_carries_what_the_client_needs_to_render(self, env, monkeypatch):
        client, _ = env
        _enforcing(monkeypatch)
        body = client.post("/api/projects/", json={"name": "Trip 2"}).json()
        assert body["code"] == "quota_exceeded"
        assert body["resource"] == "projects"
        assert body["plan"] == "free"
        assert body["limit"] == 1
        assert body["used"] == 1
        assert body["detail"]  # a sentence the user can act on

    def test_paid_plan_lifts_the_limit(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        _subscribe(engine)
        assert client.post("/api/projects/", json={"name": "Trip 2"}).status_code == 201

    def test_an_admin_comp_lifts_the_limit(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        with Session(engine) as sess:
            sess.add(Subscription(user_info_id=1, admin_override_plan=TIER_2))
            sess.commit()
        assert client.post("/api/projects/", json={"name": "Trip 2"}).status_code == 201

    def test_the_limit_is_configurable(self, env, monkeypatch):
        client, _ = env
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_PROJECTS", "2")
        assert client.post("/api/projects/", json={"name": "Trip 2"}).status_code == 201
        assert client.post("/api/projects/", json={"name": "Trip 3"}).status_code == 402

    def test_an_expired_subscription_falls_back_to_free(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        with Session(engine) as sess:
            sess.add(Subscription(user_info_id=1, plan=TIER_2, status="canceled",
                                  current_period_end=1.0))  # long past
            sess.commit()
        assert client.post("/api/projects/", json={"name": "Trip 2"}).status_code == 402


# ── Storage ───────────────────────────────────────────────────────────────────

class TestStorageQuota:
    def test_upload_succeeds_under_the_limit(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        memory_id = _memory(engine)
        res = client.post(f"/api/memories/{memory_id}/photos",
                          files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
        assert res.status_code == 201

    def test_an_upload_is_counted(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        memory_id = _memory(engine)
        client.post(f"/api/memories/{memory_id}/photos",
                    files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
        # Full-res plus generated thumbnail, both on disk.
        assert _usage(engine, 1) > 0

    def test_deleting_a_photo_gives_the_space_back(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        memory_id = _memory(engine)
        uuid = client.post(f"/api/memories/{memory_id}/photos",
                           files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")}
                           ).json()["uuid"]
        client.delete(f"/api/memories/{memory_id}/photos/{uuid}")
        assert _usage(engine, 1) == 0

    def test_upload_is_refused_once_the_plan_is_full(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        _seed_usage(engine, 1, 1 * _MB)
        memory_id = _memory(engine)
        res = client.post(f"/api/memories/{memory_id}/photos",
                          files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
        assert res.status_code == 402
        assert res.json()["resource"] == "storage"

    def test_nothing_is_written_when_the_upload_is_refused(self, env, monkeypatch,
                                                           tmp_path):
        client, engine = env
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        _seed_usage(engine, 1, 1 * _MB)
        memory_id = _memory(engine)
        client.post(f"/api/memories/{memory_id}/photos",
                    files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
        assert list((tmp_path / "users" / "1" / "memories").glob("**/*.jpg")) == []

    def test_a_paid_plan_accepts_the_same_upload(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        _seed_usage(engine, 1, 1 * _MB)
        _subscribe(engine)
        memory_id = _memory(engine)
        res = client.post(f"/api/memories/{memory_id}/photos",
                          files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
        assert res.status_code == 201

    def test_self_hosted_never_refuses_an_upload(self, env, monkeypatch):
        client, engine = env
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")  # irrelevant without billing
        _seed_usage(engine, 1, 500 * _MB)
        memory_id = _memory(engine)
        res = client.post(f"/api/memories/{memory_id}/photos",
                          files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
        assert res.status_code == 201

    def test_a_companion_spends_the_owners_quota(self, env, monkeypatch):
        """Photos on a shared trip live in the owner's tree, so they are the
        owner's bytes — not the companion's."""
        client, engine = env
        _enforcing(monkeypatch)
        with Session(engine) as sess:
            sess.add(DBProjectMember(project_id=1, user_info_id=2,
                                     role="editor", invited_by=1))
            sess.commit()
        app.dependency_overrides[get_current_user] = lambda: {
            "sub": "2", "email": "mate@example.com"
        }
        memory_id = _memory(engine)
        res = client.post(f"/api/memories/{memory_id}/photos",
                          files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
        assert res.status_code == 201
        assert _usage(engine, 1) > 0
        assert _usage(engine, 2) == 0

    def test_a_full_owner_blocks_a_companions_upload(self, env, monkeypatch):
        client, engine = env
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        _seed_usage(engine, 1, 1 * _MB)
        with Session(engine) as sess:
            sess.add(DBProjectMember(project_id=1, user_info_id=2,
                                     role="editor", invited_by=1))
            sess.commit()
        app.dependency_overrides[get_current_user] = lambda: {
            "sub": "2", "email": "mate@example.com"
        }
        memory_id = _memory(engine)
        res = client.post(f"/api/memories/{memory_id}/photos",
                          files={"file": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
        assert res.status_code == 402


# ── A HEIC photo counts as the JPEG it is stored as (#33, U8-R2-1) ────────────

def _small_heic() -> bytes:
    """A noisy gradient saved as a low-quality HEIC: its stored JPEG, at
    quality 92, is several times larger than the HEIC itself."""
    rnd = random.Random(33)
    data = bytearray()
    for y in range(192):
        for x in range(192):
            n = rnd.randrange(-20, 21)
            data += bytes((max(0, min(255, x + n)), max(0, min(255, y + n)), 128 + n))
    buf = io.BytesIO()
    Image.frombytes("RGB", (192, 192), bytes(data)).save(buf, "HEIF", quality=30)
    return buf.getvalue()


def _journal(engine, project_id=1) -> int:
    with Session(engine) as sess:
        row = DBJournalEntry(project_id=project_id, date="2026-06-01",
                             geo_mode="custom", photos_json=json.dumps([]))
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return row.id


class TestHeicStorageQuota:
    @pytest.mark.parametrize("kind", ["memories", "journal"])
    def test_a_heic_that_fits_whose_jpeg_does_not_is_refused(
            self, env, monkeypatch, tmp_path, kind):
        client, engine = env
        monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
        heic = _small_heic()
        jpeg = prepare_photo(heic)
        assert len(jpeg) > len(heic) + 1      # the case this test is about
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        # Room for the HEIC, not for its JPEG.
        _seed_usage(engine, 1, 1 * _MB - len(heic) - 1)
        row_id = _memory(engine) if kind == "memories" else _journal(engine)

        res = client.post(f"/api/{kind}/{row_id}/photos",
                          files={"file": ("p.heic", heic, "image/heic")})

        assert res.status_code == 402
        assert res.json()["resource"] == "storage"
        assert list((tmp_path / "users" / "1" / kind).glob("**/*.jpg")) == []

    def test_a_heic_whose_jpeg_fits_is_stored(self, env, monkeypatch, tmp_path):
        client, engine = env
        heic = _small_heic()
        jpeg = prepare_photo(heic)
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        _seed_usage(engine, 1, 1 * _MB - len(jpeg))
        memory_id = _memory(engine)

        res = client.post(f"/api/memories/{memory_id}/photos",
                          files={"file": ("p.heic", heic, "image/heic")})

        assert res.status_code == 201, res.text
        stored = tmp_path / "users" / "1" / "memories" / str(memory_id) / f"{res.json()['uuid']}.jpg"
        assert stored.read_bytes() == jpeg

    # Avatars count against the trip owner's storage too (#470, U8-R3-1).

    def test_a_heic_avatar_that_fits_whose_jpeg_does_not_is_refused(
            self, env, monkeypatch, tmp_path):
        client, engine = env
        monkeypatch.setattr(people_mod, "_DATA_DIR", str(tmp_path))
        heic = _small_heic()
        jpeg = prepare_photo(heic)
        assert len(jpeg) > len(heic) + 1      # the case this test is about
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        # Room for the HEIC, not for its JPEG.
        _seed_usage(engine, 1, 1 * _MB - len(heic) - 1)
        person_id = _person(engine)

        res = client.post(f"/api/people/{person_id}/avatar",
                          files={"file": ("a.heic", heic, "image/heic")})

        assert res.status_code == 402
        assert res.json()["resource"] == "storage"
        assert not (tmp_path / "users" / "1" / "people").exists()
        assert _avatar(engine, person_id) is None

    def test_a_heic_avatar_whose_jpeg_fits_is_stored(self, env, monkeypatch, tmp_path):
        client, engine = env
        monkeypatch.setattr(people_mod, "_DATA_DIR", str(tmp_path))
        heic = _small_heic()
        jpeg = prepare_photo(heic)
        _enforcing(monkeypatch)
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        _seed_usage(engine, 1, 1 * _MB - len(jpeg))
        person_id = _person(engine)

        res = client.post(f"/api/people/{person_id}/avatar",
                          files={"file": ("a.heic", heic, "image/heic")})

        assert res.status_code == 201, res.text
        stored = tmp_path / "users" / "1" / "people" / str(person_id) / f"{_avatar(engine, person_id)}.jpg"
        assert stored.read_bytes() == jpeg


def _person(engine, project_id=1) -> int:
    with Session(engine) as sess:
        row = DBPerson(project_id=project_id, name="Alice")
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return row.id


def _avatar(engine, person_id):
    with Session(engine) as sess:
        return sess.get(DBPerson, person_id).avatar_photo

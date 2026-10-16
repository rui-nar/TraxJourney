"""Tests for the admin dashboard API (issue #25).

Covers gating, metrics, per-user isolation, recent-signup windowing, seeded
admin + forced change, ADMIN_EMAILS promotion, token/me fields, user search,
and the password reset (which leaves E2EE key material untouched).
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.admin as admin_mod
import models.db as db_module
import src.admin.storage as storage_mod
import src.utils.logging as logging_mod
from api.admin import router as admin_router
from api.deps import get_current_user
from models.billing import Subscription
from models.project_db import (
    DBActivity, DBDeviceKey, DBMemory, DBProject, DBRecoveryWrap,
)
from models.user import LocalUser, UserInfo
from src.billing.subscriptions import get_subscription


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def engine(monkeypatch, tmp_path):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    storage_mod.refresh_storage_cache()
    SQLModel.metadata.create_all(test_engine)
    yield test_engine
    storage_mod.refresh_storage_cache()


def _admin_app(engine, user_payload: dict) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: user_payload
    app.include_router(admin_router)
    return app


def _mk_user(sess, *, display_name, email, is_admin=False, provider="local",
             created_at=None, username=None):
    local = LocalUser(
        username=username or email or display_name,
        password_hash=LocalUser.hash_password("pw"),
    )
    sess.add(local)
    sess.commit()
    sess.refresh(local)
    ui = UserInfo(
        local_auth_id=local.id,
        display_name=display_name,
        email=email,
        auth_provider=provider,
        is_admin=is_admin,
    )
    if created_at is not None:
        ui.created_at = created_at
    sess.add(ui)
    sess.commit()
    sess.refresh(ui)
    return ui, local


def _encrypt(sess, user_info_id, *methods):
    """Turn on E2EE for a user the way /api/encryption/enable leaves it: the
    flag, one trusted device key, and one recovery wrap per method."""
    ui = sess.get(UserInfo, user_info_id)
    ui.encryption_enabled = True
    sess.add(ui)
    sess.add(DBDeviceKey(user_info_id=user_info_id, public_key="pk", approved=True,
                         wrapped_cmk="dev-wrap", ephemeral_public_key="epk"))
    for m in methods:
        sess.add(DBRecoveryWrap(user_info_id=user_info_id, method=m,
                                wrapped_cmk=f"{m}-wrap", salt="salt"))
    sess.commit()


@pytest.fixture
def admin_client(engine):
    with Session(engine) as sess:
        admin_ui, _ = _mk_user(sess, display_name="admin", email="admin@x.io",
                               is_admin=True)
        admin_id = admin_ui.id
    payload = {"sub": str(admin_id), "email": "admin@x.io", "auth_provider": "local"}
    return TestClient(_admin_app(engine, payload)), admin_id


# ── 1. Gating ─────────────────────────────────────────────────────────────────

_ADMIN_ROUTES = [
    ("get", "/api/admin/stats"),
    ("get", "/api/admin/storage/refresh"),
    ("get", "/api/admin/users/search?q=a"),
    ("post", "/api/admin/users/1/reset-password"),
    ("post", "/api/admin/users/1/set-admin"),
    ("delete", "/api/admin/users/999999"),
    ("post", "/api/admin/broadcast-email"),
    ("get", "/api/admin/log-level"),
    ("put", "/api/admin/log-level"),
    ("delete", "/api/admin/log-level"),
]


class TestGating:
    def _call(self, client, method, path):
        if method == "get":
            return client.get(path)
        if method == "delete":
            return client.delete(path)
        if "set-admin" in path:
            body = {"is_admin": True}
        elif "broadcast-email" in path:
            body = {"subject": "s", "body": "b", "send_to_all": True}
        elif "log-level" in path:
            body = {"level": "INFO"}
        else:
            body = {}
        if method == "put":
            return client.put(path, json=body)
        return client.post(path, json=body)

    @pytest.mark.parametrize("method,path", _ADMIN_ROUTES)
    def test_admin_reaches_route(self, admin_client, engine, method, path):
        client, _ = admin_client
        # Ensure the reset target exists so it's a 200/valid path, not a 404.
        with Session(engine) as sess:
            _mk_user(sess, display_name="target", email="t@x.io")
        resp = self._call(client, method, path)
        assert resp.status_code != 403 and resp.status_code != 401

    @pytest.mark.parametrize("method,path", _ADMIN_ROUTES)
    def test_non_admin_gets_403(self, engine, method, path):
        with Session(engine) as sess:
            ui, _ = _mk_user(sess, display_name="joe", email="joe@x.io")
            uid = ui.id
        payload = {"sub": str(uid), "email": "joe@x.io", "auth_provider": "local"}
        client = TestClient(_admin_app(engine, payload))
        resp = self._call(client, method, path)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path", _ADMIN_ROUTES)
    def test_unauthenticated_gets_401(self, engine, method, path):
        # No dependency override → real HTTPBearer runs and rejects.
        app = FastAPI()
        app.include_router(admin_router)
        client = TestClient(app)
        resp = self._call(client, method, path)
        assert resp.status_code in (401, 403)  # missing bearer → 403 from HTTPBearer

    def test_unauthenticated_stats_is_401_or_403(self, engine):
        app = FastAPI()
        app.include_router(admin_router)
        resp = TestClient(app).get("/api/admin/stats")
        # FastAPI HTTPBearer returns 403 when the header is absent.
        assert resp.status_code in (401, 403)


# ── 2 & 3. Totals + per-user breakdown + isolation ────────────────────────────

class TestStats:
    def _seed_two_users(self, engine, tmp_path_bytes=(0, 0)):
        with Session(engine) as sess:
            a, _ = _mk_user(sess, display_name="Alice", email="a@x.io", is_admin=True)
            b, _ = _mk_user(sess, display_name="Bob", email="b@x.io")
            aid, bid = a.id, b.id

            pa = DBProject(user_info_id=aid, name="A1")
            pa2 = DBProject(user_info_id=aid, name="A2")
            pb = DBProject(user_info_id=bid, name="B1")
            sess.add_all([pa, pa2, pb])
            sess.commit()
            sess.refresh(pa)
            sess.refresh(pb)

            sess.add(DBActivity(id=1, user_info_id=aid, name="ride"))
            sess.add(DBActivity(id=2, user_info_id=aid, name="run"))
            sess.add(DBActivity(id=3, user_info_id=bid, name="hike"))

            sess.add(DBMemory(project_id=pa.id, date="2025-01-01"))
            sess.add(DBMemory(project_id=pa.id, date="2025-01-02"))
            sess.add(DBMemory(project_id=pb.id, date="2025-01-03"))
            sess.commit()
        return aid, bid

    def test_totals(self, engine):
        aid, bid = self._seed_two_users(engine)
        payload = {"sub": str(aid), "email": "a@x.io", "auth_provider": "local"}
        client = TestClient(_admin_app(engine, payload))
        totals = client.get("/api/admin/stats").json()["totals"]
        assert totals["users"] == 2
        assert totals["projects"] == 3
        assert totals["activities"] == 3
        assert totals["memories"] == 3

    def test_per_user_breakdown_isolated(self, engine):
        aid, bid = self._seed_two_users(engine)
        payload = {"sub": str(aid), "email": "a@x.io", "auth_provider": "local"}
        client = TestClient(_admin_app(engine, payload))
        rows = {r["id"]: r for r in client.get("/api/admin/stats").json()["users"]}

        assert rows[aid]["project_count"] == 2
        assert rows[aid]["activity_count"] == 2
        assert rows[aid]["memory_count"] == 2
        # Bob's data never leaks into Alice's row.
        assert rows[bid]["project_count"] == 1
        assert rows[bid]["activity_count"] == 1
        assert rows[bid]["memory_count"] == 1
        # Neither user has encryption on.
        assert all(r["encryption_tier"] == "none" for r in rows.values())

    def test_per_user_tier_read_from_e2ee_rows(self, engine):
        aid, bid = self._seed_two_users(engine)
        with Session(engine) as sess:
            _encrypt(sess, bid, "qna")
        payload = {"sub": str(aid), "email": "a@x.io", "auth_provider": "local"}
        client = TestClient(_admin_app(engine, payload))
        rows = {r["id"]: r for r in client.get("/api/admin/stats").json()["users"]}
        assert rows[aid]["encryption_tier"] == "none"
        assert rows[bid]["encryption_tier"] == "medium"

    def test_no_content_fields_returned(self, engine):
        aid, _ = self._seed_two_users(engine)
        payload = {"sub": str(aid), "email": "a@x.io", "auth_provider": "local"}
        client = TestClient(_admin_app(engine, payload))
        body = client.get("/api/admin/stats").json()
        blob = str(body).lower()
        # No memory/journal text keys.
        for row in body["users"]:
            assert "description" not in row
            assert "photos" not in row
        assert "description" not in blob

    def test_storage_reflected_in_totals(self, engine, tmp_path):
        with Session(engine) as sess:
            a, _ = _mk_user(sess, display_name="Alice", email="a@x.io", is_admin=True)
            aid = a.id
        d = tmp_path / "users" / str(aid)
        d.mkdir(parents=True)
        (d / "blob").write_bytes(b"z" * 128)
        storage_mod.refresh_storage_cache()

        payload = {"sub": str(aid), "email": "a@x.io", "auth_provider": "local"}
        client = TestClient(_admin_app(engine, payload))
        body = client.get("/api/admin/stats").json()
        assert body["totals"]["storage_bytes"] == 128
        row = next(r for r in body["users"] if r["id"] == aid)
        assert row["storage_bytes"] == 128


# ── 4. Recent sign-ups windowing ──────────────────────────────────────────────

class TestRecentSignups:
    def test_windowing(self, engine):
        now = time.time()
        with Session(engine) as sess:
            _mk_user(sess, display_name="admin", email="admin@x.io", is_admin=True,
                     created_at=now)  # recent
            _mk_user(sess, display_name="fresh", email="f@x.io", created_at=now - 3600)
            _mk_user(sess, display_name="old", email="o@x.io",
                     created_at=now - 30 * 24 * 3600)  # 30 days ago
            admin = sess.exec(select(UserInfo).where(UserInfo.email == "admin@x.io")).first()
            aid = admin.id
        payload = {"sub": str(aid), "email": "admin@x.io", "auth_provider": "local"}
        client = TestClient(_admin_app(engine, payload))
        totals = client.get("/api/admin/stats").json()["totals"]
        # admin + fresh are within 7 days; old is not.
        assert totals["recent_signups_7d"] == 2


# ── 12. User search ───────────────────────────────────────────────────────────

class TestSearch:
    @pytest.fixture
    def client(self, engine):
        with Session(engine) as sess:
            admin, _ = _mk_user(sess, display_name="admin", email="admin@x.io",
                                is_admin=True, username="admin")
            _mk_user(sess, display_name="Charlie Brown", email="charlie@peanuts.io",
                     username="charlie")
            _mk_user(sess, display_name="Zed", email="zed@other.io", username="zeddy")
            aid = admin.id
        payload = {"sub": str(aid), "email": "admin@x.io", "auth_provider": "local"}
        return TestClient(_admin_app(engine, payload))

    def test_matches_email_case_insensitive(self, client):
        res = client.get("/api/admin/users/search?q=PEANUTS").json()
        assert any(r["email"] == "charlie@peanuts.io" for r in res)

    def test_matches_display_name(self, client):
        res = client.get("/api/admin/users/search?q=brown").json()
        assert any(r["display_name"] == "Charlie Brown" for r in res)

    def test_matches_username(self, client):
        res = client.get("/api/admin/users/search?q=zeddy").json()
        assert any(r["username"] == "zeddy" for r in res)

    def test_excludes_non_matches(self, client):
        res = client.get("/api/admin/users/search?q=charlie").json()
        assert all(r["display_name"] != "Zed" for r in res)

    def test_respects_limit(self, client):
        res = client.get("/api/admin/users/search?q=@&limit=1").json()
        assert len(res) == 1

    def test_empty_query_returns_empty(self, client):
        assert client.get("/api/admin/users/search?q=").json() == []

    def test_results_carry_tier(self, client):
        res = client.get("/api/admin/users/search?q=charlie").json()
        assert res[0]["encryption_tier"] == "none"

    def test_results_carry_real_tier(self, client, engine):
        with Session(engine) as sess:
            charlie = sess.exec(
                select(UserInfo).where(UserInfo.display_name == "Charlie Brown")).one()
            _encrypt(sess, charlie.id, "recovery_key")
        res = client.get("/api/admin/users/search?q=charlie").json()
        assert res[0]["encryption_tier"] == "high"


# ── 13/14/15. Password reset ──────────────────────────────────────────────────

class TestResetPassword:
    @pytest.fixture
    def ctx(self, engine):
        with Session(engine) as sess:
            admin, _ = _mk_user(sess, display_name="admin", email="admin@x.io",
                                is_admin=True)
            target, target_local = _mk_user(sess, display_name="Target",
                                            email="target@x.io", username="target@x.io")
            google, _ = _mk_user(sess, display_name="Goog", email="g@x.io",
                                 provider="google")
            # Google shadow account: empty password hash.
            g_local = sess.get(LocalUser, google.local_auth_id)
            g_local.password_hash = b""
            sess.add(g_local)
            sess.commit()
            aid, tid, gid = admin.id, target.id, google.id
        payload = {"sub": str(aid), "email": "admin@x.io", "auth_provider": "local"}
        return TestClient(_admin_app(engine, payload)), engine, tid, gid

    def test_reset_none_tier_sets_temp_and_forces_change(self, ctx):
        client, engine, tid, _ = ctx
        resp = client.post(f"/api/admin/users/{tid}/reset-password", json={})
        assert resp.status_code == 200
        temp = resp.json()["temp_password"]
        assert temp

        with Session(engine) as sess:
            ui = sess.get(UserInfo, tid)
            local = sess.get(LocalUser, ui.local_auth_id)
            assert local.password_change_required is True
            assert local.verify(temp)  # new hash matches the returned temp password

    # Every tier is resettable (#630): the password is not in the E2EE key
    # chain, so a reset leaves the device keys and recovery wraps untouched.
    @pytest.mark.parametrize("method, tier", [
        ("escrow", "low"),
        ("qna", "medium"),
        ("recovery_key", "high"),
        ("passphrase", "high"),
    ])
    def test_reset_encrypted_account_keeps_key_material(self, ctx, method, tier):
        client, engine, tid, _ = ctx
        with Session(engine) as sess:
            _encrypt(sess, tid, method)
        assert client.get("/api/admin/users/search?q=target").json()[0][
            "encryption_tier"] == tier

        resp = client.post(f"/api/admin/users/{tid}/reset-password", json={})
        assert resp.status_code == 200
        temp = resp.json()["temp_password"]

        with Session(engine) as sess:
            ui = sess.get(UserInfo, tid)
            assert ui.encryption_enabled is True
            local = sess.get(LocalUser, ui.local_auth_id)
            assert local.verify(temp)
            assert local.password_change_required is True
            wraps = sess.exec(select(DBRecoveryWrap).where(
                DBRecoveryWrap.user_info_id == tid)).all()
            assert [(w.method, w.wrapped_cmk) for w in wraps] == [
                (method, f"{method}-wrap")]
            dev = sess.exec(select(DBDeviceKey).where(
                DBDeviceKey.user_info_id == tid)).one()
            assert (dev.approved, dev.wrapped_cmk) == (True, "dev-wrap")

    def test_reset_google_user_409(self, ctx):
        client, _, _, gid = ctx
        resp = client.post(f"/api/admin/users/{gid}/reset-password", json={})
        assert resp.status_code == 409

    def test_reset_unknown_user_404(self, ctx):
        client, *_ = ctx
        resp = client.post("/api/admin/users/999999/reset-password", json={})
        assert resp.status_code == 404


# ── 16. Grant / revoke admin ──────────────────────────────────────────────────

class TestSetAdmin:
    @pytest.fixture
    def ctx(self, engine):
        with Session(engine) as sess:
            admin, _ = _mk_user(sess, display_name="admin", email="admin@x.io",
                                is_admin=True)
            other_admin, _ = _mk_user(sess, display_name="Other admin",
                                       email="other-admin@x.io", is_admin=True)
            plain, _ = _mk_user(sess, display_name="Plain", email="plain@x.io")
            aid, oid, pid = admin.id, other_admin.id, plain.id
        payload = {"sub": str(aid), "email": "admin@x.io", "auth_provider": "local"}
        return TestClient(_admin_app(engine, payload)), engine, aid, oid, pid

    def test_grant_admin(self, ctx):
        client, engine, _, _, pid = ctx
        resp = client.post(f"/api/admin/users/{pid}/set-admin", json={"is_admin": True})
        assert resp.status_code == 200
        with Session(engine) as sess:
            assert sess.get(UserInfo, pid).is_admin is True

    def test_revoke_other_admin(self, ctx):
        client, engine, _, oid, _ = ctx
        resp = client.post(f"/api/admin/users/{oid}/set-admin", json={"is_admin": False})
        assert resp.status_code == 200
        with Session(engine) as sess:
            assert sess.get(UserInfo, oid).is_admin is False

    def test_cannot_revoke_own_admin(self, ctx):
        client, engine, aid, _, _ = ctx
        resp = client.post(f"/api/admin/users/{aid}/set-admin", json={"is_admin": False})
        assert resp.status_code == 409
        with Session(engine) as sess:
            assert sess.get(UserInfo, aid).is_admin is True

    def test_granting_own_admin_is_a_noop_not_blocked(self, ctx):
        client, _, aid, _, _ = ctx
        resp = client.post(f"/api/admin/users/{aid}/set-admin", json={"is_admin": True})
        assert resp.status_code == 200

    def test_set_admin_unknown_user_404(self, ctx):
        client, *_ = ctx
        resp = client.post("/api/admin/users/999999/set-admin", json={"is_admin": True})
        assert resp.status_code == 404

    def test_search_results_carry_is_admin(self, ctx):
        client, _, aid, oid, pid = ctx
        res = client.get("/api/admin/users/search?q=plain").json()
        assert res[0]["is_admin"] is False
        res = client.get("/api/admin/users/search?q=other-admin").json()
        assert res[0]["is_admin"] is True


# ── 17. Delete user ────────────────────────────────────────────────────────────

class TestDeleteUser:
    @pytest.fixture
    def ctx(self, engine):
        with Session(engine) as sess:
            admin, _ = _mk_user(sess, display_name="admin", email="admin@x.io",
                                is_admin=True)
            target, _ = _mk_user(sess, display_name="Target", email="target@x.io")
            tid = target.id
            sess.add(DBProject(user_info_id=tid, name="Trip"))
            sess.commit()
            proj = sess.exec(select(DBProject).where(DBProject.user_info_id == tid)).first()
            sess.add(DBMemory(project_id=proj.id, date="2025-01-01"))
            sess.add(DBActivity(id=42, user_info_id=tid, name="ride"))
            sess.commit()
            aid = admin.id
        payload = {"sub": str(aid), "email": "admin@x.io", "auth_provider": "local"}
        return TestClient(_admin_app(engine, payload)), engine, aid, tid

    def test_delete_removes_user_and_owned_data(self, ctx):
        client, engine, _, tid = ctx
        resp = client.delete(f"/api/admin/users/{tid}")
        assert resp.status_code == 200
        with Session(engine) as sess:
            assert sess.get(UserInfo, tid) is None
            assert sess.exec(select(DBProject).where(DBProject.user_info_id == tid)).first() is None
            assert sess.exec(select(DBMemory)).first() is None  # target was the only owner
            assert sess.exec(select(DBActivity).where(DBActivity.user_info_id == tid)).first() is None

    def test_delete_purges_storage(self, ctx, tmp_path):
        client, _, _, tid = ctx
        d = tmp_path / "users" / str(tid)
        d.mkdir(parents=True)
        (d / "photo.jpg").write_bytes(b"x")
        client.delete(f"/api/admin/users/{tid}")
        assert not d.exists()

    def test_cannot_delete_self(self, ctx):
        client, engine, aid, _ = ctx
        resp = client.delete(f"/api/admin/users/{aid}")
        assert resp.status_code == 409
        with Session(engine) as sess:
            assert sess.get(UserInfo, aid) is not None

    def test_delete_unknown_user_404(self, ctx):
        client, *_ = ctx
        resp = client.delete("/api/admin/users/999999")
        assert resp.status_code == 404


# ── 18. Broadcast email ────────────────────────────────────────────────────────
#
# FastAPI's TestClient runs BackgroundTasks inline before the response
# returns, so these assert on the fake synchronously — no real network (same
# approach as the invite-email tests in tests/test_companion_roles.py).

class _FakeEmailService:
    def __init__(self):
        self.sent: list = []

    async def send(self, message) -> None:
        self.sent.append(message)


@pytest.fixture
def fake_email(monkeypatch):
    fake = _FakeEmailService()
    monkeypatch.setattr(admin_mod, "get_email_service", lambda: fake)
    return fake


class TestBroadcastEmail:
    @pytest.fixture
    def ctx(self, engine):
        with Session(engine) as sess:
            admin, _ = _mk_user(sess, display_name="admin", email="admin@x.io",
                                is_admin=True)
            a, _ = _mk_user(sess, display_name="Alice", email="a@x.io")
            b, _ = _mk_user(sess, display_name="Bob", email="b@x.io")
            aid, alice_id, bob_id = admin.id, a.id, b.id
        payload = {"sub": str(aid), "email": "admin@x.io", "auth_provider": "local"}
        return TestClient(_admin_app(engine, payload)), alice_id, bob_id

    def test_send_to_selected_users(self, ctx, fake_email):
        client, alice_id, _bob_id = ctx
        resp = client.post("/api/admin/broadcast-email", json={
            "subject": "Hello",
            "body": "Test message",
            "user_ids": [alice_id],
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["sent_count"] == 1
        assert len(fake_email.sent) == 1
        msg = fake_email.sent[0]
        assert msg.to == "a@x.io"
        assert msg.subject == "Hello"
        assert msg.text_body == "Test message"
        assert "Test message" in msg.html_body

    def test_send_to_all_ignores_user_ids(self, ctx, fake_email):
        client, alice_id, _bob_id = ctx
        resp = client.post("/api/admin/broadcast-email", json={
            "subject": "Hello",
            "body": "Test message",
            "user_ids": [],
            "send_to_all": True,
        })
        assert resp.status_code == 200, resp.text
        # admin + alice + bob, all seeded with emails.
        assert resp.json()["sent_count"] == 3
        assert {m.to for m in fake_email.sent} == {"admin@x.io", "a@x.io", "b@x.io"}

    def test_no_recipients_selected_422(self, ctx, fake_email):
        client, *_ = ctx
        resp = client.post("/api/admin/broadcast-email", json={
            "subject": "Hello",
            "body": "Test message",
            "user_ids": [],
        })
        assert resp.status_code == 422
        assert fake_email.sent == []

    def test_html_body_escapes_and_preserves_newlines(self, ctx, fake_email):
        client, alice_id, _bob_id = ctx
        client.post("/api/admin/broadcast-email", json={
            "subject": "Hi",
            "body": "line1 <b>\nline2",
            "user_ids": [alice_id],
        })
        html_body = fake_email.sent[0].html_body
        assert "&lt;b&gt;" in html_body
        assert "<b>" not in html_body
        assert "<br>" in html_body


# ── 19. Plan override (issue #121) ─────────────────────────────────────────────

class TestSetPlan:
    @pytest.fixture
    def ctx(self, engine):
        with Session(engine) as sess:
            admin, _ = _mk_user(sess, display_name="admin", email="admin@x.io",
                                is_admin=True)
            plain, _ = _mk_user(sess, display_name="Plain", email="plain@x.io")
            aid, pid = admin.id, plain.id
        payload = {"sub": str(aid), "email": "admin@x.io", "auth_provider": "local"}
        return TestClient(_admin_app(engine, payload)), engine, pid

    def test_comp_an_account(self, ctx):
        client, engine, pid = ctx
        resp = client.put(f"/api/admin/users/{pid}/plan", json={"plan": "tier_2"})
        assert resp.status_code == 200
        with Session(engine) as sess:
            assert get_subscription(sess, pid).admin_override_plan == "tier_2"

    def test_clear_the_comp(self, ctx):
        client, engine, pid = ctx
        client.put(f"/api/admin/users/{pid}/plan", json={"plan": "tier_2"})
        client.put(f"/api/admin/users/{pid}/plan", json={"plan": ""})
        with Session(engine) as sess:
            assert get_subscription(sess, pid).admin_override_plan == ""

    def test_unknown_plan_is_rejected(self, ctx):
        client, _, pid = ctx
        resp = client.put(f"/api/admin/users/{pid}/plan", json={"plan": "enterprise"})
        assert resp.status_code == 422

    def test_unknown_user_404(self, ctx):
        client, *_ = ctx
        resp = client.put("/api/admin/users/999999/plan", json={"plan": "tier_1"})
        assert resp.status_code == 404

    def test_requires_admin(self, engine):
        with Session(engine) as sess:
            plain, _ = _mk_user(sess, display_name="Plain", email="plain@x.io")
            pid = plain.id
        payload = {"sub": str(pid), "email": "plain@x.io", "auth_provider": "local"}
        client = TestClient(_admin_app(engine, payload))
        resp = client.put(f"/api/admin/users/{pid}/plan", json={"plan": "tier_1"})
        assert resp.status_code == 403


# ── 20. Billing plan fields on stats/search (issue #194) ───────────────────────

class TestBillingFields:
    @pytest.fixture
    def ctx(self, engine):
        with Session(engine) as sess:
            admin, _ = _mk_user(sess, display_name="admin", email="admin@x.io",
                                is_admin=True)
            free, _ = _mk_user(sess, display_name="Free", email="free@x.io")
            comped, _ = _mk_user(sess, display_name="Comped", email="comped@x.io")
            paying, _ = _mk_user(sess, display_name="Paying", email="paying@x.io")
            aid, fid, cid, pid = admin.id, free.id, comped.id, paying.id

            # Comped: an override plan, never actually checked out.
            sess.add(Subscription(
                user_info_id=cid, plan="free", status="none",
                admin_override_plan="tier_2",
            ))
            # Real paying subscriber with a Stripe customer id.
            sess.add(Subscription(
                user_info_id=pid, plan="tier_1", status="active",
                provider_customer_id="cus_123",
            ))
            sess.commit()
        payload = {"sub": str(aid), "email": "admin@x.io", "auth_provider": "local"}
        return TestClient(_admin_app(engine, payload)), fid, cid, pid

    def test_free_user_has_no_subscription_row(self, ctx):
        client, fid, _, _ = ctx
        rows = {r["id"]: r for r in client.get("/api/admin/stats").json()["users"]}
        row = rows[fid]
        assert row["plan"] == "free"
        assert row["plan_name"] == "Free"
        assert row["is_comped"] is False
        assert row["subscription_status"] == "none"
        assert row["stripe_customer_url"] is None

    def test_comped_user_is_flagged_with_no_stripe_link(self, ctx):
        client, _, cid, _ = ctx
        rows = {r["id"]: r for r in client.get("/api/admin/stats").json()["users"]}
        row = rows[cid]
        assert row["plan"] == "tier_2"
        assert row["is_comped"] is True
        assert row["stripe_customer_url"] is None  # comped-only, never checked out

    def test_paying_user_has_a_stripe_link(self, ctx, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
        client, _, _, pid = ctx
        rows = {r["id"]: r for r in client.get("/api/admin/stats").json()["users"]}
        row = rows[pid]
        assert row["plan"] == "tier_1"
        assert row["is_comped"] is False
        assert row["subscription_status"] == "active"
        assert row["stripe_customer_url"] == \
            "https://dashboard.stripe.com/test/customers/cus_123"

    def test_search_results_carry_the_same_plan_fields(self, ctx, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
        client, *_ = ctx
        res = client.get("/api/admin/users/search?q=paying").json()
        assert res[0]["plan"] == "tier_1"
        assert res[0]["stripe_customer_url"] == \
            "https://dashboard.stripe.com/test/customers/cus_123"


class TestStripeCustomerUrl:
    """Unit tests for the pure URL-building helper (no DB, no HTTP)."""

    def test_empty_customer_id_is_none(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
        assert admin_mod._stripe_customer_url("") is None

    def test_test_key_uses_the_test_dashboard(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
        assert admin_mod._stripe_customer_url("cus_1") == \
            "https://dashboard.stripe.com/test/customers/cus_1"

    def test_live_key_uses_the_live_dashboard(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
        assert admin_mod._stripe_customer_url("cus_1") == \
            "https://dashboard.stripe.com/customers/cus_1"

    def test_missing_key_defaults_to_the_test_dashboard(self, monkeypatch):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        assert admin_mod._stripe_customer_url("cus_1") == \
            "https://dashboard.stripe.com/test/customers/cus_1"


# ── 8. Live log-level override (issue #208) ────────────────────────────────────

class TestLogLevel:
    """GET/PUT/DELETE /api/admin/log-level. The endpoints share the real,
    process-wide AsyncIOScheduler singleton in api.router (same one the app's
    backup/WAL/usage-reconcile jobs use), so each test resets both that
    scheduler's revert job and the logging module's override state rather
    than relying on test order."""

    @pytest.fixture(autouse=True)
    def _isolate(self):
        from api.router import _scheduler as scheduler

        def _reset():
            logging_mod.clear_level_override()
            if scheduler.get_job(admin_mod._LOG_LEVEL_REVERT_JOB_ID):
                scheduler.remove_job(admin_mod._LOG_LEVEL_REVERT_JOB_ID)

        _reset()
        yield
        _reset()

    def test_get_reflects_the_env_default(self, admin_client):
        client, _ = admin_client
        resp = client.get("/api/admin/log-level")
        assert resp.status_code == 200
        body = resp.json()
        assert body["effective_level"] == "INFO"
        assert body["source"] == "env"
        assert body["override_level"] is None

    def test_put_applies_a_timed_override(self, admin_client):
        client, _ = admin_client
        resp = client.put(
            "/api/admin/log-level", json={"level": "debug", "duration_minutes": 15}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["effective_level"] == "DEBUG"
        assert body["source"] == "override"
        assert body["override_expires_at"] is not None

        # GET reflects the same live state (process-memory, not re-derived).
        assert client.get("/api/admin/log-level").json()["source"] == "override"

    def test_put_applies_an_indefinite_override(self, admin_client):
        client, _ = admin_client
        resp = client.put("/api/admin/log-level", json={"level": "ERROR"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["override_level"] == "ERROR"
        assert body["override_expires_at"] is None

    def test_put_schedules_an_auto_revert_job(self, admin_client):
        from api.router import _scheduler as scheduler

        client, _ = admin_client
        client.put(
            "/api/admin/log-level", json={"level": "DEBUG", "duration_minutes": 5}
        )
        assert scheduler.get_job(admin_mod._LOG_LEVEL_REVERT_JOB_ID) is not None

    def test_put_indefinite_clears_a_pending_revert_job(self, admin_client):
        """An indefinite override supersedes an earlier timed one — no
        leftover revert should fire and undo it."""
        from api.router import _scheduler as scheduler

        client, _ = admin_client
        client.put(
            "/api/admin/log-level", json={"level": "DEBUG", "duration_minutes": 5}
        )
        assert scheduler.get_job(admin_mod._LOG_LEVEL_REVERT_JOB_ID) is not None

        client.put("/api/admin/log-level", json={"level": "DEBUG"})
        assert scheduler.get_job(admin_mod._LOG_LEVEL_REVERT_JOB_ID) is None

    def test_put_rejects_an_unknown_level(self, admin_client):
        client, _ = admin_client
        resp = client.put("/api/admin/log-level", json={"level": "TRACE"})
        assert resp.status_code == 422
        assert logging_mod.current_level_info().source == "env"

    @pytest.mark.parametrize("duration_minutes", [0, -5, 1441])
    def test_put_rejects_an_out_of_range_duration(self, admin_client, duration_minutes):
        client, _ = admin_client
        resp = client.put(
            "/api/admin/log-level",
            json={"level": "DEBUG", "duration_minutes": duration_minutes},
        )
        assert resp.status_code == 422
        assert logging_mod.current_level_info().source == "env"

    def test_delete_reverts_to_the_env_baseline(self, admin_client):
        client, _ = admin_client
        client.put("/api/admin/log-level", json={"level": "DEBUG"})

        resp = client.delete("/api/admin/log-level")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "env"
        assert body["effective_level"] == "INFO"
        assert logging_mod.current_level_info().source == "env"

    def test_delete_clears_a_pending_revert_job(self, admin_client):
        from api.router import _scheduler as scheduler

        client, _ = admin_client
        client.put(
            "/api/admin/log-level", json={"level": "DEBUG", "duration_minutes": 5}
        )
        assert scheduler.get_job(admin_mod._LOG_LEVEL_REVERT_JOB_ID) is not None

        client.delete("/api/admin/log-level")
        assert scheduler.get_job(admin_mod._LOG_LEVEL_REVERT_JOB_ID) is None

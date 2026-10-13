"""Deleting an account revokes its Apple token (#580, plan U3).

Apple requires an app offering Sign in with Apple to revoke the user's token
when the account is deleted. The revoke runs next to the Strava one: after
the billing steps, so a refused deletion leaves Apple untouched, and best
effort, so the ``appletoken`` row goes whatever Apple answered. It uses the
``client_id`` stored with the token, the one Apple issued it to (D5). Logs
never carry the token (#510).

Drives the real ``api.router.app`` for the endpoint tests, so both deletion
routes and the ``AccountDeletionRefused`` handler are the production ones.
"""
from __future__ import annotations

import logging

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import src.admin.storage as storage_mod
import src.auth.apple as apple
from api.deps import get_current_user
from api.router import app
from models.billing import Subscription
from models.user import AppleToken, LocalUser, UserInfo
from src.auth.account_deletion import delete_user_and_data
from src.billing.gateway import set_gateway

REFRESH = "apple-refresh-secret"
SERVICES_ID = "com.traxjourney.signin"
TEAM_ID = "TEAM123456"


@pytest.fixture
def engine(monkeypatch, tmp_path):
    eng = create_engine(
        f"sqlite:///{(tmp_path / 'deletion.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_module, "engine", eng)
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path / "data"))
    SQLModel.metadata.create_all(eng)
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield eng
    app.dependency_overrides.clear()
    set_gateway(None)
    eng.dispose()


def _pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("APPLE_TEAM_ID", TEAM_ID)
    monkeypatch.setenv("APPLE_SIGNIN_KEY_ID", "KEY1234567")
    monkeypatch.setenv("APPLE_SIGNIN_PRIVATE_KEY", _pem())


@pytest.fixture
def unconfigured(monkeypatch):
    for var in ("APPLE_TEAM_ID", "APPLE_SIGNIN_KEY_ID", "APPLE_SIGNIN_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)


class RevokeSpy:
    """Stands in for ``apple.revoke`` and records what it was given."""

    def __init__(self, monkeypatch, answer=True):
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(apple, "revoke", self)

    def __call__(self, refresh_token, client_id, secret):
        self.calls.append((refresh_token, client_id, secret))
        return self.answer


@pytest.fixture
def revoke(monkeypatch):
    return RevokeSpy(monkeypatch)


def _user(engine, *, email="apple@x.io", admin=False, token=True) -> int:
    with Session(engine) as sess:
        local = LocalUser(username=f"apple_{email}")
        sess.add(local)
        sess.commit()
        ui = UserInfo(local_auth_id=local.id, email=email, auth_provider="apple",
                      is_admin=admin)
        sess.add(ui)
        sess.commit()
        if token:
            sess.add(AppleToken(user_info_id=ui.id, refresh_token=REFRESH,
                                client_id=SERVICES_ID))
            sess.commit()
        return ui.id


def _apple_rows(engine, uid) -> int:
    with Session(engine) as sess:
        return len(sess.exec(
            select(AppleToken).where(AppleToken.user_info_id == uid)).all())


def _as(uid) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uid), "email": "u@x.io", "auth_provider": "local",
    }
    return TestClient(app)


class TestRevoke:
    def test_the_stored_token_is_revoked_under_its_client_id(
        self, engine, configured, revoke,
    ):
        uid = _user(engine)

        with Session(engine) as sess:
            delete_user_and_data(sess, uid)

        assert len(revoke.calls) == 1
        token, client_id, secret = revoke.calls[0]
        assert (token, client_id) == (REFRESH, SERVICES_ID)
        # The client secret was minted for that same client_id (D5).
        claims = jwt.decode(secret, options={"verify_signature": False})
        assert (claims["iss"], claims["sub"]) == (TEAM_ID, SERVICES_ID)

    def test_the_row_goes_after_a_successful_revoke(self, engine, configured, revoke):
        uid = _user(engine)

        with Session(engine) as sess:
            delete_user_and_data(sess, uid)

        assert _apple_rows(engine, uid) == 0
        with Session(engine) as sess:
            assert sess.get(UserInfo, uid) is None

    @pytest.mark.parametrize("failure", [
        requests.ConnectionError(f"boom {REFRESH}"),
        500,
        400,
    ])
    def test_a_failed_revoke_still_deletes_and_never_logs_the_token(
        self, engine, configured, monkeypatch, caplog, failure,
    ):
        """The real ``revoke``, with Apple failing underneath it."""
        def post(url, data=None, **kwargs):
            if isinstance(failure, Exception):
                raise failure
            resp = requests.Response()
            resp.status_code = failure
            resp._content = f'{{"error": "invalid_client", "token": "{REFRESH}"}}'.encode()
            return resp

        monkeypatch.setattr(apple.requests, "post", post)
        uid = _user(engine)

        with caplog.at_level(logging.WARNING):
            with Session(engine) as sess:
                delete_user_and_data(sess, uid)

        assert "Apple revoke failed" in caplog.text
        assert "reason=revoke_failed" in caplog.text
        assert REFRESH not in caplog.text
        assert _apple_rows(engine, uid) == 0
        with Session(engine) as sess:
            assert sess.get(UserInfo, uid) is None

    def test_apple_not_configured_is_logged_and_the_row_still_goes(
        self, engine, unconfigured, revoke, caplog,
    ):
        uid = _user(engine)

        with caplog.at_level(logging.WARNING):
            with Session(engine) as sess:
                delete_user_and_data(sess, uid)

        assert revoke.calls == []
        assert "reason=not_configured" in caplog.text
        assert REFRESH not in caplog.text
        assert _apple_rows(engine, uid) == 0

    def test_a_key_that_cannot_sign_is_logged_and_the_row_still_goes(
        self, engine, monkeypatch, revoke, caplog,
    ):
        monkeypatch.setenv("APPLE_TEAM_ID", TEAM_ID)
        monkeypatch.setenv("APPLE_SIGNIN_KEY_ID", "KEY1234567")
        monkeypatch.setenv("APPLE_SIGNIN_PRIVATE_KEY", "not a key")
        uid = _user(engine)

        with caplog.at_level(logging.WARNING):
            with Session(engine) as sess:
                delete_user_and_data(sess, uid)

        assert revoke.calls == []
        assert "reason=client_secret" in caplog.text
        assert REFRESH not in caplog.text
        assert _apple_rows(engine, uid) == 0
        with Session(engine) as sess:
            assert sess.get(UserInfo, uid) is None

    def test_no_apple_token_means_no_call(self, engine, configured, revoke):
        uid = _user(engine, token=False)

        with Session(engine) as sess:
            delete_user_and_data(sess, uid)

        assert revoke.calls == []

    def test_another_accounts_token_is_neither_revoked_nor_deleted(
        self, engine, configured, revoke,
    ):
        uid = _user(engine)
        other = _user(engine, email="other@x.io")

        with Session(engine) as sess:
            delete_user_and_data(sess, uid)

        assert len(revoke.calls) == 1
        assert _apple_rows(engine, other) == 1


class TestEndpoints:
    def test_self_delete_revokes(self, engine, configured, revoke):
        uid = _user(engine)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 200, res.text
        assert [c[:2] for c in revoke.calls] == [(REFRESH, SERVICES_ID)]
        assert _apple_rows(engine, uid) == 0

    def test_admin_delete_revokes(self, engine, configured, revoke):
        admin = _user(engine, email="admin@x.io", admin=True, token=False)
        uid = _user(engine)

        res = _as(admin).delete(f"/api/admin/users/{uid}")

        assert res.status_code == 200, res.text
        assert [c[:2] for c in revoke.calls] == [(REFRESH, SERVICES_ID)]
        assert _apple_rows(engine, uid) == 0
        with Session(engine) as sess:
            assert sess.get(UserInfo, uid) is None

    def test_a_deletion_refused_by_billing_does_not_revoke(
        self, engine, configured, revoke,
    ):
        """A live subscription and no gateway to cancel it: the deletion is
        refused, so the account keeps its Apple sign-in."""
        uid = _user(engine)
        with Session(engine) as sess:
            sess.add(Subscription(user_info_id=uid, plan="tier_2", status="active",
                                  provider_customer_id="cus_1",
                                  provider_subscription_id="sub_live"))
            sess.commit()
        set_gateway(None)

        res = _as(uid).delete("/api/auth/me")

        assert res.status_code == 409
        assert res.json()["code"] == "billing_unavailable"
        assert revoke.calls == []
        assert _apple_rows(engine, uid) == 1

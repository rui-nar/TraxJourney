"""Tests for Sign in with Apple: POST /api/auth/apple and its callback (#580).

The Apple library (src/auth/apple.py, tested in test_apple_identity.py) is
patched at its function boundary: these tests cover what the route does with
its answers. Accounts are keyed by the Apple ``sub`` and never linked by
e-mail (D2); the code is exchanged on every sign-in and a failed exchange
refuses the sign-in, so no account exists without its refresh token (D3);
the exchange and the stored token use the token's ``aud`` (D5). The callback
relays only the fields the Android app reads, to the app and nowhere else.
Logs never carry a token, a code or a nonce (#510).
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, text
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import src.auth.apple as apple
from api.auth import router as auth_router
from models.user import AppleToken, LocalUser, UserInfo

BUNDLE_ID = "com.traxjourney.app"
SERVICES_ID = "com.traxjourney.signin"
REDIRECT_URI = "https://traxjourney.test/api/auth/apple/callback"
SUB = "001234.0123456789abcdef0123456789abcdef.0042"
RELAY = "k3x9q2@privaterelay.appleid.com"
INTENT_PREFIX = "intent://callback?"
INTENT_SUFFIX = "#Intent;package=com.traxjourney.app;scheme=signinwithapple;end"
# The real exchange, kept before any test patches it, for the log test.
_REAL_EXCHANGE_CODE = apple.exchange_code


@pytest.fixture
def engine(tmp_path, monkeypatch):
    eng = create_engine(
        f"sqlite:///{(tmp_path / 'apple.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db_module, "engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("APPLE_SIGNIN_KEY_ID", "KEY1234567")
    monkeypatch.setenv("APPLE_SIGNIN_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----")
    monkeypatch.setenv("APPLE_BUNDLE_ID", BUNDLE_ID)
    monkeypatch.setenv("APPLE_SERVICES_ID", SERVICES_ID)
    monkeypatch.setenv("APPLE_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)


@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(auth_router)
    return TestClient(app)


class FakeApple:
    """Stands in for src.auth.apple's network functions and records calls."""

    def __init__(self, monkeypatch):
        self.identity = _identity()
        self.verify_error: Exception | None = None
        self.exchange_error: Exception | None = None
        self.refresh_token = "refresh-1"
        self.verify_calls: list[tuple] = []
        self.secret_calls: list[tuple] = []
        self.exchange_calls: list[tuple] = []
        monkeypatch.setattr(apple, "verify_identity_token", self._verify)
        monkeypatch.setattr(apple, "client_secret", self._secret)
        monkeypatch.setattr(apple, "exchange_code", self._exchange)

    def _verify(self, token, nonce, audiences):
        self.verify_calls.append((token, nonce, tuple(audiences)))
        if self.verify_error:
            raise self.verify_error
        return self.identity

    def _secret(self, team_id, key_id, private_key_pem, client_id, now=None):
        self.secret_calls.append((team_id, key_id, client_id))
        return "client-secret-jwt"

    def _exchange(self, code, client_id, secret, redirect_uri):
        self.exchange_calls.append((code, client_id, secret, redirect_uri))
        if self.exchange_error:
            raise self.exchange_error
        return self.refresh_token


def _identity(**overrides) -> apple.AppleIdentity:
    values = dict(sub=SUB, email=RELAY, email_verified=True,
                  is_private_email=True, aud=BUNDLE_ID)
    values.update(overrides)
    return apple.AppleIdentity(**values)


@pytest.fixture
def fake(monkeypatch, configured):
    return FakeApple(monkeypatch)


def _body(**overrides) -> dict:
    body = {"id_token": "id-token", "authorization_code": "auth-code", "nonce": "raw-nonce",
            "given_name": None, "family_name": None}
    body.update(overrides)
    return body


def _counts(engine) -> tuple[int, int, int]:
    with Session(engine) as sess:
        return tuple(
            sess.exec(select(func.count()).select_from(model)).one()
            for model in (LocalUser, UserInfo, AppleToken)
        )


# ── Configuration and failures ────────────────────────────────────────────────

@pytest.mark.parametrize("missing", ["APPLE_TEAM_ID", "APPLE_SIGNIN_KEY_ID", "APPLE_SIGNIN_PRIVATE_KEY"])
def test_unconfigured_returns_503(client, fake, monkeypatch, engine, missing):
    monkeypatch.delenv(missing)

    resp = client.post("/api/auth/apple", json=_body())

    assert resp.status_code == 503
    assert resp.json() == {"detail": "Apple auth not configured"}
    assert fake.verify_calls == []
    assert _counts(engine) == (0, 0, 0)


def test_invalid_token_returns_401_without_exchanging(client, fake, engine, metric):
    fake.verify_error = apple.AppleTokenInvalid("nonce")
    before = metric("traxjourney_logins_total", provider="apple", result="failure")

    resp = client.post("/api/auth/apple", json=_body())

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid Apple id_token"}
    assert fake.exchange_calls == []
    assert _counts(engine) == (0, 0, 0)
    assert metric("traxjourney_logins_total", provider="apple", result="failure") == before + 1


def test_rejected_code_returns_401_and_creates_no_account(client, fake, engine):
    fake.exchange_error = apple.AppleTokenInvalid("code_rejected")

    resp = client.post("/api/auth/apple", json=_body(given_name="Ada"))

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid Apple id_token"}
    assert _counts(engine) == (0, 0, 0)


@pytest.mark.parametrize("stage", ["verify", "exchange"])
def test_apple_unavailable_returns_502_and_creates_no_account(client, fake, engine, stage):
    if stage == "verify":
        fake.verify_error = apple.AppleUnavailable("jwks")
    else:
        fake.exchange_error = apple.AppleUnavailable("http")

    resp = client.post("/api/auth/apple", json=_body(given_name="Ada"))

    assert resp.status_code == 502
    assert resp.json() == {"detail": "Apple sign-in unavailable"}
    assert _counts(engine) == (0, 0, 0)


def test_rejected_code_leaves_an_existing_token_untouched(client, fake, engine):
    assert client.post("/api/auth/apple", json=_body()).status_code == 200
    fake.refresh_token = "refresh-2"
    fake.exchange_error = apple.AppleTokenInvalid("code_rejected")

    assert client.post("/api/auth/apple", json=_body()).status_code == 401

    with Session(engine) as sess:
        assert sess.exec(select(AppleToken)).one().refresh_token == "refresh-1"


def test_names_over_100_characters_are_refused(client, fake, engine):
    resp = client.post("/api/auth/apple", json=_body(given_name="A" * 101))

    assert resp.status_code == 422
    assert fake.verify_calls == []


# ── Audiences, code exchange ──────────────────────────────────────────────────

def test_verify_accepts_both_audiences(client, fake):
    client.post("/api/auth/apple", json=_body())

    assert fake.verify_calls == [("id-token", "raw-nonce", (BUNDLE_ID, SERVICES_ID))]


def test_verify_leaves_an_unset_services_id_out(client, fake, monkeypatch):
    monkeypatch.delenv("APPLE_SERVICES_ID")

    client.post("/api/auth/apple", json=_body())

    assert fake.verify_calls[0][2] == (BUNDLE_ID,)


def test_services_id_token_exchanges_with_the_redirect_uri(client, fake, engine):
    fake.identity = _identity(aud=SERVICES_ID)

    assert client.post("/api/auth/apple", json=_body()).status_code == 200

    assert fake.secret_calls == [("TEAM123456", "KEY1234567", SERVICES_ID)]
    assert fake.exchange_calls == [("auth-code", SERVICES_ID, "client-secret-jwt", REDIRECT_URI)]
    with Session(engine) as sess:
        assert sess.exec(select(AppleToken)).one().client_id == SERVICES_ID


def test_bundle_id_token_exchanges_without_a_redirect_uri(client, fake, engine):
    fake.identity = _identity(aud=BUNDLE_ID)

    assert client.post("/api/auth/apple", json=_body()).status_code == 200

    assert fake.secret_calls == [("TEAM123456", "KEY1234567", BUNDLE_ID)]
    assert fake.exchange_calls == [("auth-code", BUNDLE_ID, "client-secret-jwt", None)]
    with Session(engine) as sess:
        assert sess.exec(select(AppleToken)).one().client_id == BUNDLE_ID


def test_unusable_signing_key_returns_503_and_creates_no_account(client, fake, engine, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("Could not deserialize key data")
    monkeypatch.setattr(apple, "client_secret", broken)

    resp = client.post("/api/auth/apple", json=_body())

    assert resp.status_code == 503
    assert fake.exchange_calls == []
    assert _counts(engine) == (0, 0, 0)


# ── Accounts ──────────────────────────────────────────────────────────────────

def test_first_sign_in_creates_the_account(client, fake, engine, metric):
    before = metric("traxjourney_registrations_total", provider="apple")

    resp = client.post("/api/auth/apple", json=_body(given_name=" Ada ", family_name="Lovelace"))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["token_type"] == "bearer" and data["access_token"]
    user = data["user"]
    assert user["display_name"] == "Ada Lovelace"
    assert user["email"] == RELAY
    assert user["auth_provider"] == "apple"
    assert user["email_verified"] is True
    assert user["is_admin"] is False
    assert metric("traxjourney_registrations_total", provider="apple") == before + 1
    with Session(engine) as sess:
        info = sess.get(UserInfo, user["id"])
        assert info.apple_sub == SUB
        assert info.google_sub == ""
        shadow = sess.get(LocalUser, info.local_auth_id)
        assert shadow.password_hash == b"" and not shadow.verify("")
        token = sess.exec(select(AppleToken)).one()
        assert token.user_info_id == info.id
        assert token.refresh_token == "refresh-1"
        assert token.client_id == BUNDLE_ID
        # Encrypted at rest.
        raw = sess.exec(text("SELECT refresh_token FROM appletoken")).one()[0]
        assert raw and "refresh-1" not in raw


def test_first_sign_in_without_names_falls_back_to_the_email_local_part(client, fake):
    resp = client.post("/api/auth/apple", json=_body())

    assert resp.json()["user"]["display_name"] == "k3x9q2"


@pytest.mark.parametrize("verified", [True, False])
def test_email_verified_is_stored_as_a_real_boolean(client, fake, engine, verified):
    fake.identity = _identity(email_verified=verified)

    resp = client.post("/api/auth/apple", json=_body())

    assert resp.json()["user"]["email_verified"] is verified
    with Session(engine) as sess:
        assert sess.get(UserInfo, resp.json()["user"]["id"]).email_verified is verified
        raw = sess.exec(text("SELECT email_verified FROM userinfo")).one()[0]
        assert raw == (1 if verified else 0)


def test_second_sign_in_finds_the_account_ignores_names_and_updates_the_token(client, fake, engine, metric):
    first = client.post("/api/auth/apple", json=_body(given_name="Ada", family_name="Lovelace"))
    fake.identity = _identity(aud=SERVICES_ID)
    fake.refresh_token = "refresh-2"
    registrations = metric("traxjourney_registrations_total", provider="apple")
    logins = metric("traxjourney_logins_total", provider="apple", result="success")

    second = client.post("/api/auth/apple", json=_body(given_name="Someone", family_name="Else"))

    assert second.status_code == 200
    assert second.json()["user"]["id"] == first.json()["user"]["id"]
    assert second.json()["user"]["display_name"] == "Ada Lovelace"
    assert _counts(engine) == (1, 1, 1)
    with Session(engine) as sess:
        token = sess.exec(select(AppleToken)).one()
        assert token.refresh_token == "refresh-2"
        assert token.client_id == SERVICES_ID
    assert metric("traxjourney_registrations_total", provider="apple") == registrations
    assert metric("traxjourney_logins_total", provider="apple", result="success") == logins + 1


def test_google_account_with_the_same_email_stays_separate(client, fake, engine):
    email = "ada@example.com"
    with Session(engine) as sess:
        shadow = LocalUser(username="google_1234567890123456")
        sess.add(shadow); sess.commit(); sess.refresh(shadow)
        google = UserInfo(local_auth_id=shadow.id, google_sub="1234567890123456",
                          display_name="Ada G", email=email, auth_provider="google",
                          email_verified=True)
        sess.add(google); sess.commit(); sess.refresh(google)
        google_id = google.id
    fake.identity = _identity(email=email, is_private_email=False)

    resp = client.post("/api/auth/apple", json=_body(given_name="Ada"))

    assert resp.status_code == 200
    assert resp.json()["user"]["id"] != google_id
    assert resp.json()["user"]["auth_provider"] == "apple"
    with Session(engine) as sess:
        google = sess.get(UserInfo, google_id)
        assert google.apple_sub == "" and google.auth_provider == "google"
        assert sess.exec(select(AppleToken)).one().user_info_id == resp.json()["user"]["id"]
        assert len(sess.exec(select(UserInfo).where(UserInfo.email == email)).all()) == 2


@pytest.mark.parametrize("verified, admin", [(True, True), (False, False)])
def test_admin_email_promotes_only_when_apple_verified_it(client, fake, monkeypatch, verified, admin):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    fake.identity = _identity(email="boss@example.com", email_verified=verified)

    resp = client.post("/api/auth/apple", json=_body())

    assert resp.json()["user"]["is_admin"] is admin


def test_admin_email_promotes_an_existing_account(client, fake, monkeypatch):
    fake.identity = _identity(email="boss@example.com")
    assert client.post("/api/auth/apple", json=_body()).json()["user"]["is_admin"] is False
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")

    assert client.post("/api/auth/apple", json=_body()).json()["user"]["is_admin"] is True


# ── Callback ──────────────────────────────────────────────────────────────────

def _callback(client, data):
    return client.post("/api/auth/apple/callback", data=data, follow_redirects=False)


def _relayed(resp) -> dict[str, list[str]]:
    location = resp.headers["location"]
    assert location.startswith(INTENT_PREFIX)
    assert location.endswith(INTENT_SUFFIX)
    query = location[len(INTENT_PREFIX):-len(INTENT_SUFFIX)]
    assert "#" not in query
    return parse_qs(query, keep_blank_values=True)


def test_callback_redirects_to_the_app_with_the_allowed_fields(client):
    resp = _callback(client, {
        "code": "c0de", "id_token": "a.b.c", "state": "st&ate=1",
        "user": '{"name":{"firstName":"Ada","lastName":"Love lace"},"email":"x@y.z"}',
        "redirect_uri": "https://evil.example/", "next": "https://evil.example/",
        "extra": "<script>alert(1)</script>",
    })

    assert resp.status_code == 303
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content == b""
    assert _relayed(resp) == {
        "code": ["c0de"], "id_token": ["a.b.c"], "state": ["st&ate=1"],
        "user": ['{"name":{"firstName":"Ada","lastName":"Love lace"},"email":"x@y.z"}'],
    }
    assert "evil" not in resp.headers["location"]
    assert "script" not in resp.headers["location"]


def test_callback_relays_an_error(client):
    resp = _callback(client, {"error": "user_cancelled_authorize", "state": "s"})

    assert _relayed(resp) == {"error": ["user_cancelled_authorize"], "state": ["s"]}


def test_callback_drops_a_field_over_8_kib(client):
    resp = _callback(client, {"code": "c", "id_token": "x" * (8 * 1024 + 1), "state": "y" * (8 * 1024)})

    assert _relayed(resp) == {"code": ["c"], "state": ["y" * (8 * 1024)]}


def test_callback_keeps_the_first_of_duplicate_fields(client):
    resp = client.post(
        "/api/auth/apple/callback",
        content="code=first&code=second&id_token=" + "x" * (8 * 1024 + 1) + "&id_token=late",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert _relayed(resp) == {"code": ["first"]}


def test_callback_cannot_break_out_of_the_intent_url(client):
    resp = _callback(client, {
        "state": "x#Intent;package=evil.app;scheme=https;end",
        "code": "https://evil.example/",
    })

    location = resp.headers["location"]
    assert location.count("#") == 1
    assert "evil.app" not in location.split("#", 1)[1]
    assert _relayed(resp)["state"] == ["x#Intent;package=evil.app;scheme=https;end"]


def test_callback_with_nothing_still_goes_only_to_the_app(client):
    resp = _callback(client, {})

    assert resp.status_code == 303
    assert resp.headers["location"] == INTENT_PREFIX + INTENT_SUFFIX


def test_callback_refuses_an_oversized_body(client):
    resp = client.post(
        "/api/auth/apple/callback",
        content="code=" + "x" * (200 * 1024),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert resp.status_code == 413
    assert "location" not in resp.headers


# ── Logs ──────────────────────────────────────────────────────────────────────

# Values nothing else in a log line could contain by coincidence.
SECRET_TOKEN = "SECRETtok3n7q"
SECRET_CODE = "SECRETc0de9x"
SECRET_NONCE = "SECRETn0nce4w"
SECRET_REFRESH = "SECRETrefr3sh"


@pytest.fixture
def server_log(caplog):
    """Every logger at DEBUG, as in tests/test_access_log_privacy.py."""
    with caplog.at_level(logging.DEBUG, logger="api"), caplog.at_level(logging.DEBUG, logger="src"), \
            caplog.at_level(logging.DEBUG):
        yield caplog


def _log_text(caplog) -> str:
    return "\n".join(
        f"{r.levelname} {r.name} {r.getMessage()}" + (f"\n{r.exc_text}" if r.exc_text else "")
        for r in caplog.records
    )


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.mark.parametrize("outcome", ["success", "invalid_token", "code_rejected", "apple_down"])
def test_logs_never_contain_a_token_code_or_nonce(client, fake, monkeypatch, server_log, outcome):
    """The exchange runs for real against a stubbed HTTP answer, so its own
    log lines are checked too."""
    fake.refresh_token = SECRET_REFRESH
    answers = {
        "success": _Response(200, {"refresh_token": SECRET_REFRESH, "id_token": SECRET_TOKEN}),
        "invalid_token": _Response(200, {"refresh_token": SECRET_REFRESH}),
        "code_rejected": _Response(400, {"error": "invalid_grant",
                                         "error_description": SECRET_CODE}),
        "apple_down": _Response(503, {"error": SECRET_CODE}),
    }
    monkeypatch.setattr(apple, "exchange_code", _REAL_EXCHANGE_CODE)
    monkeypatch.setattr(apple.requests, "post", lambda *a, **k: answers[outcome])
    if outcome == "invalid_token":
        fake.verify_error = apple.AppleTokenInvalid("nonce")

    resp = client.post("/api/auth/apple", json=_body(
        id_token=SECRET_TOKEN, authorization_code=SECRET_CODE, nonce=SECRET_NONCE))

    assert resp.status_code == {"success": 200, "invalid_token": 401,
                                "code_rejected": 401, "apple_down": 502}[outcome]
    text_ = _log_text(server_log)
    for secret in (SECRET_TOKEN, SECRET_CODE, SECRET_NONCE, SECRET_REFRESH, "client-secret-jwt"):
        assert secret not in text_
    if outcome != "success":
        assert "Apple sign-in failed: " in text_


def test_callback_logs_nothing_it_relays(client, server_log):
    _callback(client, {"code": SECRET_CODE, "id_token": SECRET_TOKEN})

    text_ = _log_text(server_log)
    assert SECRET_CODE not in text_ and SECRET_TOKEN not in text_

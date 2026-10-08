"""The Strava OAuth ``state`` is a short-lived, purpose-bound token.

The authorization URL travels to Strava, browser history and access logs, so
its ``state`` must not be a session token: it used to be one, which signed the
user in for a week. These tests pin that the state and the session token are
not interchangeable in either direction, that the state expires, and that a
fresh one is relayed back to the client that asked for it. Linking itself
happens in ``POST /api/strava/complete`` (tests/test_strava_connect_binding.py).

Strava's token exchange is mocked on ``OAuth2Session.exchange_code``; the
network is never touched.
"""
from __future__ import annotations

import datetime
import logging
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.strava as strava_module
import models.db as db_module
from api.deps import (
    STRAVA_OAUTH_AUDIENCE,
    create_access_token,
    create_strava_oauth_state,
    decode_token,
    decode_token_quietly,
    get_current_user,
    jwt_secret,
)
from api.router import app
from models.user import StravaToken, UserInfo
from src.auth.oauth import OAuth2Session
from src.config.settings import Config

# RFC 7636 Appendix B.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


class _StravaConfig(Config):
    def __init__(self):
        super().__init__()
        self.set("strava.client_id", "id")
        self.set("strava.client_secret", "secret")


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(strava_module, "_cfg", _StravaConfig())
    yield engine
    engine.dispose()


@pytest.fixture
def users(engine):
    """Two users; returns their UserInfo rows (detached)."""
    with Session(engine) as sess:
        a = UserInfo(display_name="A", email="a@e.com")
        b = UserInfo(display_name="B", email="b@e.com")
        sess.add(a)
        sess.add(b)
        sess.commit()
        sess.refresh(a)
        sess.refresh(b)
        sess.expunge_all()
    return a, b


@pytest.fixture
def exchange(monkeypatch):
    calls = []

    def fake_exchange(self, code):
        calls.append(code)
        return {"access_token": "acc", "refresh_token": "ref", "expires_at": 9999999999}

    monkeypatch.setattr(OAuth2Session, "exchange_code", fake_exchange)
    return calls


@pytest.fixture
def client():
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _state(uid: int, return_to: str = "web") -> str:
    return create_strava_oauth_state(uid, CHALLENGE, return_to)


def _connect(client, return_to: str = "web"):
    return client.post(
        "/api/strava/connect", json={"challenge": CHALLENGE, "return_to": return_to})


def _callback(client, state):
    return client.get(
        "/api/strava/callback", params={"code": "the-code", "state": state},
        follow_redirects=False,
    )


def _token_row(engine, uid):
    with Session(engine) as sess:
        return sess.exec(select(StravaToken).where(StravaToken.user_info_id == uid)).first()


def _expired_state(uid: int) -> str:
    return jwt.encode(
        {
            "aud": STRAVA_OAUTH_AUDIENCE, "sub": str(uid), "jti": "n",
            "chal": CHALLENGE, "ret": "web",
            "exp": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1),
        },
        jwt_secret(), algorithm="HS256",
    )


# ── the authorize URL ────────────────────────────────────────────────────────

def test_connect_url_state_is_not_a_session_token(client, users, engine):
    a, _ = users
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(a.id)}

    resp = _connect(client)

    assert resp.status_code == 200
    assert set(resp.json()) == {"url"}  # response shape unchanged
    state = parse_qs(urlparse(resp.json()["url"]).query)["state"][0]
    with pytest.raises(HTTPException):
        decode_token(state)
    assert decode_token_quietly(state) is None
    claims = jwt.decode(state, options={"verify_signature": False})
    assert set(claims) == {"aud", "sub", "jti", "chal", "ret", "exp"}
    assert claims["sub"] == str(a.id)
    lifetime = claims["exp"] - datetime.datetime.now(datetime.timezone.utc).timestamp()
    assert 0 < lifetime <= 10 * 60


def test_connect_issues_a_fresh_nonce_each_time(client, users):
    a, _ = users
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(a.id)}

    states = [
        parse_qs(urlparse(_connect(client).json()["url"]).query)["state"][0]
        for _ in range(2)
    ]

    jtis = {jwt.decode(s, options={"verify_signature": False})["jti"] for s in states}
    assert len(jtis) == 2


# ── session auth refuses a state token ───────────────────────────────────────

def test_session_decoders_refuse_a_state_token(users):
    a, _ = users
    state = _state(a.id)

    with pytest.raises(HTTPException) as exc:
        decode_token(state)
    assert exc.value.status_code == 401
    assert decode_token_quietly(state) is None


def test_authenticated_endpoint_refuses_a_state_token(client, users):
    a, _ = users
    state = _state(a.id)

    resp = client.get("/api/strava/status", headers={"Authorization": f"Bearer {state}"})

    assert resp.status_code == 401


def test_session_tokens_still_authenticate(client, users):
    a, _ = users
    session = create_access_token(a)

    assert decode_token(session)["sub"] == str(a.id)
    resp = client.get("/api/strava/status", headers={"Authorization": f"Bearer {session}"})
    assert resp.status_code == 200


# ── the callback ─────────────────────────────────────────────────────────────

def test_callback_refuses_a_session_token_as_state(client, users, engine, exchange):
    a, _ = users

    resp = _callback(client, create_access_token(a))

    assert resp.status_code in (302, 307)
    assert resp.headers["location"].endswith(
        "/oauth_callback.html?strava=error&reason=invalid_state")
    assert exchange == []
    assert _token_row(engine, a.id) is None


def test_callback_refuses_an_expired_state_and_says_it_expired(
        client, users, engine, exchange, caplog):
    a, _ = users
    state = _expired_state(a.id)

    with caplog.at_level(logging.DEBUG, logger="api.deps"):
        resp = _callback(client, state)

    assert resp.headers["location"].endswith(
        "/oauth_callback.html?strava=error&reason=state_expired")
    assert exchange == []
    assert _token_row(engine, a.id) is None
    # Routine, like an expired session: not logged.
    assert [r for r in caplog.records if r.name == "api.deps"] == []


def test_expired_session_token_as_state_is_invalid_not_expired(client, users, exchange):
    """Only a genuine state can be reported as expired."""
    a, _ = users
    expired_session = jwt.encode(
        {"sub": str(a.id),
         "exp": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)},
        jwt_secret(), algorithm="HS256",
    )

    resp = _callback(client, expired_session)

    assert resp.headers["location"].endswith("reason=invalid_state")
    assert exchange == []


def test_invalid_state_logs_a_warning_without_the_token(client, users, exchange, caplog):
    a, _ = users
    session = create_access_token(a)

    with caplog.at_level(logging.WARNING, logger="api.deps"):
        resp = _callback(client, session)

    assert resp.headers["location"].endswith("reason=invalid_state")
    warnings = [r for r in caplog.records
                if r.name == "api.deps" and r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Strava OAuth state" in warnings[0].getMessage()
    assert session not in caplog.text


def test_callback_refuses_a_state_signed_with_another_key(client, users, engine, exchange):
    a, _ = users
    forged = jwt.encode(
        {"aud": STRAVA_OAUTH_AUDIENCE, "sub": str(a.id), "jti": "n",
         "chal": CHALLENGE, "ret": "app",
         "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)},
        "another-key-" * 4, algorithm="HS256",
    )

    resp = _callback(client, forged)

    assert resp.headers["location"].startswith(strava_module._FRONTEND_ORIGIN)
    assert resp.headers["location"].endswith("reason=invalid_state")
    assert exchange == []
    assert _token_row(engine, a.id) is None


def test_callback_relays_a_fresh_state_without_linking(client, users, engine, exchange):
    a, b = users
    state = _state(b.id)

    resp = _callback(client, state)

    location = urlparse(resp.headers["location"])
    assert location.path.endswith("/oauth_callback.html")
    assert parse_qs(location.query) == {
        "strava": ["code"], "code": ["the-code"], "state": [state]}
    assert exchange == []
    assert _token_row(engine, a.id) is None
    assert _token_row(engine, b.id) is None


def test_connect_callback_complete_round_trip(client, users, engine, exchange):
    a, _ = users
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(a.id)}
    url = _connect(client).json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]

    relayed = parse_qs(urlparse(_callback(client, state).headers["location"]).query)
    assert _token_row(engine, a.id) is None
    resp = client.post("/api/strava/complete", json={
        "code": relayed["code"][0], "state": relayed["state"][0], "verifier": VERIFIER})

    assert resp.status_code == 200 and resp.json() == {"connected": True}
    assert exchange == ["the-code"]
    assert _token_row(engine, a.id) is not None

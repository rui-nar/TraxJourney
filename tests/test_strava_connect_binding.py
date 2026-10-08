"""Strava connect is bound to the client that started it.

``GET /api/strava/callback`` is unauthenticated and its URL can be forwarded,
so it writes nothing: it relays ``code`` and ``state`` back to the client the
signed state names. Only ``POST /api/strava/complete`` links an account, and
only when the bearer is the state's user and the verifier matches the
state's challenge (docs/STRAVA_CONNECT_BINDING_PLAN.md D1-D6).

Strava's token exchange is mocked on ``OAuth2Session.exchange_code``; the
network is never touched. Fixtures are copied from
tests/test_strava_oauth_state.py.
"""
from __future__ import annotations

import datetime
import logging
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.strava as strava_module
import models.db as db_module
from api.deps import (
    STRAVA_OAUTH_AUDIENCE,
    create_access_token,
    create_strava_oauth_state,
    get_current_user,
    jwt_secret,
)
from api.router import app
from models.user import StravaToken, UserInfo
from src.auth.oauth import AuthenticationError, OAuth2Session
from src.config.settings import Config

# RFC 7636 Appendix B — the same pair the Dart tests use.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
OTHER_VERIFIER = "x" * 43

CODE = "strava-code-0123456789abcdef"
APP_RETURN = "traxjourney://app/strava-return"
FIXED_REASONS = {"denied", "no_state", "state_expired", "invalid_state", "update_required"}


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
def failing_exchange(monkeypatch):
    calls = []

    def fake_exchange(self, code):
        calls.append(code)
        raise AuthenticationError("Failed to exchange code: upstream-body-text")

    monkeypatch.setattr(OAuth2Session, "exchange_code", fake_exchange)
    return calls


@pytest.fixture
def client():
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _sign_in(user):
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user.id)}


def _state(uid: int, return_to: str = "web") -> str:
    return create_strava_oauth_state(uid, CHALLENGE, return_to)


def _signed(claims: dict, key: str | None = None) -> str:
    return jwt.encode(claims, key or jwt_secret(), algorithm="HS256")


def _claims(uid: int, *, expired: bool = False, **extra) -> dict:
    delta = datetime.timedelta(seconds=-1 if expired else 300)
    return {
        "aud": STRAVA_OAUTH_AUDIENCE, "sub": str(uid), "jti": "n",
        "exp": datetime.datetime.now(datetime.timezone.utc) + delta,
        **extra,
    }


def _expired_state(uid: int, return_to: str = "web") -> str:
    return _signed(_claims(uid, expired=True, chal=CHALLENGE, ret=return_to))


def _outdated_state(uid: int) -> str:
    """What the retired GET issued: no ``chal``, no ``ret``."""
    return _signed(_claims(uid))


def _callback(client, **params):
    return client.get("/api/strava/callback", params=params, follow_redirects=False)


def _complete(client, state, verifier=VERIFIER, code=CODE):
    return client.post(
        "/api/strava/complete", json={"code": code, "state": state, "verifier": verifier})


def _location(resp):
    assert resp.status_code in (302, 307)
    loc = resp.headers["location"]
    return loc, parse_qs(urlparse(loc).query)


def _app_log(caplog) -> str:
    """Application log lines only. The test client's own HTTP library logs
    each request URL; that is the harness, not the server."""
    return "\n".join(
        r.getMessage() for r in caplog.records
        if not r.name.startswith(("httpx", "httpcore")))


def _token_row(engine, uid):
    with Session(engine) as sess:
        return sess.exec(select(StravaToken).where(StravaToken.user_info_id == uid)).first()


# ── connect ──────────────────────────────────────────────────────────────────

def test_get_connect_is_retired_with_426(client, users, exchange):
    a, _ = users
    _sign_in(a)

    resp = client.get("/api/strava/connect")

    assert resp.status_code == 426
    assert resp.json()["detail"] == "Update the app to connect Strava."
    assert "url" not in resp.json()


@pytest.mark.parametrize("return_to", ["web", "app"])
def test_post_connect_signs_challenge_and_return_target_into_state(client, users, return_to):
    a, _ = users
    _sign_in(a)

    resp = client.post(
        "/api/strava/connect", json={"challenge": CHALLENGE, "return_to": return_to})

    assert resp.status_code == 200
    state = parse_qs(urlparse(resp.json()["url"]).query)["state"][0]
    claims = jwt.decode(
        state, jwt_secret(), algorithms=["HS256"], audience=STRAVA_OAUTH_AUDIENCE)
    assert claims["sub"] == str(a.id)
    assert claims["chal"] == CHALLENGE
    assert claims["ret"] == return_to


@pytest.mark.parametrize("body", [
    {"challenge": CHALLENGE, "return_to": "elsewhere"},
    {"challenge": CHALLENGE[:-1], "return_to": "web"},
    {"challenge": CHALLENGE[:-1] + "=", "return_to": "web"},
    {"return_to": "web"},
])
def test_post_connect_refuses_a_malformed_body(client, users, body):
    a, _ = users
    _sign_in(a)

    assert client.post("/api/strava/connect", json=body).status_code == 422


# ── callback ─────────────────────────────────────────────────────────────────

def test_callback_never_writes_a_token_row(client, users, engine, exchange):
    a, b = users
    with Session(engine) as sess:
        sess.add(StravaToken(
            user_info_id=a.id, access_token="old", refresh_token="old-r", expires_at=1.0))
        sess.commit()
    queries = [
        {"code": CODE, "state": _state(a.id)},
        {"code": CODE, "state": _state(b.id, "app")},
        {"code": CODE, "state": _expired_state(b.id)},
        {"code": CODE, "state": _outdated_state(b.id)},
        {"code": CODE, "state": create_access_token(b)},
        {"code": CODE, "state": "not-a-jwt"},
        {"code": CODE},
        {"error": "access_denied", "state": _state(b.id)},
        {},
    ]

    for params in queries:
        _callback(client, **params)

    assert exchange == []
    row = _token_row(engine, a.id)
    assert (row.access_token, row.refresh_token, row.expires_at) == ("old", "old-r", 1.0)
    assert _token_row(engine, b.id) is None


def test_callback_relays_code_and_state_to_web_page(client, users, exchange):
    a, _ = users
    state = _state(a.id, "web")

    loc, query = _location(_callback(client, code=CODE, state=state))

    assert loc.startswith(f"{strava_module._FRONTEND_ORIGIN}/oauth_callback.html?")
    assert query == {"strava": ["code"], "code": [CODE], "state": [state]}


def test_callback_relays_code_and_state_to_app_scheme(client, users, exchange):
    a, _ = users
    state = _state(a.id, "app")

    loc, query = _location(_callback(client, code=CODE, state=state))

    assert loc.startswith(f"{APP_RETURN}?")
    assert query == {"strava": ["code"], "code": [CODE], "state": [state]}


def test_callback_refuses_a_state_without_challenge_as_update_required(client, users, exchange):
    a, _ = users

    loc, query = _location(_callback(client, code=CODE, state=_outdated_state(a.id)))

    assert loc.startswith(strava_module._FRONTEND_ORIGIN)
    assert query == {"strava": ["error"], "reason": ["update_required"]}


@pytest.mark.parametrize("claims", [
    {"chal": CHALLENGE},          # no ret
    {"ret": "app"},               # no chal
    {"chal": CHALLENGE, "ret": "elsewhere"},
    {"chal": "", "ret": "web"},
    {"chal": CHALLENGE, "ret": ["app"]},
])
def test_callback_refuses_a_partly_bound_state_as_invalid(client, users, exchange, claims):
    a, _ = users

    loc, query = _location(_callback(client, code=CODE, state=_signed(_claims(a.id, **claims))))

    assert loc.startswith(strava_module._FRONTEND_ORIGIN)
    assert query == {"strava": ["error"], "reason": ["invalid_state"]}


def test_callback_error_reasons_are_fixed_tokens(client, users, exchange):
    a, _ = users

    denied_loc, denied = _location(
        _callback(client, error="access_denied", state=_state(a.id, "app")))
    assert denied_loc.startswith(f"{APP_RETURN}?")
    assert denied == {"strava": ["error"], "reason": ["denied"]}

    cases = [
        {"error": "access_denied", "state": _state(a.id)},
        {"state": _state(a.id)},                             # no code
        {"code": CODE},                                      # no state
        {"code": CODE, "state": _expired_state(a.id)},
        {"code": CODE, "state": "garbage"},
        {"code": CODE, "state": _outdated_state(a.id)},
        {"code": CODE, "error": "<script>", "state": "x" * 300},
    ]
    for params in cases:
        _, query = _location(_callback(client, **params))
        assert set(query) == {"strava", "reason"}
        assert query["strava"] == ["error"]
        assert query["reason"][0] in FIXED_REASONS
    assert exchange == []


def test_callback_routes_an_expired_app_state_to_the_app(client, users, exchange):
    a, _ = users

    resp = _callback(client, code=CODE, state=_expired_state(a.id, "app"))

    assert resp.headers["location"] == f"{APP_RETURN}?strava=error&reason=state_expired"


def test_expired_state_with_a_foreign_signature_goes_to_the_web_page(client, users, exchange):
    a, _ = users
    forged = _signed(
        _claims(a.id, expired=True, chal=CHALLENGE, ret="app"), key="another-key-" * 4)

    loc, query = _location(_callback(client, code=CODE, state=forged))

    assert loc.startswith(f"{strava_module._FRONTEND_ORIGIN}/oauth_callback.html?")
    assert query["strava"] == ["error"]


def test_expired_state_return_target_ignores_a_foreign_signature():
    """The redirect helper itself, past the decoder: a forged expired state
    names no target."""
    from api.deps import expired_strava_oauth_state_return_target

    forged = _signed(_claims(1, expired=True, ret="app"), key="another-key-" * 4)
    genuine = _signed(_claims(1, expired=True, ret="app"))
    session_like = _signed({"sub": "1", "ret": "app"})

    assert expired_strava_oauth_state_return_target(forged) is None
    assert expired_strava_oauth_state_return_target(session_like) is None
    assert expired_strava_oauth_state_return_target(genuine) == "app"


def test_update_required_refusal_logs_one_warning_without_the_state(
        client, users, exchange, caplog):
    a, _ = users
    state = _outdated_state(a.id)

    with caplog.at_level(logging.DEBUG):
        _callback(client, code=CODE, state=state)

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "update_required" in r.getMessage()]
    assert len(warnings) == 1
    assert state not in _app_log(caplog)
    assert CODE not in _app_log(caplog)


# ── complete ─────────────────────────────────────────────────────────────────

def test_complete_links_the_bearer_when_state_and_verifier_match(client, users, engine, exchange):
    a, b = users
    _sign_in(a)

    resp = _complete(client, _state(a.id))

    assert resp.status_code == 200
    assert resp.json() == {"connected": True}
    assert exchange == [CODE]
    row = _token_row(engine, a.id)
    assert row is not None and row.access_token == "acc" and row.refresh_token == "ref"
    assert _token_row(engine, b.id) is None


def test_complete_updates_an_existing_token_row(client, users, engine, exchange):
    a, _ = users
    with Session(engine) as sess:
        sess.add(StravaToken(
            user_info_id=a.id, access_token="old", refresh_token="old-r", expires_at=1.0))
        sess.commit()
    _sign_in(a)

    assert _complete(client, _state(a.id)).status_code == 200

    row = _token_row(engine, a.id)
    assert (row.access_token, row.refresh_token) == ("acc", "ref")


@pytest.mark.parametrize("state_for, bearer", [(0, 1), (1, 0)])
def test_complete_refuses_another_users_state_403(
        client, users, engine, exchange, state_for, bearer):
    """The forwarded link, either way round: one user's state completed under
    the other's session links nobody."""
    pair = users
    _sign_in(pair[bearer])

    resp = _complete(client, _state(pair[state_for].id))

    assert resp.status_code == 403
    assert exchange == []
    assert _token_row(engine, pair[0].id) is None
    assert _token_row(engine, pair[1].id) is None


def test_complete_refuses_a_wrong_verifier_403(client, users, engine, exchange):
    a, _ = users
    _sign_in(a)

    resp = _complete(client, _state(a.id), verifier=OTHER_VERIFIER)

    assert resp.status_code == 403
    assert exchange == []
    assert _token_row(engine, a.id) is None


def test_complete_refuses_an_expired_state_400(client, users, engine, exchange):
    a, _ = users
    _sign_in(a)

    resp = _complete(client, _expired_state(a.id))

    assert resp.status_code == 400
    assert resp.json()["detail"] == "state_expired"
    assert exchange == []
    assert _token_row(engine, a.id) is None


@pytest.mark.parametrize("make_state", [
    lambda u: create_access_token(u),
    lambda u: _outdated_state(u.id),
    lambda u: _signed(_claims(u.id, chal=CHALLENGE, ret="web"), key="another-key-" * 4),
    lambda u: "garbage",
])
def test_complete_refuses_an_invalid_state_400(client, users, engine, exchange, make_state):
    a, _ = users
    _sign_in(a)

    resp = _complete(client, make_state(a))

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_state"
    assert exchange == []
    assert _token_row(engine, a.id) is None


@pytest.mark.parametrize("verifier", ["short", "a" * 129, "a" * 42 + "=", "a" * 42 + "+"])
def test_complete_refuses_a_malformed_verifier_422(client, users, exchange, verifier):
    a, _ = users
    _sign_in(a)

    assert _complete(client, _state(a.id), verifier=verifier).status_code == 422
    assert exchange == []


def test_complete_requires_a_session(client, users, engine, exchange):
    a, _ = users

    resp = _complete(client, _state(a.id))

    assert resp.status_code in (401, 403)
    assert exchange == []
    assert _token_row(engine, a.id) is None


def test_complete_exchange_failure_is_502_without_exception_text(
        client, users, engine, failing_exchange, caplog):
    a, _ = users
    _sign_in(a)

    with caplog.at_level(logging.DEBUG):
        resp = _complete(client, _state(a.id))

    assert resp.status_code == 502
    assert "upstream-body-text" not in resp.text
    assert "upstream-body-text" not in _app_log(caplog)
    assert failing_exchange == [CODE]
    assert _token_row(engine, a.id) is None


def test_rfc7636_vector_matches():
    assert strava_module.pkce_challenge(VERIFIER) == CHALLENGE


def test_exchange_code_passes_a_timeout():
    oauth = OAuth2Session(_StravaConfig())
    with patch("src.auth.oauth.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"access_token": "abc"}

        oauth.exchange_code(CODE)

    assert post.call_args.kwargs["timeout"] == OAuth2Session.TOKEN_TIMEOUT


def test_no_code_state_or_verifier_in_logs(client, users, engine, exchange, caplog, monkeypatch):
    a, b = users
    states = []

    def run():
        _sign_in(a)
        url = client.post(
            "/api/strava/connect", json={"challenge": CHALLENGE, "return_to": "app"}).json()["url"]
        state = parse_qs(urlparse(url).query)["state"][0]
        states.append(state)
        client.post("/api/strava/connect", json={"challenge": CHALLENGE, "return_to": "x"})
        for params in (
            {"code": CODE, "state": state},
            {"code": CODE, "state": _expired_state(a.id)},
            {"code": CODE, "state": _outdated_state(a.id)},
            {"code": CODE, "state": create_access_token(a)},
            {"error": "access_denied", "state": state},
        ):
            states.append(params["state"])
            _callback(client, **params)
        _complete(client, state, verifier=OTHER_VERIFIER)       # wrong verifier
        _complete(client, _expired_state(a.id))                 # expired
        _complete(client, create_access_token(a))               # invalid
        _complete(client, state, verifier="bad")                # 422
        _sign_in(b)
        _complete(client, state)                                # wrong account
        _sign_in(a)
        _complete(client, state)                                # success

        def boom(self, code):
            raise AuthenticationError(f"Failed to exchange code: {code}")

        monkeypatch.setattr(OAuth2Session, "exchange_code", boom)
        _complete(client, state)                                # 502

    with caplog.at_level(logging.DEBUG):
        run()

    assert _token_row(engine, a.id) is not None  # the success path ran
    for secret in [CODE, VERIFIER, OTHER_VERIFIER, CHALLENGE, *states]:
        assert secret not in _app_log(caplog)

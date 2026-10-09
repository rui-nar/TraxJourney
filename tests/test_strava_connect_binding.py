"""Strava connect is bound to the client that started it.

``GET /api/strava/callback`` is unauthenticated and its URL can be forwarded,
so it never touches a token: it binds the code to the state it arrived with
and relays both back to the client the signed state names. Only
``POST /api/strava/complete`` links an account, and only when the bearer is
the state's user, the verifier matches the state's challenge and the code is
bound to that state (docs/STRAVA_CONNECT_BINDING_PLAN.md D1-D6, D9).

Strava's token exchange is mocked on ``OAuth2Session.exchange_code``; the
network is never touched. Fixtures are copied from
tests/test_strava_oauth_state.py.
"""
from __future__ import annotations

import datetime
import html
import logging
import re
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
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
from models.user import StravaOAuthCode, StravaToken, UserInfo
from src.auth.oauth import AuthenticationError, OAuth2Session
from src.config.settings import Config

# RFC 7636 Appendix B — the same pair the Dart tests use.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
OTHER_VERIFIER = "x" * 43

CODE = "strava-code-0123456789abcdef"
CODE_2 = "strava-code-fedcba9876543210"
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


def _jti(state: str) -> str:
    return jwt.decode(state, options={"verify_signature": False})["jti"]


def _bindings(engine) -> dict[str, str]:
    """code_hash → state_jti for every binding row."""
    with Session(engine) as sess:
        return {r.code_hash: r.state_jti for r in sess.exec(select(StravaOAuthCode)).all()}


def _continue_link(resp):
    """The Continue link of an app return's confirmation page (#584): the
    app return the callback used to redirect to."""
    assert resp.status_code == 200
    hrefs = re.findall(r'<a class="continue" href="([^"]*)"', resp.text)
    assert len(hrefs) == 1, resp.text
    loc = html.unescape(hrefs[0])
    return loc, parse_qs(urlparse(loc).query)


def _bind(client, state, code=CODE) -> str:
    """Send the code back through the callback with ``state``, as Strava
    does, and check it was relayed; returns the state."""
    resp = _callback(client, code=code, state=state)
    _, query = _continue_link(resp) if resp.status_code == 200 else _location(resp)
    assert query["strava"] == ["code"], query
    return state


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
    first = _state(a.id)
    queries = [
        {"code": CODE, "state": first},
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
    # Its one write: the code bound to the first valid state it came with.
    assert _bindings(engine) == {strava_module._code_hash(CODE): _jti(first)}


def test_callback_relays_code_and_state_to_web_page(client, users, exchange):
    a, _ = users
    state = _state(a.id, "web")

    loc, query = _location(_callback(client, code=CODE, state=state))

    assert loc.startswith(f"{strava_module._FRONTEND_ORIGIN}/oauth_callback.html?")
    assert query == {"strava": ["code"], "code": [CODE], "state": [state]}


def test_callback_relays_code_and_state_to_app_scheme(client, users, exchange):
    a, _ = users
    state = _state(a.id, "app")

    loc, query = _continue_link(_callback(client, code=CODE, state=state))

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

    resp = _complete(client, _bind(client, _state(a.id)))

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

    assert _complete(client, _bind(client, _state(a.id))).status_code == 200

    row = _token_row(engine, a.id)
    assert (row.access_token, row.refresh_token) == ("acc", "ref")


@pytest.mark.parametrize("state_for, bearer", [(0, 1), (1, 0)])
def test_complete_refuses_another_users_state_403(
        client, users, engine, exchange, state_for, bearer):
    """The forwarded link, either way round: one user's state completed under
    the other's session links nobody."""
    pair = users
    _sign_in(pair[bearer])

    resp = _complete(client, _bind(client, _state(pair[state_for].id)))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "wrong_account"
    assert exchange == []
    assert _token_row(engine, pair[0].id) is None
    assert _token_row(engine, pair[1].id) is None


def test_complete_refuses_a_wrong_verifier_403(client, users, engine, exchange):
    a, _ = users
    _sign_in(a)

    resp = _complete(client, _bind(client, _state(a.id)), verifier=OTHER_VERIFIER)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "verifier_mismatch"
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

    assert _complete(client, _bind(client, _state(a.id)), verifier=verifier).status_code == 422
    assert exchange == []


def test_complete_requires_a_session(client, users, engine, exchange):
    a, _ = users

    resp = _complete(client, _bind(client, _state(a.id)))

    assert resp.status_code in (401, 403)
    assert exchange == []
    assert _token_row(engine, a.id) is None


def test_complete_exchange_failure_is_502_without_exception_text(
        client, users, engine, failing_exchange, caplog):
    a, _ = users
    _sign_in(a)

    state = _bind(client, _state(a.id))
    with caplog.at_level(logging.DEBUG):
        resp = _complete(client, state)

    assert resp.status_code == 502
    assert "upstream-body-text" not in resp.text
    assert "upstream-body-text" not in _app_log(caplog)
    assert failing_exchange == [CODE]
    assert _token_row(engine, a.id) is None


# ── code binding (D9) ────────────────────────────────────────────────────────

def _add_binding(engine, code: str, jti: str, expires_at: float) -> None:
    with Session(engine) as sess:
        sess.add(StravaOAuthCode(
            code_hash=strava_module._code_hash(code), state_jti=jti, expires_at=expires_at))
        sess.commit()


def _attacker_state(attacker) -> str:
    """A state the attacker minted for their own account, with their own
    verifier — everything the hostile app needs besides the victim's code."""
    return create_strava_oauth_state(
        attacker.id, strava_module.pkce_challenge(OTHER_VERIFIER), "app")


def test_complete_refuses_a_code_bound_to_another_state(client, users, engine, exchange):
    """A hostile app read the victim's return URL: it completes the victim's
    code with its owner's own state, verifier and session."""
    victim, attacker = users
    victim_state = _bind(client, _state(victim.id, "app"))
    _sign_in(attacker)

    resp = _complete(client, _attacker_state(attacker), verifier=OTHER_VERIFIER)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "code_not_bound"
    assert exchange == []
    assert _token_row(engine, victim.id) is None
    assert _token_row(engine, attacker.id) is None
    assert _bindings(engine) == {strava_module._code_hash(CODE): _jti(victim_state)}


def test_callback_does_not_rebind_a_code(client, users, engine, exchange):
    victim, attacker = users
    victim_state = _bind(client, _state(victim.id))

    loc, query = _location(_callback(client, code=CODE, state=_attacker_state(attacker)))

    assert loc.startswith(f"{APP_RETURN}?")  # the attacker's state names the app
    assert query == {"strava": ["error"], "reason": ["invalid_state"]}
    assert _bindings(engine) == {strava_module._code_hash(CODE): _jti(victim_state)}


def test_callback_reload_with_same_state_relays_again(client, users, engine, exchange):
    a, _ = users
    state = _state(a.id)

    _bind(client, state)
    _bind(client, state)

    assert _bindings(engine) == {strava_module._code_hash(CODE): _jti(state)}


@pytest.mark.parametrize("winner_is_same_state", [False, True])
def test_callback_racing_on_one_code_binds_it_once(
        client, users, engine, exchange, monkeypatch, winner_is_same_state):
    """Another callback inserts the same code between this one's read and its
    insert: the primary key refuses the second insert, and the row that won
    decides."""
    a, _ = users
    state = _state(a.id)
    winner = _jti(state) if winner_is_same_state else "other-jti"
    _add_binding(engine, CODE, winner, time.time() + 300)
    real = strava_module._bound_jti
    calls = []

    def read_before_the_other_insert(sess, code_hash):
        calls.append(code_hash)
        return None if len(calls) == 1 else real(sess, code_hash)

    monkeypatch.setattr(strava_module, "_bound_jti", read_before_the_other_insert)

    _, query = _location(_callback(client, code=CODE, state=state))

    assert len(calls) == 2  # the re-read after the refused insert
    assert query["strava"] == (["code"] if winner_is_same_state else ["error"])
    assert _bindings(engine) == {strava_module._code_hash(CODE): winner}


def test_complete_without_a_callback_binding_is_refused(client, users, engine, exchange):
    a, _ = users
    _sign_in(a)

    resp = _complete(client, _state(a.id))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "code_not_bound"
    assert exchange == []
    assert _token_row(engine, a.id) is None


def test_complete_refuses_an_expired_binding(client, users, engine, exchange):
    a, _ = users
    state = _state(a.id)
    _add_binding(engine, CODE, _jti(state), time.time() - 1)
    _sign_in(a)

    resp = _complete(client, state)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "code_not_bound"
    assert exchange == []


def test_expired_binding_rows_are_pruned_on_insert(client, users, engine, exchange):
    a, _ = users
    _add_binding(engine, "old-code", "old-jti", time.time() - 1)
    _add_binding(engine, "live-code", "live-jti", time.time() + 300)
    state = _state(a.id)

    _bind(client, state)

    assert _bindings(engine) == {
        strava_module._code_hash("live-code"): "live-jti",
        strava_module._code_hash(CODE): _jti(state),
    }


def test_binding_row_is_deleted_after_a_successful_complete(client, users, engine, exchange):
    a, _ = users
    state = _bind(client, _state(a.id))
    _sign_in(a)

    assert _complete(client, state).status_code == 200
    assert _bindings(engine) == {}

    replay = _complete(client, state)
    assert replay.status_code == 403
    assert replay.json()["detail"] == "code_not_bound"
    assert exchange == [CODE]


def test_complete_survives_a_disconnect_during_the_exchange(
        client, users, engine, monkeypatch):
    """The token row a disconnect removed while Strava was being asked is
    simply inserted again: the new tokens are stored, not lost."""
    a, _ = users
    with Session(engine) as sess:
        sess.add(StravaToken(
            user_info_id=a.id, access_token="old", refresh_token="old-r", expires_at=1.0))
        sess.commit()

    def exchange_while_disconnecting(self, code):
        with Session(engine) as sess:
            for row in sess.exec(select(StravaToken)).all():
                sess.delete(row)
            sess.commit()
        return {"access_token": "acc", "refresh_token": "ref", "expires_at": 9999999999}

    monkeypatch.setattr(OAuth2Session, "exchange_code", exchange_while_disconnecting)
    state = _bind(client, _state(a.id))
    _sign_in(a)

    resp = _complete(client, state)

    assert resp.status_code == 200
    row = _token_row(engine, a.id)
    assert (row.access_token, row.refresh_token) == ("acc", "ref")


def test_complete_survives_a_disconnect_between_read_and_write(
        client, users, engine, exchange, monkeypatch):
    """The row vanishes just before the token write: the claim matches
    nothing and the tokens are inserted — no StaleDataError, no 500 — and
    the binding still goes in the same transaction."""
    a, _ = users
    with Session(engine) as sess:
        sess.add(StravaToken(
            user_info_id=a.id, access_token="old", refresh_token="old-r", expires_at=1.0))
        sess.commit()
    real = strava_module._claim_token_row
    claims = []

    def claim_after_a_disconnect(sess, user_info_id, **values):
        sess.execute(delete(StravaToken).where(StravaToken.user_info_id == user_info_id))
        claimed = real(sess, user_info_id, **values)
        claims.append(claimed)
        return claimed

    monkeypatch.setattr(strava_module, "_claim_token_row", claim_after_a_disconnect)
    state = _bind(client, _state(a.id))
    _sign_in(a)

    resp = _complete(client, state)

    assert resp.status_code == 200
    assert claims == [False]
    row = _token_row(engine, a.id)
    assert (row.access_token, row.refresh_token) == ("acc", "ref")
    assert _bindings(engine) == {}


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
        _complete(client, state, code=CODE_2)                   # not bound
        _complete(client, state)                                # success
        _complete(client, state)                                # replay
        _callback(client, code=CODE, state=_state(b.id))        # rebind refused

        def boom(self, code):
            raise AuthenticationError(f"Failed to exchange code: {code}")

        monkeypatch.setattr(OAuth2Session, "exchange_code", boom)
        _callback(client, code=CODE_2, state=state)
        _complete(client, state, code=CODE_2)                   # 502

    with caplog.at_level(logging.DEBUG):
        run()

    assert _token_row(engine, a.id) is not None  # the success path ran
    assert exchange == [CODE]
    hashes = [strava_module._code_hash(c) for c in (CODE, CODE_2)]
    for secret in [CODE, CODE_2, *hashes, VERIFIER, OTHER_VERIFIER, CHALLENGE, *states]:
        assert secret not in _app_log(caplog)

"""An app connect asks before it returns to the app (issue #584).

For a valid ``ret=app`` state with a code, ``GET /api/strava/callback`` binds
the code, then answers a page instead of redirecting into the app: a fixed
warning first, then the state's account by display name, a Continue link to
the app return and a Cancel that stays on the page
(docs/STRAVA_CONNECT_FOLLOWUPS_PLAN.md D1, D2). Web and error returns still
redirect.

Fixtures are copied from tests/test_strava_connect_binding.py.
"""
from __future__ import annotations

import html
import logging
import re
import uuid
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.strava as strava_module
import models.db as db_module
from api.deps import create_strava_oauth_state, jwt_secret
from api.router import app
from models.user import StravaOAuthCode, StravaToken, UserInfo
from src.brand import APP_NAME
from src.config.settings import Config

CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
CODE = "strava-code-0123456789abcdef"
APP_RETURN = "traxjourney://app/strava-return"
WARNING = (
    f"Only continue if you just pressed Connect Strava in the {APP_NAME} app on "
    "this phone. If someone sent you this link, cancel.")


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


def _user(engine, display_name: str) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name=display_name, email=f"{uuid.uuid4().hex}@e.com")
        sess.add(u)
        sess.commit()
        return u.id


@pytest.fixture
def client():
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _state(uid: int, return_to: str = "app") -> str:
    return create_strava_oauth_state(uid, CHALLENGE, return_to)


def _callback(client, **params):
    return client.get("/api/strava/callback", params=params, follow_redirects=False)


def _page(client, engine, display_name: str = "Ana"):
    uid = _user(engine, display_name)
    state = _state(uid)
    resp = _callback(client, code=CODE, state=state)
    assert resp.status_code == 200, resp.headers.get("location")
    return resp, state


def _text(page: str) -> str:
    """The page's visible text, tags dropped and whitespace collapsed."""
    body = page.split("<body>", 1)[1]
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


def _hrefs(page: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]*)"', page)]


def _continue_link(page: str) -> str:
    hrefs = re.findall(r'<a class="continue" href="([^"]*)"', page)
    assert len(hrefs) == 1, page
    return html.unescape(hrefs[0])


def _name_block(page: str) -> str:
    """The inner HTML of the element with class ``name``."""
    blocks = re.findall(r'<(\w+) class="name">(.*?)</\1>', page, re.DOTALL)
    assert len(blocks) == 1, page
    return blocks[0][1]


def _style(page: str) -> str:
    return re.search(r"<style>(.*?)</style>", page, re.DOTALL).group(1)


def _cancelled_section(page: str) -> str:
    return re.search(r'<section id="cancelled"[^>]*>(.*?)</section>', page, re.DOTALL).group(1)


def _bindings(engine) -> dict[str, str]:
    with Session(engine) as sess:
        return {r.code_hash: r.state_jti for r in sess.exec(select(StravaOAuthCode)).all()}


def _jti(state: str) -> str:
    return jwt.decode(state, options={"verify_signature": False})["jti"]


def _location(resp):
    assert resp.status_code in (302, 307)
    loc = resp.headers["location"]
    return loc, parse_qs(urlparse(loc).query)


# ── the page ─────────────────────────────────────────────────────────────────

def test_app_return_shows_a_page_naming_the_account(client, engine):
    resp, _ = _page(client, engine, "Ana Lima")

    assert resp.headers["content-type"].startswith("text/html")
    assert "location" not in resp.headers
    assert "Ana Lima" in _name_block(resp.text)
    assert f"linked to the {APP_NAME} account named" in _text(resp.text)


def test_display_name_is_escaped(client, engine):
    resp, _ = _page(client, engine, '<script>alert("x")</script>')

    assert "<script>" not in resp.text
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in _name_block(resp.text)


@pytest.mark.parametrize("display_name", ["", "   \t "])
def test_empty_display_name_uses_neutral_wording(client, engine, display_name):
    resp, _ = _page(client, engine, display_name)

    assert f"linked to a {APP_NAME} account." in _text(resp.text)
    assert 'class="name"' not in resp.text


def test_continue_link_is_the_app_return_with_code_and_state(client, engine):
    resp, state = _page(client, engine)

    loc = _continue_link(resp.text)

    assert loc.startswith(f"{APP_RETURN}?")
    assert parse_qs(urlparse(loc).query) == {
        "strava": ["code"], "code": [CODE], "state": [state]}
    # Exactly what the callback used to redirect to.
    assert loc == strava_module._return_url("app", strava="code", code=CODE, state=state)


def test_cancel_stays_on_the_page(client, engine):
    resp, _ = _page(client, engine)

    cancel = re.findall(r'<a class="cancel" href="([^"]*)"', resp.text)
    assert cancel == ["#cancelled"]
    assert '<section id="cancelled"' in resp.text
    app_links = [h for h in _hrefs(resp.text) if h.startswith("traxjourney:")]
    assert app_links == [_continue_link(resp.text)]
    # The :target rule swaps the prompt for the cancelled text.
    style = _style(resp.text)
    assert re.search(r"#cancelled:target\s*\{\s*display:\s*block", style)
    assert re.search(r"#cancelled:target\s*~\s*\.prompt\s*\{\s*display:\s*none", style)
    assert resp.text.index('id="cancelled"') < resp.text.index('class="prompt"')


def test_fixed_warning_precedes_the_name(client, engine):
    resp, _ = _page(client, engine, "Ana Lima")
    text = _text(resp.text)

    assert WARNING in text
    assert text.index(WARNING) < text.index("Ana Lima")
    assert resp.text.index('class="warning"') < resp.text.index('class="name"')


def test_long_display_name_is_truncated(client, engine):
    name = "A" * 39 + "BCDEFG"

    resp, _ = _page(client, engine, name)

    assert html.unescape(_name_block(resp.text)) == f"<bdi>{name[:40]}…</bdi>"
    assert "CDEFG" not in resp.text


def test_forty_character_name_is_not_cut(client, engine):
    name = "A" * 40

    resp, _ = _page(client, engine, name)

    assert _name_block(resp.text) == f"<bdi>{name}</bdi>"


def test_name_is_bidi_isolated_and_clipped(client, engine):
    resp, _ = _page(client, engine, "evil‮ecnalab")

    assert _name_block(resp.text) == "<bdi>evil‮ecnalab</bdi>"
    assert resp.text.count("‮") == 1
    rules = re.findall(r"\.name\s*\{([^}]*)\}", _style(resp.text))
    assert len(rules) == 1, rules
    assert re.search(r"overflow:\s*hidden", rules[0])
    assert re.search(r"line-height:\s*[\d.]+", rules[0])


def test_cancel_text_is_hedged(client, engine):
    resp, _ = _page(client, engine)
    cancelled = _text("<body>" + _cancelled_section(resp.text))

    assert "If you didn't press Continue, nothing was connected." in cancelled
    assert "under My Apps in your Strava settings" in cancelled
    assert "You can close this page." in cancelled
    assert APP_NAME not in cancelled
    assert "Nothing was connected." not in cancelled


def test_page_headers_forbid_caching_framing_and_referrer(client, engine):
    resp, _ = _page(client, engine)

    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")


def test_page_has_no_script(client, engine):
    resp, _ = _page(client, engine)

    assert "<script" not in resp.text.lower()
    assert not re.search(r"\son\w+=", resp.text)
    assert "javascript:" not in resp.text.lower()


# ── what still redirects ─────────────────────────────────────────────────────

def test_deleted_account_redirects_invalid_state_and_binds_nothing(client, engine):
    uid = _user(engine, "Ana")
    state = _state(uid)
    with Session(engine) as sess:
        sess.delete(sess.get(UserInfo, uid))
        sess.commit()

    loc, query = _location(_callback(client, code=CODE, state=state))

    assert loc.startswith(f"{APP_RETURN}?")
    assert query == {"strava": ["error"], "reason": ["invalid_state"]}
    assert _bindings(engine) == {}


def test_web_return_is_still_a_redirect(client, engine):
    state = _state(_user(engine, "Ana"), "web")

    loc, query = _location(_callback(client, code=CODE, state=state))

    assert loc.startswith(f"{strava_module._FRONTEND_ORIGIN}/oauth_callback.html?")
    assert query == {"strava": ["code"], "code": [CODE], "state": [state]}


def test_app_errors_still_redirect(client, engine):
    uid = _user(engine, "Ana")
    expired = jwt.encode(
        {**jwt.decode(_state(uid), options={"verify_signature": False}), "exp": 1},
        jwt_secret(), algorithm="HS256")

    denied = _callback(client, error="access_denied", state=_state(uid))
    assert denied.headers["location"] == f"{APP_RETURN}?strava=error&reason=denied"
    no_code = _callback(client, state=_state(uid))
    assert no_code.headers["location"] == f"{APP_RETURN}?strava=error&reason=denied"
    late = _callback(client, code=CODE, state=expired)
    assert late.headers["location"] == f"{APP_RETURN}?strava=error&reason=state_expired"
    assert _bindings(engine) == {}


def test_code_is_bound_before_the_page(client, engine):
    victim = _user(engine, "Victim")
    attacker = _user(engine, "Attacker")
    victim_state = _state(victim)

    assert _callback(client, code=CODE, state=victim_state).status_code == 200
    assert _bindings(engine) == {strava_module._code_hash(CODE): _jti(victim_state)}

    loc, query = _location(_callback(client, code=CODE, state=_state(attacker)))

    assert loc.startswith(f"{APP_RETURN}?")
    assert query == {"strava": ["error"], "reason": ["invalid_state"]}
    assert _bindings(engine) == {strava_module._code_hash(CODE): _jti(victim_state)}
    with Session(engine) as sess:
        assert sess.exec(select(StravaToken)).all() == []


def test_page_is_not_logged(client, engine, caplog):
    with caplog.at_level(logging.DEBUG):
        resp, state = _page(client, engine)

    app_log = "\n".join(
        r.getMessage() for r in caplog.records
        if not r.name.startswith(("httpx", "httpcore")))
    assert CODE in resp.text  # the page does carry it
    for secret in (CODE, state, strava_module._code_hash(CODE)):
        assert secret not in app_log

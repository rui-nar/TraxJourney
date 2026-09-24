"""Disconnecting Strava removes the cached raw data and revokes the app (issue #440).

Regression: ``DELETE /api/strava/disconnect`` used to delete only the
``StravaToken`` row. The cached raw activity list (``DBStravaCache``) stayed
behind, and the app was never revoked at Strava, so it kept showing as
connected in the athlete's Strava settings.

Strava is mocked at the HTTP layer (``src.auth.oauth.requests.post``) so the
tests prove the real ``OAuth2Session`` wiring — ``POST /oauth/revoke`` with
Basic client credentials and the token in the form body, never in the URL —
and never touch the network. They cannot prove the live endpoint accepts it.
"""
from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.strava as strava_module
import models.db as db_module
from api.deps import get_current_user
from api.router import app
from models.project_db import DBActivity, DBProject, DBProjectItem, DBStravaCache
from models.user import StravaToken, UserInfo
from src.auth.oauth import OAuth2Session
from src.config.settings import Config

ACCESS = "access-secret-xyz"
REFRESH = "refresh-secret-abc"


class _StravaConfig(Config):
    def __init__(self):
        super().__init__()
        self.set("strava.client_id", "id")
        self.set("strava.client_secret", "secret")


def _seed(engine, *, refresh_token: str = REFRESH) -> int:
    """One user with a token, a cached activity list, and an activity already
    added to a trip. Returns the user id."""
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u)
        sess.commit()
        sess.refresh(u)
        sess.add(StravaToken(user_info_id=u.id, access_token=ACCESS,
                             refresh_token=refresh_token,
                             expires_at=time.time() + 3600))
        sess.add(DBStravaCache(user_info_id=u.id, fetched_at=time.time(),
                               activities_json='[{"id": 1}]'))
        proj = DBProject(user_info_id=u.id, name="Trip")
        sess.add(proj)
        sess.commit()
        sess.refresh(proj)
        sess.add(DBActivity(id=1, user_info_id=u.id, name="ride"))
        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="activity", activity_id=1))
        sess.commit()
        return u.id


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    # The route builds its OAuth2Session from the module-level config, which
    # carries no Strava keys in CI.
    monkeypatch.setattr(strava_module, "_cfg", _StravaConfig())
    return engine


def _client_for(uid: int) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    return TestClient(app)


@pytest.fixture
def client(engine):
    uid = _seed(engine)
    try:
        yield _client_for(uid), uid
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def post(monkeypatch):
    """``requests.post`` as seen by OAuth2Session, answering 200 to everything."""
    mock = MagicMock()
    mock.return_value.status_code = 200
    monkeypatch.setattr("src.auth.oauth.requests.post", mock)
    return mock


def _assert_local_data_gone_but_trip_kept(engine, uid: int) -> None:
    with Session(engine) as sess:
        assert sess.exec(select(StravaToken).where(StravaToken.user_info_id == uid)).first() is None
        assert sess.get(DBStravaCache, uid) is None
        # Activities already added to a trip are the user's own and stay.
        assert sess.get(DBActivity, 1) is not None
        assert sess.exec(select(DBProjectItem).where(DBProjectItem.activity_id == 1)).first() is not None


def _assert_documented_revoke_shape(call, *, token: str, hint: str) -> None:
    assert call.args[0] == OAuth2Session.REVOKE_URL
    assert call.kwargs["auth"] == ("id", "secret")
    assert call.kwargs["data"] == {"token": token, "token_type_hint": hint}
    # The token travels in the body only — never in the URL.
    assert token not in call.args[0]
    assert "params" not in call.kwargs and "headers" not in call.kwargs
    assert call.kwargs["timeout"] == OAuth2Session.TOKEN_TIMEOUT


def test_disconnect_clears_cache_and_revokes_the_refresh_token(client, engine, post):
    tc, uid = client

    resp = tc.delete("/api/strava/disconnect")

    assert resp.status_code == 204
    _assert_local_data_gone_but_trip_kept(engine, uid)
    post.assert_called_once()
    # The refresh token is revoked (it takes the access tokens with it); no
    # refresh call is made first — Strava's revoke accepts an expired token.
    _assert_documented_revoke_shape(post.call_args, token=REFRESH, hint="refresh_token")

    assert tc.get("/api/strava/status").json() == {"connected": False}
    assert tc.get("/api/strava/cache/status").json()["cached"] is False


def test_access_token_is_revoked_when_no_refresh_token_is_stored(engine, post):
    uid = _seed(engine, refresh_token="")
    tc = _client_for(uid)
    try:
        resp = tc.delete("/api/strava/disconnect")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 204
    _assert_local_data_gone_but_trip_kept(engine, uid)
    post.assert_called_once()
    _assert_documented_revoke_shape(post.call_args, token=ACCESS, hint="access_token")


def test_failed_revoke_still_clears_local_data(client, engine, post, caplog):
    tc, uid = client
    post.return_value.status_code = 401
    post.return_value.text = '{"message":"Unauthorized"}'

    with caplog.at_level(logging.WARNING, logger="src.auth.strava_deauth"):
        resp = tc.delete("/api/strava/disconnect")

    assert resp.status_code == 204
    _assert_local_data_gone_but_trip_kept(engine, uid)
    post.assert_called_once()
    assert "strava revoke failed" in caplog.text
    assert "HTTP 401" in caplog.text
    assert ACCESS not in caplog.text and REFRESH not in caplog.text


def test_revoke_timeout_still_clears_local_data(client, engine, post, caplog):
    tc, uid = client
    post.side_effect = requests.Timeout("Read timed out")

    with caplog.at_level(logging.WARNING, logger="src.auth.strava_deauth"):
        resp = tc.delete("/api/strava/disconnect")

    assert resp.status_code == 204
    _assert_local_data_gone_but_trip_kept(engine, uid)
    assert "strava revoke failed" in caplog.text
    assert ACCESS not in caplog.text and REFRESH not in caplog.text


def test_503_is_retried_once_then_succeeds(client, engine, post, caplog):
    tc, uid = client
    post.side_effect = [MagicMock(status_code=503), MagicMock(status_code=200)]

    with caplog.at_level(logging.WARNING, logger="src.auth.strava_deauth"):
        resp = tc.delete("/api/strava/disconnect")

    assert resp.status_code == 204
    _assert_local_data_gone_but_trip_kept(engine, uid)
    assert post.call_count == 2
    assert "strava revoke failed" not in caplog.text


def test_strava_not_configured_still_clears_local_data(client, engine, post, monkeypatch):
    """OAuth2Session refuses to build without client_id/secret — that must not
    turn disconnect into a 500 or leave the data behind."""
    tc, uid = client
    monkeypatch.setattr(strava_module, "_cfg", Config())

    resp = tc.delete("/api/strava/disconnect")

    assert resp.status_code == 204
    _assert_local_data_gone_but_trip_kept(engine, uid)
    post.assert_not_called()


def test_disconnect_when_not_connected_is_a_noop(engine, post):
    """A stale cache row with no token is still swept; nothing is sent to Strava."""
    with Session(engine) as sess:
        u = UserInfo(display_name="B", email="b@e.com")
        sess.add(u)
        sess.commit()
        sess.refresh(u)
        sess.add(DBStravaCache(user_info_id=u.id, activities_json="[]"))
        sess.commit()
        uid = u.id
    tc = _client_for(uid)
    try:
        resp = tc.delete("/api/strava/disconnect")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 204
    post.assert_not_called()
    with Session(engine) as sess:
        assert sess.get(DBStravaCache, uid) is None


def test_save_cache_does_not_resurrect_the_row_after_disconnect(client, engine, post):
    """A Strava fetch that was in flight when the user disconnected finishes
    after the delete — its _save_cache must not recreate the cache row."""
    tc, uid = client
    assert tc.delete("/api/strava/disconnect").status_code == 204

    strava_module._save_cache(uid, [{"id": 2}])

    with Session(engine) as sess:
        assert sess.get(DBStravaCache, uid) is None


def test_save_cache_still_writes_while_connected(client, engine, post):
    _, uid = client

    strava_module._save_cache(uid, [{"id": 2}])

    with Session(engine) as sess:
        assert sess.get(DBStravaCache, uid).activities_json == '[{"id": 2}]'

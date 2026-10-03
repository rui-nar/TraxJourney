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


# ── Interleavings with an in-flight fetch (review R2-1 / R2-2) ─────────────────
#
# These need a real file database: the in-memory StaticPool hands every session
# the same connection and cannot show two transactions contending.

NEW_ACCESS = "rotated-access-secret"
NEW_REFRESH = "rotated-refresh-secret"


@pytest.fixture
def file_engine(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(strava_module, "_cfg", _StravaConfig())
    try:
        yield engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _disconnect(uid: int) -> None:
    """The route function itself, as the concurrent request would run it."""
    strava_module.strava_disconnect({"sub": str(uid), "email": "a@e.com"})


def _revoked_tokens(post: MagicMock) -> list:
    return [c.kwargs["data"]["token"] for c in post.call_args_list
            if c.args[0] == OAuth2Session.REVOKE_URL]


def _strava_answers(post: MagicMock, *, on_refresh=None) -> None:
    """Route the mocked requests.post: the token endpoint issues rotated
    tokens (running ``on_refresh`` first, if given), revoke answers 200."""
    def _post(url, **_kwargs):
        resp = MagicMock(status_code=200)
        if url == OAuth2Session.TOKEN_URL:
            if on_refresh is not None:
                on_refresh()
            resp.json.return_value = {"access_token": NEW_ACCESS,
                                      "refresh_token": NEW_REFRESH,
                                      "expires_at": time.time() + 21600}
        return resp
    post.side_effect = _post


def _seed_expired(engine) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u)
        sess.commit()
        sess.refresh(u)
        sess.add(StravaToken(user_info_id=u.id, access_token=ACCESS,
                             refresh_token=REFRESH, expires_at=time.time() - 60))
        sess.commit()
        return u.id


def _assert_nothing_left(engine, uid: int) -> None:
    with Session(engine) as sess:
        assert sess.exec(select(StravaToken).where(StravaToken.user_info_id == uid)).first() is None
        assert sess.get(DBStravaCache, uid) is None


def test_fetch_refresh_then_disconnect_revokes_the_rotated_token(file_engine, post, monkeypatch):
    """R2-1, first interleaving: the fetch's first request refreshes the
    expired token, the user disconnects before the fetch ends. The rotation
    was persisted at once, so disconnect revokes the NEW refresh token — the
    only one Strava still knows."""
    uid = _seed_expired(file_engine)
    _strava_answers(post)

    def _activities_page(*_args, **_kwargs):
        # Strava is answering the first page: the refresh has happened and
        # the disconnect lands now, before the fetch returns.
        _disconnect(uid)
        return MagicMock(status_code=200, json=MagicMock(return_value=[]))
    monkeypatch.setattr("src.api.strava_client.requests.request", _activities_page)

    resp = _client_for(uid).get("/api/strava/activities")

    assert resp.status_code == 200
    assert _revoked_tokens(post) == [NEW_REFRESH]
    _assert_nothing_left(file_engine, uid)


def test_disconnect_during_refresh_revokes_the_new_token_too(file_engine, post, monkeypatch):
    """R2-1, second interleaving: the disconnect reads the row while Strava is
    still answering the refresh. It revokes the old refresh token (Strava
    says 200 — it no longer knows it); when the rotation then comes back to
    a row that is gone, the new tokens are revoked as well."""
    uid = _seed_expired(file_engine)
    _strava_answers(post, on_refresh=lambda: _disconnect(uid))
    monkeypatch.setattr("src.api.strava_client.requests.request",
                        lambda *_a, **_k: MagicMock(status_code=200, json=MagicMock(return_value=[])))

    resp = _client_for(uid).get("/api/strava/activities")

    assert resp.status_code == 200
    assert _revoked_tokens(post) == [REFRESH, NEW_REFRESH]
    _assert_nothing_left(file_engine, uid)


def test_save_cache_cannot_interleave_with_a_disconnect(file_engine, post, monkeypatch):
    """R2-2: a disconnect that arrives after _save_cache checked the token but
    before it wrote must wait for that transaction, not slip in between —
    otherwise it deletes nothing and the cache row is written afterwards."""
    import threading
    import types

    with Session(file_engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u)
        sess.commit()
        sess.refresh(u)
        sess.add(StravaToken(user_info_id=u.id, access_token=ACCESS,
                             refresh_token=REFRESH, expires_at=time.time() + 3600))
        sess.commit()
        uid = u.id

    disconnect = threading.Thread(target=_disconnect, args=(uid,))
    seen = {}
    real_time = time.time

    def _time_between_check_and_write():
        # _save_cache calls time.time() after its token check and before its
        # commit: the disconnect lands here. With the write lock taken by the
        # check it must still be waiting when we return.
        if not seen:
            disconnect.start()
            disconnect.join(1.0)
            seen["disconnect_blocked"] = disconnect.is_alive()
        return real_time()
    monkeypatch.setattr(strava_module, "time",
                        types.SimpleNamespace(time=_time_between_check_and_write))

    strava_module._save_cache(uid, [{"id": 1}])
    disconnect.join(35.0)

    assert not disconnect.is_alive()
    assert seen["disconnect_blocked"] is True
    _assert_nothing_left(file_engine, uid)


# ── A rotation committed between the revoke and the delete (review R3-1) ───────
#
# Real interleavings: the disconnect / deletion runs on its own thread, and the
# mocked Strava revoke holds it at the network call while this thread commits
# the rotated tokens through the callback path, exactly as an in-flight fetch's
# refresh would. The row then holds tokens the revoke never saw.

def _seed_valid(engine) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u)
        sess.commit()
        sess.refresh(u)
        sess.add(StravaToken(user_info_id=u.id, access_token=ACCESS,
                             refresh_token=REFRESH, expires_at=time.time() + 3600))
        sess.commit()
        return u.id


def _revoke_that_waits(post: MagicMock, revoking, rotated) -> None:
    """The first revoke signals ``revoking`` and returns only once ``rotated``
    is set; every other call answers 200 at once."""
    import threading
    first = threading.Event()

    def _post(url, **_kwargs):
        if url == OAuth2Session.REVOKE_URL and not first.is_set():
            first.set()
            revoking.set()
            rotated.wait(10.0)
        return MagicMock(status_code=200)
    post.side_effect = _post


def _rotate_through_callback(uid: int) -> None:
    strava_module._persist_rotated_token(uid, {
        "access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH,
        "expires_at": time.time() + 21600,
    })


def _run_with_rotation_during_revoke(post, target, uid: int) -> None:
    import threading
    revoking, rotated = threading.Event(), threading.Event()
    _revoke_that_waits(post, revoking, rotated)
    worker = threading.Thread(target=target)
    worker.start()
    assert revoking.wait(10.0), "the revoke never went out"
    _rotate_through_callback(uid)      # commits: no lock is held during the call
    rotated.set()
    worker.join(35.0)
    assert not worker.is_alive()


def test_disconnect_revokes_tokens_rotated_during_its_revoke(file_engine, post):
    uid = _seed_valid(file_engine)

    _run_with_rotation_during_revoke(post, lambda: _disconnect(uid), uid)

    assert _revoked_tokens(post) == [REFRESH, NEW_REFRESH]
    _assert_nothing_left(file_engine, uid)


def test_account_deletion_revokes_tokens_rotated_during_its_revoke(file_engine, post, monkeypatch):
    from src.auth.account_deletion import delete_user_and_data

    monkeypatch.setenv("STRAVA_CLIENT_ID", "id")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "secret")
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    uid = _seed_valid(file_engine)

    def _delete():
        with Session(file_engine) as sess:
            delete_user_and_data(sess, uid)
    _run_with_rotation_during_revoke(post, _delete, uid)

    assert _revoked_tokens(post) == [REFRESH, NEW_REFRESH]
    _assert_nothing_left(file_engine, uid)
    with Session(file_engine) as sess:
        assert sess.get(UserInfo, uid) is None

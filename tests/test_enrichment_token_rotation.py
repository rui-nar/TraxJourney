"""Stream enrichment must save a Strava token rotation the moment it happens
(issue #512).

``api.activities._strava_client_for_user`` used to build its own StravaAPI
without ``on_token_refresh``: a refresh during enrichment rotated the tokens in
memory only. The next request then refreshed with a refresh token Strava had
already retired, and a disconnect meanwhile revoked that retired token — Strava
answers 200 — leaving the app authorised. The client now comes from
``api.strava._strava_client_for_token``, which persists or revokes every
rotation (issue #440).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, SQLModel, select

import api.activities as activities_module
import api.strava as strava_module
import models.db as db_module
from models.user import StravaToken, UserInfo
from src.auth.oauth import OAuth2Session
from src.config.settings import Config
from src.models.activity import Activity

ACCESS = "access-secret-xyz"
REFRESH = "refresh-secret-abc"
NEW_ACCESS = "rotated-access-secret"
NEW_REFRESH = "rotated-refresh-secret"


class _StravaConfig(Config):
    def __init__(self):
        super().__init__()
        self.set("strava.client_id", "id")
        self.set("strava.client_secret", "secret")


# A real file database, as in tests/test_strava_disconnect.py: the rotation is
# written in its own session while the disconnect runs in another.
@pytest.fixture
def file_engine(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'rotation.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(strava_module, "_cfg", _StravaConfig())
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def post(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("src.auth.oauth.requests.post", mock)
    return mock


@pytest.fixture
def streams(monkeypatch):
    """Strava answers every streams request with a short track."""
    body = {"latlng": {"data": [[48.0, 2.0], [48.0, 2.01]]},
            "distance": {"data": [0.0, 740.0]},
            "altitude": {"data": [100.0, 110.0]}}
    monkeypatch.setattr("src.api.strava_client.requests.request",
                        lambda *_a, **_k: MagicMock(status_code=200, json=MagicMock(return_value=body)))


def _strava_answers(post: MagicMock, *, on_refresh=None) -> None:
    """The token endpoint issues rotated tokens (running ``on_refresh`` first,
    if given); revoke answers 200."""
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


def _revoked_tokens(post: MagicMock) -> list:
    return [c.kwargs["data"]["token"] for c in post.call_args_list
            if c.args[0] == OAuth2Session.REVOKE_URL]


def _enrich_one(uid: int) -> Activity:
    act = Activity.from_strava_api({"id": 7, "name": "Ride", "type": "Ride"})
    client = activities_module._strava_client_for_user(uid)
    assert client is not None
    activities_module._enrich_activities([act], client)
    return act


def test_refresh_during_enrichment_stores_the_rotated_tokens(file_engine, post, streams):
    uid = _seed_expired(file_engine)
    _strava_answers(post)

    act = _enrich_one(uid)

    assert act.summary_polyline  # the streams request went through
    with Session(file_engine) as sess:
        row = sess.exec(select(StravaToken).where(StravaToken.user_info_id == uid)).one()
        assert (row.access_token, row.refresh_token) == (NEW_ACCESS, NEW_REFRESH)
        assert row.expires_at > time.time()
    assert _revoked_tokens(post) == []


def test_refresh_after_a_disconnect_revokes_the_new_tokens(file_engine, post, streams):
    """The user disconnects while Strava is answering enrichment's refresh:
    the disconnect revokes the old refresh token, so the rotation that comes
    back to a missing row must revoke the new one too."""
    uid = _seed_expired(file_engine)
    _strava_answers(post, on_refresh=lambda: strava_module.strava_disconnect(
        {"sub": str(uid), "email": "a@e.com"}))

    _enrich_one(uid)

    assert _revoked_tokens(post) == [REFRESH, NEW_REFRESH]
    with Session(file_engine) as sess:
        assert sess.exec(select(StravaToken).where(StravaToken.user_info_id == uid)).first() is None


def test_no_client_without_a_stored_token(file_engine):
    assert activities_module._strava_client_for_user(12345) is None

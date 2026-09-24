"""Unit tests for OAuth2Session."""

import pytest
import requests
from unittest.mock import MagicMock, patch

from src.config.settings import Config
from src.auth.oauth import OAuth2Session
from src.exceptions.errors import AuthenticationError, TokenError


class DummyConfig(Config):
    def __init__(self):
        super().__init__()
        self.set("strava.client_id", "id")
        self.set("strava.client_secret", "secret")


def test_authorization_url_contains_required_params():
    config = DummyConfig()
    oauth = OAuth2Session(config)
    url = oauth.authorization_url(scope="read")
    assert "client_id=id" in url
    assert "response_type=code" in url
    assert "scope=read" in url


@patch("src.auth.oauth.requests.post")
def test_exchange_code_success(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"access_token": "abc"}

    config = DummyConfig()
    oauth = OAuth2Session(config)
    result = oauth.exchange_code("code123")
    assert result["access_token"] == "abc"
    mock_post.assert_called_once()


@patch("src.auth.oauth.requests.post")
def test_exchange_code_failure(mock_post):
    mock_post.return_value.status_code = 400
    mock_post.return_value.text = "error"

    config = DummyConfig()
    oauth = OAuth2Session(config)
    with pytest.raises(AuthenticationError):
        oauth.exchange_code("badcode")


@patch("src.auth.oauth.requests.post")
def test_refresh_token_success(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"access_token": "new"}

    config = DummyConfig()
    oauth = OAuth2Session(config)
    result = oauth.refresh_token("r123")
    assert result["access_token"] == "new"


@patch("src.auth.oauth.requests.post")
def test_refresh_token_failure(mock_post):
    mock_post.return_value.status_code = 401
    mock_post.return_value.text = "fail"

    config = DummyConfig()
    oauth = OAuth2Session(config)
    with pytest.raises(TokenError):
        oauth.refresh_token("bad")


# ---------------------------------------------------------------------------
# Revoke (issue #440) — the documented shape of POST /oauth/revoke: HTTP Basic
# client credentials, the token in the form body with a type hint, never in
# the URL. Nothing here can prove the live endpoint accepts the call.
# ---------------------------------------------------------------------------

@patch("src.auth.oauth.requests.post")
def test_revoke_uses_basic_auth_and_form_body(mock_post):
    mock_post.return_value.status_code = 200

    OAuth2Session(DummyConfig()).revoke("tok-123", "refresh_token")

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == OAuth2Session.REVOKE_URL
    assert kwargs["auth"] == ("id", "secret")
    assert kwargs["data"] == {"token": "tok-123", "token_type_hint": "refresh_token"}
    assert "tok-123" not in args[0]
    assert "params" not in kwargs and "headers" not in kwargs
    assert kwargs["timeout"] == OAuth2Session.TOKEN_TIMEOUT


@patch("src.auth.oauth.requests.post")
def test_revoke_retries_once_on_503(mock_post):
    ok = MagicMock(status_code=200)
    unavailable = MagicMock(status_code=503)
    mock_post.side_effect = [unavailable, ok]

    OAuth2Session(DummyConfig()).revoke("tok-123", "access_token")

    assert mock_post.call_count == 2


@patch("src.auth.oauth.requests.post")
def test_revoke_gives_up_after_second_503(mock_post):
    mock_post.return_value.status_code = 503

    with pytest.raises(AuthenticationError) as excinfo:
        OAuth2Session(DummyConfig()).revoke("tok-123", "access_token")
    assert mock_post.call_count == 2
    assert "HTTP 503" in str(excinfo.value)


@patch("src.auth.oauth.requests.post")
def test_revoke_failure_raises_without_leaking_body(mock_post):
    mock_post.return_value.status_code = 401
    mock_post.return_value.text = '{"message":"Unauthorized","token":"tok-123"}'

    with pytest.raises(AuthenticationError) as excinfo:
        OAuth2Session(DummyConfig()).revoke("tok-123", "refresh_token")
    assert mock_post.call_count == 1  # only 503 is retried
    assert "HTTP 401" in str(excinfo.value)
    assert "tok-123" not in str(excinfo.value)


@patch("src.auth.oauth.requests.post")
def test_refresh_token_has_a_timeout(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"access_token": "new"}

    OAuth2Session(DummyConfig()).refresh_token("r123")

    assert mock_post.call_args.kwargs["timeout"] == OAuth2Session.TOKEN_TIMEOUT
"""Unit tests for OAuth2Session."""

import pytest
import requests
from unittest.mock import patch

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
# Deauthorize (issue #440)
# ---------------------------------------------------------------------------

@patch("src.auth.oauth.requests.post")
def test_deauthorize_sends_bearer_header_only(mock_post):
    mock_post.return_value.status_code = 200

    config = DummyConfig()
    oauth = OAuth2Session(config)
    oauth.deauthorize("tok-123")

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == OAuth2Session.DEAUTHORIZE_URL
    assert kwargs["headers"] == {"Authorization": "Bearer tok-123"}
    assert "tok-123" not in args[0]
    assert "data" not in kwargs and "params" not in kwargs
    assert kwargs["timeout"] == OAuth2Session.TOKEN_TIMEOUT


@patch("src.auth.oauth.requests.post")
def test_deauthorize_failure_raises_without_leaking_body(mock_post):
    mock_post.return_value.status_code = 401
    mock_post.return_value.text = '{"message":"Authorization Error","access_token":"tok-123"}'

    config = DummyConfig()
    oauth = OAuth2Session(config)
    with pytest.raises(AuthenticationError) as excinfo:
        oauth.deauthorize("tok-123")
    assert "HTTP 401" in str(excinfo.value)
    assert "tok-123" not in str(excinfo.value)


@patch("src.auth.oauth.requests.post")
def test_refresh_token_has_a_timeout(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"access_token": "new"}

    OAuth2Session(DummyConfig()).refresh_token("r123")

    assert mock_post.call_args.kwargs["timeout"] == OAuth2Session.TOKEN_TIMEOUT
"""Unit tests for StravaAPI client with mocked requests."""

import logging
import time
import pytest
from unittest.mock import patch, MagicMock

from src.config.settings import Config
from src.api.strava_client import StravaAPI, RateLimiter
from src.auth.oauth import OAuth2Session
from src.exceptions.errors import APIError, AuthenticationError


class DummyConfig(Config):
    def __init__(self):
        super().__init__()
        self.set("strava.client_id", "id")
        self.set("strava.client_secret", "secret")


def _client_with_token(expires_offset: int = 1000) -> StravaAPI:
    config = DummyConfig()
    client = StravaAPI(config)
    client.token_data = {
        "access_token": "t",
        "refresh_token": "r",
        "expires_at": time.time() + expires_offset,
    }
    return client


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def test_set_token_and_store(tmp_path, monkeypatch):
    config = DummyConfig()
    client = StravaAPI(config)
    dummy_token = {"access_token": "abc", "refresh_token": "r", "expires_at": time.time() + 1000}
    client.set_token(dummy_token)
    assert client.token_data["access_token"] == "abc"


def test_ensure_token_refresh(monkeypatch):
    client = _client_with_token(expires_offset=-10)

    refreshed = {"access_token": "new", "refresh_token": "r2", "expires_at": time.time() + 1000}
    monkeypatch.setattr(OAuth2Session, "refresh_token", lambda self, rt: refreshed)

    client._ensure_token()
    assert client.token_data["access_token"] == "new"


def test_on_token_refresh_fires_when_an_expired_token_is_refreshed(monkeypatch):
    """Issue #440: the owner of the client persists a rotation the moment it
    happens, so the callback must fire from the pre-request refresh."""
    client = _client_with_token(expires_offset=-10)
    refreshed = {"access_token": "new", "refresh_token": "r2", "expires_at": time.time() + 1000}
    monkeypatch.setattr(OAuth2Session, "refresh_token", lambda self, rt: refreshed)
    seen = []
    client.on_token_refresh = seen.append

    client._ensure_token()

    assert seen == [refreshed]


@patch("src.api.strava_client.requests.request")
def test_on_token_refresh_fires_on_the_401_refresh_path(mock_req, monkeypatch):
    client = _client_with_token()
    refreshed = {"access_token": "new", "refresh_token": "r2", "expires_at": time.time() + 1000}
    monkeypatch.setattr(OAuth2Session, "refresh_token", lambda self, rt: refreshed)
    unauthorized = MagicMock(status_code=401, text="expired")
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"id": 1}
    mock_req.side_effect = [unauthorized, ok]
    seen = []
    client.on_token_refresh = seen.append

    assert client.request("GET", "/athlete") == {"id": 1}

    assert seen == [refreshed]
    assert mock_req.call_args.kwargs["headers"] == {"Authorization": "Bearer new"}


def test_request_without_token_raises():
    config = DummyConfig()
    client = StravaAPI(config)
    client.token_data = {}
    with pytest.raises(AuthenticationError):
        client.request("GET", "/athlete/activities")


def test_clear_token_removes_data():
    client = _client_with_token()
    client.clear_token()
    assert client.token_data == {}


# ---------------------------------------------------------------------------
# Logging (issue #205) — the client used to swallow both of these failures
# with a bare `except Exception: pass`.
# ---------------------------------------------------------------------------

@patch("src.api.strava_client.requests.request")
def test_request_401_refresh_failure_is_logged(mock_req, caplog):
    """A failed token refresh during the 401 retry path must be logged, not
    silently discarded — the caller still gets AuthenticationError either way."""
    client = _client_with_token()
    mock_req.return_value = MagicMock(status_code=401, text="unauthorized")

    with patch.object(OAuth2Session, "refresh_token", side_effect=Exception("refresh token revoked")):
        with caplog.at_level(logging.WARNING, logger="src.api.strava_client"):
            with pytest.raises(AuthenticationError):
                client.request("GET", "/test")

    assert "token refresh after 401 failed" in caplog.text


# ---------------------------------------------------------------------------
# Successful requests
# ---------------------------------------------------------------------------

@patch("src.api.strava_client.requests.request")
def test_get_activities_success(mock_req):
    client = _client_with_token()
    mock_req.return_value.status_code = 200
    mock_req.return_value.json.return_value = [{"id": 1}]

    result = client.get_activities()
    assert result == [{"id": 1}]
    mock_req.assert_called_once()


@patch("src.api.strava_client.requests.request")
def test_request_passes_params(mock_req):
    client = _client_with_token()
    mock_req.return_value.status_code = 200
    mock_req.return_value.json.return_value = []

    client.get_activities(per_page=50, page=2)
    _, kwargs = mock_req.call_args
    assert kwargs["params"]["per_page"] == 50
    assert kwargs["params"]["page"] == 2


# ---------------------------------------------------------------------------
# Socket timeout (issue #148)
# ---------------------------------------------------------------------------

@patch("src.api.strava_client.requests.request")
def test_request_always_passes_a_timeout(mock_req):
    """Without this, requests waits forever and a stalled Strava connection
    parks a worker thread indefinitely — the silent half of issue #148."""
    client = _client_with_token()
    mock_req.return_value.status_code = 200
    mock_req.return_value.json.return_value = []

    client.get_activities()
    _, kwargs = mock_req.call_args
    assert kwargs["timeout"] == StravaAPI.TIMEOUT
    assert kwargs["timeout"] is not None


@patch("src.api.strava_client.requests.request")
def test_request_honours_a_caller_supplied_timeout(mock_req):
    client = _client_with_token()
    mock_req.return_value.status_code = 200
    mock_req.return_value.json.return_value = []

    client.request("GET", "/test", timeout=(1, 2))
    _, kwargs = mock_req.call_args
    assert kwargs["timeout"] == (1, 2)


@patch("src.api.strava_client.time.sleep")
@patch("src.api.strava_client.requests.request")
def test_retries_keep_the_caller_supplied_timeout(mock_req, mock_sleep):
    """The timeout is read once, outside the retry loop: popping it per attempt
    would silently drop the caller's budget on every retry."""
    client = _client_with_token()
    fail = MagicMock(status_code=500, text="oops")
    success = MagicMock(status_code=200)
    success.json.return_value = {"ok": True}
    mock_req.side_effect = [fail, success]

    client.request("GET", "/test", max_retries=3, timeout=(1, 2))

    assert mock_req.call_count == 2
    for call in mock_req.call_args_list:
        assert call.kwargs["timeout"] == (1, 2)


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

@patch("src.api.strava_client.time.sleep")
@patch("src.api.strava_client.requests.request")
def test_request_retries_on_500_then_succeeds(mock_req, mock_sleep):
    client = _client_with_token()
    fail = MagicMock(status_code=500, text="oops")
    success = MagicMock(status_code=200)
    success.json.return_value = {"ok": True}
    mock_req.side_effect = [fail, fail, success]

    result = client.request("GET", "/test", max_retries=3)
    assert result == {"ok": True}
    assert mock_req.call_count == 3
    # Slept twice: after attempt 0 (1s) and attempt 1 (2s)
    assert mock_sleep.call_count == 2


@patch("src.api.strava_client.time.sleep")
@patch("src.api.strava_client.requests.request")
def test_request_500_exponential_backoff(mock_req, mock_sleep):
    client = _client_with_token()
    mock_req.return_value = MagicMock(status_code=500, text="err")

    with pytest.raises(APIError):
        client.request("GET", "/test", max_retries=3)

    sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
    assert sleep_args == [1, 2]  # 2**0, 2**1; no sleep on last attempt


@patch("src.api.strava_client.time.sleep")
@patch("src.api.strava_client.requests.request")
def test_request_raises_after_max_retries_on_500(mock_req, mock_sleep):
    client = _client_with_token()
    mock_req.return_value = MagicMock(status_code=500, text="oops")

    with pytest.raises(APIError, match="after 3 attempts"):
        client.request("GET", "/test", max_retries=3)
    assert mock_req.call_count == 3


@patch("src.api.strava_client.time.sleep")
@patch("src.api.strava_client.requests.request")
def test_request_retries_on_429_and_succeeds(mock_req, mock_sleep):
    client = _client_with_token()
    rate_limited = MagicMock(status_code=429, headers={"Retry-After": "1"})
    success = MagicMock(status_code=200)
    success.json.return_value = {"ok": True}
    mock_req.side_effect = [rate_limited, success]

    result = client.request("GET", "/test", max_retries=3)
    assert result == {"ok": True}
    mock_sleep.assert_called_once_with(60)  # max(1, 60) = 60


@patch("src.api.strava_client.time.sleep")
@patch("src.api.strava_client.requests.request")
def test_request_429_respects_large_retry_after(mock_req, mock_sleep):
    client = _client_with_token()
    rate_limited = MagicMock(status_code=429, headers={"Retry-After": "120"})
    success = MagicMock(status_code=200)
    success.json.return_value = {}
    mock_req.side_effect = [rate_limited, success]

    client.request("GET", "/test", max_retries=2)
    mock_sleep.assert_called_once_with(120)  # max(120, 60) = 120


@patch("src.api.strava_client.requests.request")
def test_request_raises_immediately_on_4xx_no_retry(mock_req):
    """4xx errors (not 429) must raise immediately without retrying."""
    client = _client_with_token()
    mock_req.return_value = MagicMock(status_code=403, text="forbidden")

    with pytest.raises(APIError, match="403"):
        client.request("GET", "/test", max_retries=3)
    assert mock_req.call_count == 1


@patch("src.api.strava_client.requests.request")
def test_request_api_error(mock_req):
    """Backward-compat: 5xx eventually raises APIError."""
    client = _client_with_token()
    mock_req.return_value = MagicMock(status_code=500, text="oops")
    with patch("src.api.strava_client.time.sleep"):
        with pytest.raises(APIError):
            client.request("GET", "/athlete/activities", max_retries=1)


# ---------------------------------------------------------------------------
# RateLimiter unit tests
# ---------------------------------------------------------------------------

def test_rate_limiter_acquire_does_not_block_under_limit():
    limiter = RateLimiter()
    start = time.monotonic()
    for _ in range(10):
        limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # 10 requests should be near-instant


def test_rate_limiter_current_usage_tracks_count():
    limiter = RateLimiter()
    for _ in range(5):
        limiter.acquire()
    assert limiter.current_usage == 5


def test_rate_limiter_usage_drops_outside_window():
    """Timestamps older than WINDOW_SECONDS should not count toward usage."""
    limiter = RateLimiter()
    old_time = time.monotonic() - limiter.WINDOW_SECONDS - 1
    limiter._timestamps.extend([old_time] * 10)
    assert limiter.current_usage == 0


def test_rate_limiter_window_constants():
    limiter = RateLimiter()
    assert limiter.MAX_REQUESTS == 100
    assert limiter.WINDOW_SECONDS == 900

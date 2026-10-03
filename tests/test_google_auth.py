"""Tests for the /api/auth/google login endpoint.

Focus: verification is resilient to small host clock drift (Google mints tokens
against its own clock, so a server lagging by a second must not reject fresh
tokens), and failure reasons are exposed only on dev builds.
Failure logs carry the exception class and a fixed reason label, never the
exception text: google-auth quotes the posted token in it (#510).
"""
import base64
import json
import logging
from unittest.mock import patch

import pytest

from fastapi.testclient import TestClient


def _client() -> TestClient:
    import api.router as router
    return TestClient(router.app)


def test_google_login_forwards_clock_skew_tolerance():
    """The Google verification must allow a non-zero clock-skew window so a
    server whose clock lags slightly does not reject freshly minted tokens."""
    import api.auth as auth

    with patch.object(
        auth, "verify_oauth2_token",
        side_effect=ValueError("Token used too early, 1 < 2."),
    ) as mock_verify, patch.object(auth, "_google_client_id", "client-123"):
        resp = _client().post("/api/auth/google", json={"id_token": "x"})

    assert resp.status_code == 401
    assert auth._GOOGLE_CLOCK_SKEW_SECONDS > 0
    _, kwargs = mock_verify.call_args
    assert kwargs.get("clock_skew_in_seconds") == auth._GOOGLE_CLOCK_SKEW_SECONDS


def test_google_login_keeps_response_generic_and_logs_reason(caplog):
    """The client only ever sees a generic 401; the reason is logged
    server-side as the exception class and a fixed label — never the
    exception text, in the response body or in the log."""
    import api.auth as auth

    with patch.object(
        auth, "verify_oauth2_token", side_effect=ValueError("boom-reason"),
    ), patch.object(auth, "_google_client_id", "client-123"), \
            caplog.at_level(logging.WARNING, logger="api.auth"):
        resp = _client().post("/api/auth/google", json={"id_token": "x"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid Google id_token"
    assert "boom-reason" not in resp.json()["detail"]
    assert "ValueError (reason=other)" in caplog.text
    assert "boom-reason" not in caplog.text


def test_google_login_unconfigured_returns_503():
    """With no client id configured the endpoint reports unavailable, not 401."""
    import api.auth as auth

    with patch.object(auth, "_google_client_id", ""):
        resp = _client().post("/api/auth/google", json={"id_token": "x"})

    assert resp.status_code == 503


# A value nothing else in a log line could contain by coincidence.
SECRET = "SECRETq7x9v2"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _server_log_text(caplog) -> str:
    return "\n".join(
        f"{r.levelname} {r.name} {r.getMessage()}" + (f"\n{r.exc_text}" if r.exc_text else "")
        for r in caplog.records
    )


@pytest.fixture
def server_log(caplog):
    """Every logger at DEBUG, as in tests/test_access_log_privacy.py."""
    with caplog.at_level(logging.DEBUG, logger="api"), caplog.at_level(logging.DEBUG, logger="src"), \
            caplog.at_level(logging.DEBUG):
        yield caplog


# The garbage token's payload segment is valid base64 of a JSON string, not an
# object, so google-auth's MalformedError quotes that raw segment.
_THREE_SEGMENT_PAYLOAD = _b64(json.dumps(SECRET + "payload").encode())
_THREE_SEGMENT_TOKEN = ".".join(
    (_b64(b'{"alg": "RS256"}'), _THREE_SEGMENT_PAYLOAD, _b64(SECRET.encode()))
)


@pytest.mark.parametrize("id_token, fragments", [
    ("SECRETtoken", ["SECRETtoken"]),
    (_THREE_SEGMENT_TOKEN, [_THREE_SEGMENT_TOKEN, *_THREE_SEGMENT_TOKEN.split("."), SECRET]),
], ids=["malformed", "three-segment-garbage"])
def test_google_login_failure_never_logs_the_token(server_log, id_token, fragments):
    """The real google-auth verification (certs stubbed, no network) rejects
    the token; no log line may carry the token or any of its segments."""
    import api.auth as auth

    with patch("google.oauth2.id_token._fetch_certs", return_value={}), \
            patch.object(auth, "_google_client_id", "client-123"):
        resp = _client().post("/api/auth/google", json={"id_token": id_token})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid Google id_token"
    text = _server_log_text(server_log)
    for fragment in fragments:
        assert fragment not in text
    assert "MalformedError (reason=malformed)" in text


def test_google_login_expired_token_logs_expired_reason(server_log):
    """Clock-skew diagnosis needs the expired / too-early distinction, so the
    mapped reason says so even though the exception text is dropped."""
    from google.auth import exceptions

    import api.auth as auth

    with patch.object(
        auth, "verify_oauth2_token",
        side_effect=exceptions.InvalidValue(f"Token expired, 1 < 2 {SECRET}"),
    ), patch.object(auth, "_google_client_id", "client-123"):
        resp = _client().post("/api/auth/google", json={"id_token": "x"})

    assert resp.status_code == 401
    text = _server_log_text(server_log)
    assert "Google id_token verification failed: InvalidValue (reason=expired)" in text
    assert SECRET not in text

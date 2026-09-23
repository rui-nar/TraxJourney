"""Auth-failure logging (issue #205, Unit F).

`decode_token`/`require_admin` used to fail silently on every path — an
expired token, a tampered/forged token, and a malformed ``sub`` claim were all
indistinguishable 401/403s with nothing server-side to investigate from.

Per docs/LOGGING_OBSERVABILITY_PLAN.md Section 5's open decision #3, plain
expiry (routine, high-volume — every issued token expires eventually) and
actually-suspicious failures (bad signature, malformed claims — possible
forged/tampered token or a client bug) get two distinct signals: expiry stays
quiet (see the comment in `decode_token`), the rest logs at WARNING.
"""
from __future__ import annotations

import datetime
import logging

import jwt
import pytest
from fastapi import HTTPException

from api.deps import (
    _JWT_ALGORITHM,
    decode_token,
    decode_token_quietly,
    jwt_secret,
    require_admin,
)


def _token(payload: dict, secret: str | None = None) -> str:
    return jwt.encode(payload, secret or jwt_secret(), algorithm=_JWT_ALGORITHM)


class TestExpiredTokenIsQuiet:
    def test_expired_token_raises_401(self, caplog):
        token = _token({
            "sub": "1",
            "exp": datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=1),
        })
        with caplog.at_level(logging.DEBUG, logger="api.deps"):
            with pytest.raises(HTTPException) as exc:
                decode_token(token)
        assert exc.value.status_code == 401

        # Chosen behavior (see docs/LOGGING_OBSERVABILITY_PLAN.md Section 5,
        # open decision #3): plain expiry is routine/high-volume and is not
        # logged at all, so no WARNING (or any) record should appear.
        assert caplog.records == []

    def test_raw_token_never_appears_in_logs(self, caplog):
        token = _token({
            "sub": "1",
            "exp": datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=1),
        })
        with caplog.at_level(logging.DEBUG, logger="api.deps"):
            with pytest.raises(HTTPException):
                decode_token(token)
        assert token not in caplog.text


class TestInvalidSignatureTokenWarns:
    def test_invalid_signature_logs_warning(self, caplog):
        forged = _token({"sub": "1"}, secret="a-different-key" * 4)
        with caplog.at_level(logging.WARNING, logger="api.deps"):
            with pytest.raises(HTTPException) as exc:
                decode_token(forged)
        assert exc.value.status_code == 401

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "invalid" in warnings[0].message.lower()

    def test_raw_token_never_appears_in_logs(self, caplog):
        forged = _token({"sub": "1"}, secret="a-different-key" * 4)
        with caplog.at_level(logging.WARNING, logger="api.deps"):
            with pytest.raises(HTTPException):
                decode_token(forged)
        assert forged not in caplog.text

    def test_malformed_token_string_logs_warning(self, caplog):
        """Not even parseable as a JWT (e.g. a client bug) — still InvalidTokenError."""
        with caplog.at_level(logging.WARNING, logger="api.deps"):
            with pytest.raises(HTTPException) as exc:
                decode_token("not-a-jwt-at-all")
        assert exc.value.status_code == 401
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1


class TestQuietDecodeNeverLogs:
    """`decode_token_quietly` is for callers that only *observe* a token (the
    access-log middleware binding user_id) and never reject the request on it
    — so it must not emit the warning that belongs to the rejecting path
    (issue #446), whatever is wrong with the token."""

    def test_valid_token_decodes(self):
        assert decode_token_quietly(_token({"sub": "1"}))["sub"] == "1"

    @pytest.mark.parametrize("bad_token", [
        "not-a-jwt-at-all",
        _token({"sub": "1"}, secret="a-different-key" * 4),
        _token({
            "sub": "1",
            "exp": datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=1),
        }),
    ], ids=["malformed", "forged", "expired"])
    def test_bad_token_is_none_and_silent(self, caplog, bad_token):
        with caplog.at_level(logging.DEBUG, logger="api.deps"):
            assert decode_token_quietly(bad_token) is None
        assert caplog.records == []


class TestMalformedSubClaimWarns:
    @pytest.mark.parametrize("payload", [
        {},  # KeyError — no 'sub' at all
        {"sub": None},  # TypeError
        {"sub": "not-a-number"},  # ValueError
    ])
    def test_malformed_sub_logs_warning(self, caplog, payload):
        with caplog.at_level(logging.WARNING, logger="api.deps"):
            with pytest.raises(HTTPException) as exc:
                require_admin(payload)
        assert exc.value.status_code == 403

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "sub" in warnings[0].message.lower()

    def test_raw_token_never_appears_in_logs(self, caplog):
        """The claim is logged for investigation, but the signed token that
        carried it never is — require_admin only ever sees the decoded dict,
        never the raw JWT string, so this asserts there's nothing token-shaped
        in the message."""
        token = _token({"sub": "not-a-number"})
        payload = decode_token(token)
        with caplog.at_level(logging.WARNING, logger="api.deps"):
            with pytest.raises(HTTPException):
                require_admin(payload)
        assert token not in caplog.text

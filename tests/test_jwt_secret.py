"""The session signing key must be configured, or nothing starts (issue #156).

`api/deps.py` used to fall back to a constant that ships in this public
repository. Every deployment that never set `JWT_SECRET` therefore signed tokens
with a key anyone could read, and the token payload carries `is_admin`. The
failure mode was silence — the app worked perfectly and said nothing.

These lock the fallback out permanently. `conftest.py` sets a real key for the
suite, so each case here clears or overrides it explicitly.
"""
from __future__ import annotations

import jwt
import pytest

from api.deps import _PUBLISHED_DEFAULT, create_access_token, decode_token, jwt_secret
from models.user import UserInfo


class TestJwtSecret:
    def test_unset_is_refused(self, monkeypatch):
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="JWT_SECRET is not configured"):
            jwt_secret()

    def test_blank_is_refused(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET", "   ")
        with pytest.raises(RuntimeError):
            jwt_secret()

    def test_the_old_published_default_is_refused_by_name(self, monkeypatch):
        """Copying it out of an old README is as forgeable as never setting it."""
        monkeypatch.setenv("JWT_SECRET", _PUBLISHED_DEFAULT)
        with pytest.raises(RuntimeError):
            jwt_secret()

    def test_the_error_names_the_fix(self, monkeypatch):
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError) as exc:
            jwt_secret()
        assert "openssl rand -hex 32" in str(exc.value)

    def test_a_configured_key_is_returned_stripped(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET", "  a-real-key  ")
        assert jwt_secret() == "a-real-key"

    def test_read_per_call_not_cached_at_import(self, monkeypatch):
        """So a restart rotates the key, and a test can change it."""
        monkeypatch.setenv("JWT_SECRET", "first")
        assert jwt_secret() == "first"
        monkeypatch.setenv("JWT_SECRET", "second")
        assert jwt_secret() == "second"


class TestTokensUseIt:
    def _user(self):
        return UserInfo(id=1, email="a@b.c", display_name="A",
                        auth_provider="local", is_admin=False)

    def test_a_token_signed_with_another_key_is_rejected(self, monkeypatch):
        """The point of the whole change: a key we did not choose must not verify."""
        monkeypatch.setenv("JWT_SECRET", "the-real-key" * 4)
        forged = jwt.encode({"sub": "1", "is_admin": True},
                            _PUBLISHED_DEFAULT, algorithm="HS256")
        with pytest.raises(Exception) as exc:
            decode_token(forged)
        assert "401" in str(exc.value) or "Invalid token" in str(exc.value)

    def test_round_trip_under_a_configured_key(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET", "another-real-key" * 4)
        token = create_access_token(self._user())
        assert decode_token(token)["sub"] == "1"

    def test_rotating_the_key_invalidates_existing_tokens(self, monkeypatch):
        """Operators need to know this: rotation signs everyone out."""
        monkeypatch.setenv("JWT_SECRET", "key-one" * 8)
        token = create_access_token(self._user())
        assert decode_token(token)["sub"] == "1"

        monkeypatch.setenv("JWT_SECRET", "key-two" * 8)
        with pytest.raises(Exception):
            decode_token(token)

    def test_signing_without_a_key_raises_rather_than_signing(self, monkeypatch):
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError):
            create_access_token(self._user())


# ── A secret PyJWT cannot use (issue #453) ─────────────────────────────────

import os  # noqa: E402

_PEM = ("-----BEGIN PUBLIC KEY-----\n"
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n"
        "-----END PUBLIC KEY-----")
_SSH = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx operator@host"
#: What os.environ holds on Linux for a Windows-1252 "é" in an .env file:
#: bytes that aren't UTF-8 come through as lone surrogates.
_NOT_UTF8 = "caf\udce9-" + "x" * 32
_HEX = "3f" * 32


def _set_secret(monkeypatch, value: str) -> None:
    # A copy of the environment rather than setenv: Windows cannot hold a
    # lone surrogate in a real environment variable.
    monkeypatch.setattr(os, "environ", {**os.environ, "JWT_SECRET": value})


class TestUnusableSecretIsRefused:
    @pytest.mark.parametrize("value, says", [
        (_NOT_UTF8, "UTF-8"),
        (_PEM, "key"),
        (_SSH, "key"),
    ], ids=["not-utf8", "pem", "ssh"])
    def test_refused_with_a_readable_message(self, monkeypatch, value, says):
        _set_secret(monkeypatch, value)

        with pytest.raises(RuntimeError) as exc:
            jwt_secret()

        message = str(exc.value)
        assert "JWT_SECRET" in message and says in message
        assert "openssl rand -hex 32" in message

    @pytest.mark.parametrize("value", [_NOT_UTF8, _PEM, _SSH],
                             ids=["not-utf8", "pem", "ssh"])
    def test_startup_fails_before_anything_else(self, monkeypatch, value):
        import api.router as router
        from fastapi.testclient import TestClient

        _set_secret(monkeypatch, value)
        # The worker path: the lifespan checks the secret and then yields,
        # touching nothing else, so a passing check boots cleanly here.
        monkeypatch.setattr(router, "_IS_API_PROCESS", False)

        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            with TestClient(router.app):
                pass

    def test_a_hex_secret_boots(self, monkeypatch):
        import api.router as router
        from fastapi.testclient import TestClient

        _set_secret(monkeypatch, _HEX)
        monkeypatch.setattr(router, "_IS_API_PROCESS", False)

        with TestClient(router.app) as client:
            assert client.get("/api/version").status_code == 200
        assert jwt_secret() == _HEX

    def test_the_check_runs_once_per_value_not_per_request(self, monkeypatch):
        """jwt_secret() is on every authenticated request (decode_token)."""
        import api.deps as deps

        calls = []
        real_encode = deps.jwt.encode

        def counting_encode(*a, **kw):
            calls.append(1)
            return real_encode(*a, **kw)

        monkeypatch.setattr(deps.jwt, "encode", counting_encode)
        _set_secret(monkeypatch, "a" * 64)
        for _ in range(5):
            jwt_secret()
        assert len(calls) == 1

        _set_secret(monkeypatch, "b" * 64)
        jwt_secret()
        jwt_secret()
        assert len(calls) == 2

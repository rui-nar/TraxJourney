"""Tests for src/auth/apple.py, the Sign in with Apple library (#580).

Apple is never contacted: the JWKS is a locally generated RSA key served
through a stubbed urllib opener, and /auth/token and /auth/revoke are a
mocked ``requests.post``. Failure logs must never carry a token, a code, a
secret or an exception's text (the #510 rule).
"""
import hashlib
import io
import json
import logging
import time
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from src.auth import apple

KID = "test-kid"
BUNDLE_ID = "com.traxjourney.app"
SERVICES_ID = "com.traxjourney.web"
NONCE = "raw-nonce-SECRETnonce"
SECRET = "SECRETmarker"

_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_EC_KEY = ec.generate_private_key(ec.SECP256R1())
_EC_PEM = _EC_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def _jwks(*pairs) -> dict:
    keys = []
    for kid, key in pairs:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
        keys.append(jwk)
    return {"keys": keys}


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": "https://appleid.apple.com",
        "aud": BUNDLE_ID,
        "exp": now + 600,
        "iat": now,
        "sub": "001234.abcdef.1234",
        "nonce": hashlib.sha256(NONCE.encode()).hexdigest(),
        "email": "x@privaterelay.appleid.com",
        "email_verified": "true",
        "is_private_email": "true",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not _MISSING}


_MISSING = object()


def _token(claims=None, kid=KID, key=_RSA_KEY) -> str:
    return jwt.encode(
        claims if claims is not None else _claims(), key,
        algorithm="RS256", headers={"kid": kid},
    )


class _FakeOpener:
    """Stands in for urllib's opener inside PyJWKClient.fetch_data."""

    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return io.BytesIO(body)


@pytest.fixture
def jwks(monkeypatch):
    """A fresh JWKS client per test (its cache would leak across tests) and an
    opener serving the test key set; tests swap ``.payload``/``.error``."""
    opener = _FakeOpener(payload=_jwks((KID, _RSA_KEY)))
    monkeypatch.setattr(apple, "_jwks_client", apple._new_jwks_client())
    monkeypatch.setattr("jwt.jwks_client.urllib.request.build_opener", lambda *a: opener)
    return opener


@pytest.fixture
def server_log(caplog):
    with caplog.at_level(logging.DEBUG, logger="src"), caplog.at_level(logging.DEBUG):
        yield caplog


def _log_text(caplog) -> str:
    return "\n".join(
        f"{r.levelname} {r.name} {r.getMessage()}" + (f"\n{r.exc_text}" if r.exc_text else "")
        for r in caplog.records
    )


def _assert_clean(caplog, *fragments):
    text = _log_text(caplog)
    for fragment in fragments:
        assert fragment not in text


def _verify(token, nonce=NONCE, audiences=(BUNDLE_ID, SERVICES_ID)):
    return apple.verify_identity_token(token, nonce, audiences)


# --- verify_identity_token ---------------------------------------------------

def test_valid_token_returns_identity(jwks):
    identity = _verify(_token())
    assert identity == apple.AppleIdentity(
        sub="001234.abcdef.1234",
        email="x@privaterelay.appleid.com",
        email_verified=True,
        is_private_email=True,
        aud=BUNDLE_ID,
    )


def test_services_id_audience_is_accepted_and_reported(jwks):
    identity = _verify(_token(_claims(aud=SERVICES_ID)))
    assert identity.aud == SERVICES_ID


def test_wrong_audience_is_rejected(jwks):
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(_claims(aud="com.someone.else")))
    assert info.value.reason == "audience"


def test_empty_audiences_are_ignored_not_matched(jwks):
    """An unset Services ID must not let a token with an empty aud through.
    PyJWT reads an empty aud as an absent one."""
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(_claims(aud="")), audiences=(BUNDLE_ID, ""))
    assert info.value.reason == "missing_claim"


def test_no_audience_configured_rejects_everything(jwks):
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(), audiences=("", ""))
    assert info.value.reason == "audience"


def test_wrong_issuer_is_rejected(jwks):
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(_claims(iss="https://evil.example")))
    assert info.value.reason == "issuer"


@pytest.mark.parametrize("claim_nonce", [
    hashlib.sha256(b"another-nonce").hexdigest(),
    NONCE,  # the raw nonce instead of its hash
    _MISSING,
], ids=["other-nonce", "raw-not-hashed", "missing"])
def test_wrong_nonce_is_rejected(jwks, claim_nonce):
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(_claims(nonce=claim_nonce)))
    assert info.value.reason == "nonce"


def test_empty_raw_nonce_is_rejected(jwks):
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(_claims(nonce=hashlib.sha256(b"").hexdigest())), nonce="")
    assert info.value.reason == "nonce"


def test_expired_token_is_rejected(jwks):
    now = int(time.time())
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(_claims(iat=now - 3600, exp=now - 60)))
    assert info.value.reason == "expired"


def test_expiry_has_ten_second_leeway(jwks):
    now = int(time.time())
    _verify(_token(_claims(iat=now - 3600, exp=now - 5)))


def test_unknown_kid_is_rejected(jwks):
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(kid="rotated-away", key=_OTHER_RSA_KEY))
    assert info.value.reason == "unknown_kid"


def test_signature_from_another_key_is_rejected(jwks):
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(key=_OTHER_RSA_KEY))
    assert info.value.reason == "signature"


def test_hs256_token_is_rejected(jwks):
    token = jwt.encode(_claims(), "shared-secret-long-enough-for-hs256!", algorithm="HS256", headers={"kid": KID})
    with pytest.raises(apple.AppleTokenInvalid):
        _verify(token)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c"])
def test_malformed_token_is_rejected(jwks, token):
    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(token)
    assert info.value.reason == "malformed"


@pytest.mark.parametrize("error", [
    URLError("down"),
    TimeoutError("timed out"),
], ids=["url-error", "timeout"])
def test_jwks_down_is_unavailable(jwks, error):
    jwks.error = error
    with pytest.raises(apple.AppleUnavailable):
        _verify(_token())


@pytest.mark.parametrize("payload", [
    b"<html>not json</html>",
    [],
    {"keys": []},
    {"nope": 1},
], ids=["not-json", "not-object", "empty-set", "no-keys"])
def test_unusable_jwks_is_unavailable(jwks, payload):
    jwks.payload = payload
    with pytest.raises(apple.AppleUnavailable):
        _verify(_token())


def test_jwks_is_cached_between_verifications(jwks):
    _verify(_token())
    _verify(_token())
    assert jwks.calls == 1


# The unknown-kid refetch (U1R1-1). The clock is moved past any refetch
# cooldown, so each test reaches the refetch itself, not the cooldown branch.

ROTATED_KID = "rotated-in"


@pytest.fixture
def clock(monkeypatch):
    """time.monotonic, moved forward by hand."""
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


def _warm_cache_then_pass_cooldown(jwks, clock):
    _verify(_token())
    assert jwks.calls == 1
    clock[0] += 60


def test_rotated_kid_is_found_by_the_refetch(jwks, clock):
    _warm_cache_then_pass_cooldown(jwks, clock)
    jwks.payload = _jwks((KID, _RSA_KEY), (ROTATED_KID, _OTHER_RSA_KEY))

    identity = _verify(_token(kid=ROTATED_KID, key=_OTHER_RSA_KEY))

    assert identity.sub == "001234.abcdef.1234"
    assert jwks.calls == 2


@pytest.mark.parametrize("payload", [
    b"<html>not json</html>",
    [],
    {"keys": []},
    {"nope": 1},
    {"keys": [{"kty": "nope", "kid": ROTATED_KID}]},
], ids=["not-json", "not-object", "empty-set", "no-keys", "unusable-key"])
def test_unusable_refetch_is_unavailable_not_unknown_kid(jwks, clock, payload):
    _warm_cache_then_pass_cooldown(jwks, clock)
    jwks.payload = payload

    with pytest.raises(apple.AppleUnavailable) as info:
        _verify(_token(kid=ROTATED_KID, key=_OTHER_RSA_KEY))

    assert info.value.reason == "jwks"
    assert jwks.calls == 2


def test_kid_still_missing_after_a_good_refetch_is_unknown_kid(jwks, clock):
    _warm_cache_then_pass_cooldown(jwks, clock)

    with pytest.raises(apple.AppleTokenInvalid) as info:
        _verify(_token(kid=ROTATED_KID, key=_OTHER_RSA_KEY))

    assert info.value.reason == "unknown_kid"
    assert jwks.calls == 2


def test_unknown_kids_refetch_at_most_once_per_cooldown(jwks, clock):
    _warm_cache_then_pass_cooldown(jwks, clock)
    for kid in ("first-unknown", "second-unknown"):
        with pytest.raises(apple.AppleTokenInvalid):
            _verify(_token(kid=kid, key=_OTHER_RSA_KEY))
    assert jwks.calls == 2
    _verify(_token())  # a known kid is still served from the cache
    assert jwks.calls == 2


@pytest.mark.parametrize("value, expected", [
    ("true", True),
    ("false", False),
    (True, True),
    (False, False),
    (_MISSING, False),
    ("True", False),
    (1, False),
    ("yes", False),
], ids=["str-true", "str-false", "bool-true", "bool-false", "missing",
        "str-True", "int-1", "str-yes"])
def test_email_flags_parse_strings_and_booleans(jwks, value, expected):
    identity = _verify(_token(_claims(email_verified=value, is_private_email=value)))
    assert identity.email_verified is expected
    assert identity.is_private_email is expected


def test_missing_email_is_none(jwks):
    assert _verify(_token(_claims(email=_MISSING))).email is None


def test_verify_failures_never_log_the_token_or_nonce(jwks, server_log, monkeypatch):
    bad = [
        _token(_claims(aud="other")),
        _token(_claims(nonce="0" * 64)),
        _token(kid="nope", key=_OTHER_RSA_KEY),
        "garbage." + SECRET + ".token",
    ]
    for token in bad:
        with pytest.raises(apple.AppleTokenInvalid):
            _verify(token)
    monkeypatch.setattr(apple, "_jwks_client", apple._new_jwks_client())
    jwks.error = URLError(SECRET)
    with pytest.raises(apple.AppleUnavailable):
        _verify(_token())
    _assert_clean(server_log, SECRET, NONCE, *bad, *[p for t in bad for p in t.split(".") if len(p) > 8])


# --- client_secret -----------------------------------------------------------

def test_client_secret_claims_and_lifetime():
    now = 1_700_000_000
    secret = apple.client_secret("TEAM123", "KEY456", _EC_PEM, SERVICES_ID, now=now)
    header = jwt.get_unverified_header(secret)
    assert header["alg"] == "ES256"
    assert header["kid"] == "KEY456"
    claims = jwt.decode(
        secret, _EC_KEY.public_key(), algorithms=["ES256"],
        audience="https://appleid.apple.com", options={"verify_exp": False},
    )
    assert claims["iss"] == "TEAM123"
    assert claims["sub"] == SERVICES_ID
    assert claims["aud"] == "https://appleid.apple.com"
    assert claims["iat"] == now
    assert 0 < claims["exp"] - claims["iat"] <= 300


def test_client_secret_defaults_to_current_time():
    secret = apple.client_secret("TEAM123", "KEY456", _EC_PEM, BUNDLE_ID)
    claims = jwt.decode(
        secret, _EC_KEY.public_key(), algorithms=["ES256"], audience="https://appleid.apple.com",
    )
    assert abs(claims["iat"] - time.time()) < 5


# --- exchange_code -----------------------------------------------------------

def _response(status, body=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    if isinstance(body, (dict, list)):
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError("not json")
    return resp


CODE = "SECRETcode-abc"
CLIENT_SECRET = "SECRETclient.secret.jwt"
REFRESH = "SECRETrefresh-xyz"
REDIRECT = "https://val.traxjourney.com/api/auth/apple/callback"


def test_exchange_success_returns_refresh_token():
    with patch.object(apple.requests, "post", return_value=_response(
        200, {"refresh_token": REFRESH, "access_token": "a", "id_token": "i"},
    )) as post:
        assert apple.exchange_code(CODE, SERVICES_ID, CLIENT_SECRET, REDIRECT) == REFRESH
    args, kwargs = post.call_args
    assert args[0] == "https://appleid.apple.com/auth/token"
    assert kwargs["data"] == {
        "client_id": SERVICES_ID,
        "client_secret": CLIENT_SECRET,
        "code": CODE,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
    }
    assert kwargs["timeout"] > 0


def test_exchange_omits_redirect_uri_when_none():
    with patch.object(apple.requests, "post", return_value=_response(
        200, {"refresh_token": REFRESH},
    )) as post:
        apple.exchange_code(CODE, BUNDLE_ID, CLIENT_SECRET, None)
    assert "redirect_uri" not in post.call_args.kwargs["data"]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_exchange_4xx_is_code_rejected(status, server_log):
    with patch.object(apple.requests, "post", return_value=_response(
        status, {"error": "invalid_grant", "error_description": SECRET},
    )):
        with pytest.raises(apple.AppleTokenInvalid) as info:
            apple.exchange_code(CODE, SERVICES_ID, CLIENT_SECRET, REDIRECT)
    assert info.value.reason == "code_rejected"
    text = _log_text(server_log)
    assert "invalid_grant" in text
    _assert_clean(server_log, SECRET, CODE, CLIENT_SECRET)


def test_exchange_4xx_logs_tell_a_config_fault_from_a_bad_code(server_log):
    """R2-3: invalid_client (a wrong key id or Services ID) and invalid_grant
    (a replayed or expired code) both raise code_rejected, but their log
    lines differ by Apple's OAuth code — and never carry its free text."""
    lines = {}
    for error in ("invalid_client", "invalid_grant"):
        server_log.clear()
        with patch.object(apple.requests, "post", return_value=_response(
            400, {"error": error, "error_description": f"{SECRET} {error} details"},
        )):
            with pytest.raises(apple.AppleTokenInvalid) as info:
                apple.exchange_code(CODE, SERVICES_ID, CLIENT_SECRET, REDIRECT)
        assert info.value.reason == "code_rejected"
        lines[error] = _log_text(server_log)
        assert f"error={error}" in lines[error]
        _assert_clean(server_log, SECRET, "details", CODE, CLIENT_SECRET)
    assert lines["invalid_client"] != lines["invalid_grant"]
    assert "invalid_grant" not in lines["invalid_client"]
    assert "invalid_client" not in lines["invalid_grant"]


def test_exchange_undocumented_error_is_not_logged(server_log):
    with patch.object(apple.requests, "post", return_value=_response(400, {"error": SECRET})):
        with pytest.raises(apple.AppleTokenInvalid):
            apple.exchange_code(CODE, SERVICES_ID, CLIENT_SECRET, REDIRECT)
    assert "error=other" in _log_text(server_log)
    _assert_clean(server_log, SECRET)


@pytest.mark.parametrize("status", [500, 502, 503, 302])
def test_exchange_5xx_is_unavailable(status):
    with patch.object(apple.requests, "post", return_value=_response(status, {"error": "x"})):
        with pytest.raises(apple.AppleUnavailable):
            apple.exchange_code(CODE, SERVICES_ID, CLIENT_SECRET, REDIRECT)


@pytest.mark.parametrize("error", [
    requests.Timeout(f"timed out {SECRET}"),
    requests.ConnectionError(f"refused {SECRET}"),
], ids=["timeout", "connection"])
def test_exchange_network_failure_is_unavailable(error, server_log):
    with patch.object(apple.requests, "post", side_effect=error):
        with pytest.raises(apple.AppleUnavailable):
            apple.exchange_code(CODE, SERVICES_ID, CLIENT_SECRET, REDIRECT)
    _assert_clean(server_log, SECRET, CODE, CLIENT_SECRET)


@pytest.mark.parametrize("body", [
    {"access_token": "a"},
    {"refresh_token": ""},
    {"refresh_token": 42},
    [],
    None,
], ids=["missing", "empty", "not-str", "not-object", "not-json"])
def test_exchange_without_refresh_token_is_unavailable(body):
    with patch.object(apple.requests, "post", return_value=_response(200, body)):
        with pytest.raises(apple.AppleUnavailable):
            apple.exchange_code(CODE, SERVICES_ID, CLIENT_SECRET, REDIRECT)


# --- revoke ------------------------------------------------------------------

def test_revoke_success():
    with patch.object(apple.requests, "post", return_value=_response(200)) as post:
        assert apple.revoke(REFRESH, SERVICES_ID, CLIENT_SECRET) is True
    args, kwargs = post.call_args
    assert args[0] == "https://appleid.apple.com/auth/revoke"
    assert kwargs["data"] == {
        "client_id": SERVICES_ID,
        "client_secret": CLIENT_SECRET,
        "token": REFRESH,
        "token_type_hint": "refresh_token",
    }


@pytest.mark.parametrize("outcome", [
    _response(400, {"error": "invalid_client"}),
    _response(500),
    requests.Timeout(f"timed out {SECRET}"),
    RuntimeError(f"boom {SECRET}"),
], ids=["4xx", "5xx", "timeout", "unexpected"])
def test_revoke_failure_returns_false_and_never_raises(outcome, server_log):
    kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    with patch.object(apple.requests, "post", **kwargs):
        assert apple.revoke(REFRESH, SERVICES_ID, CLIENT_SECRET) is False
    assert "Apple revoke failed" in _log_text(server_log)
    _assert_clean(server_log, SECRET, REFRESH, CLIENT_SECRET)


# --- config loader -----------------------------------------------------------

def test_config_unescapes_private_key_and_defaults_bundle_id():
    escaped = _EC_PEM.strip().replace("\n", "\\n")
    cfg = apple.load_apple_config({
        "APPLE_TEAM_ID": "TEAM123",
        "APPLE_SIGNIN_KEY_ID": "KEY456",
        "APPLE_SIGNIN_PRIVATE_KEY": escaped,
        "APPLE_SERVICES_ID": SERVICES_ID,
        "APPLE_REDIRECT_URI": REDIRECT,
    })
    assert cfg.private_key == _EC_PEM.strip()
    assert cfg.bundle_id == "com.traxjourney.app"
    assert cfg.configured
    assert cfg.audiences == ("com.traxjourney.app", SERVICES_ID)
    assert cfg.redirect_uri == REDIRECT
    # The unescaped key signs.
    apple.client_secret(cfg.team_id, cfg.key_id, cfg.private_key, SERVICES_ID)
    assert "PRIVATE KEY" not in repr(cfg)


@pytest.mark.parametrize("missing", ["APPLE_TEAM_ID", "APPLE_SIGNIN_KEY_ID", "APPLE_SIGNIN_PRIVATE_KEY"])
def test_config_not_configured_without_team_key_or_private_key(missing):
    env = {
        "APPLE_TEAM_ID": "TEAM123",
        "APPLE_SIGNIN_KEY_ID": "KEY456",
        "APPLE_SIGNIN_PRIVATE_KEY": _EC_PEM,
    }
    env.pop(missing)
    cfg = apple.load_apple_config(env)
    assert not cfg.configured
    assert cfg.audiences == ("com.traxjourney.app",)


def test_config_bundle_id_override():
    cfg = apple.load_apple_config({"APPLE_BUNDLE_ID": "com.example.other"})
    assert cfg.bundle_id == "com.example.other"


def test_config_services_id_without_redirect_uri_is_left_out_and_warned_once(server_log, monkeypatch):
    """R2-2: Services-ID codes cannot be exchanged without the redirect URI,
    so the Services ID is not accepted as an audience: web and Android tokens
    are refused up front, and the log names the missing variable once."""
    monkeypatch.setattr(apple, "_warned_missing_redirect_uri", False)
    env = {
        "APPLE_TEAM_ID": "TEAM123",
        "APPLE_SIGNIN_KEY_ID": "KEY456",
        "APPLE_SIGNIN_PRIVATE_KEY": _EC_PEM,
        "APPLE_SERVICES_ID": SERVICES_ID,
    }
    for _ in range(3):
        cfg = apple.load_apple_config(env)
        assert cfg.configured
        assert cfg.services_id == SERVICES_ID
        assert cfg.audiences == ("com.traxjourney.app",)
    warnings = [r for r in server_log.records if "APPLE_REDIRECT_URI" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert SERVICES_ID not in warnings[0].getMessage()


def test_config_services_id_with_redirect_uri_is_not_warned(server_log, monkeypatch):
    monkeypatch.setattr(apple, "_warned_missing_redirect_uri", False)
    cfg = apple.load_apple_config({"APPLE_SERVICES_ID": SERVICES_ID, "APPLE_REDIRECT_URI": REDIRECT})
    assert cfg.audiences == ("com.traxjourney.app", SERVICES_ID)
    assert "APPLE_REDIRECT_URI" not in _log_text(server_log)

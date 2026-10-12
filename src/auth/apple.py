"""Sign in with Apple: id_token verification and the token lifecycle (#580).

Four network-facing pieces, all on plain arguments with no ORM, so the login
route and account deletion can call them outside a DB session:

- :func:`verify_identity_token` checks an id_token against Apple's JWKS,
  including the nonce (D4) and either of the two audiences (D5).
- :func:`client_secret` mints the short-lived ES256 JWT Apple wants in place
  of a static client secret.
- :func:`exchange_code` redeems the authorization code for the refresh token
  that account deletion later revokes (D3).
- :func:`revoke` revokes that refresh token, best effort.

Apple is an external service whose answers can be slow, malformed or hostile.
Failures that are the caller's (a bad token, a rejected code) raise
:class:`AppleTokenInvalid`; failures that are Apple's raise
:class:`AppleUnavailable`. Both carry only a fixed reason label, and nothing
here ever logs a token, a code, a secret or an exception's text.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import jwt
import requests

from src.utils.logging import get_logger

_log = get_logger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"
DEFAULT_BUNDLE_ID = "com.traxjourney.app"

_LEEWAY_SECONDS = 10
_SECRET_LIFETIME_SECONDS = 300
_HTTP_TIMEOUT_SECONDS = 10
_JWKS_TIMEOUT_SECONDS = 5
# Apple rotates its signing keys rarely, and an unknown kid forces a refetch
# anyway, so the key set can be held for an hour.
_JWKS_LIFESPAN_SECONDS = 3600

# The OAuth error codes Apple documents for /auth/token. Only these are
# logged: anything else in the body is Apple's free text and is dropped.
_KNOWN_TOKEN_ERRORS = frozenset({
    "invalid_request", "invalid_client", "invalid_grant",
    "unauthorized_client", "unsupported_grant_type", "invalid_scope",
})


class AppleTokenInvalid(Exception):
    """The id_token or the authorization code was refused. ``reason`` is a
    fixed label, safe to log; the message is the label and nothing else."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AppleUnavailable(Exception):
    """Apple could not be reached or answered something unusable. ``reason``
    is a fixed label, safe to log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AppleIdentity:
    sub: str
    email: str | None
    email_verified: bool
    is_private_email: bool
    aud: str


@dataclass(frozen=True)
class AppleConfig:
    team_id: str
    key_id: str
    private_key: str = field(repr=False)
    bundle_id: str
    services_id: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.team_id and self.key_id and self.private_key)

    @property
    def audiences(self) -> tuple[str, ...]:
        """The bundle id (native iOS) and the Services ID (web, Android),
        whichever are set."""
        return tuple(a for a in (self.bundle_id, self.services_id) if a)


def load_apple_config(environ: Mapping[str, str] | None = None) -> AppleConfig:
    """Read the ``APPLE_*`` runtime env vars. The private key may be given on
    one line with literal ``\\n`` sequences, as env files require."""
    env = os.environ if environ is None else environ
    return AppleConfig(
        team_id=env.get("APPLE_TEAM_ID", "").strip(),
        key_id=env.get("APPLE_SIGNIN_KEY_ID", "").strip(),
        private_key=env.get("APPLE_SIGNIN_PRIVATE_KEY", "").replace("\\n", "\n").strip(),
        bundle_id=env.get("APPLE_BUNDLE_ID", "").strip() or DEFAULT_BUNDLE_ID,
        services_id=env.get("APPLE_SERVICES_ID", "").strip(),
        redirect_uri=env.get("APPLE_REDIRECT_URI", "").strip(),
    )


def _new_jwks_client() -> jwt.PyJWKClient:
    # No per-kid LRU (cache_keys): it never expires, so a key Apple withdrew
    # would stay trusted for the life of the process. The key-set cache
    # expires and refreshes on an unknown kid.
    return jwt.PyJWKClient(
        APPLE_KEYS_URL,
        cache_jwk_set=True,
        lifespan=_JWKS_LIFESPAN_SECONDS,
        timeout=_JWKS_TIMEOUT_SECONDS,
    )


_jwks_client = _new_jwks_client()


def _signing_key(kid: str) -> Any:
    try:
        # Loads (or serves from cache) the whole set first, so a fetch or
        # parse failure is told apart from a kid the set does not hold.
        _jwks_client.get_signing_keys()
    except (jwt.PyJWTError, ValueError) as exc:
        _log.warning("Apple JWKS unavailable: %s (reason=jwks)", type(exc).__name__)
        raise AppleUnavailable("jwks") from None
    try:
        return _jwks_client.get_signing_key(kid).key
    except jwt.PyJWKClientConnectionError as exc:
        _log.warning("Apple JWKS unavailable: %s (reason=jwks)", type(exc).__name__)
        raise AppleUnavailable("jwks") from None
    except (jwt.PyJWTError, ValueError):
        raise AppleTokenInvalid("unknown_kid") from None


def _claim_bool(value: Any) -> bool:
    """Apple sends these flags as ``"true"``/``"false"`` strings, sometimes
    as JSON booleans. Anything else is False — never ``bool(value)``, which
    reads ``"false"`` as True."""
    if value is True or value == "true":
        return True
    return False


def _reason_for(exc: jwt.InvalidTokenError) -> str:
    if isinstance(exc, jwt.ExpiredSignatureError):
        return "expired"
    if isinstance(exc, jwt.ImmatureSignatureError):
        return "not_yet_valid"
    if isinstance(exc, jwt.InvalidAudienceError):
        return "audience"
    if isinstance(exc, jwt.InvalidIssuerError):
        return "issuer"
    if isinstance(exc, jwt.MissingRequiredClaimError):
        return "missing_claim"
    if isinstance(exc, jwt.InvalidAlgorithmError):
        return "algorithm"
    if isinstance(exc, jwt.InvalidSignatureError):
        return "signature"
    if isinstance(exc, jwt.DecodeError):
        return "malformed"
    return "invalid"


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_identity_token(token: str, nonce: str, audiences: Iterable[str]) -> AppleIdentity:
    """Verify an Apple id_token and return who it names.

    Checks the RS256 signature against Apple's JWKS, ``iss``, ``aud`` against
    ``audiences``, ``exp`` with a 10 s leeway, and that the token's ``nonce``
    claim is the SHA-256 hex of the raw ``nonce`` the client sent (D4).
    Raises :class:`AppleTokenInvalid` or, when the key set cannot be had,
    :class:`AppleUnavailable`.
    """
    accepted = [a for a in audiences if a]
    if not accepted:
        raise AppleTokenInvalid("audience")
    if not isinstance(token, str) or not token:
        raise AppleTokenInvalid("malformed")
    if not isinstance(nonce, str) or not nonce:
        raise AppleTokenInvalid("nonce")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise AppleTokenInvalid("malformed") from None
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise AppleTokenInvalid("unknown_kid")

    key = _signing_key(kid)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=accepted,
            issuer=APPLE_ISSUER,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["iss", "aud", "exp", "iat", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise AppleTokenInvalid(_reason_for(exc)) from None
    except jwt.PyJWTError:
        raise AppleTokenInvalid("invalid") from None

    claim_nonce = claims.get("nonce")
    if not isinstance(claim_nonce, str) or not hmac.compare_digest(
        claim_nonce.encode("utf-8"), sha256_hex(nonce).encode("utf-8"),
    ):
        raise AppleTokenInvalid("nonce")
    sub, aud = claims.get("sub"), claims.get("aud")
    if not isinstance(sub, str) or not sub:
        raise AppleTokenInvalid("missing_claim")
    # Apple issues a single audience; a list would leave the exchange and the
    # revoke without one client_id to use (D5).
    if not isinstance(aud, str):
        raise AppleTokenInvalid("audience")
    email = claims.get("email")
    return AppleIdentity(
        sub=sub,
        email=email if isinstance(email, str) and email else None,
        email_verified=_claim_bool(claims.get("email_verified")),
        is_private_email=_claim_bool(claims.get("is_private_email")),
        aud=aud,
    )


def client_secret(
    team_id: str,
    key_id: str,
    private_key_pem: str,
    client_id: str,
    now: float | None = None,
) -> str:
    """The ES256 client-secret JWT for ``/auth/token`` and ``/auth/revoke``,
    signed with the Sign in with Apple key. ``client_id`` is the token's
    ``aud`` (bundle id or Services ID), as D5 requires. Valid 5 minutes."""
    issued = int(time.time() if now is None else now)
    return jwt.encode(
        {
            "iss": team_id,
            "iat": issued,
            "exp": issued + _SECRET_LIFETIME_SECONDS,
            "aud": APPLE_ISSUER,
            "sub": client_id,
        },
        private_key_pem,
        algorithm="ES256",
        headers={"kid": key_id},
    )


def _error_label(resp: requests.Response) -> str:
    """Apple's OAuth ``error`` code when it is one of the documented ones,
    else ``other``. Never the body text."""
    try:
        body = resp.json()
    except ValueError:
        return "other"
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, str) and error in _KNOWN_TOKEN_ERRORS else "other"


def exchange_code(code: str, client_id: str, secret: str, redirect_uri: str | None) -> str:
    """Redeem an authorization code at ``/auth/token`` and return the refresh
    token (D3).

    ``redirect_uri`` is sent when not None: Services-ID codes (web, Android)
    need it, bundle-id codes (native iOS) do not (R1-1). A 4xx answer raises
    ``AppleTokenInvalid("code_rejected")``; a 5xx, a timeout, a network error
    or an answer without a refresh token raises :class:`AppleUnavailable`.
    """
    data = {
        "client_id": client_id,
        "client_secret": secret,
        "code": code,
        "grant_type": "authorization_code",
    }
    if redirect_uri is not None:
        data["redirect_uri"] = redirect_uri
    try:
        resp = requests.post(
            APPLE_TOKEN_URL, data=data,
            timeout=_HTTP_TIMEOUT_SECONDS, allow_redirects=False,
        )
    except requests.RequestException as exc:
        _log.warning("Apple code exchange failed: %s (reason=network)", type(exc).__name__)
        raise AppleUnavailable("network") from None

    if 400 <= resp.status_code < 500:
        _log.warning(
            "Apple code exchange rejected: HTTP %s error=%s (reason=code_rejected)",
            resp.status_code, _error_label(resp),
        )
        raise AppleTokenInvalid("code_rejected")
    if resp.status_code != 200:
        _log.warning("Apple code exchange failed: HTTP %s (reason=http)", resp.status_code)
        raise AppleUnavailable("http")
    try:
        body = resp.json()
    except ValueError:
        body = None
    refresh_token = body.get("refresh_token") if isinstance(body, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        _log.warning("Apple code exchange answered without a refresh token (reason=no_refresh_token)")
        raise AppleUnavailable("no_refresh_token")
    return refresh_token


def revoke(refresh_token: str, client_id: str, secret: str) -> bool:
    """Revoke a refresh token at ``/auth/revoke``. Best effort: never raises;
    any failure is logged, without the token, and reported as False so the
    caller still removes the local data."""
    try:
        resp = requests.post(
            APPLE_REVOKE_URL,
            data={
                "client_id": client_id,
                "client_secret": secret,
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except Exception as exc:
        _log.warning("Apple revoke failed: %s (reason=network)", type(exc).__name__)
        return False
    if resp.status_code != 200:
        _log.warning("Apple revoke failed: HTTP %s (reason=http)", resp.status_code)
        return False
    return True

"""FastAPI dependencies — JWT Bearer authentication for the REST API.

Flutter (and any non-Reflex client) authenticates via:
    Authorization: Bearer <jwt>

The JWT is obtained from POST /api/auth/token (password flow)
or POST /api/auth/google (Google id_token flow).
"""
from __future__ import annotations

import os
import datetime
import secrets
from dataclasses import dataclass
from typing import Annotated, Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import select

from models.db import get_session
from models.user import UserInfo
from src.utils.logging import get_logger

_log = get_logger(__name__)

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRY_HOURS = 24 * 7  # 7 days

#: What this module used to fall back to. Still rejected by name: a deployment
#: that copied it from an older README is exactly as forgeable as one that never
#: set the variable at all.
_PUBLISHED_DEFAULT = "change-me-in-production"


def jwt_secret() -> str:
    """The session signing key. Raises when it is not safely configured.

    There is deliberately no fallback (issue #156). A default that ships in a
    public repository is a valid signing key on every deployment that never set
    one, and the payload carries ``is_admin`` — so a forged token is
    indistinguishable from a real one. The old failure mode was silence: the app
    worked perfectly and nothing said it was insecure.

    Read per call rather than cached at import, matching the rest of the config
    (see :func:`src.billing.entitlements.billing_enabled`), so a test can set it
    and a restart is enough to rotate.
    """
    secret = os.environ.get("JWT_SECRET", "").strip()
    if not secret or secret == _PUBLISHED_DEFAULT:
        raise RuntimeError(
            "JWT_SECRET is not configured. Generate one with "
            "`openssl rand -hex 32` and set it as an environment variable "
            "(see .env.example). Note that changing it signs everyone out."
        )
    if secret != _usable_secret:
        _check_usable(secret)
    return secret


#: The last secret :func:`_check_usable` accepted. jwt_secret() runs on every
#: authenticated request, so the check is done once per value, not per call.
_usable_secret: Optional[str] = None


def _check_usable(secret: str) -> None:
    """Refuse a secret PyJWT cannot sign with (issue #453).

    Two shapes pass the checks above yet fail every login and authenticated
    request with a 500: bytes that are not UTF-8 (Linux hands them over as lone
    surrogates, which PyJWT cannot encode), and a PEM or SSH key, which PyJWT
    refuses as an HMAC secret. Signing and verifying one token with it follows
    PyJWT's own rules rather than a copy of them, and fails at boot instead.
    """
    global _usable_secret
    fix = ("Generate one with `openssl rand -hex 32` and set it as an "
           "environment variable (see .env.example).")
    try:
        secret.encode("utf-8")
    except UnicodeEncodeError:
        raise RuntimeError(
            "JWT_SECRET is not valid UTF-8 text: check the encoding of the file "
            f"it was set from. {fix}") from None
    try:
        probe = jwt.encode({"probe": True}, secret, algorithm=_JWT_ALGORITHM)
        jwt.decode(probe, secret, algorithms=[_JWT_ALGORITHM])
    except jwt.InvalidKeyError as exc:
        # PyJWT's reason names the key's shape, never its value.
        raise RuntimeError(
            f"JWT_SECRET cannot sign sessions ({exc}): it must be a random "
            f"shared secret, not a public or private key. {fix}") from None
    except Exception as exc:  # noqa: BLE001 — any refusal is a boot failure
        raise RuntimeError(
            f"JWT_SECRET cannot be used as a signing key ({type(exc).__name__}). "
            f"{fix}") from None
    _usable_secret = secret


def create_access_token(
    user_info: UserInfo, password_change_required: bool = False
) -> str:
    """Create a signed JWT for the given UserInfo.

    ``password_change_required`` is carried from the LocalUser row (it lives on
    the credential, not the profile) so the client can force a password change
    before granting access to the app.
    """
    payload = {
        "sub": str(user_info.id),
        "local_auth_id": user_info.local_auth_id,
        "email": user_info.email,
        "display_name": user_info.display_name,
        "avatar_url": user_info.avatar_url,
        "auth_provider": user_info.auth_provider,
        "is_admin": bool(user_info.is_admin),
        "email_verified": bool(user_info.email_verified),
        "password_change_required": bool(password_change_required),
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=_JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=_JWT_ALGORITHM)


#: Audience of the Strava OAuth ``state`` token. Session tokens carry no
#: ``aud`` claim, and PyJWT refuses a token that has one when the caller names
#: no audience, so :func:`_verify` turns a state token away on its own — and
#: :func:`decode_strava_oauth_state_full` requires the claim, which turns a
#: session token away in the other direction.
STRAVA_OAUTH_AUDIENCE = "strava_oauth"
_STRAVA_STATE_EXPIRY_MINUTES = 10
#: Where the callback sends the browser back to: the web popup page or the
#: app's custom scheme (docs/STRAVA_CONNECT_BINDING_PLAN.md D4, D5).
STRAVA_RETURN_TARGETS = frozenset({"web", "app"})


@dataclass(frozen=True)
class StravaOAuthState:
    """What a valid Strava ``state`` names: the user the connect was started
    for, the starting client's challenge, and where the callback returns."""
    user_info_id: int
    challenge: str
    return_to: str


class OutdatedStravaOAuthState(Exception):
    """A genuine state issued before connects were bound to their client — it
    has neither ``chal`` nor ``ret``. Not logged here: the callback logs one
    line per refusal, so connects caught across a deploy show up."""


def create_strava_oauth_state(user_info_id: int, challenge: str, return_to: str) -> str:
    """A short-lived signed ``state`` for the Strava authorization URL.

    The URL travels to Strava, browser history and access logs, so the state
    carries only what the flow needs — never the session token, which would
    sign that user in for a week. ``chal`` binds the flow to the client that
    holds the matching verifier; ``ret`` is the only input to the callback's
    redirect target.
    """
    payload = {
        "aud": STRAVA_OAUTH_AUDIENCE,
        "sub": str(user_info_id),
        "jti": secrets.token_urlsafe(16),
        "chal": challenge,
        "ret": return_to,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=_STRAVA_STATE_EXPIRY_MINUTES),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=_JWT_ALGORITHM)


def decode_strava_oauth_state_full(token: str) -> Optional[StravaOAuthState]:
    """The :class:`StravaOAuthState` a ``state`` carries; None when it is not
    a valid state token (a session token included).

    Raises :class:`jwt.ExpiredSignatureError` for a genuine state that ran out
    — a user who lingered at Strava — so the caller can say so, and
    :class:`OutdatedStravaOAuthState` for a genuine state from before ``chal``
    and ``ret`` existed. Like :func:`decode_token`, expiry is not logged and
    an invalid state is.
    """
    try:
        payload = jwt.decode(
            token, jwt_secret(), algorithms=[_JWT_ALGORITHM],
            audience=STRAVA_OAUTH_AUDIENCE,
            options={"require": ["aud", "sub", "jti", "exp"]},
        )
        if "chal" not in payload and "ret" not in payload:
            raise OutdatedStravaOAuthState()
        challenge, return_to = payload.get("chal"), payload.get("ret")
        if not isinstance(challenge, str) or not challenge:
            raise jwt.InvalidTokenError("missing or malformed chal claim")
        if not isinstance(return_to, str) or return_to not in STRAVA_RETURN_TARGETS:
            raise jwt.InvalidTokenError("missing or unknown ret claim")
        return StravaOAuthState(int(payload["sub"]), challenge, return_to)
    except (jwt.ExpiredSignatureError, OutdatedStravaOAuthState):
        raise
    except (jwt.PyJWTError, ValueError) as exc:
        # Never log the token itself.
        _log.warning("invalid Strava OAuth state rejected: %s", exc)
        return None


def expired_strava_oauth_state_return_target(token: str) -> Optional[str]:
    """The ``ret`` of an expired state, so the callback can still send the
    user back to the client that started the flow.

    Signature and audience are verified — only expiry is not — so a forged
    token cannot pick the redirect. For the redirect only: never use this to
    pick a user. None when the token does not verify or names no known
    target.
    """
    try:
        payload = jwt.decode(
            token, jwt_secret(), algorithms=[_JWT_ALGORITHM],
            audience=STRAVA_OAUTH_AUDIENCE,
            options={"verify_exp": False},
        )
    except jwt.PyJWTError:
        return None
    ret = payload.get("ret")
    return ret if isinstance(ret, str) and ret in STRAVA_RETURN_TARGETS else None


def _verify(token: str) -> dict:
    """The one place that knows how a session JWT is verified.

    Naming no audience is what refuses a Strava state token here (see
    :data:`STRAVA_OAUTH_AUDIENCE`).
    """
    return jwt.decode(token, jwt_secret(), algorithms=[_JWT_ALGORITHM])


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises HTTPException on failure.

    For the code path that *rejects* the request on a bad token — it owns the
    ``invalid JWT rejected`` warning. A caller that only observes the token
    uses :func:`decode_token_quietly` instead.
    """
    try:
        return _verify(token)
    except jwt.ExpiredSignatureError:
        # Not logged: every issued token expires eventually, so on a running app
        # with several concurrent users this is routine and high-volume, not a
        # signal worth a line per occurrence (open decision #3,
        # docs/LOGGING_OBSERVABILITY_PLAN.md Section 5). InvalidTokenError below
        # (bad signature / malformed token) is the one that's actually suspicious.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        )
    except jwt.InvalidTokenError as exc:
        # Bad signature or malformed token — could be a forged/tampered token or
        # a client bug, unlike plain expiry above. Never log the token itself.
        _log.warning("invalid JWT rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )


def decode_token_quietly(token: str) -> Optional[dict]:
    """Decode and verify a JWT; None instead of raising or logging on failure.

    For best-effort callers that only *observe* a token and never reject the
    request on it — the access-log middleware binding user_id
    (api.middleware._resolve_user_id). The warning :func:`decode_token` emits
    belongs to the path that actually rejects the request, and would otherwise
    fire twice for one forged token and once per scrape of ``/metrics``, whose
    bearer token is not a JWT at all (issue #446).
    """
    try:
        return _verify(token)
    except jwt.PyJWTError:  # a bad or expired token
        return None
    except RuntimeError:
        # jwt_secret() refusing the key. Boot refuses it first (issue #453);
        # this only matters if the variable changes under a running process,
        # and an observer still must not turn that into a 500.
        return None


_bearer = HTTPBearer()
_optional_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> dict:
    """FastAPI dependency — validates JWT and returns the decoded payload.

    Reuses the decode the access-log middleware (api.middleware._resolve_user_id)
    already performed for this same token, when present, instead of decoding it
    a second time. Falls back to decoding here when there is
    nothing to reuse — e.g. middleware isn't installed, as in most unit tests.
    """
    cached = getattr(request.state, "jwt_payload", None)
    if cached is not None:
        return cached
    return decode_token(credentials.credentials)


def get_optional_current_user(
    request: Request,
    credentials: Annotated[
        Optional[HTTPAuthorizationCredentials], Depends(_optional_bearer)
    ],
) -> Optional[dict]:
    """FastAPI dependency — returns the decoded JWT payload if a valid Bearer
    token is present, or None if no token was supplied.  Never raises 401.
    Used on public endpoints that want to behave differently for logged-in users.

    Reuses the middleware's decode the same way ``get_current_user`` does.
    """
    if credentials is None:
        return None
    cached = getattr(request.state, "jwt_payload", None)
    if cached is not None:
        return cached
    try:
        return decode_token(credentials.credentials)
    except HTTPException:
        return None


def require_admin(
    current_user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """FastAPI dependency — 403 unless the caller is an admin.

    Re-reads ``is_admin`` from the DB rather than trusting the (possibly stale)
    token claim, so revoking admin takes effect immediately. Unauthenticated
    callers already get a 401 from ``get_current_user``.
    """
    try:
        user_info_id = int(current_user["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        # A well-formed, validly-signed token with a malformed/missing 'sub' is
        # more suspicious than plain expiry (see decode_token) — could be a
        # forged/tampered token or a client bug. Log the claim name and error,
        # never the raw token.
        _log.warning(
            "malformed 'sub' claim in token payload (%s): %r",
            type(exc).__name__, current_user.get("sub"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )
    with get_session() as sess:
        user_info = sess.get(UserInfo, user_info_id)
        if user_info is None or not user_info.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin access required",
            )
    return current_user

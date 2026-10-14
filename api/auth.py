"""REST auth endpoints consumed by Flutter (and any non-Reflex client).

Routes:
    POST   /api/auth/token           — email + password → JWT
    POST   /api/auth/register        — create account → JWT
    POST   /api/auth/google          — Google id_token → JWT
    POST   /api/auth/apple           — Apple id_token + authorization code → JWT
    POST   /api/auth/apple/callback  — Apple's form_post, relayed to the Android app
    POST   /api/auth/verify-email    — confirm an address from an emailed token
    POST   /api/auth/resend-verification — reissue and resend that token
    GET    /api/auth/me              — current user profile
    PUT    /api/auth/me              — update display name → refreshed JWT
    POST   /api/auth/change-password — change password (local accounts only)
    DELETE /api/auth/me              — delete account + all associated data
    POST   /api/auth/app-opened      — record an app launch (metrics only)
"""
from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import parse_qsl, quote, urlencode

import bcrypt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from google.auth.transport import requests as google_requests
from google.oauth2.id_token import verify_oauth2_token
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import select

from src.auth import apple
from src.auth.email_verification import (
    consume_token,
    issue_token,
    send_verification_email,
)
from src.email.address import is_valid_email, normalize_email
from src.utils.rate_limit import consume_rate_limit

from models.db import get_session
from models.user import LocalUser

from models.user import AppleToken, UserInfo
from api.deps import create_access_token, get_current_user
import os

from src.config.settings import Config
from src.utils.logging import get_logger
from src.utils.metrics import APP_OPENS, LOGINS, REGISTRATIONS

_log = get_logger(__name__)

# Google mints id_tokens against its own clock. Without a tolerance, a server
# whose clock lags by even a second rejects fresh tokens as "used too early".
# A small skew window absorbs normal NTP drift on any host (dev or deployed)
# without meaningfully weakening verification. Google's libraries support this
# exact knob for this exact reason.
_GOOGLE_CLOCK_SKEW_SECONDS = 10

# google-auth's exception types don't separate the failure reasons (expired,
# too early and wrong audience are all InvalidValue), and its messages quote
# the posted token on malformed input. So the reason is looked up by keyword
# and only this fixed label is logged — never the message text (#510).
_GOOGLE_FAILURE_REASONS = (
    ("expired", "expired"),
    ("too early", "too_early"),
    ("audience", "audience"),
    ("issuer", "issuer"),
    ("signature", "signature"),
    ("segment", "malformed"),
)


def _google_failure_reason(exc: Exception) -> str:
    message = str(exc).lower()
    for keyword, reason in _GOOGLE_FAILURE_REASONS:
        if keyword in message:
            return reason
    return "other"


# Sign in with Apple (#580). Each value posted to the login route or relayed
# by the callback is at most 8 KiB; Apple's id_tokens are about 1 KiB.
_APPLE_FIELD_MAX = 8 * 1024
# The callback relays these form fields to the Android app, and no others.
_APPLE_CALLBACK_FIELDS = ("code", "id_token", "state", "user", "error")
# Bounds the callback's form body before it is parsed: five fields of 8 KiB,
# with room for percent-encoding.
_APPLE_CALLBACK_BODY_MAX = 128 * 1024
_APPLE_INTENT_SUFFIX = "#Intent;package=com.traxjourney.app;scheme=signinwithapple;end"

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Env var takes priority; fall back to config file for local dev.
_google_client_id = (
    os.environ.get("GOOGLE_CLIENT_ID")
    or Config("config/config.json").get("google.client_id")
    or ""
)


def _admin_emails() -> set[str]:
    """Case-insensitive set of emails promoted to admin via ADMIN_EMAILS.

    Comma-separated; whitespace and empty entries ignored. Read at call time so
    tests (and ops) can set it without reimporting the module.
    """
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _is_admin_email(email: str) -> bool:
    return bool(email) and email.lower() in _admin_emails()


# ── Request / response schemas ────────────────────────────────────────────────

class TokenRequest(BaseModel):
    username: str = Field(description="Email address or username")
    password: str = Field(description="Account password")

class RegisterRequest(BaseModel):
    """New accounts are keyed by email address (issue #110).

    An address is required because pending invites are matched against it —
    an account with no email can never receive one. Existing accounts are
    unaffected: only registration validates, so the seeded ``admin`` account
    (``src/admin/bootstrap.py``, username "admin", no email) still logs in.
    """

    username: str = Field(description="Email address — also the login identifier")
    password: str = Field(description="Account password")
    first_name: str = Field(description="Given name; combined into the display name")
    last_name: str = Field(description="Family name; combined into the display name")

    @field_validator("username")
    @classmethod
    def _must_be_email(cls, v: str) -> str:
        if not is_valid_email(v):
            raise ValueError("Must be a valid email address")
        return normalize_email(v)

    @field_validator("first_name", "last_name")
    @classmethod
    def _must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Must not be empty")
        return v.strip()

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

class GoogleTokenRequest(BaseModel):
    id_token: str = Field(description="JWT credential from Google One Tap or GIS")

class AppleTokenRequest(BaseModel):
    id_token: str = Field(max_length=_APPLE_FIELD_MAX, description="identityToken from Sign in with Apple")
    authorization_code: str = Field(
        max_length=_APPLE_FIELD_MAX,
        description="authorizationCode from the same sign-in; the server redeems it")
    nonce: str = Field(
        max_length=_APPLE_FIELD_MAX,
        description="The raw nonce; Apple was given its SHA-256")
    given_name: str | None = Field(
        None, max_length=100,
        description="Sent by Apple on the first sign-in only; used when the account is created")
    family_name: str | None = Field(
        None, max_length=100,
        description="Sent by Apple on the first sign-in only; used when the account is created")

class UpdateProfileRequest(BaseModel):
    display_name: str = Field(description="New public display name")

    # A blank name would show to companions as "Traveller" (issue #507), so
    # clearing it is refused rather than stored.
    @field_validator("display_name")
    @classmethod
    def _must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Display name must not be empty")
        return v.strip()

class VerifyEmailRequest(BaseModel):
    token: str = Field(description="Verification token from the emailed link")

class ChangePasswordRequest(BaseModel):
    current_password: str = Field(description="Current password for verification")
    new_password: str = Field(description="New password to set")

class AppOpenedRequest(BaseModel):
    session_state: Literal["resumed", "login_required"] = Field(
        description="'resumed' if a cached session was still valid on launch, "
                    "'login_required' if the user had to sign in from scratch"
    )

class TokenResponse(BaseModel):
    access_token: str = Field(description="JWT bearer token")
    token_type: str = Field("bearer", description="Always 'bearer'")
    user: dict = Field(description="User profile (id, email, display_name, avatar_url, auth_provider)")

class OkOut(BaseModel):
    ok: bool = Field(True, description="Always true on success")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user_info_for_local_id(sess, local_auth_id: int) -> UserInfo | None:
    return sess.exec(
        select(UserInfo).where(UserInfo.local_auth_id == local_auth_id)
    ).first()


def _token_response(
    user_info: UserInfo, password_change_required: bool = False
) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user_info, password_change_required),
        user={
            "id": user_info.id,
            "email": user_info.email,
            "display_name": user_info.display_name,
            "avatar_url": user_info.avatar_url,
            "auth_provider": user_info.auth_provider,
            "is_admin": bool(user_info.is_admin),
            "email_verified": bool(user_info.email_verified),
            "password_change_required": bool(password_change_required),
        },
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/token", response_model=TokenResponse, summary="Login with email + password")
def login(body: TokenRequest):
    """Email + password login — returns a JWT."""
    with get_session() as sess:
        user = sess.exec(
            select(LocalUser).where(LocalUser.username == body.username)
        ).first()
        if not user or not user.enabled or not user.verify(body.password):
            LOGINS.labels("password", "failure").inc()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        user_info = _user_info_for_local_id(sess, user.id)
        if user_info is None:
            # Auto-create UserInfo for legacy local-auth users. The username
            # is the email since #110: it goes in ``email``, never in the
            # public name, which stays blank until the user sets one (#507).
            # A pre-#110 username that isn't an address (the seeded "admin")
            # is not one to mail, so it is left out.
            user_info = UserInfo(
                local_auth_id=user.id,
                display_name="",
                email=(normalize_email(user.username)
                       if is_valid_email(user.username) else ""),
                auth_provider="local",
            )
            sess.add(user_info)
            sess.commit()
            sess.refresh(user_info)
        # Promote to admin if this account's email/username is in ADMIN_EMAILS.
        promote = _is_admin_email(user_info.email) or _is_admin_email(user.username)
        if promote and not user_info.is_admin:
            user_info.is_admin = True
            sess.add(user_info)
            sess.commit()
            sess.refresh(user_info)
        LOGINS.labels("password", "success").inc()
        return _token_response(user_info, user.password_change_required)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED,
             summary="Register a new account")
def register(body: RegisterRequest, background_tasks: BackgroundTasks):
    """Create a new local account — returns a JWT.

    Also queues a verification email (issue #110). Queued, not awaited: the
    account is usable immediately and a slow or dead relay must never fail a
    registration that otherwise succeeded.
    """
    with get_session() as sess:
        existing = sess.exec(
            select(LocalUser).where(LocalUser.username == body.username)
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already taken",
            )
        local_user = LocalUser()  # type: ignore
        local_user.username = body.username
        local_user.password_hash = LocalUser.hash_password(body.password)
        local_user.enabled = True
        sess.add(local_user)
        sess.commit()
        sess.refresh(local_user)

        user_info = UserInfo(
            local_auth_id=local_user.id,
            display_name=body.display_name,
            # The username *is* the address (validated above), so the account
            # is reachable from the moment it exists — previously this was
            # hardcoded empty, which left every local account unmailable.
            email=body.username,
            auth_provider="local",
            is_admin=_is_admin_email(body.username),
        )
        sess.add(user_info)
        sess.commit()
        sess.refresh(user_info)
        REGISTRATIONS.labels("password").inc()

        verification = issue_token(sess, user_info)
        background_tasks.add_task(
            send_verification_email, user_info.email, user_info.display_name,
            verification.token)

        return _token_response(user_info)


@router.post("/google", response_model=TokenResponse, summary="Login with Google")
def google_login(body: GoogleTokenRequest):
    """Verify a Google id_token and return a JWT (for Flutter / native clients)."""
    if not _google_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google auth not configured",
        )
    try:
        id_info = verify_oauth2_token(
            body.id_token,
            google_requests.Request(),
            _google_client_id,
            clock_skew_in_seconds=_GOOGLE_CLOCK_SKEW_SECONDS,
        )
    except Exception as exc:
        # google-auth raises ValueError with a specific reason (expired token,
        # clock skew "used too early", wrong issuer/audience, bad signature).
        # The client only ever sees a generic 401, so log the real reason here —
        # without it every Google auth failure is undiagnosable. The message
        # itself can quote the token, so only its class and mapped reason go out.
        _log.warning(
            "Google id_token verification failed: %s (reason=%s)",
            type(exc).__name__, _google_failure_reason(exc),
        )
        LOGINS.labels("google", "failure").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google id_token",
        )

    google_sub = id_info["sub"]
    email = id_info.get("email", "")
    name = id_info.get("name", "") or email.split("@")[0]
    picture = id_info.get("picture", "")

    with get_session() as sess:
        user_info = sess.exec(
            select(UserInfo).where(UserInfo.google_sub == google_sub)
        ).first()

        if user_info is None:
            REGISTRATIONS.labels("google").inc()  # first sight of this Google account
            shadow = LocalUser()  # type: ignore
            shadow.username = f"google_{google_sub[:16]}"
            shadow.password_hash = b""
            shadow.enabled = True
            sess.add(shadow)
            sess.commit()
            sess.refresh(shadow)

            user_info = UserInfo(
                local_auth_id=shadow.id,
                google_sub=google_sub,
                display_name=name,
                email=email,
                avatar_url=picture,
                auth_provider="google",
                is_admin=_is_admin_email(email),
            )
            sess.add(user_info)
            sess.commit()
            sess.refresh(user_info)
        elif _is_admin_email(user_info.email) and not user_info.is_admin:
            user_info.is_admin = True
            sess.add(user_info)
            sess.commit()
            sess.refresh(user_info)

        LOGINS.labels("google", "success").inc()
        return _token_response(user_info)


def _apple_failure(exc: Exception, status_code: int, detail: str) -> HTTPException:
    """Log an Apple failure as its class and fixed reason label — never the
    token, the code or the exception text — and build the client's error."""
    _log.warning(
        "Apple sign-in failed: %s (reason=%s)",
        type(exc).__name__, getattr(exc, "reason", "other"),
    )
    LOGINS.labels("apple", "failure").inc()
    return HTTPException(status_code=status_code, detail=detail)


@router.post("/apple", response_model=TokenResponse, summary="Login with Apple")
def apple_login(body: AppleTokenRequest):
    """Verify an Apple id_token, redeem its authorization code and return a JWT.

    The code is exchanged on every sign-in and the refresh token stored, so a
    deleted account can always be revoked at Apple: a sign-in whose exchange
    fails is refused. Accounts are keyed by the Apple ``sub`` and never linked
    to another account by e-mail.
    """
    config = apple.load_apple_config()
    if not config.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Apple auth not configured",
        )
    try:
        identity = apple.verify_identity_token(body.id_token, body.nonce, config.audiences)
    except apple.AppleTokenInvalid as exc:
        raise _apple_failure(exc, status.HTTP_401_UNAUTHORIZED, "Invalid Apple id_token")
    except apple.AppleUnavailable as exc:
        raise _apple_failure(exc, status.HTTP_502_BAD_GATEWAY, "Apple sign-in unavailable")

    # The code is redeemed under the client the token was issued to (D5).
    # Services-ID codes (web, Android) were requested with a redirect URI,
    # which the exchange must repeat; native iOS codes carry none.
    client_id = identity.aud
    redirect_uri = (config.redirect_uri or None) if client_id == config.services_id else None
    try:
        secret = apple.client_secret(config.team_id, config.key_id, config.private_key, client_id)
    except Exception as exc:
        # The configured key cannot sign: a deployment fault, not the user's.
        _log.warning("Apple client secret failed: %s (reason=client_secret)", type(exc).__name__)
        LOGINS.labels("apple", "failure").inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Apple auth not configured",
        )
    try:
        refresh_token = apple.exchange_code(body.authorization_code, client_id, secret, redirect_uri)
    except apple.AppleTokenInvalid as exc:
        raise _apple_failure(exc, status.HTTP_401_UNAUTHORIZED, "Invalid Apple id_token")
    except apple.AppleUnavailable as exc:
        raise _apple_failure(exc, status.HTTP_502_BAD_GATEWAY, "Apple sign-in unavailable")

    with get_session() as sess:
        user_info = sess.exec(
            select(UserInfo).where(UserInfo.apple_sub == identity.sub)
        ).first()
        # An address Apple has not verified never promotes to admin.
        email = identity.email or ""
        admin = identity.email_verified and _is_admin_email(email)

        token = None
        if user_info is None:
            REGISTRATIONS.labels("apple").inc()  # first sight of this Apple account
            # Apple sends the name on the first sign-in only.
            name = " ".join(
                n.strip() for n in (body.given_name, body.family_name) if n and n.strip()
            ) or email.split("@")[0]
            # One commit for the shadow user, the profile and the token: an
            # account must never exist without the token that revokes it.
            shadow = LocalUser()  # type: ignore
            shadow.username = f"apple_{identity.sub}"
            shadow.password_hash = b""
            shadow.enabled = True
            sess.add(shadow)
            sess.flush()

            user_info = UserInfo(
                local_auth_id=shadow.id,
                apple_sub=identity.sub,
                display_name=name,
                email=email,
                auth_provider="apple",
                email_verified=identity.email_verified,
                is_admin=admin,
            )
            sess.add(user_info)
            sess.flush()
            token = AppleToken(user_info_id=user_info.id)

        # An account deleted after the lookup leaves the fresh token in no row
        # its deletion could revoke (U3R1-2): its UPDATEs match nothing, and
        # SQLite takes an AppleToken INSERT for the gone account, so the
        # account is looked up again once this session holds the write lock.
        try:
            if token is None:  # an existing account
                if admin and not user_info.is_admin:
                    user_info.is_admin = True
                    sess.add(user_info)
                token = sess.exec(
                    select(AppleToken).where(AppleToken.user_info_id == user_info.id)
                ).first() or AppleToken(user_info_id=user_info.id)
            token.refresh_token = refresh_token
            token.client_id = client_id
            sess.add(token)
            sess.flush()
            gone = sess.exec(
                select(UserInfo.id).where(UserInfo.id == user_info.id)
            ).first() is None
        except StaleDataError:
            gone = True
        if gone:
            sess.rollback()
            apple.revoke(refresh_token, client_id, secret)
            _log.warning("Apple sign-in failed: account deleted during sign-in (reason=account_gone)")
            LOGINS.labels("apple", "failure").inc()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Apple id_token",
            )
        sess.commit()
        sess.refresh(user_info)

        LOGINS.labels("apple", "success").inc()
        return _token_response(user_info)


@router.post("/apple/callback", status_code=status.HTTP_303_SEE_OTHER,
             summary="Relay Apple's form_post to the Android app",
             response_class=RedirectResponse)
async def apple_callback(request: Request):
    """Apple's web flow posts its result here (``response_mode=form_post``);
    this hands it to the Android app's Custom Tab through an ``intent://``
    redirect.

    Public, so it trusts nothing: only the five fields the app reads are
    relayed, each at most 8 KiB, and the redirect always goes to the app.
    Nothing is verified or logged here; ``POST /api/auth/apple`` does that.
    """
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > _APPLE_CALLBACK_BODY_MAX:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Request too large",
            )
    # A field's first occurrence decides: an oversized one is dropped, not
    # replaced by a later duplicate.
    seen: set[str] = set()
    fields: dict[str, str] = {}
    for key, value in parse_qsl(raw.decode("utf-8", errors="replace")):
        if key not in _APPLE_CALLBACK_FIELDS or key in seen:
            continue
        seen.add(key)
        if len(value.encode("utf-8")) <= _APPLE_FIELD_MAX:
            fields[key] = value
    query = urlencode(
        [(k, fields[k]) for k in _APPLE_CALLBACK_FIELDS if k in fields], quote_via=quote,
    )
    return RedirectResponse(
        f"intent://callback?{query}{_APPLE_INTENT_SUFFIX}",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/verify-email", response_model=OkOut, summary="Confirm an email address")
def verify_email(body: VerifyEmailRequest):
    """Consume a verification token and mark the account's address verified.

    Unauthenticated on purpose: the recipient clicks this from their inbox and
    may not have a session in that browser. The token is the proof — it is
    single-use, expires, and only matches the address it was issued for.
    """
    with get_session() as sess:
        user = consume_token(sess, body.token)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This verification link is invalid or has expired. "
                       "Request a new one from your account.",
            )
        return {"ok": True}


@router.post("/resend-verification", response_model=OkOut,
             summary="Resend the verification email")
def resend_verification(
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
):
    """Issue a fresh verification token and email it, invalidating the previous
    one. No-op (still 200) for an already-verified account, so the client never
    has to branch on it."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        user_info = sess.get(UserInfo, user_info_id)
        if user_info is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="User not found")
        if user_info.email_verified or not user_info.email:
            return {"ok": True}

        if not consume_rate_limit(f"verify-resend:{user_info_id}"):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many verification emails requested. "
                       "Please wait a few minutes and try again.",
            )

        verification = issue_token(sess, user_info)
        background_tasks.add_task(
            send_verification_email, user_info.email, user_info.display_name,
            verification.token)
        return {"ok": True}


@router.get("/me", summary="Get current user profile")
def me(current_user: Annotated[dict, Depends(get_current_user)]):
    """Return the current user's profile decoded from the JWT.

    ``email_verified`` is re-read from the database rather than trusted from
    the token (issue #110): verifying does not mint a new JWT, so the claim
    would keep reporting False until the old token expired — the client would
    show "verify your email" to someone who just did.
    """
    profile = dict(current_user)
    with get_session() as sess:
        user_info = sess.get(UserInfo, int(current_user["sub"]))
        if user_info is not None:
            profile["email_verified"] = bool(user_info.email_verified)
    return profile


@router.post("/app-opened", response_model=OkOut, summary="Record an app launch (metrics only)")
def app_opened(body: AppOpenedRequest):
    """Fired once per app launch by the client's startup path.

    Unauthenticated: a launch with no valid cached session must still be
    counted, and there is no token to attach in that case. Distinct from
    ``traxjourney_logins_total`` — that only counts a fresh credential
    submission, so it misses every launch where a cached session was simply
    resumed, undercounting how often people actually come back to the app.
    """
    APP_OPENS.labels(body.session_state).inc()
    return {"ok": True}


@router.put("/me", response_model=TokenResponse, summary="Update display name")
def update_me(
    body: UpdateProfileRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Update display name and return a refreshed JWT."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        user_info = sess.get(UserInfo, user_info_id)
        if user_info is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        user_info.display_name = body.display_name
        sess.add(user_info)
        sess.commit()
        sess.refresh(user_info)
        return _token_response(user_info)


@router.post("/change-password", response_model=TokenResponse, summary="Change password")
def change_password(
    body: ChangePasswordRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Change password — local (email) accounts only. Returns 403 for Google accounts.

    Returns a fresh JWT (like PUT /me) rather than just {"ok": true}: the old
    token still carries password_change_required=True as a baked-in claim, so
    without a new token the client has no way to learn the flag cleared.
    """
    if current_user.get("auth_provider") != "local":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change is only available for email accounts",
        )
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        user_info = sess.get(UserInfo, user_info_id)
        if user_info is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        local_user = sess.get(LocalUser, user_info.local_auth_id)
        if local_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local user not found")
        if not local_user.verify(body.current_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect",
            )
        local_user.password_hash = LocalUser.hash_password(body.new_password)
        # A successful change satisfies any forced-change requirement.
        local_user.password_change_required = False
        sess.add(local_user)
        sess.commit()
        sess.refresh(user_info)
        return _token_response(user_info, local_user.password_change_required)


@router.delete("/me", response_model=OkOut, summary="Delete account")
def delete_account(current_user: Annotated[dict, Depends(get_current_user)]):
    """Permanently delete the current user's account and all associated data."""
    from src.auth.account_deletion import delete_user_and_data, purge_user_files

    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        if sess.get(UserInfo, user_info_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        delete_user_and_data(sess, user_info_id)
    purge_user_files(user_info_id)
    return {"ok": True}

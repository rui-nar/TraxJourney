"""Strava OAuth + activity sync endpoints.

Routes:
    POST   /api/strava/connect              — returns OAuth URL, its state bound to the client's challenge
    GET    /api/strava/connect              — retired: 426 Upgrade Required
    GET    /api/strava/callback             — binds the code to its state, relays both to the starting client
    POST   /api/strava/complete             — exchanges the code and links the bearer's account
    GET    /api/strava/status               — {"connected": bool}
    DELETE /api/strava/disconnect           — revokes at Strava, removes token + cached activity list
    GET    /api/strava/activities           — browse user's Strava activities (with filters)
    GET    /api/strava/cache/status         — cache age + activity count
    POST   /api/projects/{name}/strava/sync — syncs Strava activities into a project
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import date, datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from models.db import get_session

from api.deps import get_current_user
from api.geo import bust_geo_cache
from api.project_access import OwnerParam, resolve_project
from models.project_db import DBProjectItem, DBStravaCache
from models.user import StravaOAuthCode, StravaToken, UserInfo
from src.api.strava_client import StravaAPI
from src.auth.oauth import OAuth2Session
from src.auth.strava_deauth import deauthorize_strava
from src.billing.entitlements import ensure_trip_days_quota
from src.config.settings import Config
from src.filters.filter_engine import FilterCriteria, FilterEngine
from src.models.activity import Activity, parse_activities_or_log, strip_heartrate
from src.project.project_io import ProjectIO
from src.project.project_repo import ProjectRepo
from src.utils.logging import get_logger
from src.web_pages.render import render_strava_return_confirm

_log = get_logger(__name__)

_project_repo = ProjectRepo()

router = APIRouter(tags=["strava"])

_cfg = Config("config/config.json")
if os.environ.get("STRAVA_CLIENT_ID"):
    _cfg.set("strava.client_id", os.environ["STRAVA_CLIENT_ID"])
if os.environ.get("STRAVA_CLIENT_SECRET"):
    _cfg.set("strava.client_secret", os.environ["STRAVA_CLIENT_SECRET"])

_CALLBACK_URI = os.environ.get(
    "STRAVA_REDIRECT_URI",
    "http://localhost:8000/api/strava/callback",
)
_FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5500")
_CACHE_TTL = int(os.environ.get("STRAVA_CACHE_TTL", 3600))


# ── Response schemas ──────────────────────────────────────────────────────────

_BASE64URL_43 = r"^[A-Za-z0-9_-]{43}$"
_BASE64URL_43_128 = r"^[A-Za-z0-9_-]{43,128}$"


class ConnectIn(BaseModel):
    challenge: str = Field(
        min_length=43, max_length=43, pattern=_BASE64URL_43,
        description="base64url(sha256(verifier)), no padding; the verifier stays on the client",
    )
    return_to: Literal["web", "app"] = Field(
        description="Where the callback returns: the web popup page or the app's custom scheme",
    )

class CompleteIn(BaseModel):
    code: str = Field(min_length=1, max_length=256, description="Authorization code Strava returned")
    state: str = Field(min_length=1, max_length=2048, description="The state the callback relayed")
    verifier: str = Field(
        min_length=43, max_length=128, pattern=_BASE64URL_43_128,
        description="The verifier whose challenge was sent to POST /api/strava/connect",
    )

class ConnectUrlOut(BaseModel):
    url: str = Field(description="Strava OAuth authorization URL to redirect the user to")

class CompleteOut(BaseModel):
    connected: bool = Field(description="True once the Strava account is linked")

class StravaStatusOut(BaseModel):
    connected: bool = Field(description="True if a Strava token is stored and non-empty")

class CacheStatusOut(BaseModel):
    cached: bool = Field(description="True if a cached activity list exists")
    count: int = Field(description="Number of activities in the cache")
    age_seconds: Optional[float] = Field(None, description="Age of the cache in seconds, or null if not cached")

class SyncResultOut(BaseModel):
    added: int = Field(description="Number of activities added to the project")
    total: int = Field(description="Total activities in the project after sync")

class ActivitiesPageOut(BaseModel):
    activities: List[dict] = Field(description="Page of activity objects, each with an 'in_project' flag")
    total: int = Field(description="Total matching activities across all pages")
    page: int = Field(description="Current 1-based page number")
    per_page: int = Field(description="Items per page")
    has_more: bool = Field(description="True if more pages are available")
    cached: bool = Field(description="True if the activity list was served from cache")


# ── Activity cache ─────────────────────────────────────────────────────────────

#: The cache of users with end-to-end encryption on, by user id:
#: ``(fetched_at, activities_json)``, the two columns a ``DBStravaCache`` row
#: would hold. The raw list carries activity names and tracks in plaintext,
#: which such an account must not have on disk, so it lives in this process
#: only (docs/E2EE_REMNANTS_PLAN.md decision 8) — enough with one API process.
#: Lost on restart, which costs one Strava refetch.
_memory_cache: Dict[int, tuple[float, str]] = {}


def _is_encrypted(sess, user_info_id: int) -> bool:
    ui = sess.get(UserInfo, user_info_id)
    return bool(ui is not None and ui.encryption_enabled)


def _cached_entry(user_info_id: int) -> tuple[float, str] | None:
    """The user's cached ``(fetched_at, activities_json)``, whatever its age,
    from whichever store holds it for this account."""
    with get_session() as sess:
        if _is_encrypted(sess, user_info_id):
            return _memory_cache.get(user_info_id)
        row = sess.get(DBStravaCache, user_info_id)
    if row is None:
        return None
    return row.fetched_at, row.activities_json


def _load_cache(user_info_id: int) -> Dict[str, Any] | None:
    """Return the cached payload if it exists and is within TTL, else None."""
    entry = _cached_entry(user_info_id)
    if entry is None:
        return None
    fetched_at, activities_json = entry
    age = time.time() - fetched_at
    if age > _CACHE_TTL:
        return None
    try:
        return {"fetched_at": fetched_at, "activities": json.loads(activities_json)}
    except Exception:
        return None


def _claim_token_row(sess, user_info_id: int, **values) -> bool:
    """UPDATE the user's token row and report whether it still exists.

    One statement is both the lock and the check (issue #440): a write is
    what starts SQLite's transaction and takes its write lock — pysqlite opens
    none for a SELECT, so a check by SELECT could be overtaken by a disconnect
    committing before the write that followed — and on Postgres it waits on
    the row lock a concurrent DELETE holds, then re-evaluates. So a disconnect
    either committed first (no row matched: the user is gone) or queues
    behind this transaction. Given no values it is a no-op write, the idiom
    ``src.billing.subscriptions.lock_account`` uses. Must be the session's
    first write, before the reads and writes it protects.
    """
    if not values:
        values = {"user_info_id": StravaToken.user_info_id}
    result = sess.execute(
        update(StravaToken).where(StravaToken.user_info_id == user_info_id).values(**values)
    )
    return result.rowcount == 1


def _save_cache(user_info_id: int, raw_activities: List[Dict[str, Any]]) -> None:
    """Persist the raw Strava activity list to the DB cache.

    Stored whole, so it is the one place a Strava payload reaches the disk
    unparsed — heart rate is scrubbed here (issue #442), the same way the
    parsed ``Activity`` never carries it.

    No-op once the user has disconnected: a fetch that was in flight when
    ``DELETE /api/strava/disconnect`` ran must not recreate the cache row it
    just removed (issue #440). The token row is claimed first, so the
    disconnect cannot slip in between the check and the write.

    For an account with encryption on, the list goes to ``_memory_cache``
    instead and any row left in the table is deleted. Encryption is read
    after the claim, so an ``enable`` that committed first is seen, and one
    queued behind this transaction deletes the row this writes.
    """
    activities_json = json.dumps([strip_heartrate(a) for a in raw_activities])
    with get_session() as sess:
        if not _claim_token_row(sess, user_info_id):
            sess.rollback()
            return
        row = sess.get(DBStravaCache, user_info_id)
        if _is_encrypted(sess, user_info_id):
            if row is not None:
                sess.delete(row)
            _forget_expired_memory_entries()
            # Stored before the commit, under the claim: a disconnect queued
            # behind it then removes this entry rather than missing it.
            _memory_cache[user_info_id] = (time.time(), activities_json)
            sess.commit()
            return
        if row is None:
            row = DBStravaCache(user_info_id=user_info_id)
            sess.add(row)
        row.fetched_at = time.time()
        row.activities_json = activities_json
        sess.commit()


def _forget_expired_memory_entries() -> None:
    """Drop in-memory lists past the TTL, which no reader would serve, so the
    dict holds at most the lists fetched within the last TTL."""
    cutoff = time.time() - _CACHE_TTL
    for uid, (fetched_at, _) in list(_memory_cache.items()):
        if fetched_at < cutoff:
            _memory_cache.pop(uid, None)


def _invalidate_cache(user_info_id: int) -> None:
    """Remove the cached activity row so the next request re-fetches from Strava."""
    _memory_cache.pop(user_info_id, None)
    with get_session() as sess:
        row = sess.get(DBStravaCache, user_info_id)
        if row is not None:
            sess.delete(row)
            sess.commit()


def _fetch_all_strava(
    client: StravaAPI,
    after: Optional[int] = None,
    before: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Paginate through Strava activities and return the raw list."""
    all_raw: List[Dict[str, Any]] = []
    page = 1
    while True:
        params: Dict[str, Any] = {"per_page": 200, "page": page}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        batch = client.get_activities(**params)
        if not isinstance(batch, list) or not batch:
            break
        all_raw.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return all_raw


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strava_client_for_token(token_row: StravaToken) -> StravaAPI:
    """Build a StravaAPI instance pre-loaded with tokens from the DB.

    Rotated tokens are persisted the moment the client refreshes them, not
    after the request's last Strava call — see :func:`_persist_rotated_token`.
    """
    client = StravaAPI(_cfg)
    client.token_data = {
        "access_token": token_row.access_token,
        "refresh_token": token_row.refresh_token,
        "expires_at": token_row.expires_at,
    }
    user_info_id = token_row.user_info_id
    client.on_token_refresh = lambda token_data: _persist_rotated_token(user_info_id, token_data)
    return client


def _persist_rotated_token(user_info_id: int, token_data: Dict[str, Any]) -> None:
    """Store the tokens Strava just issued — or revoke them if the user has
    disconnected meanwhile (issue #440).

    Runs from ``StravaAPI.on_token_refresh`` as soon as a refresh happens, in
    its own session: the fetch that triggered it may have pages to go, and the
    rotated refresh token exists nowhere else until it is stored. A disconnect
    that already removed the row revoked the previous refresh token — one
    Strava no longer knows and answers 200 for — so the app stays authorised
    unless these new tokens are revoked too.
    """
    fields = {
        "access_token": token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": float(token_data.get("expires_at", 0)),
    }
    with get_session() as sess:
        still_connected = _claim_token_row(sess, user_info_id, **fields)
        if still_connected:
            sess.commit()
        else:
            sess.rollback()
    if not still_connected:
        deauthorize_strava(user_info_id, fields["access_token"], fields["refresh_token"], cfg=_cfg)


# ── Endpoints ─────────────────────────────────────────────────────────────────

#: Where an app-started flow returns (docs/STRAVA_CONNECT_BINDING_PLAN.md D4).
#: Host ``app`` so the app's router sees the path ``/strava-return``.
_APP_RETURN_URI = "traxjourney://app/strava-return"


def pkce_challenge(verifier: str) -> str:
    """``base64url(sha256(verifier))`` without padding — RFC 7636 ``S256``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _ensure_strava_configured() -> None:
    if not _cfg.validate_strava_config():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Strava not configured (missing client_id/secret in config.json)",
        )


def _return_url(return_to: Optional[str], **params: str) -> str:
    """The URL that hands the browser back to the client that started the flow.

    ``return_to`` comes only from a signature-verified state, never from the
    request; anything else lands on the web page. Only fixed tokens and the
    relayed code and state go in the query.
    """
    base = _APP_RETURN_URI if return_to == "app" else f"{_FRONTEND_ORIGIN}/oauth_callback.html"
    return f"{base}?{urlencode(params)}"


def _return_redirect(return_to: Optional[str], **params: str) -> RedirectResponse:
    """Send the browser back to the client that started the flow."""
    return RedirectResponse(_return_url(return_to, **params))


#: Headers of the app-return confirmation page: it holds a code, so it is
#: never cached, framed or sent on as a referrer, and it runs no script.
_CONFIRM_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}


def _app_return_page(display_name: str, code: str, state: str) -> HTMLResponse:
    """Ask before an app connect returns to the app (issue #584, plan D1).

    Its Continue link is the app return the callback used to redirect to;
    Cancel stays on the page. Never log the body: it holds the code.
    """
    html = render_strava_return_confirm(
        display_name=display_name,
        continue_url=_return_url("app", strava="code", code=code, state=state),
        cancel_url="#cancelled",
    )
    return HTMLResponse(html, headers=_CONFIRM_PAGE_HEADERS)


def _code_hash(code: str) -> str:
    """The key a Strava code is bound under: the code itself is never stored."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _bound_jti(sess, code_hash: str) -> Optional[str]:
    """The ``jti`` of the state the code is bound to, expired or not."""
    row = sess.get(StravaOAuthCode, code_hash)
    return row.state_jti if row is not None else None


def _bind_code(code_hash: str, state_jti: str, expires_at: float) -> bool:
    """Bind a code to the state it arrived with; first seen wins (D9).

    True when the code is now bound to ``state_jti`` — newly, or by an
    earlier callback with the same state (a reload). False when another
    state holds it: nothing is written then. Expired rows are pruned before
    the insert, in its transaction. Two callbacks racing on one code cannot
    both bind it: the primary key refuses the second insert, which then
    compares against the row that won.
    """
    with get_session() as sess:
        sess.execute(delete(StravaOAuthCode).where(StravaOAuthCode.expires_at <= time.time()))
        bound = _bound_jti(sess, code_hash)
        if bound is not None:
            sess.rollback()
            return bound == state_jti
        sess.add(StravaOAuthCode(
            code_hash=code_hash, state_jti=state_jti, expires_at=expires_at))
        try:
            sess.commit()
            return True
        except IntegrityError:
            sess.rollback()
    with get_session() as sess:
        return _bound_jti(sess, code_hash) == state_jti


@router.post("/api/strava/connect", response_model=ConnectUrlOut,
             summary="Get Strava OAuth URL")
def strava_connect(
    body: ConnectIn,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the Strava OAuth authorization URL to redirect the user to.

    Its state names the caller, the client's challenge and where to return,
    so only the client holding the verifier can complete the connect.
    """
    _ensure_strava_configured()
    from api.deps import create_strava_oauth_state

    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        user_info = sess.exec(
            select(UserInfo).where(UserInfo.id == user_info_id)
        ).first()
        if user_info is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        state_token = create_strava_oauth_state(user_info.id, body.challenge, body.return_to)

    oauth = OAuth2Session(_cfg)
    oauth.redirect_uri = _CALLBACK_URI
    base_url = oauth.authorization_url()
    return {"url": f"{base_url}&state={state_token}"}


@router.get("/api/strava/connect", summary="Retired: answers 426 Upgrade Required")
def strava_connect_retired(current_user: Annotated[dict, Depends(get_current_user)]):
    """The unbound connect, retired for ``POST /api/strava/connect``.

    Its state could be completed by whoever opened the URL, so a build that
    still calls this cannot start a connect at all. It issues nothing.
    """
    raise HTTPException(
        status_code=status.HTTP_426_UPGRADE_REQUIRED,
        detail="Update the app to connect Strava.",
    )


@router.get("/api/strava/callback", include_in_schema=False)
def strava_callback(
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
):
    """Handle Strava's OAuth redirect: relay ``code`` and ``state`` to the
    client that started the flow, which completes it with its verifier.

    Unauthenticated and reachable with any query string, so it never
    exchanges the code or touches a token. Its one write, for a valid state
    only, binds the code to that state's ``jti`` so the code cannot be
    completed with any other state (D9). The redirect target comes only from
    the verified state's ``ret``. An app return is not redirected: it gets a
    page naming the account, whose Continue link is that redirect (#584).
    """
    if not state:
        return _return_redirect(None, strava="error", reason="no_state")

    # Only a state issued by strava_connect is accepted — a session token is not.
    import jwt
    from api.deps import (
        OutdatedStravaOAuthState,
        decode_strava_oauth_state_full,
        expired_strava_oauth_state_return_target,
    )
    try:
        decoded = decode_strava_oauth_state_full(state)
    except jwt.ExpiredSignatureError:
        return _return_redirect(
            expired_strava_oauth_state_return_target(state),
            strava="error", reason="state_expired",
        )
    except OutdatedStravaOAuthState:
        # A connect started by a build from before the binding, caught
        # across a deploy. Never log the state.
        _log.warning("Strava OAuth state without client binding refused (update_required)")
        return _return_redirect(None, strava="error", reason="update_required")
    if decoded is None:
        return _return_redirect(None, strava="error", reason="invalid_state")

    if error or not code:
        return _return_redirect(decoded.return_to, strava="error", reason="denied")
    if decoded.return_to == "app":
        # The page names the state's account; one deleted since the connect
        # started has nothing to name or link, so nothing is bound.
        with get_session() as sess:
            user_info = sess.get(UserInfo, decoded.user_info_id)
            display_name = user_info.display_name if user_info is not None else None
        if display_name is None:
            return _return_redirect("app", strava="error", reason="invalid_state")
    if not _bind_code(_code_hash(code), decoded.jti, decoded.expires_at):
        # The code already came back with another state: someone is trying
        # to pair it with a connect of their own.
        return _return_redirect(decoded.return_to, strava="error", reason="invalid_state")
    if decoded.return_to == "app":
        return _app_return_page(display_name, code, state)
    return _return_redirect(decoded.return_to, strava="code", code=code, state=state)


@router.post("/api/strava/complete", response_model=CompleteOut,
             summary="Complete Strava connect")
def strava_complete(
    body: CompleteIn,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Exchange the relayed code and link the Strava account to the caller.

    Links only when the caller is the user the state was issued for, the
    verifier matches the state's challenge and the code came back from
    Strava with this state. The binding goes with the token write, and a
    Strava code exchanges once, so a replay fails twice over. Codes, states
    and verifiers are never logged.
    """
    _ensure_strava_configured()
    import jwt
    from api.deps import OutdatedStravaOAuthState, decode_strava_oauth_state_full

    try:
        decoded = decode_strava_oauth_state_full(body.state)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="state_expired")
    except OutdatedStravaOAuthState:
        decoded = None
    if decoded is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_state")

    user_info_id = int(current_user["sub"])
    if decoded.user_info_id != user_info_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="wrong_account")
    if not hmac.compare_digest(pkce_challenge(body.verifier), decoded.challenge):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="verifier_mismatch")
    # The code must have come back from Strava with this very state (D9):
    # a code taken from someone else's return URL is bound to their state.
    code_hash = _code_hash(body.code)
    with get_session() as sess:
        binding = sess.get(StravaOAuthCode, code_hash)
        bound = (binding is not None and binding.expires_at > time.time()
                 and binding.state_jti == decoded.jti)
    if not bound:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="code_not_bound")

    try:
        oauth = OAuth2Session(_cfg)
        oauth.redirect_uri = _CALLBACK_URI
        token_data = oauth.exchange_code(body.code)
    except Exception as exc:
        # The type only: an upstream message is not ours to log or relay.
        _log.warning("Strava code exchange failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Strava did not accept the connection. Please try again.",
        )

    fields = {
        "access_token": token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": float(token_data.get("expires_at", time.time() + 21600)),
    }
    # The claim is the session's first write (#440): a disconnect either
    # committed first — no row matched, so this inserts — or queues behind
    # this transaction. No read precedes it that a disconnect could stale.
    with get_session() as sess:
        if not _claim_token_row(sess, user_info_id, **fields):
            sess.add(StravaToken(user_info_id=user_info_id, **fields))
        sess.execute(delete(StravaOAuthCode).where(StravaOAuthCode.code_hash == code_hash))
        sess.commit()

    return {"connected": True}


@router.get("/api/strava/status", response_model=StravaStatusOut,
            summary="Get Strava connection status")
def strava_status(current_user: Annotated[dict, Depends(get_current_user)]):
    """Return whether the current user has connected their Strava account."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = sess.exec(
            select(StravaToken).where(StravaToken.user_info_id == user_info_id)
        ).first()
    return {"connected": row is not None and bool(row.access_token)}


@router.delete("/api/strava/disconnect", status_code=status.HTTP_204_NO_CONTENT,
               summary="Disconnect Strava account")
def strava_disconnect(current_user: Annotated[dict, Depends(get_current_user)]):
    """Disconnect Strava: revoke the app at Strava, then drop the stored token
    and the cached raw activity list (issue #440).

    Activities still in a trip are the user's own and stay; those in none are
    deleted (issue #509). The revoke is best effort — if Strava is unreachable
    the local data is removed all the same, so the user is disconnected either
    way.
    """
    user_info_id = int(current_user["sub"])
    # Read, then revoke outside any session — the Strava round trip (up to two
    # attempts × timeout) must not sit on an open transaction.
    with get_session() as sess:
        row = sess.exec(
            select(StravaToken).where(StravaToken.user_info_id == user_info_id)
        ).first()
        tokens = (row.access_token, row.refresh_token) if row else None
    if tokens is not None:
        deauthorize_strava(user_info_id, *tokens, cfg=_cfg)
    # A refresh in flight may have rotated the tokens while Strava was being
    # called (review R3-1): its callback commits the new ones and, revoked as
    # the old ones were, they would go with the row here, still valid. So the
    # row is claimed first — a callback queued behind this transaction then
    # matches no row and revokes its own tokens — and read under that claim;
    # a rotation that landed earlier shows as a refresh token other than the
    # one revoked, and is revoked after the commit, never under the lock.
    rotated = None
    with get_session() as sess:
        if _claim_token_row(sess, user_info_id):
            row = sess.exec(
                select(StravaToken).where(StravaToken.user_info_id == user_info_id)
            ).first()
            if tokens is None or row.refresh_token != tokens[1]:
                rotated = (row.access_token, row.refresh_token)
            sess.delete(row)
        cache_row = sess.get(DBStravaCache, user_info_id)
        if cache_row is not None:
            sess.delete(cache_row)
        _memory_cache.pop(user_info_id, None)
        # Strava rows no trip holds any more go too (issue #509); those still
        # in a trip, the user's or a companion's, stay.
        _project_repo.delete_unreferenced_strava_activities(sess, user_info_id)
        sess.commit()
    if rotated is not None:
        deauthorize_strava(user_info_id, *rotated, cfg=_cfg)


@router.get("/api/strava/activities", response_model=ActivitiesPageOut,
            summary="Browse Strava activities")
def strava_activities(
    current_user: Annotated[dict, Depends(get_current_user)],
    start_date: Optional[str] = None,   # YYYY-MM-DD
    end_date: Optional[str] = None,     # YYYY-MM-DD
    types: Optional[str] = None,        # comma-separated, e.g. "Run,Ride"
    project: Optional[str] = None,      # project name to compute in_project
    refresh: bool = False,              # bypass cache and re-fetch from Strava
    page: int = 1,                      # 1-based page number
    per_page: int = 50,                 # items per page (max 200)
    owner: OwnerParam = None,           # project's owner, if not the caller
):
    """Browse the current user's Strava activities with optional filters.

    Activities are served from a per-user cache (default TTL: 1 hour).
    Filters (date, type) are applied in-memory so filter changes are instant.
    Results are paginated: use `page` / `per_page` to walk through them.
    Pass `refresh=true` to force a full re-fetch and rebuild the cache.
    Each activity includes an `in_project` flag when `project` is specified.
    """
    user_info_id = int(current_user["sub"])

    after_epoch: Optional[int] = None
    before_epoch: Optional[int] = None
    if start_date:
        try:
            sd = date.fromisoformat(start_date)
            after_epoch = int(datetime(sd.year, sd.month, sd.day, tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    if end_date:
        try:
            ed = date.fromisoformat(end_date)
            before_epoch = int(datetime(ed.year, ed.month, ed.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass

    use_date_api = after_epoch is not None or before_epoch is not None

    cached = False
    with get_session() as sess:
        token_row = sess.exec(
            select(StravaToken).where(StravaToken.user_info_id == user_info_id)
        ).first()
        if not token_row:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Strava not connected",
            )

        raw_list: Optional[List[Dict[str, Any]]] = None

        if use_date_api:
            client = _strava_client_for_token(token_row)
            raw_list = _fetch_all_strava(client, after=after_epoch, before=before_epoch)
        else:
            if not refresh:
                cache_data = _load_cache(user_info_id)
                if cache_data is not None:
                    raw_list = cache_data["activities"]
                    cached = True
            if raw_list is None:
                client = _strava_client_for_token(token_row)
                raw_list = _fetch_all_strava(client)
                _save_cache(user_info_id, raw_list)

    activities: List[Activity] = parse_activities_or_log(raw_list, "strava_browse")

    if types:
        type_set = {t.strip() for t in types.split(",") if t.strip()}
        criteria = FilterCriteria(activity_types=type_set)
        activities = FilterEngine.apply(activities, criteria)

    activities.sort(key=lambda a: a.start_date, reverse=True)

    in_project_ids: set = set()
    if project:
        with get_session() as sess:
            try:
                proj_row = resolve_project(sess, user_info_id, project, owner)
            except HTTPException:
                proj_row = None
            if proj_row:
                item_rows = sess.exec(
                    select(DBProjectItem).where(
                        DBProjectItem.project_id == proj_row.id,
                        DBProjectItem.item_type == "activity",
                    )
                ).all()
                in_project_ids = {r.activity_id for r in item_rows if r.activity_id is not None}

    total = len(activities)
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    offset = (page - 1) * per_page
    page_activities = activities[offset: offset + per_page]
    has_more = (offset + per_page) < total

    result = []
    for a in page_activities:
        d = a.to_strava_dict()
        d["in_project"] = a.id in in_project_ids
        result.append(d)

    return {
        "activities": result,
        "total": total,
        "page": page,
        "per_page": per_page,
        "has_more": has_more,
        "cached": cached,
    }


@router.get("/api/strava/cache/status", response_model=CacheStatusOut,
            summary="Get activity cache status")
def strava_cache_status(current_user: Annotated[dict, Depends(get_current_user)]):
    """Return metadata about the current user's Strava activity cache."""
    user_info_id = int(current_user["sub"])
    entry = _cached_entry(user_info_id)
    if entry is None or not entry[1]:
        return {"cached": False, "count": 0, "age_seconds": None}
    try:
        fetched_at, activities_json = entry
        age = time.time() - fetched_at
        count = len(json.loads(activities_json))
        return {"cached": True, "count": count, "age_seconds": round(age)}
    except Exception:
        return {"cached": False, "count": 0, "age_seconds": None}


@router.post("/api/projects/{name}/strava/sync", response_model=SyncResultOut,
             summary="Sync Strava activities into project")
def strava_sync(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Fetch all Strava activities and add new ones to the project.

    Paginates through the Strava activities endpoint (200 per page) until
    an empty page is returned. Only activities not already in the project are
    added. Returns the count added and the new total.
    """
    user_info_id = int(current_user["sub"])
    user_id = current_user["sub"]

    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id

        token_row = sess.exec(
            select(StravaToken).where(StravaToken.user_info_id == user_info_id)
        ).first()
        if not token_row:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Strava not connected — go to projects and click 'Connect Strava'",
            )

        client = _strava_client_for_token(token_row)
        all_raw = _fetch_all_strava(client)
        _save_cache(user_info_id, all_raw)

        # An id another account already holds is theirs, even when both
        # accounts are linked to the same Strava athlete.
        activities = _project_repo.own_activities_only(
            sess, user_info_id, parse_activities_or_log(all_raw, "strava_sync"))
        project_row_id = row.id

    added_holder: Dict[str, int] = {}

    def _add(project) -> None:
        # Plan limit on trip length (issue #121) — an import that reaches
        # outside the trip's current span stretches it. Re-checked from
        # scratch on every retry attempt, in its own short-lived read-only
        # session, against current DB state rather than the stale snapshot of
        # a previous failed attempt.
        with get_session() as qsess:
            ensure_trip_days_quota(
                qsess, project_row_id, owner_id,
                *[a.start_date_local for a in activities],
            )
        added_holder["added"] = project.add_activities(activities)

    # New activity rows record the IMPORTER (the caller), not the project
    # owner — a companion's imports must stay tied to their Strava account.
    # save_project_with_retry rather than a blind save_project: this is a
    # load-mutate-save, and the blind variant rewrites every field of the row
    # from the snapshot loaded before the mutation — so a PUT /day-meta (or any
    # other write) committing during the Strava fetch, which is a network round
    # trip and by far the widest window in the app, was silently overwritten
    # with pre-request values. Mirrors the bulk import in api/activities.py.
    project = _project_repo.save_project_with_retry(
        owner_id, name, _add, activity_user_id=user_info_id,
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    added = added_holder["added"]

    if added > 0:
        bust_geo_cache(owner_id, name)
    return {"added": added, "total": len(project.activities)}

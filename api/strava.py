"""Strava OAuth + activity sync endpoints.

Routes:
    GET    /api/strava/connect              — returns OAuth URL to redirect user to
    GET    /api/strava/callback             — exchanges auth code, stores token, redirects to app
    GET    /api/strava/status               — {"connected": bool}
    DELETE /api/strava/disconnect           — removes stored Strava token
    GET    /api/strava/activities           — browse user's Strava activities (with filters)
    GET    /api/strava/cache/status         — cache age + activity count
    POST   /api/projects/{name}/strava/sync — syncs Strava activities into a project
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from models.db import get_session

from api.deps import get_current_user
from api.geo import bust_geo_cache
from api.project_access import OwnerParam, resolve_project
from models.project_db import DBProjectItem, DBStravaCache
from models.user import StravaToken, UserInfo
from src.api.strava_client import StravaAPI
from src.auth.oauth import OAuth2Session
from src.billing.entitlements import ensure_trip_days_quota
from src.config.settings import Config
from src.filters.filter_engine import FilterCriteria, FilterEngine
from src.models.activity import Activity, parse_activities_or_log
from src.project.project_io import ProjectIO
from src.project.project_repo import ProjectRepo

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

class ConnectUrlOut(BaseModel):
    url: str = Field(description="Strava OAuth authorization URL to redirect the user to")

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

def _load_cache(user_info_id: int) -> Dict[str, Any] | None:
    """Return the cached payload if it exists and is within TTL, else None."""
    with get_session() as sess:
        row = sess.get(DBStravaCache, user_info_id)
    if row is None:
        return None
    age = time.time() - row.fetched_at
    if age > _CACHE_TTL:
        return None
    try:
        return {"fetched_at": row.fetched_at, "activities": json.loads(row.activities_json)}
    except Exception:
        return None


def _save_cache(user_info_id: int, raw_activities: List[Dict[str, Any]]) -> None:
    """Persist the raw Strava activity list to the DB cache."""
    with get_session() as sess:
        row = sess.get(DBStravaCache, user_info_id)
        if row is None:
            row = DBStravaCache(user_info_id=user_info_id)
            sess.add(row)
        row.fetched_at = time.time()
        row.activities_json = json.dumps(raw_activities)
        sess.commit()


def _invalidate_cache(user_info_id: int) -> None:
    """Remove the cached activity row so the next request re-fetches from Strava."""
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
    """Build a StravaAPI instance pre-loaded with tokens from the DB."""
    client = StravaAPI(_cfg)
    client.token_data = {
        "access_token": token_row.access_token,
        "refresh_token": token_row.refresh_token,
        "expires_at": token_row.expires_at,
    }
    return client


def _save_refreshed_token(sess, token_row: StravaToken, client: StravaAPI) -> None:
    """Persist token back to DB if StravaAPI refreshed it during the request."""
    new = client.token_data
    if (
        new.get("access_token") != token_row.access_token
        or new.get("expires_at", 0) != token_row.expires_at
    ):
        token_row.access_token = new.get("access_token", token_row.access_token)
        token_row.refresh_token = new.get("refresh_token", token_row.refresh_token)
        token_row.expires_at = new.get("expires_at", token_row.expires_at)
        sess.add(token_row)
        sess.commit()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/strava/connect", response_model=ConnectUrlOut,
            summary="Get Strava OAuth URL")
def strava_connect(current_user: Annotated[dict, Depends(get_current_user)]):
    """Return the Strava OAuth authorization URL to redirect the user to."""
    if not _cfg.validate_strava_config():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Strava not configured (missing client_id/secret in config.json)",
        )
    from api.deps import create_access_token
    from models.user import UserInfo
    from sqlmodel import select

    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        user_info = sess.exec(
            select(UserInfo).where(UserInfo.id == user_info_id)
        ).first()
        if user_info is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        state_token = create_access_token(user_info)

    oauth = OAuth2Session(_cfg)
    oauth.redirect_uri = _CALLBACK_URI
    base_url = oauth.authorization_url()
    return {"url": f"{base_url}&state={state_token}"}


@router.get("/api/strava/callback", include_in_schema=False)
def strava_callback(
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
):
    """Handle Strava OAuth redirect — exchanges code for tokens and redirects to the Flutter app."""
    if error or not code:
        return RedirectResponse(f"{_FRONTEND_ORIGIN}/oauth_callback.html?strava=error")

    if not state:
        return RedirectResponse(f"{_FRONTEND_ORIGIN}/oauth_callback.html?strava=error&reason=no_state")

    from api.deps import decode_token
    try:
        payload = decode_token(state)
    except HTTPException:
        return RedirectResponse(f"{_FRONTEND_ORIGIN}/oauth_callback.html?strava=error&reason=invalid_state")

    user_info_id = int(payload["sub"])

    try:
        oauth = OAuth2Session(_cfg)
        oauth.redirect_uri = _CALLBACK_URI
        token_data = oauth.exchange_code(code)
    except Exception as exc:
        return RedirectResponse(
            f"{_FRONTEND_ORIGIN}/oauth_callback.html?strava=error&reason={str(exc)[:80]}"
        )

    with get_session() as sess:
        existing = sess.exec(
            select(StravaToken).where(StravaToken.user_info_id == user_info_id)
        ).first()
        if existing:
            existing.access_token = token_data.get("access_token", "")
            existing.refresh_token = token_data.get("refresh_token", "")
            existing.expires_at = float(token_data.get("expires_at", time.time() + 21600))
            sess.add(existing)
        else:
            row = StravaToken(
                user_info_id=user_info_id,
                access_token=token_data.get("access_token", ""),
                refresh_token=token_data.get("refresh_token", ""),
                expires_at=float(token_data.get("expires_at", time.time() + 21600)),
            )
            sess.add(row)
        sess.commit()

    return RedirectResponse(f"{_FRONTEND_ORIGIN}/oauth_callback.html?strava=connected")


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
    """Remove the stored Strava token for the current user."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = sess.exec(
            select(StravaToken).where(StravaToken.user_info_id == user_info_id)
        ).first()
        if row:
            sess.delete(row)
            sess.commit()


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
            _save_refreshed_token(sess, token_row, client)
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
                _save_refreshed_token(sess, token_row, client)

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
    with get_session() as sess:
        row = sess.get(DBStravaCache, user_info_id)
    if row is None or not row.activities_json:
        return {"cached": False, "count": 0, "age_seconds": None}
    try:
        age = time.time() - row.fetched_at
        count = len(json.loads(row.activities_json))
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
        _save_refreshed_token(sess, token_row, client)

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

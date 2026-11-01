"""Immich proxy endpoints (self-hosted photo library integration, issue #33).

Immich (https://immich.app) sends no CORS headers, so the Flutter web client
cannot call a user's Immich server directly from the browser. Every route here
proxies through our own API instead — clients never see the user's Immich
server URL or API key, and image bytes are streamed through, never persisted
or logged server-side.

Routes:
    GET    /api/immich/status                 — connection status
    PUT    /api/immich/config                 — validate & store server_url + api_key
    DELETE /api/immich/disconnect             — remove stored config
    GET    /api/immich/albums                 — list the user's albums, owned and shared
    POST   /api/immich/search                 — search Immich by taken date range
    GET    /api/immich/assets/{id}/thumbnail  — proxy an asset's thumbnail bytes
    GET    /api/immich/assets/{id}/original   — proxy an asset's original bytes
"""
from __future__ import annotations

from typing import Annotated, List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from api.deps import get_current_user
from models.db import get_session
from models.user import ImmichToken
from src.immich.client import (  # noqa: F401  (_http and the timeouts are used by scripts and tests)
    _ALLOWED_HOSTS_ENV,
    _DOWNLOAD_TIMEOUT,
    _SEARCH_TIMEOUT,
    _VALIDATE_TIMEOUT,
    ImmichAddressNotAllowed,
    ImmichBadAnswer,
    ImmichClient,
    ImmichRejected,
    ImmichUnreachable,
    _http,
)
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/immich", tags=["immich"])
_log = get_logger(__name__)


def _not_allowed(status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=("This Immich server's address is not allowed. A server on a private "
                f"network must be listed by the operator in {_ALLOWED_HOSTS_ENV}."),
    )


# ── Response / request schemas ─────────────────────────────────────────────────

class ImmichStatusOut(BaseModel):
    connected: bool = Field(description="True if an Immich token is stored with a non-empty api_key")
    server_url: Optional[str] = Field(None, description="Configured Immich server URL, or null if not connected")


class ImmichConfigRequest(BaseModel):
    server_url: str = Field(description="Base URL of the user's Immich server, e.g. https://immich.example.com")
    api_key: str = Field(description="Immich API key")


class ImmichConfigOut(BaseModel):
    connected: bool = Field(True, description="Always true on success")
    server_url: str = Field(description="The saved Immich server URL")


class ImmichSearchRequest(BaseModel):
    taken_after: str = Field(description="ISO 8601 timestamp — lower bound for photo taken date")
    taken_before: str = Field(description="ISO 8601 timestamp — upper bound for photo taken date")


class ImmichCandidateOut(BaseModel):
    id: str = Field(description="Immich asset id")
    taken_at: str = Field(description="Capture timestamp (Immich's fileCreatedAt)")
    local_taken_at: Optional[str] = Field(
        None,
        description="Wall-clock local capture time (Immich's localDateTime, written with a "
                    "trailing Z that is not UTC), or null. Its date part is the local capture day.",
    )
    lat: Optional[float] = Field(None, description="GPS latitude from EXIF, or null if unavailable")
    lon: Optional[float] = Field(None, description="GPS longitude from EXIF, or null if unavailable")
    thumb_url: str = Field(description="Our own proxied thumbnail URL for this asset")


class ImmichSearchOut(BaseModel):
    candidates: List[ImmichCandidateOut] = Field(
        description="Day-matched Immich assets. No geo filtering is done here — "
                     "the client's own matching engine does day+geo filtering."
    )


class ImmichAlbumOut(BaseModel):
    id: str = Field(description="Immich album id")
    name: str = Field(description="Album name")
    asset_count: Optional[int] = Field(None, description="Number of assets in the album, or null")
    start_date: Optional[str] = Field(None, description="Earliest asset date, or null")
    end_date: Optional[str] = Field(None, description="Latest asset date, or null")
    thumb_url: Optional[str] = Field(None, description="Our own proxied URL of the album cover, or null")
    shared: bool = Field(description="True if the album is shared")


class ImmichAlbumsOut(BaseModel):
    albums: List[ImmichAlbumOut] = Field(description="The user's own albums, then those shared with them")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_token(user_info_id: int) -> ImmichToken | None:
    with get_session() as sess:
        return sess.exec(
            select(ImmichToken).where(ImmichToken.user_info_id == user_info_id)
        ).first()


def _require_token(user_info_id: int) -> ImmichToken:
    tok = _get_token(user_info_id)
    if tok is None or not tok.api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Immich not connected",
        )
    return tok


def _headers(api_key: str) -> dict:
    return {"x-api-key": api_key}


def _client_error(exc: Exception, what: str) -> HTTPException:
    """A client failure as a 502. A 403 names the permissions the key needs."""
    if isinstance(exc, ImmichAddressNotAllowed):
        return _not_allowed(status.HTTP_502_BAD_GATEWAY)
    if isinstance(exc, ImmichUnreachable):
        detail = f"Could not reach Immich: {exc}"
    elif isinstance(exc, ImmichRejected):
        detail = str(exc) if exc.status == 403 else f"{what} failed (HTTP {exc.status})"
    else:
        detail = f"{what} returned an unreadable answer"
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


def _proxy_asset(tok: ImmichToken, asset_id: str, subpath: str) -> StreamingResponse:
    """Stream an asset's bytes from the user's Immich server through to the caller."""
    url = f"{tok.server_url}/api/assets/{asset_id}/{subpath}"
    try:
        resp = _http.get(
            url, headers=_headers(tok.api_key), timeout=_DOWNLOAD_TIMEOUT, stream=True,
        )
    except ImmichAddressNotAllowed:
        raise _not_allowed(status.HTTP_502_BAD_GATEWAY)
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach Immich: {exc}",
        )
    if resp.status_code != 200:
        resp.close()
    if resp.status_code == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Immich asset not found")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Immich returned HTTP {resp.status_code}",
        )
    content_type = resp.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(resp.iter_content(chunk_size=65536), media_type=content_type)


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status", response_model=ImmichStatusOut, summary="Get Immich connection status")
def immich_status(current_user: Annotated[dict, Depends(get_current_user)]):
    """Return whether the current user has a stored, non-empty Immich API key."""
    user_info_id = int(current_user["sub"])
    tok = _get_token(user_info_id)
    connected = tok is not None and bool(tok.api_key)
    return {"connected": connected, "server_url": tok.server_url if connected else None}


# ── Config (connect / update) ───────────────────────────────────────────────────

@router.put("/config", response_model=ImmichConfigOut, summary="Configure Immich server + API key")
def immich_config(
    body: ImmichConfigRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Validate the given server_url + api_key against Immich and store them.

    Validation calls `GET {server_url}/api/users/me` with the `x-api-key`
    header — Immich's `/api/server/ping` requires no auth at all, so it can't
    tell us whether the key is valid; `/api/users/me` does require it.
    Returns 422 if the server can't be reached or rejects the key.
    """
    user_info_id = int(current_user["sub"])
    server_url = body.server_url.strip().rstrip("/")
    api_key = body.api_key.strip()

    try:
        resp = _http.get(
            f"{server_url}/api/users/me", headers=_headers(api_key), timeout=_VALIDATE_TIMEOUT,
        )
    except ImmichAddressNotAllowed:
        raise _not_allowed(status.HTTP_422_UNPROCESSABLE_ENTITY)
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Could not reach Immich server: {exc}",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Immich rejected the server URL / API key (HTTP {resp.status_code})",
        )

    with get_session() as sess:
        tok = sess.exec(
            select(ImmichToken).where(ImmichToken.user_info_id == user_info_id)
        ).first()
        if tok is None:
            tok = ImmichToken(user_info_id=user_info_id)
            sess.add(tok)
        tok.server_url = server_url
        tok.api_key = api_key
        sess.commit()

    return {"connected": True, "server_url": server_url}


# ── Disconnect ────────────────────────────────────────────────────────────────

@router.delete("/disconnect", status_code=status.HTTP_204_NO_CONTENT, summary="Disconnect Immich account")
def immich_disconnect(current_user: Annotated[dict, Depends(get_current_user)]):
    """Remove the stored Immich config for the current user. No-op if not connected."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        tok = sess.exec(
            select(ImmichToken).where(ImmichToken.user_info_id == user_info_id)
        ).first()
        if tok is not None:
            sess.delete(tok)
            sess.commit()


# ── Search ────────────────────────────────────────────────────────────────────

@router.post("/search", response_model=ImmichSearchOut, summary="Search Immich assets by taken date range")
def immich_search(
    body: ImmichSearchRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Passthrough to Immich's `POST /api/search/metadata`, every page of it
    up to the client's page cap; a cut result is logged.

    No geo filtering happens here — every day-matched asset (with its lat/lon,
    if EXIF GPS is present) is returned as-is; the Flutter client's own
    `photo_match.dart` engine does day+geo matching.
    """
    user_info_id = int(current_user["sub"])
    tok = _require_token(user_info_id)

    client = ImmichClient(tok.server_url, tok.api_key)
    try:
        assets, truncated = client.search_assets(body.taken_after, body.taken_before)
    except (ImmichAddressNotAllowed, ImmichUnreachable, ImmichRejected, ImmichBadAnswer) as exc:
        raise _client_error(exc, "Immich search")
    if truncated:
        _log.warning("immich search truncated user=%s kept=%d", user_info_id, len(assets))

    return {"candidates": [
        {
            "id": a.id,
            "taken_at": a.file_created_at,
            "local_taken_at": a.local_date_time,
            "lat": a.lat,
            "lon": a.lon,
            "thumb_url": f"/api/immich/assets/{a.id}/thumbnail",
        }
        for a in assets
    ]}


# ── Albums ────────────────────────────────────────────────────────────────────

@router.get("/albums", response_model=ImmichAlbumsOut, summary="List the user's Immich albums")
def immich_albums(current_user: Annotated[dict, Depends(get_current_user)]):
    """The user's own Immich albums and those shared with them, each once."""
    tok = _require_token(int(current_user["sub"]))
    client = ImmichClient(tok.server_url, tok.api_key)
    try:
        albums = client.list_albums()
    except (ImmichAddressNotAllowed, ImmichUnreachable, ImmichRejected, ImmichBadAnswer) as exc:
        raise _client_error(exc, "Immich album listing")
    return {"albums": [
        {
            "id": a.id,
            "name": a.name,
            "asset_count": a.asset_count,
            "start_date": a.start_date,
            "end_date": a.end_date,
            "thumb_url": (f"/api/immich/assets/{a.thumbnail_asset_id}/thumbnail"
                          if a.thumbnail_asset_id else None),
            "shared": a.shared,
        }
        for a in albums
    ]}


# ── Asset proxying ────────────────────────────────────────────────────────────

@router.get("/assets/{asset_id}/thumbnail", summary="Proxy an Immich asset thumbnail")
def immich_asset_thumbnail(
    asset_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Stream the thumbnail bytes for one asset from the user's Immich server."""
    tok = _require_token(int(current_user["sub"]))
    return _proxy_asset(tok, asset_id, "thumbnail")


@router.get("/assets/{asset_id}/original", summary="Proxy an Immich asset original")
def immich_asset_original(
    asset_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Stream the original file bytes for one asset from the user's Immich server."""
    tok = _require_token(int(current_user["sub"]))
    return _proxy_asset(tok, asset_id, "original")

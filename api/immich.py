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
    POST   /api/immich/search                 — search Immich by taken date range
    GET    /api/immich/assets/{id}/thumbnail  — proxy an asset's thumbnail bytes
    GET    /api/immich/assets/{id}/original   — proxy an asset's original bytes
"""
from __future__ import annotations

from typing import Annotated, List, Optional

import json as _json
import os

import requests
import urllib3
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from api.deps import get_current_user
from models.db import get_session
from models.user import ImmichToken
from src.utils.safe_fetch import DestinationRefused, FetchRefused, SafeResponse, open_url

router = APIRouter(prefix="/api/immich", tags=["immich"])

# (connect, read) seconds — same shape as the other outbound clients in this
# codebase (see src/api/strava_client.py, src/api/polarsteps_client.py).
_VALIDATE_TIMEOUT: tuple = (5, 10)
_SEARCH_TIMEOUT: tuple = (5, 30)
# Original photos can be multi-MB, so downloads get a more generous read budget.
_DOWNLOAD_TIMEOUT: tuple = (5, 60)

# Body caps: a JSON answer is small; an original can be a large photo.
_MAX_JSON_BYTES = 20 * 1024 * 1024
_MAX_ASSET_BYTES = 200 * 1024 * 1024
# A streamed original may take a while on a slow uplink (and is paced by the
# downstream client); the per-read wait, _DOWNLOAD_TIMEOUT's read part, still
# bounds a stalled server.
_DOWNLOAD_TOTAL_SECONDS = 30 * 60

# The Immich server URL is the user's choice, so every call to it goes through
# the guarded fetch (src/utils/safe_fetch): public addresses only, unless the
# operator lists the host in IMMICH_ALLOWED_HOSTS (a self-hosted Immich on the
# LAN or another container). Loopback and link-local stay refused regardless.
_ALLOWED_HOSTS_ENV = "IMMICH_ALLOWED_HOSTS"


def _allowed_private_hosts() -> list:
    """Hosts the operator allows to be private, read per call (runtime env)."""
    return [h.strip() for h in os.environ.get(_ALLOWED_HOSTS_ENV, "").split(",") if h.strip()]


class ImmichAddressNotAllowed(Exception):
    """The Immich server URL points at an address the policy refuses."""


class _Response:
    """The slice of a requests.Response this module uses. A non-streamed
    response is read in full when it is opened, so a failure while reading
    the body surfaces there, as a requests exception, like before."""

    def __init__(self, resp: SafeResponse, *, stream: bool):
        self._resp = resp
        self.status_code = resp.status
        self.headers = resp.headers
        self._body = None if stream else resp.read_all()

    def json(self):
        body = self._body if self._body is not None else self._resp.read_all()
        charset = None
        for part in (self.headers.get("content-type") or "").split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip().strip('"')
        try:
            return _json.loads(body.decode(charset) if charset else body)
        except LookupError:  # an unknown charset: let json detect UTF-8/16/32
            return _json.loads(body)

    def iter_content(self, chunk_size=None):
        return self._resp.iter_bytes()

    def close(self):
        self._resp.close()


class _GuardedHttp:
    """requests-shaped get/post over the guarded fetch. Network failures come
    back as requests exceptions, as before; a refused address as
    ImmichAddressNotAllowed."""

    def _open(self, method, url, *, headers, body, timeout, max_bytes, total, stream):
        try:
            resp = open_url(
                url, method=method, headers=headers, body=body,
                max_bytes=max_bytes, total_timeout=total, idle_timeout=float(timeout[1]),
                allowed_private_hosts=_allowed_private_hosts(),
            )
        except DestinationRefused as exc:
            raise ImmichAddressNotAllowed(str(exc)) from None
        except FetchRefused as exc:
            raise requests.RequestException(str(exc)) from None
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise requests.ConnectionError(str(exc)) from None
        try:
            return _Response(resp, stream=stream)
        except FetchRefused as exc:
            raise requests.RequestException(str(exc)) from None
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise requests.ConnectionError(str(exc)) from None

    def get(self, url, headers=None, timeout=_VALIDATE_TIMEOUT, stream=False):
        if stream:
            return self._open("GET", url, headers=headers, body=None, timeout=timeout,
                              max_bytes=_MAX_ASSET_BYTES, total=_DOWNLOAD_TOTAL_SECONDS, stream=True)
        return self._open("GET", url, headers=headers, body=None, timeout=timeout,
                          max_bytes=_MAX_JSON_BYTES, total=float(sum(timeout)), stream=False)

    def post(self, url, headers=None, json=None, timeout=_SEARCH_TIMEOUT):
        body = _json.dumps(json).encode() if json is not None else None
        hdrs = {**(headers or {}), "Content-Type": "application/json"}
        return self._open("POST", url, headers=hdrs, body=body, timeout=timeout,
                          max_bytes=_MAX_JSON_BYTES, total=float(sum(timeout)), stream=False)


_http = _GuardedHttp()


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
    lat: Optional[float] = Field(None, description="GPS latitude from EXIF, or null if unavailable")
    lon: Optional[float] = Field(None, description="GPS longitude from EXIF, or null if unavailable")
    thumb_url: str = Field(description="Our own proxied thumbnail URL for this asset")


class ImmichSearchOut(BaseModel):
    candidates: List[ImmichCandidateOut] = Field(
        description="Day-matched Immich assets. No geo filtering is done here — "
                     "the client's own matching engine does day+geo filtering."
    )


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
    """Thin passthrough to Immich's `POST /api/search/metadata`.

    No geo filtering happens here — every day-matched asset (with its lat/lon,
    if EXIF GPS is present) is returned as-is; the Flutter client's own
    `photo_match.dart` engine does day+geo matching.
    """
    user_info_id = int(current_user["sub"])
    tok = _require_token(user_info_id)

    payload = {
        "takenAfter": body.taken_after,
        "takenBefore": body.taken_before,
        "withExif": True,
    }
    try:
        resp = _http.post(
            f"{tok.server_url}/api/search/metadata",
            headers=_headers(tok.api_key),
            json=payload,
            timeout=_SEARCH_TIMEOUT,
        )
    except ImmichAddressNotAllowed:
        raise _not_allowed(status.HTTP_502_BAD_GATEWAY)
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach Immich: {exc}",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Immich search failed (HTTP {resp.status_code})",
        )

    items = resp.json().get("assets", {}).get("items", [])
    candidates = []
    for item in items:
        asset_id = item.get("id")
        if not asset_id:
            continue
        exif = item.get("exifInfo") or {}
        candidates.append({
            "id": asset_id,
            "taken_at": item.get("fileCreatedAt"),
            "lat": exif.get("latitude"),
            "lon": exif.get("longitude"),
            "thumb_url": f"/api/immich/assets/{asset_id}/thumbnail",
        })
    return {"candidates": candidates}


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

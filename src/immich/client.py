"""Client for a user's Immich server (issue #33).

Used by the proxy routes in ``api/immich.py`` and by background jobs. Every
call goes through the guarded fetch (src/utils/safe_fetch): public addresses
only, unless the operator lists the host in IMMICH_ALLOWED_HOSTS (a
self-hosted Immich on the LAN or another container). Loopback and link-local
stay refused regardless.

Minimum Immich version: v5.9.0. Album photos are listed through
``POST /api/search/metadata`` with ``albumIds``, a date window and
``type: IMAGE`` (OD1). ``localDateTime`` is wall-clock local time written with
a trailing ``Z``: its date part is the local capture day, never converted.
"""
from __future__ import annotations

import json as _json
import math
import os
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple
from urllib.parse import quote

import requests
import urllib3

from src.utils.safe_fetch import DestinationRefused, FetchRefused, SafeResponse, open_url

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

_ALLOWED_HOSTS_ENV = "IMMICH_ALLOWED_HOSTS"

# Search paging (D10, D12): 1,000 items a page; the dialog's search stops
# after 10 pages and says so, the album iterator fails past 10,000 items.
_PAGE_SIZE = 1000
_SEARCH_MAX_PAGES = 10
_ALBUM_MAX_ITEMS = 10_000

_THUMBNAIL_SIZES = ("thumbnail", "preview", "fullsize")

# What the feature needs from the user's API key (OD5).
REQUIRED_PERMISSIONS = ("album.read", "asset.read", "asset.view", "asset.download")


def _allowed_private_hosts() -> list:
    """Hosts the operator allows to be private, read per call (runtime env)."""
    return [h.strip() for h in os.environ.get(_ALLOWED_HOSTS_ENV, "").split(",") if h.strip()]


# ── Errors ────────────────────────────────────────────────────────────────────

class ImmichError(Exception):
    """Any failure talking to Immich."""


class ImmichAddressNotAllowed(ImmichError):
    """The Immich server URL points at an address the policy refuses."""


class ImmichUnreachable(ImmichError):
    """The server could not be reached, or its answer could not be read."""


class ImmichRejected(ImmichError):
    """Immich answered with an HTTP status other than 200."""

    def __init__(self, status: int):
        self.status = status
        if status == 403:
            message = ("Immich refused the request (HTTP 403). The API key needs the "
                       f"permissions {', '.join(REQUIRED_PERMISSIONS[:-1])} and "
                       f"{REQUIRED_PERMISSIONS[-1]}.")
        else:
            message = f"Immich returned HTTP {status}"
        super().__init__(message)


class ImmichBadAnswer(ImmichError):
    """Immich answered 200 with something that is not the expected shape."""


class ImmichTooManyAssets(ImmichError):
    """The album holds more photos in the window than the iterator allows."""

    def __init__(self, max_items: int):
        self.max_items = max_items
        super().__init__(f"More than {max_items} photos in the album for these dates")


# ── Guarded HTTP ──────────────────────────────────────────────────────────────

class _Response:
    """The slice of a requests.Response this module uses. A non-streamed
    response is read in full when it is opened, so a failure while reading
    the body surfaces there, as a requests exception, like before."""

    def __init__(self, resp: SafeResponse, *, stream: bool):
        self._resp = resp
        self.status_code = resp.status
        self.headers = resp.headers
        self._body = None if stream else resp.read_all()

    @property
    def content(self) -> bytes:
        if self._body is None:
            self._body = self._resp.read_all()
        return self._body

    def json(self):
        body = self.content
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
        except ValueError as exc:  # http.client refuses e.g. a header with a line break
            raise requests.RequestException(str(exc)) from None
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


# The one instance every caller goes through; tests patch its get/post.
_http = _GuardedHttp()


# ── Parsing ───────────────────────────────────────────────────────────────────

def _coordinate(value, limit: float):
    """A finite number within +/-limit, else None (bool is not a number here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and -limit <= value <= limit else None


def _text(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _count(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    asset_count: Optional[int]
    start_date: Optional[str]
    end_date: Optional[str]
    thumbnail_asset_id: Optional[str]
    shared: bool


@dataclass(frozen=True)
class Asset:
    id: str
    type: Optional[str]
    # Wall-clock local time with a trailing Z; its date part is the local day.
    local_date_time: Optional[str]
    # A true UTC instant.
    file_created_at: str
    lat: Optional[float]
    lon: Optional[float]
    mime: Optional[str]
    file_size: Optional[int]
    original_file_name: Optional[str]
    is_trashed: bool
    is_archived: bool
    # The original's size from EXIF, as stored: not turned by its orientation.
    width: Optional[int] = None
    height: Optional[int] = None


def _asset(item) -> Optional[Asset]:
    """One search item, or None when it has no usable id or capture time."""
    if not isinstance(item, dict):
        return None
    asset_id = _text(item.get("id"))
    created = _text(item.get("fileCreatedAt"))
    if asset_id is None or created is None:
        return None
    exif = item.get("exifInfo")
    if not isinstance(exif, dict):
        exif = {}
    return Asset(
        id=asset_id,
        type=_text(item.get("type")),
        local_date_time=_text(item.get("localDateTime")),
        file_created_at=created,
        lat=_coordinate(exif.get("latitude"), 90.0),
        lon=_coordinate(exif.get("longitude"), 180.0),
        mime=_text(item.get("originalMimeType")),
        file_size=_count(exif.get("fileSizeInByte")),
        original_file_name=_text(item.get("originalFileName")),
        is_trashed=item.get("isTrashed") is True,
        # Later Immich versions replace isArchived with a visibility field.
        is_archived=item.get("isArchived") is True or item.get("visibility") == "archive",
        width=_count(exif.get("exifImageWidth")) or None,
        height=_count(exif.get("exifImageHeight")) or None,
    )


def _album(item, *, listed_as_shared: bool) -> Optional[Album]:
    if not isinstance(item, dict):
        return None
    album_id = _text(item.get("id"))
    if album_id is None:
        return None
    name = item.get("albumName")
    return Album(
        id=album_id,
        name=name if isinstance(name, str) else "",
        asset_count=_count(item.get("assetCount")),
        start_date=_text(item.get("startDate")),
        end_date=_text(item.get("endDate")),
        thumbnail_asset_id=_text(item.get("albumThumbnailAssetId")),
        shared=listed_as_shared or item.get("shared") is True,
    )


def _next_page(value, page: int) -> Optional[int]:
    """Immich's nextPage (a string, a number or null) as the next page number."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ImmichBadAnswer("Immich search answered an unreadable next page")
    try:
        number = int(value)
    except ValueError:
        raise ImmichBadAnswer("Immich search answered an unreadable next page") from None
    if number <= page:  # a server that never moves on would page forever
        raise ImmichBadAnswer("Immich search answered a next page that does not advance")
    return number


# ── Client ────────────────────────────────────────────────────────────────────

class ImmichClient:
    """One user's Immich server. Never logs the key, the URL or image bytes."""

    def __init__(self, server_url: str, api_key: str) -> None:
        self._server_url = server_url.rstrip("/")
        self._headers = {"x-api-key": api_key}

    def _url(self, path: str) -> str:
        return f"{self._server_url}{path}"

    @staticmethod
    def _asset_path(asset_id: str, tail: str) -> str:
        return f"/api/assets/{quote(asset_id, safe='')}/{tail}"

    def _get(self, path: str, *, timeout=_SEARCH_TIMEOUT, stream: bool = False) -> _Response:
        try:
            if stream:
                resp = _http.get(self._url(path), headers=self._headers, timeout=timeout, stream=True)
            else:
                resp = _http.get(self._url(path), headers=self._headers, timeout=timeout)
        except requests.RequestException as exc:
            raise ImmichUnreachable(str(exc)) from None
        if resp.status_code != 200:
            resp.close()
            raise ImmichRejected(resp.status_code)
        return resp

    def _get_json(self, path: str):
        resp = self._get(path)
        try:
            return resp.json()
        except ValueError:
            raise ImmichBadAnswer("Immich returned an unreadable answer") from None

    def _search_page(self, payload: dict, page: int) -> Tuple[list, Optional[int]]:
        """One page of POST /api/search/metadata: its raw items and the next page."""
        try:
            resp = _http.post(self._url("/api/search/metadata"), headers=self._headers,
                              json={**payload, "page": page, "size": _PAGE_SIZE},
                              timeout=_SEARCH_TIMEOUT)
        except requests.RequestException as exc:
            raise ImmichUnreachable(str(exc)) from None
        if resp.status_code != 200:
            raise ImmichRejected(resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise ImmichBadAnswer("Immich search returned an unreadable answer") from None
        assets = data.get("assets") if isinstance(data, dict) else None
        items = assets.get("items") if isinstance(assets, dict) else None
        if not isinstance(items, list):
            raise ImmichBadAnswer("Immich search returned an answer of the wrong shape")
        return items, _next_page(assets.get("nextPage"), page)

    def list_albums(self) -> List[Album]:
        """The user's own albums, then the ones shared with them, each once."""
        albums: dict = {}
        for path, listed_as_shared in (("/api/albums", False), ("/api/albums?shared=true", True)):
            data = self._get_json(path)
            if not isinstance(data, list):
                raise ImmichBadAnswer("Immich album list has the wrong shape")
            for item in data:
                album = _album(item, listed_as_shared=listed_as_shared)
                if album is None:
                    continue
                if album.id in albums:
                    if album.shared and not albums[album.id].shared:
                        albums[album.id] = album
                    continue
                albums[album.id] = album
        return list(albums.values())

    def iter_album_assets(self, album_id: str, taken_after: str, taken_before: str, *,
                          max_items: int = _ALBUM_MAX_ITEMS) -> Iterator[Asset]:
        """The album's images taken in the window, every page of them.

        Raises ImmichTooManyAssets rather than yield item ``max_items + 1``:
        a silent cut would leave photos unconsidered (D12).
        """
        payload = {
            "albumIds": [album_id],
            "takenAfter": taken_after,
            "takenBefore": taken_before,
            "type": "IMAGE",
            "withExif": True,
        }
        page: Optional[int] = 1
        yielded = 0
        while page is not None:
            items, next_page = self._search_page(payload, page)
            for item in items:
                asset = _asset(item)
                if asset is None:
                    continue
                if yielded >= max_items:
                    raise ImmichTooManyAssets(max_items)
                yielded += 1
                yield asset
            page = next_page

    def search_assets(self, taken_after: str, taken_before: str, *,
                      max_pages: int = _SEARCH_MAX_PAGES) -> Tuple[List[Asset], bool]:
        """Every asset taken in the window, up to ``max_pages`` pages.

        Returns ``(assets, truncated)``; truncated is True when Immich had
        more pages than were read.
        """
        payload = {"takenAfter": taken_after, "takenBefore": taken_before, "withExif": True}
        assets: List[Asset] = []
        page = 1
        for _ in range(max_pages):
            items, next_page = self._search_page(payload, page)
            assets.extend(a for a in map(_asset, items) if a is not None)
            if next_page is None:
                return assets, False
            page = next_page
        return assets, True

    def thumbnail_bytes(self, asset_id: str, size: str) -> bytes:
        """An asset's thumbnail; ``size=thumbnail`` is WebP on v5.9.0."""
        if size not in _THUMBNAIL_SIZES:
            raise ValueError(f"unknown thumbnail size {size!r}")
        resp = self._get(self._asset_path(asset_id, f"thumbnail?size={size}"),
                         timeout=_DOWNLOAD_TIMEOUT)
        return resp.content

    def open_original(self, asset_id: str) -> _Response:
        """The original file, streamed. The caller closes the response."""
        return self._get(self._asset_path(asset_id, "original"),
                         timeout=_DOWNLOAD_TIMEOUT, stream=True)

    def open_fullsize(self, asset_id: str) -> _Response:
        """The full-resolution JPEG Immich renders, streamed. The caller
        closes the response (OD3: a JPEG asset is applied from this)."""
        return self._get(self._asset_path(asset_id, "thumbnail?size=fullsize"),
                         timeout=_DOWNLOAD_TIMEOUT, stream=True)

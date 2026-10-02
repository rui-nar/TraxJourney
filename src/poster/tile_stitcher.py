"""Web Mercator tile math + Mapbox raster-tile stitching for poster basemaps
(issue #14, Unit B).

Two layers:
  - Pure pixel-math (no network): standard Web Mercator slippy-map tile math
    to pick a zoom level, tile index range, and crop rectangle for a
    geographic bounding box at a target output pixel size. Fully
    unit-testable without touching the network.
  - Tile fetching + stitching: a minimal Mapbox raster-tile HTTP client
    (``MapboxTileClient``) plus ``render_basemap``, which fetches every tile
    in the computed range, pastes them into a Pillow canvas, and crops/resizes
    to the exact requested size.

``render_basemap`` is the public entry point a later unit (Unit E) wires into
``src/poster/poster_job_runner.py`` in place of its solid-grey placeholder —
this module does not touch the job runner itself.
"""
from __future__ import annotations

import io
import math
import os
import threading
import time
from typing import Callable, Dict, Optional, Tuple

import requests
from PIL import Image

from src.config.settings import Config
from src.exceptions.errors import APIError

# Pillow's decompression-bomb guard (default ~89M px, ~179M before raising)
# exists to protect against maliciously crafted untrusted image files — not
# applicable here: this module only ever creates its own canvases at sizes it
# computed itself. A0 @ 300dpi (~9933x14043, ~140M px) is a legitimate target,
# and intermediate stitched-tile crops can be larger still before the final
# resize, so the check is disabled for this process.
Image.MAX_IMAGE_PIXELS = None

# ── Server-side Mapbox token config ───────────────────────────────────────────
# Mirrors api/activities.py's Strava client-id/secret pattern: env var takes
# priority over config/config.json, applied once at import time.

_cfg = Config("config/config.json")
if os.environ.get("MAPBOX_TOKEN"):
    _cfg.set("mapbox.token", os.environ["MAPBOX_TOKEN"])


def _mapbox_token() -> str:
    """Return the configured Mapbox token (env var takes priority over config file)."""
    return os.environ.get("MAPBOX_TOKEN") or _cfg.get("mapbox.token") or ""


# ── Tunables ──────────────────────────────────────────────────────────────────

DEFAULT_TILE_SIZE = 512  # logical tile grid size (see DEFAULT_PIXEL_RATIO)
DEFAULT_MAX_ZOOM = 22    # Mapbox's practical raster max zoom (matches flutter_client's kMaxMapZoom)
DEFAULT_MAX_TILES = 4096  # sanity cap — see render_basemap's docstring for reasoning

# Mapbox's Styles API returns a tile of ``tile_size * pixel_ratio`` actual
# pixels: a `@2x` request at tilesize=512 yields a 1024x1024 image, NOT a
# 512x512 one. The two are deliberately kept separate here:
#   - ``tile_size`` is the *logical* Web Mercator grid unit. All projection
#     math (lonlat_to_pixel, tile_range_for_bounds, crop_rect_for_bounds, and
#     poster_renderer._Projector) is expressed in these logical units.
#   - ``pixel_ratio`` is how many real image pixels each logical unit occupies,
#     and is applied *only* inside render_basemap's stitch/crop step.
# Conflating them means pasting 1024px tiles on a 512px stride, which
# overwrites three quarters of every tile and yields a scrambled, 2x-zoomed
# basemap misaligned with the route and pins.
DEFAULT_PIXEL_RATIO = 2

# The style used by the client's "view" satellite basemap
# (flutter_client/lib/src/projects/basemaps.dart, kMapboxViewUrl) — reused here
# so the poster's basemap visually matches the interactive map.
DEFAULT_STYLE_USERNAME = "port82"
DEFAULT_STYLE_ID = "cmot5rk5l007301sfe4g2fyqz"

# (z, x, y) -> raw tile image bytes (e.g. PNG), sized tile_size x tile_size.
TileFetcher = Callable[[int, int, int], bytes]


# ── Pure pixel math ───────────────────────────────────────────────────────────

def lonlat_to_pixel(lon: float, lat: float, zoom: int, tile_size: int = DEFAULT_TILE_SIZE) -> Tuple[float, float]:
    """Project (lon, lat) degrees to global pixel coordinates at a given zoom.

    Standard Web Mercator slippy-map projection. The whole world is
    ``tile_size * 2**zoom`` pixels square; (0, 0) is the NW corner
    (lon=-180, lat=~+85.0511, the Mercator latitude limit).
    """
    n = 2 ** zoom
    world_px = tile_size * n
    x = (lon + 180.0) / 360.0 * world_px

    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * world_px
    return x, y


def zoom_for_target_size(
    bounds: Dict[str, float],
    target_width: int,
    target_height: int,
    tile_size: int = DEFAULT_TILE_SIZE,
    max_zoom: int = DEFAULT_MAX_ZOOM,
) -> int:
    """Smallest integer zoom whose native tile resolution covers *bounds* at
    >= (*target_width*, *target_height*) pixels.

    Picking the smallest sufficient zoom (rather than the highest available)
    avoids upscaling low-resolution tiles to fill a large print canvas. If
    even ``max_zoom`` doesn't reach the target (the bounding box is very
    small relative to the requested pixel size), ``max_zoom`` is returned as
    the best available resolution.
    """
    for z in range(0, max_zoom + 1):
        x0, y0 = lonlat_to_pixel(bounds["west"], bounds["north"], z, tile_size)
        x1, y1 = lonlat_to_pixel(bounds["east"], bounds["south"], z, tile_size)
        if abs(x1 - x0) >= target_width and abs(y1 - y0) >= target_height:
            return z
    return max_zoom


def tile_range_for_bounds(
    bounds: Dict[str, float],
    zoom: int,
    tile_size: int = DEFAULT_TILE_SIZE,
) -> Tuple[int, int, int, int]:
    """Return the (x_min, x_max, y_min, y_max) tile indices covering *bounds* at *zoom*.

    Indices are clamped to the valid ``[0, 2**zoom - 1]`` range. Assumes
    ``bounds["west"] < bounds["east"]`` (no antimeridian crossing) — trip
    bounding boxes are built from a single contiguous track/memory extent, so
    this holds in practice for ``api/poster.py``'s ``BoundsIn``.
    """
    n = 2 ** zoom
    x0, y0 = lonlat_to_pixel(bounds["west"], bounds["north"], zoom, tile_size)
    x1, y1 = lonlat_to_pixel(bounds["east"], bounds["south"], zoom, tile_size)

    # Subtract a tiny epsilon before floor()ing the far edge so a bound that
    # lands exactly on a tile boundary doesn't pull in an extra empty tile.
    x_min = max(0, min(int(math.floor(x0 / tile_size)), n - 1))
    x_max = max(0, min(int(math.floor((x1 - 1e-9) / tile_size)), n - 1))
    y_min = max(0, min(int(math.floor(y0 / tile_size)), n - 1))
    y_max = max(0, min(int(math.floor((y1 - 1e-9) / tile_size)), n - 1))
    return x_min, x_max, y_min, y_max


def crop_rect_for_bounds(
    bounds: Dict[str, float],
    zoom: int,
    tile_size: int = DEFAULT_TILE_SIZE,
) -> Tuple[float, float, float, float]:
    """Return the (left, top, right, bottom) pixel crop rect for *bounds*
    within the stitched-tile canvas produced by ``tile_range_for_bounds`` at
    the same *zoom*.

    The canvas's (0, 0) is the NW corner of tile ``(x_min, y_min)``; the
    returned rect is relative to that origin and sub-pixel accurate (a
    bounding box's edges rarely land exactly on a tile boundary).
    """
    x_min, _, y_min, _ = tile_range_for_bounds(bounds, zoom, tile_size)
    origin_x = x_min * tile_size
    origin_y = y_min * tile_size

    x0, y0 = lonlat_to_pixel(bounds["west"], bounds["north"], zoom, tile_size)
    x1, y1 = lonlat_to_pixel(bounds["east"], bounds["south"], zoom, tile_size)
    return x0 - origin_x, y0 - origin_y, x1 - origin_x, y1 - origin_y


# ── Tile fetching ─────────────────────────────────────────────────────────────

class MapboxTileClient:
    """Minimal HTTP client for fetching Mapbox raster tiles.

    Mirrors the general shape of ``src/api/strava_client.py``'s ``StravaAPI``
    (a class wrapping ``requests`` with basic retry handling). Transient
    (5xx / network) failures are retried ``MAX_RETRIES`` times with a short
    exponential backoff.

    A 429 (the account's rate limit, shared with the other stacks and the
    clients' interactive maps) is waited out on its own budget of
    ``MAX_RATE_LIMIT_WAIT_S`` total sleep per tile (issue #517): long enough
    that a limit window used up at its very start still ends with an attempt
    after Mapbox's reset.

    Each thread gets its own ``requests.Session``, so consecutive tiles reuse
    keep-alive connections instead of opening a new TCP + TLS connection per
    tile. One per thread because ``Session`` is not documented as
    thread-safe. Sessions are never closed explicitly: RQ work horses and the
    bench are short-lived processes.

    ``deadline`` (a ``time.monotonic()`` instant) bounds every wait and every
    request: a wait that would leave no time for a request is not taken, a
    request's timeout is cut to what is left, and with less than
    ``MIN_REQUEST_S`` left nothing is sent. Every failure, deadline included,
    raises ``APIError``.
    """

    MAX_RETRIES = 3
    MAX_RATE_LIMIT_WAIT_S = 65.0
    MIN_REQUEST_S = 0.5
    # Bounds on a single 429 wait, whatever the headers say.
    MIN_RATE_LIMIT_SLEEP_S = 1.0
    MAX_RATE_LIMIT_SLEEP_S = 30.0
    # Margin past X-Rate-Limit-Reset, so the retry lands after the reset.
    RATE_LIMIT_RESET_MARGIN_S = 1.0

    def __init__(
        self,
        token: str,
        *,
        style_username: str = DEFAULT_STYLE_USERNAME,
        style_id: str = DEFAULT_STYLE_ID,
        tile_size: int = DEFAULT_TILE_SIZE,
        pixel_ratio: int = DEFAULT_PIXEL_RATIO,
        timeout: float = 15.0,
        deadline: Optional[float] = None,
    ):
        if not token:
            raise APIError("MAPBOX_TOKEN is not configured; cannot fetch basemap tiles")
        self.token = token
        self.style_username = style_username
        self.style_id = style_id
        self.tile_size = tile_size
        self.pixel_ratio = pixel_ratio
        self.timeout = timeout
        self.deadline = deadline
        self._local = threading.local()

    def _session(self) -> requests.Session:
        """This thread's session, created on its first fetch."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
        return session

    def _request_timeout(self) -> float:
        """The next request's timeout, cut to what the deadline leaves.

        Raises ``APIError`` when less than ``MIN_REQUEST_S`` is left, so
        ``requests`` is never handed a timeout <= 0.
        """
        if self.deadline is None:
            return self.timeout
        left = self.deadline - time.monotonic()
        if left < self.MIN_REQUEST_S:
            raise APIError("Mapbox tile fetch ran out of time before its deadline")
        return min(self.timeout, left)

    def _wait(self, seconds: float, reason: str) -> None:
        """Sleep *seconds*, unless no request could follow before the deadline."""
        if self.deadline is not None and (
            time.monotonic() + seconds + self.MIN_REQUEST_S > self.deadline
        ):
            raise APIError(
                f"Mapbox tile fetch failed ({reason}); no time left before the "
                f"deadline to wait {seconds:.0f} s and retry"
            )
        time.sleep(seconds)

    def _rate_limit_wait(self, resp, attempt: int) -> float:
        """Seconds to wait after a tile's *attempt*-th 429 (0-based).

        ``X-Rate-Limit-Reset`` (a Unix timestamp, what Mapbox documents) wins,
        plus a margin; then ``Retry-After`` in seconds; then ``2 ** attempt``.
        A missing, malformed or past header counts as absent. The result is
        clamped to [MIN_RATE_LIMIT_SLEEP_S, MAX_RATE_LIMIT_SLEEP_S].
        """
        headers = getattr(resp, "headers", None) or {}
        wait: Optional[float] = None
        reset = _header_seconds(headers, "X-Rate-Limit-Reset")
        until_reset = None if reset is None else reset - time.time()
        if until_reset is not None and until_reset > 0:
            wait = until_reset + self.RATE_LIMIT_RESET_MARGIN_S
        if wait is None:
            retry_after = _header_seconds(headers, "Retry-After")
            if retry_after is not None and retry_after >= 0:
                wait = retry_after
        if wait is None:
            wait = float(2 ** attempt)
        return min(max(wait, self.MIN_RATE_LIMIT_SLEEP_S), self.MAX_RATE_LIMIT_SLEEP_S)

    def fetch_tile(self, z: int, x: int, y: int) -> bytes:
        """Fetch one raster tile's raw image bytes (PNG) from Mapbox's Styles API.

        The returned image is ``tile_size * pixel_ratio`` px square — the
        caller (``render_basemap``) is responsible for stitching on that
        stride, not on ``tile_size``.
        """
        retina = "@2x" if self.pixel_ratio == 2 else ""
        url = (
            f"https://api.mapbox.com/styles/v1/{self.style_username}/{self.style_id}"
            f"/tiles/{self.tile_size}/{z}/{x}/{y}{retina}"
        )
        session = self._session()
        last_error: Optional[str] = None
        attempt = 0  # 5xx / network failures, bounded by MAX_RETRIES
        rate_limited = 0  # 429s, bounded by MAX_RATE_LIMIT_WAIT_S of sleep
        rate_limit_waited = 0.0
        while attempt < self.MAX_RETRIES:
            timeout = self._request_timeout()
            try:
                resp = session.get(url, params={"access_token": self.token}, timeout=timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
                attempt += 1
                if attempt < self.MAX_RETRIES:
                    self._wait(2 ** (attempt - 1), last_error)
                continue

            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 429:
                wait = self._rate_limit_wait(resp, rate_limited)
                rate_limited += 1
                if rate_limit_waited + wait > self.MAX_RATE_LIMIT_WAIT_S:
                    raise APIError(
                        f"Mapbox tile fetch failed (429): still rate-limited after "
                        f"waiting {rate_limit_waited:.0f} s"
                    )
                self._wait(wait, "429")
                rate_limit_waited += wait
                continue
            if resp.status_code >= 500:
                last_error = f"Server error {resp.status_code}"
                attempt += 1
                if attempt < self.MAX_RETRIES:
                    self._wait(2 ** (attempt - 1), last_error)
                continue
            # Other 4xx (bad token, missing tile, etc.) — not retryable.
            raise APIError(f"Mapbox tile fetch failed ({resp.status_code}): {resp.text[:200]}")

        raise APIError(f"Mapbox tile fetch failed after {self.MAX_RETRIES} attempts: {last_error}")


def _header_seconds(headers, name: str) -> Optional[float]:
    """Header *name* as a finite float, or None when missing or malformed."""
    try:
        value = float(headers.get(name))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _default_tile_fetcher(
    tile_size: int = DEFAULT_TILE_SIZE,
    pixel_ratio: int = DEFAULT_PIXEL_RATIO,
    timeout: float = 15.0,
    deadline: Optional[float] = None,
) -> TileFetcher:
    """Build the default network tile_fetcher from the configured MAPBOX_TOKEN.

    Constructed lazily — only called from inside ``render_basemap`` when no
    fetcher is injected — so importing this module and running its
    pure-math / injected-fetcher tests never requires a token or network
    access. *deadline* is handed to the client (see ``MapboxTileClient``).
    """
    client = MapboxTileClient(
        _mapbox_token(), tile_size=tile_size, pixel_ratio=pixel_ratio,
        timeout=timeout, deadline=deadline,
    )
    return client.fetch_tile


# ── Stitching ─────────────────────────────────────────────────────────────────

def render_basemap(
    bounds: Dict[str, float],
    target_width: int,
    target_height: int,
    *,
    tile_fetcher: Optional[TileFetcher] = None,
    tile_size: int = DEFAULT_TILE_SIZE,
    pixel_ratio: int = DEFAULT_PIXEL_RATIO,
    max_zoom: int = DEFAULT_MAX_ZOOM,
    max_tiles: int = DEFAULT_MAX_TILES,
    deadline: Optional[float] = None,
    tile_timeout: float = 15.0,
) -> Image.Image:
    """Render a stitched Mapbox raster basemap covering *bounds* at exactly
    (*target_width*, *target_height*) pixels.

    ``bounds`` is a dict shaped like ``api/poster.py``'s ``BoundsIn``
    (``north``/``south``/``east``/``west`` floats, in degrees). Picks the
    smallest zoom whose native resolution covers the target size (see
    ``zoom_for_target_size``), then for each tile in the covering range:
    downscales it directly to its footprint in the *final* (target_width,
    target_height) output and pastes it there. The output canvas is the only
    full-size allocation this function makes — memory scales with the
    *poster's* output size, not with how many source tiles the bounding box
    happens to need.

    This matters because a large-but-legitimate bounding box (e.g. a
    multi-country trip) can need hundreds of tiles even though the final
    poster is a fixed, modest size: an earlier version of this function built
    one giant canvas at *native tile resolution* across the whole tile range
    first and only cropped/resized it down at the very end. For a
    Toulouse-to-Nordkapp trip that pre-crop canvas was ~757M pixels (~2.3GB
    just for the RGB buffer, before the RGBA conversion and card-rendering
    memory on top) — which is what OOM-killed a real production job (issue
    #14 follow-up) despite the request needing "only" 722 tiles, well under
    the existing tile-count sanity cap. Downscaling per-tile instead avoids
    that intermediate no matter how large the bounding box is.

    Trade-off: resizing each tile independently, rather than resizing one
    fully-assembled mosaic, can leave very faint seam differences at tile
    boundaries compared to the old approach (a LANCZOS filter's support
    window no longer sees a neighboring tile's pixels at the edge). This is
    imperceptible in practice — Mapbox tile boundaries already exist in the
    source imagery — and is a firmly better trade than the render OOM-killing
    the whole worker process.

    ``tile_fetcher`` is ``(z, x, y) -> bytes`` (raw tile image bytes, e.g.
    PNG, sized ``tile_size * pixel_ratio`` square — see
    ``DEFAULT_PIXEL_RATIO``) — inject a fake in tests to avoid real network
    calls; Unit E's tests can do the same. When omitted, a real
    ``MapboxTileClient`` built from the configured ``MAPBOX_TOKEN`` is used.
    Each fetched tile's own reported size wins over the declared
    ``pixel_ratio``, so an injected 1x fake fetcher still stitches correctly
    against the default 2x production setting.

    Raises ``ValueError`` if the tile range at the chosen zoom would exceed
    *max_tiles* — a sanity guard against pathological inputs (now mostly a
    network/time bound, since memory no longer scales with tile count). A
    legitimate A0-sized request needs on the order of a few hundred tiles
    regardless of the bounding box's real-world size, because
    ``zoom_for_target_size`` always picks resolution to match the *output*
    pixel size, not the bbox's geographic extent — but a bbox that is much
    wider than it is tall (or vice versa) can force a zoom high enough to
    blow up the other dimension's tile count, which this cap catches.

    ``deadline`` (a ``time.monotonic()`` timestamp) is an optional wall-clock
    budget for the whole fetch loop: tiles are fetched one at a time, so a
    tile count under *max_tiles* can still run long if each fetch is merely
    slow rather than failing outright — a cap on *count* alone doesn't catch
    that. Past the deadline, ``TimeoutError`` is raised immediately rather
    than continuing to fetch the remaining tiles. ``None`` (the default,
    used by the full-resolution render) means no budget. *tile_timeout* is
    the per-request timeout passed to the default ``MapboxTileClient`` when
    *tile_fetcher* is not injected. That default client also gets *deadline*,
    so its retry waits (a 429 can wait up to a minute) stay within budget.
    """
    zoom = zoom_for_target_size(bounds, target_width, target_height, tile_size, max_zoom)
    x_min, x_max, y_min, y_max = tile_range_for_bounds(bounds, zoom, tile_size)
    tiles_x = x_max - x_min + 1
    tiles_y = y_max - y_min + 1
    if tiles_x * tiles_y > max_tiles:
        raise ValueError(
            f"Basemap render would fetch {tiles_x * tiles_y} tiles at zoom {zoom}, "
            f"exceeding the cap of {max_tiles}; check bounds for an unexpectedly "
            "oblong or oversized extent"
        )

    fetcher = tile_fetcher or _default_tile_fetcher(
        tile_size, pixel_ratio, timeout=tile_timeout, deadline=deadline
    )

    left, top, right, bottom = crop_rect_for_bounds(bounds, zoom, tile_size)
    canvas = Image.new("RGB", (target_width, target_height))

    # The stitch stride is the tiles' *actual* pixel width, which is only
    # known once the first tile is decoded — a `@2x` request returns
    # tile_size*2 px. Deriving it from the image rather than trusting
    # ``pixel_ratio`` keeps injected 1x test fetchers working against the 2x
    # production default, and means a change in Mapbox's response size can
    # never silently reintroduce the overlapping-paste corruption. The scale
    # factors below depend on it, so they're computed lazily on the first
    # tile too.
    scale_x = scale_y = crop_left_real = crop_top_real = None
    fetched = 0
    total = tiles_x * tiles_y
    for ty in range(y_min, y_max + 1):
        for tx in range(x_min, x_max + 1):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"Basemap tile fetch exceeded its time budget after "
                    f"{fetched} of {total} tiles"
                )
            tile_bytes = fetcher(zoom, tx, ty)
            fetched += 1
            tile_img = Image.open(io.BytesIO(tile_bytes)).convert("RGB")
            stride = tile_img.width

            if scale_x is None:
                ratio = stride / tile_size
                crop_left_real = left * ratio
                crop_top_real = top * ratio
                crop_w_real = (right - left) * ratio
                crop_h_real = (bottom - top) * ratio
                scale_x = target_width / crop_w_real
                scale_y = target_height / crop_h_real

            # This tile's footprint in the stitched-canvas's real pixel space
            # (the giant intermediate the old algorithm actually allocated),
            # translated into the final output's pixel space without ever
            # allocating that intermediate.
            rel_left = (tx - x_min) * stride - crop_left_real
            rel_top = (ty - y_min) * stride - crop_top_real
            # round(), not int()/floor(), on every edge: two tiles sharing a
            # geometric boundary compute that boundary from the same rel_*
            # value, so rounding it the same way both times leaves no gap or
            # overlap between them in the output.
            dst_left = round(rel_left * scale_x)
            dst_top = round(rel_top * scale_y)
            dst_right = round((rel_left + stride) * scale_x)
            dst_bottom = round((rel_top + stride) * scale_y)
            dst_w = dst_right - dst_left
            dst_h = dst_bottom - dst_top
            if dst_w < 1 or dst_h < 1:
                continue  # this tile's content rounds away to nothing in the output

            if (dst_w, dst_h) != (stride, stride):
                tile_img = tile_img.resize((dst_w, dst_h), Image.LANCZOS)
            # Pillow clips automatically when part (or all) of the pasted
            # image falls outside the canvas — expected at the fetch range's
            # edges, where a tile's real footprint only partially overlaps
            # the requested bounds.
            canvas.paste(tile_img, (dst_left, dst_top))

    if scale_x is None:  # no tiles in range — degenerate bounds
        raise ValueError("Basemap render produced an empty tile range")

    return canvas

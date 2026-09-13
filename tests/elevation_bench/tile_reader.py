"""Elevation from the AWS Open Data terrarium tileset (issue #386, unit 2).

**Measurement tooling, not production code.** It was written as the production
reader for the flatness oracle in
:func:`src.models.track_edit.terrain_corrected_gain`, and the measurement it
enabled — ``terrarium_measure.py`` beside it — found this tileset (~30 m SRTM
underneath) reads real relief as flat too often to substitute safely; Copernicus
GLO-30 does better. With nothing in the application calling it, keeping it under
``src/`` would be speculative code. It lives here so the measurement stays
reproducible.

It reads the Mapzen/AWS "terrain tiles" in terrarium encoding: plain
``{z}/{x}/{y}.png`` on a public S3 bucket, no token, open licence with
attribution, and safe to cache on disk because terrain does not change. Nothing
here decides whether that elevation is GOOD enough — that is the measurement's
job. This only has to return the tileset's value at a coordinate, the same way
every time.

Three things are easy to get wrong and are pinned by tests:

**Half a pixel.** Pixel ``(i, j)`` of a Web Mercator tile covers global pixel
space ``[i, i+1)``, so its VALUE sits at ``i + 0.5``. Interpolating between
pixel corners instead of pixel centres shifts every sample by half a pixel —
about 6 m at zoom 13 and 52 degrees north — which is an invented bias the
moment it is compared against anything else.

**Tile edges.** The four samples a bilinear read needs can straddle two or four
tiles. A read that only looks inside one tile either clamps at the edge or
indexes past it.

**"No data" versus "not now".** A tile that does not exist (404) is a normal
state — open ocean, the poles — and remembering that saves asking again. A
tile the server failed to send (5xx, timeout) is NOT absent, and remembering
THAT would blind the reader to a whole tile permanently because S3 hiccupped
once. The two must never share a cache entry.
"""
from __future__ import annotations

import io
import math
import os
import tempfile
import time
from array import array
from collections import OrderedDict
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

import requests
from PIL import Image

TERRARIUM_URL = (
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png")

#: Deepest zoom the tileset publishes.
TERRARIUM_MAX_ZOOM = 15

#: Pixels along each side of a terrarium tile.
TILE_SIZE = 256

#: Web Mercator's latitude limit; beyond it there are no tiles.
MAX_LATITUDE = 85.05112878

#: The zoom read by default. Measured against Berlin's 1 m lidar at zooms 12,
#: 13, 14 and 15, the error statistics and every verdict count agreed to within
#: one window: the ~30 m SRTM source underneath is the limit, and reading deeper
#: only interpolates the same posts at four times the tiles per zoom level.
DEFAULT_ZOOM = 13

#: Decoded tiles kept in memory. One track touches a handful; a trip a few dozen.
MEMORY_TILES = 64

#: Retries for a transient failure, with exponential backoff.
MAX_RETRIES = 3

#: Statuses that mean "not now" rather than "not there". 408 and 429 are
#: 4xx codes but say nothing about whether the tile exists: an earlier version
#: stored them as absence, and a single rate limit then blinded the reader to
#: that tile permanently — the exact failure this module exists to prevent.
_TRANSIENT_STATUS = frozenset({408, 425, 429})

#: What a fetcher returns, and what the disk cache stores for, a tile that
#: genuinely does not exist.
MISSING = b""

#: How long a tile that failed TRANSIENTLY is skipped before it is tried again.
#:
#: Not a cache of absence: it lives in memory only and expires. It exists
#: because one bilinear read touches four pixels, often all in one tile, and a
#: failing tile was otherwise re-fetched for each of them — four times per
#: point, each with its own backed-off retries. A 4000-point track during a
#: bucket outage would have spent hours asleep. Failing fast for a short window
#: costs nothing once the bucket recovers.
TRANSIENT_RETRY_AFTER_S = 30.0

TileFetcher = Callable[[int, int, int], Optional[bytes]]


class TransientTileError(Exception):
    """The server could not send a tile right now. Not the same as absent."""


class TileConfigurationError(Exception):
    """The request itself is wrong — a bad URL, a missing credential.

    Neither absence nor a passing outage, so it is not swallowed into "no data":
    a misconfigured endpoint quietly returning nothing looks exactly like a
    world with no terrain, and would never be noticed.
    """


def decode_terrarium(r: int, g: int, b: int) -> float:
    """Elevation in metres from one terrarium pixel: ``R*256 + G + B/256 - 32768``."""
    return (r * 256 + g + b / 256.0) - 32768.0


def lonlat_to_pixel(lon: float, lat: float, zoom: int) -> Tuple[float, float]:
    """Global Web Mercator pixel coordinates of a point, at *zoom*.

    ``(0, 0)`` is the north-west corner of the world at that zoom, and pixel
    ``(i, j)`` spans ``[i, i+1)`` — so a pixel's value belongs at ``i + 0.5``.
    """
    lat = max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))
    world = TILE_SIZE * (1 << zoom)
    x = (lon + 180.0) / 360.0 * world
    siny = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * world
    return x, y


def fetch_terrarium_tile(z: int, x: int, y: int,
                         timeout: float = 15.0) -> Optional[bytes]:
    """Fetch one tile's PNG bytes from the public bucket.

    Returns :data:`MISSING` for a tile that does not exist, and raises
    :class:`TransientTileError` when the server fails to send one that might —
    the caller must not remember the latter as absent.
    """
    url = TERRARIUM_URL.format(z=z, x=x, y=y)
    last: Optional[str] = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last = str(exc)
        else:
            if resp.status_code == 200:
                return resp.content
            if resp.status_code in (403, 404, 410):
                # S3 answers 403 for a key that is not there when listing is
                # not allowed; 410 says gone. These — and only these — mean
                # "no such tile".
                return MISSING
            if resp.status_code not in _TRANSIENT_STATUS and resp.status_code < 500:
                # 400, 401 and the like: the request is wrong, and asking again
                # will not change that. Raised loudly rather than retried, and
                # not caught as transient, so it cannot pass for "no data". This
                # does NOT catch a misspelt bucket, path or extension: S3 answers
                # those with 404, indistinguishable from an absent tile, so a
                # caller that must have data checks a tile known to exist.
                raise TileConfigurationError(
                    f"terrarium tile {z}/{x}/{y} refused: HTTP {resp.status_code}")
            last = f"HTTP {resp.status_code}"
        if attempt < MAX_RETRIES - 1:
            time.sleep(2 ** attempt)
    raise TransientTileError(
        f"terrarium tile {z}/{x}/{y} unavailable after {MAX_RETRIES} attempts: "
        f"{last}")


class TerrariumReader:
    """Bilinear elevation from terrarium tiles, cached in memory and on disk.

    ``fetch`` is injectable so tests never touch the network; production uses
    :func:`fetch_terrarium_tile`. ``cache_dir`` may be ``None`` to keep tiles in
    memory only.
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        *,
        zoom: int = DEFAULT_ZOOM,
        fetch: Optional[TileFetcher] = None,
        memory_tiles: int = MEMORY_TILES,
        retry_after_s: float = TRANSIENT_RETRY_AFTER_S,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if not 0 <= zoom <= TERRARIUM_MAX_ZOOM:
            raise ValueError(
                f"terrarium publishes zooms 0-{TERRARIUM_MAX_ZOOM}, not {zoom}")
        self.cache_dir = cache_dir
        self.zoom = zoom
        self._fetch = fetch or fetch_terrarium_tile
        self._memory: "OrderedDict[Tuple[int, int, int], Optional[array]]" = (
            OrderedDict())
        self._memory_tiles = max(1, memory_tiles)
        self._retry_after = retry_after_s
        self._clock = clock or time.monotonic
        #: Tiles that failed transiently, and when. Never written to disk.
        self._unavailable: "dict[Tuple[int, int, int], float]" = {}

    # ── Public ──────────────────────────────────────────────────────────────
    def elevation(self, lat: float, lon: float) -> Optional[float]:
        """The tileset's elevation at a point, or ``None`` where it has none.

        Bilinear between the four pixel CENTRES around the point, reading
        across tile edges. ``None`` if any of the four is unavailable: an
        interpolation that silently leans on three corners, or on a zero for the
        fourth, is a wrong number that looks like a right one.
        """
        if not (math.isfinite(lat) and math.isfinite(lon)):
            return None
        gx, gy = lonlat_to_pixel(lon, lat, self.zoom)
        # Shift to pixel-centre space: pixel i's value sits at i + 0.5.
        cx, cy = gx - 0.5, gy - 0.5
        x0, y0 = math.floor(cx), math.floor(cy)
        fx, fy = cx - x0, cy - y0

        z00 = self._pixel(x0, y0)
        z10 = self._pixel(x0 + 1, y0)
        z01 = self._pixel(x0, y0 + 1)
        z11 = self._pixel(x0 + 1, y0 + 1)
        if z00 is None or z10 is None or z01 is None or z11 is None:
            return None
        return (z00 * (1 - fx) * (1 - fy) + z10 * fx * (1 - fy)
                + z01 * (1 - fx) * fy + z11 * fx * fy)

    def elevations(
        self, points: Iterable[Tuple[float, float]]
    ) -> List[Optional[float]]:
        """:meth:`elevation` for each ``(lat, lon)`` in *points*, in order."""
        return [self.elevation(lat, lon) for lat, lon in points]

    def tile_grid(self, z: int, x: int, y: int) -> Optional[array]:
        """One tile's decoded 256 x 256 elevations, row-major, or ``None``.

        For bulk readers that sample many points per tile and would rather not
        go through :meth:`elevation` one point at a time. Same caching and the
        same no-data rules.
        """
        return self._tile(z, x, y)

    # ── Tiles ───────────────────────────────────────────────────────────────
    def _pixel(self, gx: int, gy: int) -> Optional[float]:
        """The decoded value of global pixel ``(gx, gy)``, across tile edges."""
        span = TILE_SIZE * (1 << self.zoom)
        # Longitude wraps; latitude does not.
        gx %= span
        if not 0 <= gy < span:
            return None
        tx, px = divmod(gx, TILE_SIZE)
        ty, py = divmod(gy, TILE_SIZE)
        grid = self._tile(self.zoom, tx, ty)
        if grid is None:
            return None
        return grid[py * TILE_SIZE + px]

    def _tile(self, z: int, x: int, y: int) -> Optional[array]:
        key = (z, x, y)
        if key in self._memory:
            self._memory.move_to_end(key)
            return self._memory[key]

        failed_at = self._unavailable.get(key)
        if failed_at is not None:
            if self._clock() - failed_at < self._retry_after:
                return None
            del self._unavailable[key]

        raw = self._read_disk(z, x, y)
        if raw is None:
            try:
                raw = self._fetch(z, x, y)
            except TransientTileError:
                # Not absent — just not now. Skipped for a short window so the
                # other three corners of this read do not each re-fetch it, but
                # never remembered as absence: not on disk, and not past
                # ``retry_after_s``.
                now = self._clock()
                self._unavailable[key] = now
                # Drop entries whose window has passed, so a long outage across
                # many tiles cannot grow this without bound.
                for stale in [k for k, t in self._unavailable.items()
                              if now - t >= self._retry_after]:
                    del self._unavailable[stale]
                return None
            if raw is None:
                raw = MISSING
            self._write_disk(z, x, y, raw)

        grid = self._decode(raw) if raw else None
        self._memory[key] = grid
        if len(self._memory) > self._memory_tiles:
            self._memory.popitem(last=False)
        return grid

    @staticmethod
    def _decode(raw: bytes) -> Optional[array]:
        try:
            image = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            return None
        if image.size != (TILE_SIZE, TILE_SIZE):
            return None
        data = image.tobytes()
        grid = array("f", bytes(4 * TILE_SIZE * TILE_SIZE))
        for i in range(TILE_SIZE * TILE_SIZE):
            r, g, b = data[3 * i], data[3 * i + 1], data[3 * i + 2]
            grid[i] = (r * 256 + g + b / 256.0) - 32768.0
        return grid

    # ── Disk ────────────────────────────────────────────────────────────────
    def _path(self, z: int, x: int, y: int) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, "terrarium", str(z), str(x),
                            f"{y}.png")

    def _read_disk(self, z: int, x: int, y: int) -> Optional[bytes]:
        path = self._path(z, x, y)
        if path is None or not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    def _write_disk(self, z: int, x: int, y: int, raw: bytes) -> None:
        path = self._path(z, x, y)
        if path is None:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write-then-rename, so a crash mid-write cannot leave a truncated PNG
        # that decodes as "no tile" for ever after. The temporary name is unique:
        # a fixed "{path}.part" let two processes fetching the same tile write
        # and rename one file, and one of them crashed.
        fd, partial = tempfile.mkstemp(dir=os.path.dirname(path),
                                       suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            os.replace(partial, path)
        except PermissionError:
            # On Windows a rename onto a file another process has open fails.
            # That process wrote the same tile, so the copy already on disk is
            # the one this write would have produced.
            _discard(partial)
            if not os.path.exists(path):
                raise
        except BaseException:
            _discard(partial)
            raise


def _discard(partial: str) -> None:
    """Best-effort removal of a temporary tile. A scanner holding the file open
    must not replace the error that brought us here with its own."""
    try:
        os.remove(partial)
    except OSError:
        pass


def sample_along(
    reader: TerrariumReader, points: Sequence[Tuple[float, float]]
) -> Optional[List[float]]:
    """Elevation at every point, or ``None`` if the tileset lacks any of them.

    All or nothing, because :func:`terrain_corrected_gain` needs a terrain value
    aligned with every recorded sample and refuses a series whose length does
    not match. A track with a hole in its terrain is better treated as "no
    terrain" — falling back to the recording, which is a normal state — than
    patched with invented values.
    """
    values = reader.elevations(points)
    if any(v is None for v in values):
        return None
    return [float(v) for v in values]

"""Measure global terrain models against 1 m lidar, for the #386 oracle.

    python -m tests.elevation_bench.terrarium_measure [--models terrarium copernicus gedtm]

Not a test, and not collected by pytest: it downloads ~20 MB per lidar tile,
a Copernicus tile and a few hundred terrarium tiles, and needs numpy (and
rasterio for GEDTM30), neither of which the application depends on. It exists so
the verdict on terrain substitution is a number anyone can re-derive rather than
a figure quoted from a transcript.

What it measures, and what it deliberately does not
--------------------------------------------------
The flatness oracle (:func:`src.models.track_edit.terrain_corrected_gain`) asks
one question per 500 m of path: does the terrain model show relief here? Its two
wrong answers are NOT symmetric, and the first version of this measurement
treated them as if they were:

* **Flat ground read as relief** keeps the recording. That is today's behaviour:
  the phantom climb is not removed, and nothing is made worse. A **missed fix**.
* **Relief read as flat** swaps the model in over a real hill. That can erase
  climb that happened. **Harm.**

Counting both as "misreads" made a missed fix look like a failure: the headline
"26 of 26 flat windows misread" in a city was entirely the harmless kind. But
"relief read as flat" is not the only harm either. On a recording that did not
need correcting, ANY substitution replaces good data with the model's own error,
in either direction. So the primary output here is the **outcome** — what the
shipped oracle reports end to end against the truth, for recordings of every
quality, in metres per kilometre — with the per-window verdicts kept as a
diagnostic and their two kinds named for what they are.

Ground truth
------------
Berlin's DGM1: airborne lidar, BARE EARTH, 1 m grid, licensed dl-de/zero-2-0,
https://gdi.berlin.de/data/dgm1/atom/. The sites separate causes that a flat
test would conflate:

* **Tempelhofer Feld**, **Tegel airfield** — flat open ground. Transects are
  confined to rectangles inside each airfield. An earlier version used a whole
  2 km lidar tile at Tempelhof and called it "open, no buildings, no trees"; most
  of that tile is streets, houses and the Ringbahn. Two airfields rather than
  one, because a single site's figure is a handful of independent 500 m patches.
  Both are still Berlin, on one Copernicus tile.
* **Tegel runway berms** — ground that CHANGED after the model's data was
  taken: two ridges about 5 m high and 20 m wide along the closed southern
  runway, in the 2025 lidar and absent from Copernicus (2011-15). Measured as
  its own site, because a first Tegel rectangle straddled them and charged their
  effect to "flat open ground".
* **Luebars** — MIXED rural ground: by OpenStreetMap land use about a quarter
  fields and meadow, a third village and allotments, a quarter wood and wetland.
  Not "rolling farmland"; its lines that are mostly field fare no better.
* **Mitte** — flat ground under a dense city.
* **Grunewald** — flat-ish ground under forest.
* **Teufelsberg**, **Mueggelberge** — real relief.

"Truth" for a gain is the gain pipeline applied to the lidar itself, so every
figure is compared like for like and the pipeline's own smoothing floor cancels.
A line with any gap in the lidar (a site's edge at the Berlin border) is dropped,
and the report says how many.

The recording
-------------
The oracle never sees a perfect path. Each transect is recorded with AR(1)
vertical drift, optional white noise, and AR(1) horizontal drift on a fixed
60 s scale (the path comes from GPS whatever the altimeter is), smoothed over
100 m of ground before the model is read. Recording classes run from a
drifting phone to a clean track, including how devices ENCODE altitude —
smoothed on the device, rounded to whole metres — because a gate that reads the
recording can be fooled by the encoding. Four seeds each; see :data:`RECORDINGS`.

"Worse" is counted per LINE, not per line and seed: the four seeds share a
line's terrain, and a line's terrain explains five to ten times more of the
spread than its seed does, so line x seed counts overstate how many independent
cases agree.
"""
from __future__ import annotations

import argparse
import io
import math
import os
import sys
import zipfile
from typing import Callable, Dict, Sequence, Tuple

try:
    import numpy as np
except ImportError:                                    # pragma: no cover
    raise SystemExit(
        "terrarium_measure needs numpy, which is not an application "
        "dependency: pip install numpy") from None
import requests

from src.models.track_edit import (
    TERRAIN_RELIEF_M,
    TERRAIN_WINDOW_M,
    _noise_estimate,
    _relief,
    _relief_span,
    _terrain_windows,
    elevation_gain,
    terrain_corrected_gain,
)
from tests.elevation_bench import utm
from tests.elevation_bench.generators import _ar1
from tests.elevation_bench.tile_reader import TILE_SIZE, TerrariumReader

CACHE = os.path.join(os.path.expanduser("~"), ".cache",
                     "viewtrip-terrarium-measure")
DGM1_URL = "https://gdi.berlin.de/data/dgm1/atom/DGM1_{e}_{n}.zip"
SAMPLE_M = 5.0
SPEED_MS = 5.0
PATH_SMOOTH_M = 100.0
HORIZONTAL_SIGMA_M = 5.0
HORIZONTAL_TAU_S = 60.0
#: (label, vertical drift sigma m, drift correlation s, extra white noise m,
#:  smoothed on the device over this many m, rounded to this many m).
#:
#: The first version measured only phone-class drift, which is exactly the
#: recording the oracle is built to help, and so could not see the case it
#: harms: a CLEAN recording has almost no phantom climb to remove, and
#: substituting the terrain model then replaces good data with the model's own
#: error, in either direction. The barometers drift with the weather over hours
#: and carry a few tenths of a metre of short-term noise; an earlier "barometric"
#: class (1 m over 10 minutes, no short-term noise) matched no real sensor.
#:
#: How much a phone drifts, and how fast, is not known from real recordings; the
#: "less" and "slower" phone classes bracket it, because a gate that reads
#: disagreement with the model keeps less of the benefit as the drift shrinks.
#:
#: The smoothed and rounded classes exist because a gate on the recording's
#: short-term noise looked
#: like it separated the classes, and only did because none of them was encoded
#: the way devices encode altitude: a phone that smooths its own altitude keeps
#: all its drift but measures almost no noise, and whole-metre rounding gives a
#: clean track noise right at any plausible threshold.
RECORDINGS = (
    ("phone drift", 4.0, 60.0, 0.0, 0.0, 0.0),
    ("phone drift + 3 m white", 4.0, 60.0, 3.0, 0.0, 0.0),
    ("phone drift, smoothed over 25 m", 4.0, 60.0, 0.0, 25.0, 0.0),
    ("phone, less drift: 3 m / 60 s", 3.0, 60.0, 0.0, 0.0, 0.0),
    ("phone, less drift: 2 m / 60 s", 2.0, 60.0, 0.0, 0.0, 0.0),
    ("phone, slower drift: 4 m / 300 s", 4.0, 300.0, 0.0, 0.0, 0.0),
    ("barometer, 1 m / 1 h + 0.2 m", 1.0, 3600.0, 0.2, 0.0, 0.0),
    ("barometer, 2 m / 3 h + 0.3 m", 2.0, 10800.0, 0.3, 0.0, 0.0),
    ("clean", 0.0, 60.0, 0.0, 0.0, 0.0),
    ("clean, rounded to 1 m", 0.0, 60.0, 0.0, 0.0, 1.0),
)
SEEDS = (0, 1, 2, 3)
#: A line is "worse" when, averaged over its seeds, the oracle's total lands this
#: much further from the truth than the recording's. Transects are 0.6-4 km; the
#: 1 m first used counted rounding as harm (the median "worse" run was 1.3 m).
WORSE_M = 2.0
#: Candidate gates on recording quality, none shipped: measured because whether
#: any gate can separate recordings that need correcting from ones that do not
#: is the next decision.
#:
#: * ``noise`` — substitute only when the recording's measured short-term noise
#:   (``_noise_estimate``) exceeds the threshold, in m. It measures how altitude
#:   is ENCODED, not the drift that makes phantom climb.
#: * ``drift`` — substitute a window only when the recording disagrees with the
#:   model across it by more than the margin, in m (range of recording minus
#:   model over the window the oracle judges). For a drifting recording that
#:   disagreement is mostly the drift; for a GOOD recording it is the model's own
#:   error, so this gate lets substitution through exactly where the model is
#:   wrong. The 6 m margin was chosen on this data.
GATES = (("noise", 0.30), ("drift", 4.0), ("drift", 6.0))


# ── Coordinates, vectorised ──────────────────────────────────────────────────
def from_utm_array(easting, northing):
    """:func:`utm.from_utm` over arrays — the same constants, so the tests that
    pin the scalar version pin this one's arithmetic too."""
    xi = northing / (utm._K0 * utm._AA)
    eta = (easting - utm._FE) / (utm._K0 * utm._AA)
    xi_p, eta_p = xi.copy(), eta.copy()
    for j, b in enumerate(utm._BETA, start=1):
        xi_p = xi_p - b * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
        eta_p = eta_p - b * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
    chi = np.arcsin(np.sin(xi_p) / np.cosh(eta_p))
    phi = chi.copy()
    for j, d in enumerate(utm._DELTA, start=1):
        phi = phi + d * np.sin(2 * j * chi)
    lam = utm._LON0 + np.arctan2(np.sinh(eta_p), np.cos(xi_p))
    return np.degrees(phi), np.degrees(lam)


def bilinear(grid, col, row):
    """Bilinear read at fractional (col, row) pixel-CENTRE positions; NaN outside
    the grid rather than a clamped edge value."""
    c0 = np.floor(col).astype(int)
    r0 = np.floor(row).astype(int)
    fc, fr = col - c0, row - r0
    ok = (c0 >= 0) & (c0 < grid.shape[1] - 1) & (r0 >= 0) & (r0 < grid.shape[0] - 1)
    c0 = np.clip(c0, 0, grid.shape[1] - 2)
    r0 = np.clip(r0, 0, grid.shape[0] - 2)
    value = (grid[r0, c0] * (1 - fc) * (1 - fr) + grid[r0, c0 + 1] * fc * (1 - fr)
             + grid[r0 + 1, c0] * (1 - fc) * fr + grid[r0 + 1, c0 + 1] * fc * fr)
    return np.where(ok, value, np.nan)


# ── Ground truth ─────────────────────────────────────────────────────────────
def dgm1_grid(e_km: int, n_km: int):
    """One 2 km Berlin DGM1 tile as a 2000 x 2000 grid, row 0 at the SOUTH edge."""
    path = os.path.join(CACHE, f"DGM1_{e_km}_{n_km}.npy")
    if os.path.exists(path):
        return np.load(path).astype(np.float64)
    zpath = os.path.join(CACHE, f"DGM1_{e_km}_{n_km}.zip")
    if not os.path.exists(zpath):
        resp = requests.get(DGM1_URL.format(e=e_km, n=n_km), timeout=600)
        resp.raise_for_status()
        with open(zpath, "wb") as handle:
            handle.write(resp.content)
    with zipfile.ZipFile(zpath) as archive:
        member = next(m for m in archive.namelist() if m.endswith(".xyz"))
        raw = np.loadtxt(io.TextIOWrapper(archive.open(member)), dtype=np.float64)
    # Place every point by its own coordinates rather than by file order: a row
    # off by one is a 1 m shift, and it would be charged to the model.
    e0, n0 = e_km * 1000, n_km * 1000
    cols = np.round(raw[:, 0] - e0 - 0.5).astype(int)
    rows = np.round(raw[:, 1] - n0 - 0.5).astype(int)
    ok = (cols >= 0) & (cols < 2000) & (rows >= 0) & (rows < 2000)
    grid = np.full((2000, 2000), np.nan, dtype=np.float32)
    grid[rows[ok], cols[ok]] = raw[ok, 2]
    np.save(path, grid)
    return grid.astype(np.float64)


class Lidar:
    """One or more DGM1 tiles side by side, sampled in UTM 33N."""

    def __init__(self, tiles: Sequence[Tuple[int, int]]):
        es = sorted({e for e, _ in tiles})
        ns = sorted({n for _, n in tiles})
        self.e0, self.n0 = es[0] * 1000, ns[0] * 1000
        grid = np.full((1000 * (ns[-1] - ns[0] + 2), 1000 * (es[-1] - es[0] + 2)),
                       np.nan)
        for e_km, n_km in tiles:
            r = (n_km - ns[0]) * 1000
            c = (e_km - es[0]) * 1000
            grid[r:r + 2000, c:c + 2000] = dgm1_grid(e_km, n_km)
        self.grid = grid

    def at(self, easting, northing):
        # DGM1 pixel centres sit at .5 m.
        return bilinear(self.grid, easting - self.e0 - 0.5,
                        northing - self.n0 - 0.5)


# ── The models under test ────────────────────────────────────────────────────
class Terrarium:
    name = "terrarium z13"

    #: A z13 tile over central Berlin, which the tileset certainly has. A
    #: misspelt bucket, path or extension answers 404 exactly as an ocean tile
    #: does, so without this check a misconfigured URL reads as a world without
    #: terrain.
    KNOWN_TILE = (13, 4400, 2686)

    def __init__(self, zoom: int = 13):
        self.zoom = zoom
        self.reader = TerrariumReader(os.path.join(CACHE, "tiles"), zoom=zoom)
        self._grids: Dict[Tuple[int, int], object] = {}
        if self.reader.tile_grid(*self.KNOWN_TILE) is None:
            raise SystemExit(
                f"terrarium tile {self.KNOWN_TILE} is missing: the tileset URL is "
                f"wrong or the service is down, not a world without terrain")

    def _grid(self, tx: int, ty: int):
        key = (tx, ty)
        if key in self._grids:
            return self._grids[key]
        tile = self.reader.tile_grid(self.zoom, tx, ty)
        if tile is None:
            # Not remembered here: the reader already remembers a truly absent
            # tile, and a tile that failed transiently must be asked for again
            # rather than read as NaN for the rest of the run.
            return np.full((TILE_SIZE, TILE_SIZE), np.nan)
        self._grids[key] = np.asarray(tile, dtype=np.float64).reshape(
            TILE_SIZE, TILE_SIZE)
        return self._grids[key]

    def _pixels(self, gx, gy):
        tx, px = np.divmod(gx, TILE_SIZE)
        ty, py = np.divmod(gy, TILE_SIZE)
        out = np.empty(gx.shape)
        for a, b in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
            mask = (tx == a) & (ty == b)
            out[mask] = self._grid(a, b)[py[mask], px[mask]]
        return out

    def at(self, lat, lon):
        world = TILE_SIZE * (1 << self.zoom)
        x = (lon + 180.0) / 360.0 * world
        s = np.sin(np.radians(lat))
        y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * world
        # A Web Mercator pixel's value sits at its centre.
        cx, cy = x - 0.5, y - 0.5
        x0, y0 = np.floor(cx).astype(int), np.floor(cy).astype(int)
        fx, fy = cx - x0, cy - y0
        return (self._pixels(x0, y0) * (1 - fx) * (1 - fy)
                + self._pixels(x0 + 1, y0) * fx * (1 - fy)
                + self._pixels(x0, y0 + 1) * (1 - fx) * fy
                + self._pixels(x0 + 1, y0 + 1) * fx * fy)


class Copernicus:
    """Copernicus GLO-30, a SURFACE model, read with Pillow and no GDAL.

    GeoKey 1025 is 2 — PixelIsPoint — so the tiepoint is the CENTRE of pixel
    (0, 0): the opposite convention to a Web Mercator tile, asserted from the
    file rather than assumed.
    """
    name = "copernicus GLO-30 (surface)"
    URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
           "Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM/"
           "Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM.tif")

    def __init__(self, lat_deg: int = 52, lon_deg: int = 13):
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        path = os.path.join(CACHE, f"copernicus_N{lat_deg}_E{lon_deg}.tif")
        if not os.path.exists(path):
            resp = requests.get(self.URL.format(lat=lat_deg, lon=lon_deg),
                                timeout=600)
            resp.raise_for_status()
            with open(path, "wb") as handle:
                handle.write(resp.content)
        image = Image.open(path)
        tags = image.tag_v2
        keys = tags[34735]
        geokeys = {keys[4 + i * 4]: keys[4 + i * 4 + 3] for i in range(keys[3])}
        if geokeys.get(1025) != 2:
            raise ValueError("expected PixelIsPoint")
        self.dlon, self.dlat = tags[33550][0], tags[33550][1]
        self.lon0, self.lat0 = tags[33922][3], tags[33922][4]
        self.grid = np.array(image, dtype=np.float64)

    def at(self, lat, lon):
        return bilinear(self.grid, (lon - self.lon0) / self.dlon,
                        (self.lat0 - lat) / self.dlat)


class Gedtm30:
    """GEDTM30 v1.2 — a global BARE-EARTH model, CC-BY-4.0 (OpenLandMap).

    Needs rasterio to range-read one window of a global Cloud Optimized GeoTIFF;
    rasterio is not an application dependency. PixelIsArea: the transform's
    origin is a pixel CORNER. Scale, offset and nodata are applied from the
    file's own metadata rather than assumed.
    """
    name = "GEDTM30 v1.2 (bare earth)"
    URL = ("/vsicurl/https://s3.opengeohub.org/global/dtm/v1.2/"
           "gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif")
    # Must cover every site. An earlier window stopped at 52.55 N, missed
    # Luebars at 52.62 N entirely, and the site then reported 0 of 0 runs rather
    # than failing — the silent drop that biases a table without saying so.
    BOUNDS = (13.15, 52.38, 13.70, 52.66)          # west, south, east, north

    def __init__(self):
        path = os.path.join(CACHE, "gedtm30_berlin_v2.npz")
        if not os.path.exists(path):
            try:
                import rasterio
                from rasterio.windows import from_bounds
            except ImportError:
                raise SystemExit("the GEDTM30 source needs rasterio: "
                                 "pip install rasterio") from None
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
                with rasterio.open(self.URL) as ds:
                    window = from_bounds(*self.BOUNDS, ds.transform)
                    window = window.round_offsets().round_lengths()
                    raw = ds.read(1, window=window).astype(np.float64)
                    if ds.nodata is not None:
                        raw[raw == ds.nodata] = np.nan
                    scale = (ds.scales or (1.0,))[0] or 1.0
                    offset = (ds.offsets or (0.0,))[0] or 0.0
                    t = ds.window_transform(window)
            np.savez(path, grid=raw * scale + offset,
                     transform=np.array([t.a, t.c, t.e, t.f]))
        data = np.load(path)
        self.grid = data["grid"]
        self.dlon, self.lon0, self.dlat, self.lat0 = data["transform"]

    def at(self, lat, lon):
        # PixelIsArea: half a pixel to reach centre coordinates.
        return bilinear(self.grid, (lon - self.lon0) / self.dlon - 0.5,
                        (lat - self.lat0) / self.dlat - 0.5)


MODELS: Dict[str, Callable[[], object]] = {
    "terrarium": Terrarium,
    "copernicus": Copernicus,
    "gedtm": Gedtm30,
}


# ── Sites ────────────────────────────────────────────────────────────────────
def _grid_lines(e_min, e_max, n_min, n_max, step=100.0):
    """East-west and north-south lines across a rectangle, sampled every 5 m."""
    lines = []
    for n in np.arange(n_min + step / 2, n_max, step):
        e = np.arange(e_min, e_max + 1e-9, SAMPLE_M)
        lines.append((e, np.full(e.shape, n)))
    for e in np.arange(e_min + step / 2, e_max, step):
        n = np.arange(n_min, n_max + 1e-9, SAMPLE_M)
        lines.append((np.full(n.shape, e), n))
    return lines


def _tile_lines(e_km: int, n_km: int):
    e0, n0 = e_km * 1000, n_km * 1000
    return _grid_lines(e0 + 50, e0 + 1950, n0 + 50, n0 + 1950)


def _embankment(height_m: float):
    """Add two trapezoidal embankments to the LIDAR only — ground the model does
    not have, taller than the drift gate's margin. Crest 10 m wide, 1:1.5
    sides, one north-south at E 391700 (crossed by every east-west Tempelhofer
    line) and one east-west at N 5814850 (crossed by every north-south line)."""
    def edit(lidar: "Lidar") -> None:
        rows, cols = lidar.grid.shape
        run = 1.5 * height_m
        def profile(offset):
            return height_m * np.clip((5.0 + run - np.abs(offset)) / run, 0.0, 1.0)
        east = lidar.e0 + np.arange(cols) + 0.5
        north = lidar.n0 + np.arange(rows) + 0.5
        lidar.grid = lidar.grid + np.maximum(profile(east - 391700.0)[None, :],
                                             profile(north - 5814850.0)[:, None])
    return edit


#: (label, what the ground is, lidar tiles, transects[, edit to the lidar])
SITES = [
    # The park interior: a rectangle at least 60 m inside Tempelhofer Feld on
    # every side, chosen once against the park's outline, so that no street,
    # house or railway reaches a transect. It straddles two lidar tiles.
    # Measured against OSM relation 7317281 the margins were 51 m (east, at
    # 392354) and then 59.6 m north and 59.8 m east; the edges below clear 60 m.
    ("Tempelhofer Feld", "flat open, park interior",
     [(390, 5814), (392, 5814)],
     _grid_lines(391004.0, 392344.0, 5814350.0, 5815374.0)),
    # The same lines with 8 m embankments the model does not have: SYNTHETIC,
    # because the question — what happens where a good recording disagrees with
    # the model by more than the drift gate's margin — needs ground that is
    # certainly missing from the model, at a height chosen rather than found.
    ("Tempelhofer + 8 m embankment", "SYNTHETIC: ground the model lacks",
     [(390, 5814), (392, 5814)],
     _grid_lines(391004.0, 392344.0, 5814350.0, 5815374.0), _embankment(8.0)),
    # A second flat open site, so the airfield figure is not one place's: the
    # strip between the southern runway's berms (below) and the service
    # buildings, over 100 m from the berms' flanks and 58 m or more from any
    # building, wood, scrub or apron in OSM. Its north-south lines are only
    # 280 m, so lines are 50 m apart rather than 100 to keep the site's length
    # comparable.
    ("Tegel airfield", "flat open, runways and grass",
     [(382, 5824), (384, 5824)],
     _grid_lines(383020.0, 384400.0, 5824680.0, 5824960.0, step=50.0)),
    # The closed southern runway 08R/26L, where two ridges ~5 m high run for
    # 2.3 km: in the 2025 lidar, not in Copernicus. Probably demolition
    # stockpiles. The previous Tegel rectangle straddled them.
    ("Tegel runway berms", "open, ground changed since model",
     [(382, 5824), (384, 5824)],
     _grid_lines(383030.0, 384300.0, 5824250.0, 5824650.0)),
    # MIXED rural ground. It was added as "rolling farmland", but by OSM land use
    # it is about 23% field and meadow, 35% village and allotments and 27% wood
    # and wetland, and the lines that are mostly field do no better than the
    # rest. Lines reaching past the Berlin border have no lidar and are dropped.
    ("Luebars", "mixed rural: village, fields, woods",
     [(386, 5830), (388, 5830)],
     _grid_lines(386050.0, 389950.0, 5830050.0, 5831950.0)),
    ("Mitte", "flat, dense city", [(390, 5818)], _tile_lines(390, 5818)),
    ("Grunewald", "flat-ish, forest", [(378, 5814)], _tile_lines(378, 5814)),
    ("Teufelsberg", "relief, rubble hill", [(380, 5816)], _tile_lines(380, 5816)),
    ("Mueggelberge", "relief, natural hills", [(406, 5808)],
     _tile_lines(406, 5808)),
]


# ── Measurements ─────────────────────────────────────────────────────────────
def _smooth(values, span_m: float):
    k = max(1, int(round(span_m / SAMPLE_M)))
    if k % 2 == 0:
        k += 1
    padded = np.pad(values, k // 2, mode="edge")
    csum = np.cumsum(np.insert(padded, 0, 0.0))
    return (csum[k:] - csum[:-k]) / k


def verdicts(truth, model, window_m: float, threshold_m: float) -> Dict[str, int]:
    """Per-window verdicts, named for their consequence."""
    counts = {"flat ok": 0, "MISSED FIX": 0, "relief ok": 0, "HARM": 0}
    n = len(truth)
    dist = [i * SAMPLE_M / 1000.0 for i in range(n)]
    for lo, hi in _terrain_windows(dist, n, window_m):
        span = _relief_span(lo, hi, n, window_m, dist)
        t, m = truth[span], model[span]
        if len(t) < 3 or np.isnan(t).any() or np.isnan(m).any():
            continue
        truly_relief = _relief(list(t)) >= threshold_m
        read_relief = _relief(list(m)) >= threshold_m
        if truly_relief:
            counts["relief ok" if read_relief else "HARM"] += 1
        else:
            counts["MISSED FIX" if read_relief else "flat ok"] += 1
    return counts


def _splice(base, modelled, lo: int, hi: int):
    """``base`` with one window's steps taken from ``modelled``: what the oracle
    does to that window, left in the context of the whole line."""
    out = np.array(base, dtype=np.float64)
    out[lo:hi] = base[lo] + (modelled[lo:hi] - modelled[lo])
    out[hi:] = base[hi:] + (out[hi - 1] - base[hi - 1])
    return out


def window_harm(truth, modelled, dist, drift_m: float = 0.0):
    """What one substituted window does to a line's gain, in metres.

    A run total nets phantom removed in one window against real climb erased in
    another, so it can hide local harm. For every window the oracle hands to the
    model (the model reads no relief there) this reports:

    * ``isolated`` — the pipeline over that window of truth, minus over that
      window of model: real climb LOST, with the window's edges as fresh starts
      for the pipeline's hysteresis and smoothing;
    * ``lost`` / ``added`` — the same window's model steps spliced into the
      otherwise true line, and the whole line's gain compared: loss AND gain, in
      context. The isolated figure alone cannot show a substitution that invents
      climb, which is how it harms a flat line.

    Ungated, this is independent of the recording class: the oracle's choice of
    window and the steps it splices depend only on the model. With ``drift_m``
    it is the drift gate's choice for a CLEAN recording, whose disagreement with
    the model is exactly the model's own error: it shows which windows the gate
    lets through when the recording did not need correcting.
    """
    n = len(truth)
    truth_gain = elevation_gain(list(truth), dist)
    isolated = lost = added = 0.0
    relief = 0
    for lo, hi in _terrain_windows(dist, n, TERRAIN_WINDOW_M):
        span = _relief_span(lo, hi, n, TERRAIN_WINDOW_M, dist)
        m = modelled[span]
        if len(m) < 3 or _relief(list(m)) >= TERRAIN_RELIEF_M:
            continue                                   # recording kept
        if drift_m > 0:
            disagreement = truth[span] - m
            if disagreement.max() - disagreement.min() <= drift_m:
                continue                               # gated: recording kept
        window_dist = dist[lo:hi]
        isolated = max(isolated,
                       elevation_gain(list(truth[lo:hi]), window_dist)
                       - elevation_gain(list(modelled[lo:hi]), window_dist))
        delta = elevation_gain(list(_splice(truth, modelled, lo, hi)),
                               dist) - truth_gain
        lost = max(lost, -delta)
        added = max(added, delta)
        if _relief(list(truth[span])) >= TERRAIN_RELIEF_M:
            relief += 1
    return isolated, lost, added, relief


def gated_gain(recorded, terrain, distances_km, drift_m: float) -> float:
    """:func:`terrain_corrected_gain` with a DRIFT gate: a window the model reads
    as flat is substituted only when the recording disagrees with the model
    across it by more than ``drift_m``.

    A copy of the oracle's loop with one added condition, not an import, because
    the gate is a candidate and nothing in ``src`` should carry it yet. The copy
    is checked against the real oracle on every run (``drift_m=0`` must match it
    exactly), so it cannot silently drift from what it copies.
    """
    n = len(recorded)
    series = [recorded[0]]
    for lo, hi in _terrain_windows(distances_km, n, TERRAIN_WINDOW_M):
        span = _relief_span(lo, hi, n, TERRAIN_WINDOW_M, distances_km)
        verdict = terrain[span]
        flat = len(verdict) >= 3 and _relief(verdict) < TERRAIN_RELIEF_M
        if flat and drift_m > 0:
            disagreement = [r - t for r, t in zip(recorded[span], verdict)]
            flat = max(disagreement) - min(disagreement) > drift_m
        source = terrain if flat else recorded
        for previous, current in zip(source[lo:hi], source[lo + 1:hi]):
            series.append(series[-1] + (current - previous))
    return elevation_gain(series, distances_km, noise=_noise_estimate(recorded))


def _gate_label(gate) -> str:
    kind, value = gate
    return f"{kind} {value:g}"


def _new_tally():
    return {"truth": 0.0, "recording": 0.0, "oracle": 0.0, "km": 0.0,
            "added": 0.0, "path_added": 0.0, "erased": 0, "line_excess": [],
            "noise": [],
            "gated": {g: {"oracle": 0.0, "added": 0.0, "line_excess": []}
                      for g in GATES}}


def _record(truth, vertical, smooth_m: float, round_m: float):
    recording = truth + vertical
    if smooth_m > 0:
        recording = _smooth(recording, smooth_m)
    if round_m > 0:
        recording = np.round(recording / round_m) * round_m
    return list(recording)


def site_outcome(lidar: Lidar, lines, model) -> Dict:
    """The shipped oracle end to end, for every recording class at one site.

    The drifted path — and so the model read along it — depends only on the line
    and the seed, never on the recording class, so it is read once per run and
    every class is scored against the same terrain.
    """
    h_tau = HORIZONTAL_TAU_S * SPEED_MS / SAMPLE_M
    tallies = {label: _new_tally() for label, *_ in RECORDINGS}
    window_gates = [0.0] + [value for kind, value in GATES if kind == "drift"]
    windows = {g: {"isolated": 0.0, "lost": 0.0, "added": 0.0, "relief": 0}
               for g in window_gates}
    lines_valid = runs = model_gaps = lidar_gaps = 0
    for index, (e, n) in enumerate(lines):
        truth = lidar.at(e, n)
        if np.isnan(truth).any():
            continue                                   # no lidar: past the border
        lines_valid += 1
        count = len(e)
        dist = [i * SAMPLE_M / 1000.0 for i in range(count)]
        km = (count - 1) * SAMPLE_M / 1000.0
        truth_gain = elevation_gain(list(truth), dist)
        line_excess = {label: [] for label in tallies}
        gated_excess = {label: {g: [] for g in GATES} for label in tallies}
        for seed in SEEDS:
            # Distinct streams per line, seed and component: 10 * (1000 i + s)
            # plus a component digit. The first version used base, base+1,
            # base+2 with base = 1000 i + s, so seed 1's vertical noise was seed
            # 0's easting drift.
            base = (1000 * index + seed) * 10
            pe = _smooth(e + np.array(_ar1(count, HORIZONTAL_SIGMA_M, h_tau,
                                           base + 1)), PATH_SMOOTH_M)
            pn = _smooth(n + np.array(_ar1(count, HORIZONTAL_SIGMA_M, h_tau,
                                           base + 2)), PATH_SMOOTH_M)
            lat, lon = from_utm_array(pe, pn)
            modelled = model.at(lat, lon)
            perfect = lidar.at(pe, pn)
            model_gap = bool(np.isnan(modelled).any())
            lidar_gap = bool(np.isnan(perfect).any())
            model_gaps += model_gap
            lidar_gaps += lidar_gap
            if model_gap or lidar_gap:
                continue                               # counted as a shortfall
            runs += 1
            for g in window_gates:
                isolated, lost, added, relief = window_harm(truth, modelled, dist, g)
                w = windows[g]
                w["isolated"] = max(w["isolated"], isolated)
                w["lost"] = max(w["lost"], lost)
                w["added"] = max(w["added"], added)
                w["relief"] += relief
            terrain = list(modelled)
            for label, sigma_v, tau_s, white_m, smooth_m, round_m in RECORDINGS:
                t = tallies[label]
                vertical = np.array(_ar1(count, sigma_v,
                                         tau_s * SPEED_MS / SAMPLE_M, base))
                if white_m > 0:
                    rng = np.random.default_rng(base + 3)
                    vertical = vertical + rng.normal(0.0, white_m, count)
                recording = _record(truth, vertical, smooth_m, round_m)
                recorded_gain = elevation_gain(recording, dist)
                oracle_gain = terrain_corrected_gain(recording, terrain, dist)
                if gated_gain(recording, terrain, dist, 0.0) != oracle_gain:
                    raise SystemExit("gated_gain no longer matches "
                                     "terrain_corrected_gain; update the copy")
                path_gain = terrain_corrected_gain(recording, list(perfect), dist)
                rec_err = abs(recorded_gain - truth_gain)
                excess = abs(oracle_gain - truth_gain) - rec_err
                t["truth"] += truth_gain
                t["recording"] += recorded_gain
                t["oracle"] += oracle_gain
                t["km"] += km
                t["added"] += excess
                t["path_added"] += abs(path_gain - truth_gain) - rec_err
                # Erasure is judged against what the user would have seen
                # otherwise, not the truth alone: a recording whose own band
                # already sits below the truth is not the oracle's doing.
                t["erased"] += oracle_gain < min(truth_gain, recorded_gain) - 10.0
                line_excess[label].append(excess)
                noise = _noise_estimate(recording)[0]
                t["noise"].append(noise)
                for g in GATES:
                    kind, value = g
                    if kind == "noise":
                        gated = oracle_gain if noise > value else recorded_gain
                    else:
                        gated = gated_gain(recording, terrain, dist, value)
                    gated_excess[label][g].append(abs(gated - truth_gain) - rec_err)
                    t["gated"][g]["oracle"] += gated
                    t["gated"][g]["added"] += abs(gated - truth_gain) - rec_err
        for label, t in tallies.items():
            if line_excess[label]:
                t["line_excess"].append(float(np.mean(line_excess[label])))
                for g in GATES:
                    t["gated"][g]["line_excess"].append(
                        float(np.mean(gated_excess[label][g])))
    return {"tallies": tallies, "windows": windows, "lines": len(lines),
            "lines_valid": lines_valid, "runs": runs,
            "model_gaps": model_gaps, "lidar_gaps": lidar_gaps}


def _removed(recording: float, truth: float, oracle: float) -> str:
    excess = recording - truth
    if excess <= 0:
        return "n/a"
    pct = (recording - oracle) / excess * 100
    return f"{pct:5.0f}%" + ("" if 0 <= pct <= 100 else " !")


def _worse(line_excess) -> str:
    return f"{sum(x > WORSE_M for x in line_excess)}/{len(line_excess)}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", choices=sorted(MODELS),
                        default=["terrarium", "copernicus"])
    args = parser.parse_args(argv)
    os.makedirs(CACHE, exist_ok=True)

    sites = []
    for label, kind, tiles, lines, *edit in SITES:
        lidar = Lidar(tiles)
        for change in edit:
            change(lidar)
        sites.append((label, kind, lidar, lines))

    for key in args.models:
        model = MODELS[key]()
        print(f"\n=== {model.name}  (window {TERRAIN_WINDOW_M:.0f} m, "
              f"relief threshold {TERRAIN_RELIEF_M:.0f} m)")
        results = []
        for site_label, kind, lidar, lines in sites:
            o = site_outcome(lidar, lines, model)
            expected = o["lines_valid"] * len(SEEDS)
            if o["runs"] != expected:
                # Refuse a PARTIAL site as firmly as an empty one: one failed tile
                # once took a site from 92 runs to 20, and the row still printed.
                source = " and ".join(
                    part for part, gaps in (
                        ("the model is missing data", o["model_gaps"]),
                        ("the lidar has a gap along a drifted path", o["lidar_gaps"]))
                    if gaps)
                raise SystemExit(
                    f"{model.name} measured {o['runs']} of {expected} runs at "
                    f"{site_label}: {source}. Refusing to print rows that would "
                    f"read as a result.")
            results.append((site_label, kind, o))

        print("  OUTCOME - the shipped oracle end to end.\n"
              "    lines    lines measured / lines laid; a line with any gap in "
              "the lidar is dropped\n"
              "    removed  share of the recording's excess over truth taken away "
              "(! when over 100% or negative)\n"
              "    m/km     how much further from truth the oracle lands than the "
              "recording, per km (negative = closer)\n"
              f"    worse    LINES whose seed-averaged total lands more than "
              f"{WORSE_M:.0f} m further from truth\n"
              "    erased   runs more than 10 m below both truth and recording\n"
              "    path     m/km when the LIDAR is substituted along the same "
              "drifted path: what the path alone costs\n"
              "    gates    the same with a candidate gate: 'noise g' substitutes "
              "only when the recording's measured noise exceeds g m; 'drift d' "
              "substitutes a window only when the recording and model disagree "
              "across it by more than d m")
        for label, *_ in RECORDINGS:
            print(f"  recording: {label}")
            header = (f"  {'site':18s} {'lines':>5s} {'truth':>6s} {'rec':>6s} "
                      f"{'oracle':>6s} {'removed':>8s} {'m/km':>6s} {'worse':>6s} "
                      f"{'erased':>7s} {'path':>6s}")
            for g in GATES:
                header += f" | {_gate_label(g):>9s}: {'removed':>8s} {'m/km':>6s} {'worse':>6s}"
            print(header)
            for site_label, kind, o in results:
                t = o["tallies"][label]
                row = (f"  {site_label:18s} {o['lines_valid']:2d}/{o['lines']:<2d} "
                       f"{t['truth']:6.0f} {t['recording']:6.0f} {t['oracle']:6.0f} "
                       f"{_removed(t['recording'], t['truth'], t['oracle']):>8s} "
                       f"{t['added'] / t['km']:+6.2f} {_worse(t['line_excess']):>6s} "
                       f"{t['erased']:3d}/{o['runs']:<3d} "
                       f"{t['path_added'] / t['km']:+6.2f}")
                for g in GATES:
                    gt = t["gated"][g]
                    row += (f" | {'':9s}  "
                            f"{_removed(t['recording'], t['truth'], gt['oracle']):>8s} "
                            f"{gt['added'] / t['km']:+6.2f} "
                            f"{_worse(gt['line_excess']):>6s}")
                print(row)
            sys.stdout.flush()

        print("  NOISE the noise gate reads, per recording class: min / median / "
              "max of _noise_estimate over every run at each site, in m")
        for label, *_ in RECORDINGS:
            cells = []
            for site_label, kind, o in results:
                v = o["tallies"][label]["noise"]
                cells.append(f"{site_label[:10]:10s} {min(v):.2f}/"
                             f"{float(np.median(v)):.2f}/{max(v):.2f}")
            print(f"  {label:32s} " + "  ".join(cells))

        print("  WINDOWS the oracle substitutes, worst over every run. 'ungated' is "
              "the same for every recording class; 'drift d' is what the drift gate "
              "lets through for a CLEAN recording, whose disagreement with the model "
              "is the model's own error.\n"
              "    isolated  real climb lost, the window measured on its own\n"
              "    lost/added  the window spliced into the true line: climb lost "
              "and climb invented, in context\n"
              f"    relief    substituted windows where the truth HAS relief, "
              f"SUMMED over all runs (lines x {len(SEEDS)} seeds); the "
              "per-window count on the true path is HARM under VERDICTS")
        for site_label, kind, o in results:
            for g, w in o["windows"].items():
                gate = "ungated" if g == 0 else f"drift {g:g}"
                print(f"  {site_label:18s} {gate:8s} isolated {w['isolated']:4.1f} m  "
                      f"lost {w['lost']:4.1f} m  added {w['added']:4.1f} m  relief "
                      f"{w['relief']:4d}")

        print("  VERDICTS per window on the true path (diagnostic). MISSED FIX "
              "leaves the recording alone - no worse than today. HARM swaps the "
              "model in over relief.")
        for label, kind, lidar, lines in sites:
            total: Dict[str, int] = {}
            errors = []
            for e, n in lines:
                truth = lidar.at(e, n)
                lat, lon = from_utm_array(e, n)
                modelled = model.at(lat, lon)
                if np.isnan(truth).any() or np.isnan(modelled).any():
                    continue
                for k, v in verdicts(truth, modelled, TERRAIN_WINDOW_M,
                                     TERRAIN_RELIEF_M).items():
                    total[k] = total.get(k, 0) + v
                errors.append(float(np.std(modelled - truth)))
            sd = float(np.median(errors)) if errors else float("nan")
            print(f"  {label:18s} {kind:36s} error sd {sd:5.2f} m  "
                  + "  ".join(f"{k} {v}" for k, v in total.items()))
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

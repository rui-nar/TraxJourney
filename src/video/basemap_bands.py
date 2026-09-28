"""Basemap of every frame of a trip video, from a few stitched *sheets*
(docs/TRIP_VIDEO_PLAN.md, U5).

The camera's zoom is continuous; map tiles come in integer zoom levels. Each
frame is drawn from the integer *band* at or just below its zoom, stitched at
the tiles' own retina resolution (:data:`PIXEL_RATIO` sheet pixels per
logical pixel), so a band covers the frame with one to two sheet pixels per
frame pixel: scaled down, never blown up, for a quarter of the tiles the band
above would need. In the last :data:`FADE` of a zoom level below an integer
the frame is a cross-fade of the bands on either side, so labels and detail
don't pop as the camera zooms through a level. The camera's mode floors are
integers, so a clip followed at its floor draws from one band.

A *sheet* is one ``render_basemap`` image at one band: the union of the
viewports consecutive frames need from that band. Sheets are planned for the
whole video before frame 1 — which is when the job's tile budget is checked —
and each is stitched once, at its first frame, and dropped after its last.
A band's sheet is closed and a new one started when the next viewport would
make it too large (a long follow at street level) or when the band goes
unused for a while, so memory stays bounded by a few frames' worth of map,
not by the trip's extent.

Over budget, the plan is redone with a lower highest band — coarser tiles,
scaled up onto the frame — until it fits. The camera's framing never changes;
only the basemap's resolution does.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image

from src.poster.tile_stitcher import TileFetcher, render_basemap, tile_range_for_bounds
from src.video.camera import TILE_SIZE, Shot, lonlat_to_world, world_to_lonlat

Size = Tuple[int, int]

# Sheet pixels per logical (512-grid) pixel: Mapbox's @2x tiles, unscaled.
PIXEL_RATIO = 2
# In the last this-much of a zoom level, the frame fades to the next band.
FADE = 0.2
# A band unused for longer than this closes its sheet: re-stitching it later
# costs a few tiles, keeping it would cost memory for the whole gap.
GAP_FRAMES = 15
# A sheet may grow to this many frames' area (≈ 75 MB at 1080p); one
# viewport is at most four.
SHEET_MAX_FRAMES = 12
# Extra pixels around each viewport, for the resampling filter's support.
MARGIN_PX = 2
# The default job tile budget: Mapbox bills per tile.
MAX_TILES = 3000


@dataclass
class Sheet:
    band: int
    x0: int
    y0: int
    x1: int
    y1: int
    first: int      # first frame that uses it
    last: int       # last frame that uses it

    @property
    def bounds(self) -> Dict[str, float]:
        world = _world(self.band)
        west, north = world_to_lonlat(self.x0 / world, self.y0 / world)
        east, south = world_to_lonlat(self.x1 / world, self.y1 / world)
        return {"west": west, "south": south, "east": east, "north": north}

    @property
    def tiles(self) -> int:
        x_min, x_max, y_min, y_max = tile_range_for_bounds(self.bounds, self.band, TILE_SIZE)
        return (x_max - x_min + 1) * (y_max - y_min + 1)


@dataclass
class BandPlan:
    sheets: List[Sheet]
    # Per frame: (sheet index, weight) for each band it is drawn from.
    frames: List[List[Tuple[int, float]]]
    max_band: Optional[int]    # the cap the plan was made with, None if uncapped
    tiles: int


def _world(band: int) -> int:
    """The world's width in sheet pixels at *band*."""
    return TILE_SIZE * PIXEL_RATIO * 2 ** band


def _smoothstep(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3.0 - 2.0 * u)


def band_weights(zoom: float, max_band: Optional[int] = None) -> List[Tuple[int, float]]:
    """The bands a frame at *zoom* is drawn from, with their weights (sum 1).

    ``floor(zoom)`` alone, except in the last :data:`FADE` of the level, a
    smoothstep blend towards the next band. Bands above *max_band* are drawn
    from *max_band* instead.
    """
    k = math.floor(zoom)
    d = zoom - k
    if d > 1.0 - FADE:
        w = _smoothstep((d - (1.0 - FADE)) / FADE)
        layers = [(k, 1.0 - w), (k + 1, w)]
    else:
        layers = [(k, 1.0)]
    out: Dict[int, float] = {}
    for band, w in layers:
        band = max(0, band if max_band is None else min(band, max_band))
        if w > 0:
            out[band] = out.get(band, 0.0) + w
    return sorted(out.items())


def view_rect(shot: Shot, band: int, size: Size) -> Tuple[float, float, float, float]:
    """The frame's viewport in *band*'s sheet pixels, (left, top, right, bottom)."""
    world = _world(band)
    x, y = lonlat_to_world(shot.lon, shot.lat)
    k = PIXEL_RATIO * 2.0 ** (band - shot.zoom)
    hw, hh = size[0] / 2.0 * k, size[1] / 2.0 * k
    return x * world - hw, y * world - hh, x * world + hw, y * world + hh


def _plan(shots: Sequence[Shot], size: Size, max_band: Optional[int]) -> BandPlan:
    max_area = SHEET_MAX_FRAMES * size[0] * size[1]
    sheets: List[Sheet] = []
    open_: Dict[int, int] = {}    # band -> index of its open sheet
    frames: List[List[Tuple[int, float]]] = []
    for n, shot in enumerate(shots):
        refs = []
        for band, weight in band_weights(shot.zoom, max_band):
            world = _world(band)
            l, t, r, b = view_rect(shot, band, size)
            rect = (max(0, math.floor(l) - MARGIN_PX), max(0, math.floor(t) - MARGIN_PX),
                    min(world, math.ceil(r) + MARGIN_PX), min(world, math.ceil(b) + MARGIN_PX))
            i = open_.get(band)
            if i is not None:
                s = sheets[i]
                u = (min(s.x0, rect[0]), min(s.y0, rect[1]), max(s.x1, rect[2]), max(s.y1, rect[3]))
                if n - s.last <= GAP_FRAMES and (u[2] - u[0]) * (u[3] - u[1]) <= max_area:
                    s.x0, s.y0, s.x1, s.y1 = u
                    s.last = n
                else:
                    i = None
            if i is None:
                i = len(sheets)
                sheets.append(Sheet(band, *rect, first=n, last=n))
                open_[band] = i
            refs.append((i, weight))
        frames.append(refs)
    return BandPlan(sheets, frames, max_band, sum(s.tiles for s in sheets))


def plan_bands(shots: Sequence[Shot], size: Size, max_tiles: int = MAX_TILES) -> BandPlan:
    """The sheets for *shots*, within *max_tiles* tiles when any cap allows:
    each step over budget lowers the highest band by one. At band 0 the whole
    world is one tile, so the loop ends; the last plan is used even if it is
    still over, rather than failing the render."""
    plan = _plan(shots, size, None)
    while plan.tiles > max_tiles:
        top = max(s.band for s in plan.sheets)
        if top == 0:
            break
        plan = _plan(shots, size, top - 1)
    return plan


def _crop_scaled(sheet_img: Image.Image, sheet: Sheet, rect: Tuple[float, float, float, float],
                 size: Size) -> Image.Image:
    """*rect* (band sheet pixels) of *sheet*, scaled to *size*. The part of
    *rect* outside the sheet — beyond the world's edge — stays black."""
    l, t, r, b = (rect[0] - sheet.x0, rect[1] - sheet.y0, rect[2] - sheet.x0, rect[3] - sheet.y0)
    w, h = sheet_img.size
    # An area average when shrinking (the usual case: one to two sheet
    # pixels per frame pixel) — as smooth as bilinear there, and faster.
    resample = Image.BOX if r - l >= size[0] else Image.BILINEAR
    if l >= 0 and t >= 0 and r <= w and b <= h:
        return sheet_img.resize(size, resample, box=(l, t, r, b))
    out = Image.new("RGB", size)
    il, it, ir, ib = max(l, 0.0), max(t, 0.0), min(r, float(w)), min(b, float(h))
    sx, sy = size[0] / (r - l), size[1] / (b - t)
    dl, dt = round((il - l) * sx), round((it - t) * sy)
    dw, dh = round((ir - l) * sx) - dl, round((ib - t) * sy) - dt
    if ir > il and ib > it and dw > 0 and dh > 0:
        out.paste(sheet_img.resize((dw, dh), resample, box=(il, it, ir, ib)), (dl, dt))
    return out


class Basemaps:
    """Each frame's basemap, stitching sheets as they are first needed and
    dropping them after their last frame. Frames are meant to be asked for in
    order; an earlier one still works, re-stitching a dropped sheet."""

    def __init__(self, shots: Sequence[Shot], size: Size, plan: BandPlan, *,
                 tile_fetcher: Optional[TileFetcher] = None,
                 render: Callable[..., Image.Image] = render_basemap) -> None:
        self.shots = shots
        self.size = size
        self.plan = plan
        self._fetcher = tile_fetcher
        self._render = render
        self._images: Dict[int, Image.Image] = {}
        self.tiles_fetched = 0

    def _sheet(self, i: int) -> Image.Image:
        img = self._images.get(i)
        if img is None:
            s = self.plan.sheets[i]
            # max_zoom=band pins render_basemap to this band (it would pick
            # band + 1 for a PIXEL_RATIO-sized target), and at that zoom a
            # @2x tile lands on the sheet unscaled.
            img = self._render(s.bounds, s.x1 - s.x0, s.y1 - s.y0,
                               tile_fetcher=self._fetcher, max_zoom=s.band)
            self.tiles_fetched += s.tiles
            self._images[i] = img
        return img

    def frame(self, n: int) -> Image.Image:
        shot = self.shots[n]
        out: Optional[Image.Image] = None
        acc = 0.0
        for i, weight in self.plan.frames[n]:
            s = self.plan.sheets[i]
            img = _crop_scaled(self._sheet(i), s, view_rect(shot, s.band, self.size), self.size)
            acc += weight
            out = img if out is None else Image.blend(out, img, weight / acc)
        for i in [i for i, s in self._images.items() if self.plan.sheets[i].last <= n]:
            del self._images[i]
        return out

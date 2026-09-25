"""Real A0 poster rendering (issue #14).

Wires together the tile stitcher (``tile_stitcher.render_basemap``), the type
scale (``typography.TypeScale``), content-driven card measurement
(``card_layout.layout_card``), route-aware placement
(``card_placement.place_cards``) and per-day metrics
(``day_metrics.compute_day_metrics``) into the actual PNG/PDF poster.

Layers:
  - ``assemble_card_content`` is pure Python (no Pillow) — given the request's
    ``config`` flags, one memory dict, and its computed day metrics, it returns
    an ordered list of content blocks. Fully unit-testable.
  - ``card_layout.layout_card`` measures those blocks into positioned drawing
    ops and an exact card height.
  - ``_compose_poster_image`` turns ops, a stitched basemap and placed
    rectangles into pixels at whatever size/DPI its caller asks for.
  - ``render_poster`` (full resolution, saves PNG+PDF) and
    ``render_poster_preview`` (small/fast, returns PNG bytes) are thin callers
    of ``_compose_poster_image``.

Everything physical — type sizes, card dimensions, padding, pin radius — is
specified in points or millimetres and converted using the render's DPI, so the
preview, the 150 DPI render and a 300 DPI render are the same design at
different resolutions rather than three different-looking outputs.
"""
from __future__ import annotations

import io
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter

from models.db import get_session
from models.project_db import DBProject
from src.billing.trip_days import bounds, normalise
from src.poster.card_layout import (
    CARD_MAX_HEIGHT_MM,
    CardLayout,
    PhotoOp,
    RuleOp,
    TextOp,
    card_width_px,
    layout_card,
    mm_to_px,
)
from src.poster.card_placement import PinSpec, Rect, place_cards
from src.poster.day_metrics import compute_day_metrics
from src.poster.perimeter_placement import place_cards_perimeter
from src.poster.route_index import RouteIndex
from src.poster.theme import PosterTheme, get_theme
from src.poster.tile_stitcher import (
    DEFAULT_MAX_ZOOM,
    DEFAULT_TILE_SIZE,
    TileFetcher,
    crop_rect_for_bounds,
    lonlat_to_pixel,
    render_basemap,
    tile_range_for_bounds,
    zoom_for_target_size,
)
from src.poster.typography import TypeScale, strip_unsupported
from src.project.project_repo import ProjectRepo
from src.project.repo_core import _compute_stats

_log = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]

_repo = ProjectRepo()

# Scratch context for measuring emoji runs at their native size.
_MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))

# ── Resolution ────────────────────────────────────────────────────────────────
# ISO 216 A-series (short edge x long edge, mm). Only the *canvas*/PDF page
# size varies by paper size — every physical drawing metric below (card
# widths, margins, type scale, route width, ...) stays exactly as-is
# regardless of paper size; see _target_size/_pdf_resolution.
_PAPER_SIZES_MM: Dict[str, Tuple[float, float]] = {
    "A0": (841.0, 1189.0),
    "A1": (594.0, 841.0),
    "A2": (420.0, 594.0),
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
}

# 150 DPI (~4967x7022 px) rather than a "true" 300 DPI A0 (~9933x14043,
# ~140M px): still genuinely print-quality at poster viewing distance, keeps a
# comfortable margin under tile_stitcher's tile cap, and halves the in-memory
# uncompressed canvas versus 300 DPI.
_DPI = 150.0

# Longest side (px) of the low-res layout preview.
_PREVIEW_MAX_DIMENSION = 900

# At the preview's much lower effective DPI, _ROUTE_WIDTH_MM/_ROUTE_CASING_MM
# round down to a literal 1px line (see _draw_route), so plain ImageDraw's lack
# of anti-aliasing shows up as a hard staircase on every diagonal segment. The
# route is drawn at this many times the preview's own resolution and box-
# filtered back down (see _draw_route) so it comes out with real edge
# anti-aliasing instead. Cheap specifically because the preview canvas is
# small: at the actual preview ceiling (_PREVIEW_MAX_DIMENSION x its A-series
# counterpart, ~900x637), a 4x scratch layer is ~3600x2548 RGBA, ~37MB — the
# full A0 render's ~4967x7022 canvas is left alone (see _draw_route) because
# the same factor there would be a further ~2.2GB on top of an already ~140MB
# buffer.
_ROUTE_SUPERSAMPLE = 4

# The preview fetches a real basemap, but at a deliberately small tile budget —
# it must stay fast and cheap. If the fetch fails it degrades to a flat colour
# and reports a warning rather than pretending the poster will look like that.
_PREVIEW_MAX_TILES = 24

# A tile count under the budget above doesn't bound *time* — each fetch is
# sequential, so a merely-slow (not failing) connection can still run past
# what a client is willing to wait on (issue #14: "preview failed with a
# timeout"). _PREVIEW_TOTAL_BUDGET_S is a single wall-clock deadline for the
# *whole* preview compose (basemap fetch AND card rendering — see
# _compose_poster_image's docstring for why card rendering needs one too),
# well under the client's own ~20s HTTP timeout so the server always gives up
# first, in a way that degrades to a warning/legend rather than the request
# just being killed with nothing to show for it. _PREVIEW_TILE_TIMEOUT_S is
# the per-request timeout for an individual tile fetch within that budget.
_PREVIEW_TOTAL_BUDGET_S = 10.0
_PREVIEW_TILE_TIMEOUT_S = 5.0

# Real card measurement (font metrics, and especially photo decode via
# _photo_resolver) is per-memory CPU/IO cost that scales with trip size and
# photo count — for a real trip with real photos, this alone ran the preview
# well past the client's own timeout before it ever reached placement or
# drawing, despite those already being budgeted above (issue #14: "preview
# keeps failing on real data"). The preview exists to show *where* cards
# would land, not what is on them, so it skips content measurement entirely
# and gives every card this same placeholder size instead — a plausible
# "average" card, not the bare minimum, so the preview's card count and
# density still read like the real poster's. Real card_layout.layout_card
# measurement still runs for the full render.
_PREVIEW_CARD_HEIGHT_MM = 0.3 * CARD_MAX_HEIGHT_MM

# ── Palette ───────────────────────────────────────────────────────────────────
# Card/legend surfaces, text and divider colours come from the active
# ``PosterTheme`` (src/poster/theme.py); what remains here is the map furniture,
# which reads over satellite imagery rather than over a card and is therefore
# the same in both themes.
_PIN_COLOR = (188, 60, 44)
_PIN_OUTLINE = (255, 255, 255)
_LEADER_COLOR = (72, 78, 88)
_ROUTE_COLOR = (24, 46, 122)
_ROUTE_CASING = (255, 255, 255)

_MISSING_BASEMAP_COLOR = (226, 228, 231)

# ── Physical drawing metrics (mm unless stated) ───────────────────────────────
_PIN_RADIUS_MM = 1.5
_PIN_OUTLINE_MM = 0.45
_LEADER_WIDTH_MM = 0.28
_ROUTE_WIDTH_MM = 0.85
_ROUTE_CASING_MM = 1.5
_CARD_BORDER_MM = 0.22
_CARD_GUTTER_MM = 3.0
_PAGE_MARGIN_MM = 8.0
_LEGEND_GAP_MM = 2.0

_EMPTY_METRICS: Dict[str, Any] = {
    "distance_m": 0.0,
    "elevation_m": 0.0,
    "encounter_count": 0,
    "counters": [],
    "tag_pie": {},
}


class BasemapUnavailable(RuntimeError):
    """The basemap could not be fetched for a preview render."""


def _target_size(orientation: str, dpi: float = _DPI, paper_size: str = "A0") -> Tuple[int, int]:
    """Pixel (width, height) for *orientation* at *dpi*."""
    short_mm, long_mm = _PAPER_SIZES_MM[paper_size]
    long_px = round(long_mm / 25.4 * dpi)
    short_px = round(short_mm / 25.4 * dpi)
    if orientation == "portrait":
        return short_px, long_px
    return long_px, short_px


def _pdf_resolution(width_px: int, orientation: str, paper_size: str = "A0") -> float:
    """DPI to pass to ``Image.save(..., "PDF", resolution=...)`` so the saved
    PDF's physical page size is exactly *paper_size*."""
    short_mm, long_mm = _PAPER_SIZES_MM[paper_size]
    width_in = (long_mm if orientation == "landscape" else short_mm) / 25.4
    return width_px / width_in


def _notify(progress: Optional[ProgressFn], stage: str) -> None:
    if progress is not None:
        progress(stage)


# ── Projection: lon/lat -> the same pixel frame render_basemap produced ──────

class _Projector:
    """Projects (lon, lat) degrees into the exact ``target_w`` x ``target_h``
    pixel frame that ``render_basemap`` crops and resizes its output to.

    Mirrors ``render_basemap``'s own math (same zoom choice, same crop-rect
    rounding) so memory pins and the route land on the basemap pixels a human
    would expect, rather than drifting from a slightly different projection.
    """

    def __init__(
        self,
        bounds: Dict[str, float],
        target_w: int,
        target_h: int,
        tile_size: int = DEFAULT_TILE_SIZE,
        max_zoom: int = DEFAULT_MAX_ZOOM,
    ):
        self.tile_size = tile_size
        self.zoom = zoom_for_target_size(bounds, target_w, target_h, tile_size, max_zoom)
        left, top, right, bottom = crop_rect_for_bounds(bounds, self.zoom, tile_size)
        # crop_rect_for_bounds returns a rect relative to the stitched tile
        # canvas's own origin (the NW corner of tile (x_min, y_min)), not the
        # absolute world-pixel frame lonlat_to_pixel returns points in — fold
        # that origin in here so crop_left/crop_top are absolute world-pixel
        # offsets, matching project()'s px/py.
        x_min, _, y_min, _ = tile_range_for_bounds(bounds, self.zoom, tile_size)
        self.crop_left = x_min * tile_size + round(left)
        self.crop_top = y_min * tile_size + round(top)
        # round() the crop box exactly like render_basemap does before it
        # crops, or the resize scale would be a fraction of a percent off from
        # the basemap's actual crop.
        self.scale_x = target_w / (round(right) - round(left))
        self.scale_y = target_h / (round(bottom) - round(top))

    def project(self, lon: float, lat: float) -> Tuple[float, float]:
        px, py = lonlat_to_pixel(lon, lat, self.zoom, self.tile_size)
        return (px - self.crop_left) * self.scale_x, (py - self.crop_top) * self.scale_y


# ── Pure content assembly (no Pillow) ─────────────────────────────────────────

def assemble_card_content(
    config: Dict[str, bool],
    memory: Dict[str, Any],
    metrics: Dict[str, Any],
    day_number: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Turn enabled ``config`` flags + one memory + its day metrics into an
    ordered list of content blocks for a poster card.

    Pure and Pillow-free so it's unit-testable without any image library.
    ``card_layout.layout_card`` turns the returned blocks into positioned
    drawing ops. Each block is a dict shaped ``{"kind": <str>, ...}``;
    ``kind`` determines the rest of its fields:

      - ``name`` / ``description`` / ``date``: ``{"text": str}``
      - ``day_badge``: ``{"day_number": int}``
      - ``hero_photo``: ``{"uuid": str}`` (first photo only)
      - ``photos``: ``{"uuids": list[str]}`` (all photos)
      - ``distance`` / ``elevation``: ``{"value_m": float}``
      - ``counters``: ``{"items": list[{"name": str, "value": ...}]}``
      - ``tag_pie``: ``{"data": dict[str, float]}``
      - ``encounters``: ``{"count": int}``

    A configurable field is included only when its ``config`` flag is set *and*
    there is actual content for it (e.g. ``hero_photo`` is skipped if the memory
    has no photos; ``counters``/``tag_pie`` are skipped if empty).

    Returns ``[]`` when none of the configurable fields produced anything. That
    is the signal the caller keys off to skip the card entirely: a card with no
    content is a content-free box with a leader line pointing at it, so such a
    memory belongs in the legend instead (see ``_compose_poster_image``).

    ``day_badge`` and ``name`` are deliberately *outside* that gating and are
    prepended, in that order, to any card that has content at all. They identify
    which memory the card belongs to; without them a card showing only distance
    and counters is an anonymous stats box. In particular ``name`` is no longer
    gated by ``memory_text``, which now governs only the date and description.
    """
    blocks: List[Dict[str, Any]] = []

    if config.get("memory_text"):
        # The date rides along with the memory text: a dated card reads as a
        # journal entry, and it is what the overflow legend refers to.
        if memory.get("date"):
            blocks.append({"kind": "date", "text": _format_date(memory["date"])})
        if memory.get("description"):
            blocks.append({"kind": "description", "text": memory["description"]})

    photo_uuids = memory.get("photo_uuids") or []
    if config.get("hero_photo") and photo_uuids:
        blocks.append({"kind": "hero_photo", "uuid": photo_uuids[0]})
    if config.get("all_photos") and photo_uuids:
        blocks.append({"kind": "photos", "uuids": list(photo_uuids)})

    if config.get("distance"):
        blocks.append({"kind": "distance", "value_m": metrics.get("distance_m", 0.0)})
    if config.get("elevation"):
        blocks.append({"kind": "elevation", "value_m": metrics.get("elevation_m", 0.0)})
    if config.get("counters") and metrics.get("counters"):
        blocks.append({"kind": "counters", "items": metrics["counters"]})
    if config.get("tag_pie") and metrics.get("tag_pie"):
        blocks.append({"kind": "tag_pie", "data": metrics["tag_pie"]})
    if config.get("encounters"):
        blocks.append({"kind": "encounters", "count": metrics.get("encounter_count", 0)})

    if not blocks:
        return []

    lead: List[Dict[str, Any]] = []
    if day_number is not None:
        lead.append({"kind": "day_badge", "day_number": day_number})
    if memory.get("name"):
        lead.append({"kind": "name", "text": memory["name"]})
    return lead + blocks


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _format_date(value: str) -> str:
    """'2026-07-14' -> '14 Jul 2026'; anything unparseable passes through."""
    parts = str(value)[:10].split("-")
    if len(parts) != 3:
        return str(value)
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{day} {_MONTHS[month - 1]} {year}"
    except (ValueError, IndexError):
        return str(value)


# ── Trip day numbering ────────────────────────────────────────────────────────
# The app numbers trip days with one canonical formula (``dayTripNumbering`` in
# flutter_client/lib/src/projects/project_notifier.dart):
#
#     dayNumber = (date - trip_start).days + 1
#
# where ``trip_start`` is the project's explicit start-date override when set,
# and otherwise the earliest dated thing in the trip. Rest days count, so the
# span comes from min/max over dates rather than from list order. Date parsing
# and min/max are reused from src/billing/trip_days.py (the same helpers the
# billing day-count uses) rather than reimplemented here.

def _trip_span(project: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(start, end)`` ISO dates for *project*, or ``(None, None)``.

    Collected from every dated collection the in-memory ``Project`` actually
    carries — a partially-loaded project (or a future one that drops a
    collection) simply contributes fewer dates rather than raising.
    """
    values: List[Any] = []
    for activity in getattr(project, "activities", None) or []:
        values.append(getattr(activity, "start_date_local", None))
    for memory in getattr(project, "memories", None) or []:
        values.append(getattr(memory, "date", None))
    for entry in getattr(project, "journal_entries", None) or []:
        values.append(getattr(entry, "date", None))
    for item in getattr(project, "items", None) or []:
        segment = getattr(item, "segment", None)
        if segment is not None:
            values.append(getattr(segment, "date", None))

    earliest, latest = bounds(values)
    start = normalise(getattr(project, "trip_start", None)) or earliest
    end = normalise(getattr(project, "trip_end", None)) or latest
    return start, end


def _day_number(value: Any, trip_start: Optional[str]) -> Optional[int]:
    """Which day of the trip *value* falls on (1-based), or None if unknowable."""
    day = normalise(value)
    start = normalise(trip_start)
    if day is None or start is None:
        return None
    return (date.fromisoformat(day) - date.fromisoformat(start)).days + 1


# ── Drawing ───────────────────────────────────────────────────────────────────

def _trip_memories(requested: List[Dict[str, Any]], project) -> List[Dict[str, Any]]:
    """The requested memories that belong to *project*, each with only the
    photos that memory holds.

    The request names its memories itself: the client sends their text,
    which on an encrypted trip only it can read. Their ids and photo names are
    its word, though, and photos are read from disk by id, so anything not of
    this trip is left out rather than drawn.
    """
    if project is None:
        return []
    held = {m.id: {p for p in (m.photos or []) if p} for m in project.memories
            if m.id is not None}
    kept = []
    for memory in requested:
        photos = held.get(memory.get("id"))
        if photos is None:
            continue
        kept.append({**memory, "photo_uuids": [
            p for p in (memory.get("photo_uuids") or []) if p in photos]})
    return kept


def _photo_resolver(user_id: str, memory_id: Any) -> Callable[[str], Optional[Path]]:
    """Map a photo uuid to its on-disk thumbnail, or None if absent."""
    try:
        from api.memories import _photo_dir
    except Exception:  # pragma: no cover - import guard for non-API contexts
        _log.warning("Could not import api.memories; poster photos disabled", exc_info=True)
        return lambda uuid: None

    from src.utils.photo_paths import photo_file

    photo_dir = _photo_dir(user_id, memory_id)

    def resolve(uuid: str) -> Optional[Path]:
        path = photo_file(photo_dir, uuid, "_thumb")
        return path if path is not None and path.exists() else None

    return resolve


def _decimate_pixels(points: Sequence[Tuple[float, float]], threshold: float = 1.0
                      ) -> List[Tuple[float, float]]:
    """Drop consecutive points that haven't moved a full pixel from the last
    kept one — same "why draw what nobody can tell apart" idea already used
    for the live map's own tiles (``src/tile_renderer.py``'s per-tile
    decimation), applied once in canvas-pixel space instead of per-tile in
    geographic space.

    A real multi-week trip's full-resolution GPS track is hundreds of
    thousands of points (api/geo.py's own docstring: a 120-activity trip is
    ~17.7MB of expanded coordinates) — decoding and projecting all of them,
    then bucketing every resulting segment into ``RouteIndex``, is real,
    unbudgeted work that ran on both the full render and the preview alike.
    At the preview's small canvas especially, the vast majority of those
    points land within the same handful of pixels as their neighbours and
    are pure waste to keep: projecting a synthetic 120-activity/504k-point
    trip took ~3s just for route projection + RouteIndex construction at
    preview scale; decimated first, the same route was ~1,300 points and the
    same two steps took a few hundredths of a second — a threshold small
    enough that the drawn line looks identical.
    """
    if len(points) < 3:
        return list(points)
    out = [points[0]]
    last_x, last_y = points[0]
    for x, y in points[1:-1]:
        if abs(x - last_x) >= threshold or abs(y - last_y) >= threshold:
            out.append((x, y))
            last_x, last_y = x, y
    out.append(points[-1])
    return out


def _project_route(project: Any, projector: _Projector) -> List[List[Tuple[float, float]]]:
    """Project every track feature into canvas pixel coordinates.

    Reuses ``api.geo``'s own server-side GeoJSON feature builder (the same data
    ``GET /api/geo/project`` serves the client) rather than reimplementing
    polyline decoding, and projects through the same ``_Projector`` used for
    pins so route and pins stay aligned. Each projected line is then decimated
    to the canvas's own pixel resolution (see ``_decimate_pixels``) — this is
    resolution-aware, so a full A0 render keeps far more detail than a small
    preview, exactly as much as each one can actually show.
    """
    try:
        from api.geo import _build_full_geo_features
    except Exception:
        _log.warning("Could not import api.geo; skipping route polyline", exc_info=True)
        return []

    lines: List[List[Tuple[float, float]]] = []
    for feature in _build_full_geo_features(project, encoded=False):
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        projected = [projector.project(lon, lat) for lon, lat in coords]
        lines.append(_decimate_pixels(projected))
    return lines


def _draw_route(canvas: Image.Image, lines: Sequence[Sequence[Tuple[float, float]]],
                dpi: float, *, supersample: int = 1) -> None:
    """Draw the track as a white-cased coloured line, so it stays legible over
    both bright and dark satellite imagery.

    Plain ``ImageDraw`` is never anti-aliased, so a straight ``draw.line()``
    produces a hard, stair-stepped edge on any diagonal segment. At the full
    render's DPI the line is wide enough (~5px, ~9px cased at 150 DPI) that
    those steps are a minor fraction of the stroke's own width — consistent
    with this module's "still genuinely print-quality at poster viewing
    distance" tradeoff for ``_DPI`` — so ``supersample`` is left at 1 there.
    The preview is the case that actually needs it: its much lower effective
    DPI can floor the same physical width to a literal 1px line, where every
    stair-step *is* the whole line (see ``_ROUTE_SUPERSAMPLE``'s comment for
    the numbers).

    With ``supersample`` > 1, the casing and route are instead drawn onto a
    scratch layer that many times larger, then box-filtered back down to the
    canvas's own size — an exact area-average per output pixel, so a diagonal
    edge comes out with real intermediate alpha instead of a hard on/off
    transition.
    """
    casing = mm_to_px(_ROUTE_CASING_MM, dpi)
    width = mm_to_px(_ROUTE_WIDTH_MM, dpi)

    if supersample <= 1:
        draw = ImageDraw.Draw(canvas)
        for line in lines:
            draw.line(list(line), fill=_ROUTE_CASING, width=casing, joint="curve")
        for line in lines:
            draw.line(list(line), fill=_ROUTE_COLOR, width=width, joint="curve")
        return

    ss = supersample
    layer = Image.new("RGBA", (canvas.width * ss, canvas.height * ss), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    for line in lines:
        scaled = [(x * ss, y * ss) for x, y in line]
        layer_draw.line(scaled, fill=_ROUTE_CASING, width=casing * ss, joint="curve")
    for line in lines:
        scaled = [(x * ss, y * ss) for x, y in line]
        layer_draw.line(scaled, fill=_ROUTE_COLOR, width=width * ss, joint="curve")
    layer = layer.resize((canvas.width, canvas.height), Image.BOX)
    canvas.alpha_composite(layer)


def _draw_pin(draw: ImageDraw.ImageDraw, x: float, y: float, dpi: float) -> None:
    r = max(3, mm_to_px(_PIN_RADIUS_MM, dpi))
    draw.ellipse([x - r, y - r, x + r, y + r], fill=_PIN_COLOR,
                 outline=_PIN_OUTLINE, width=max(1, mm_to_px(_PIN_OUTLINE_MM, dpi)))


def _draw_leader(draw: ImageDraw.ImageDraw, x: float, y: float,
                 anchor: Tuple[float, float], dpi: float) -> None:
    draw.line([(x, y), anchor], fill=_LEADER_COLOR,
              width=max(1, mm_to_px(_LEADER_WIDTH_MM, dpi)))


def _paste_cover(canvas: Image.Image, path: Path, box: Tuple[int, int, int, int]) -> None:
    """Paste an image into *box*, centre-cropped to fill it.

    Cover rather than fit: letterboxed thumbnails of assorted aspect ratios
    make a card look broken, whereas a uniform crop keeps the grid regular.
    """
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return
    try:
        with Image.open(path) as src:
            photo = src.convert("RGB")
    except Exception:
        _log.warning(
            "Could not open cover photo %s; leaving card slot blank", path, exc_info=True,
        )
        return

    src_w, src_h = photo.size
    if src_w == 0 or src_h == 0:
        return
    scale = max(w / src_w, h / src_h)
    new_size = (max(1, round(src_w * scale)), max(1, round(src_h * scale)))
    photo = photo.resize(new_size, Image.LANCZOS)
    left = (photo.width - w) // 2
    top = (photo.height - h) // 2
    canvas.paste(photo.crop((left, top, left + w, top + h)), (x, y))


def _draw_card_chrome(
    canvas: Image.Image, rect: Rect, dpi: float, theme: PosterTheme
) -> None:
    """The app's floating-card treatment: a rounded translucent surface with a
    hairline border and one soft shadow dropped downwards.

    Surface, border and shadow colours all come from *theme*; the geometry
    (radius, blur, drop) is shared by both themes — see ``theme.py``.

    Both the shadow and the surface are composited through a layer only as
    large as the card plus its blur radius. Allocating a full-canvas RGBA layer
    per card instead would cost ~139MB *per card* at A0/150 DPI (and be blurred
    at that size), which is ruinous for both memory and time on a real poster.
    The surface is composited rather than drawn straight onto the canvas
    because it is deliberately translucent (94%): ``ImageDraw`` would replace
    those pixels outright, so the basemap would not show through at all.
    """
    radius = mm_to_px(theme.radius_mm, dpi)
    offset = mm_to_px(theme.shadow_offset_mm, dpi)
    blur = mm_to_px(theme.shadow_blur_mm, dpi)
    pad = blur * 3 + offset  # enough room for the blur to fall off to nothing

    card_w = rect.right - rect.left
    card_h = rect.bottom - rect.top
    tile_x = int(rect.left) - pad
    tile_y = int(rect.top) - pad
    tile_w = int(card_w) + 2 * pad
    tile_h = int(card_h) + 2 * pad

    tile = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        [pad + offset, pad + offset, pad + offset + card_w, pad + offset + card_h],
        radius=radius, fill=theme.shadow_fill,
    )
    tile = tile.filter(ImageFilter.GaussianBlur(blur))

    surface = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
    ImageDraw.Draw(surface).rounded_rectangle(
        [pad, pad, pad + card_w, pad + card_h],
        radius=radius, fill=theme.card_fill, outline=theme.border_fill,
        width=max(1, mm_to_px(_CARD_BORDER_MM, dpi)),
    )
    tile = Image.alpha_composite(tile, surface)

    # alpha_composite needs the destination box fully inside the canvas; cards
    # sit within the page margin but the padded shadow tile can hang off it.
    canvas.alpha_composite(
        tile.crop((
            max(0, -tile_x), max(0, -tile_y),
            tile_w - max(0, tile_x + tile_w - canvas.width),
            tile_h - max(0, tile_y + tile_h - canvas.height),
        )),
        (max(0, tile_x), max(0, tile_y)),
    )


def _draw_scaled_run(
    canvas: Image.Image, op: TextOp, scale: TypeScale, ox: int, oy: int
) -> None:
    """Draw one emoji run, rendered at its face's native size then scaled down.

    Colour emoji fonts are CBDT bitmap fonts carrying a single fixed strike —
    Noto Color Emoji only opens at 109px — so they cannot simply be drawn at
    8pt body size. The run is rasterised at that native size onto a
    transparent tile with ``embedded_color`` (which is what actually produces
    colour rather than a monochrome mask), resized to the text size, and
    composited onto the card.

    The tile is aligned to the text's ascent so emoji sit on the same visual
    line as the type around them rather than floating above or below it.
    """
    face = op.face
    try:
        width = max(1, round(_MEASURE.textlength(op.text, font=face)))
        ascent, descent = face.getmetrics()
        height = max(1, ascent + descent)

        tile = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((0, 0), op.text, font=face, embedded_color=True)

        target_w = max(1, round(width * op.scale))
        target_h = max(1, round(height * op.scale))
        tile = tile.resize((target_w, target_h), Image.LANCZOS)

        # Sit the emoji's optical block on the text baseline: the surrounding
        # text is drawn from its ascender at op.y, so nudge down by the
        # difference between the text ascent and the scaled tile's own.
        text_ascent = scale.font(op.style).getmetrics()[0]
        dy = max(0, text_ascent - round(ascent * op.scale))
        canvas.alpha_composite(tile, (ox + op.x, oy + op.y + dy))
    except Exception:
        _log.warning("Could not draw emoji run %r", op.text, exc_info=True)


def _draw_card(canvas: Image.Image, rect: Rect, layout, scale: TypeScale,
               theme: PosterTheme) -> None:
    """Draw one memory card — themed chrome, then its measured layout ops."""
    _draw_card_chrome(canvas, rect, scale.dpi, theme)
    _draw_card_ops(canvas, rect, layout, scale, theme)


def _draw_card_ops(canvas: Image.Image, rect: Rect, layout, scale: TypeScale,
                   theme: PosterTheme) -> None:
    """Execute a measured layout's ops at *rect*'s position.

    Text colours are resolved through *theme* by each style's semantic role
    (see ``typography.TextStyle.role``), and the stat panels' separating rules
    use the theme's divider colour.
    """
    draw = ImageDraw.Draw(canvas)
    ox, oy = int(rect.left), int(rect.top)

    for op in layout.ops:
        if isinstance(op, TextOp):
            style = scale.style(op.style)
            if op.face is not None:
                _draw_scaled_run(canvas, op, scale, ox, oy)
            else:
                draw.text((ox + op.x, oy + op.y), op.text,
                          font=scale.font(op.style), fill=theme.text_color(style))
        elif isinstance(op, PhotoOp):
            _paste_cover(canvas, op.path, (ox + op.x, oy + op.y, op.w, op.h))
        elif isinstance(op, RuleOp):
            draw.line([(ox + op.x, oy + op.y), (ox + op.x + op.w, oy + op.y)],
                      fill=theme.divider, width=1)


def _draw_legend(
    canvas: Image.Image,
    entries: List[Dict[str, Any]],
    pin_xy: Dict[Any, Tuple[float, float]],
    scale: TypeScale,
    target_w: int,
    target_h: int,
    theme: PosterTheme,
) -> None:
    """Number the overflowed pins on the map and list them bottom-left.

    ``entries`` is the memory dicts, in placement order, whose card could not
    be placed (see ``card_placement.CardPlacement``). The legend box is drawn
    with the same themed chrome as a card, so it reads as part of the same
    family rather than as a separate white panel.
    """
    draw = ImageDraw.Draw(canvas)
    dpi = scale.dpi
    margin = mm_to_px(_PAGE_MARGIN_MM, dpi)
    pin_r = mm_to_px(_PIN_RADIUS_MM, dpi)
    line_h = scale.line_height("legend")
    gap = mm_to_px(_LEGEND_GAP_MM, dpi)
    font = scale.font("legend")
    index_font = scale.font("legend_index")
    width = card_width_px(dpi)

    box_h = len(entries) * line_h + 2 * gap
    top = max(margin, target_h - margin - box_h)
    _draw_card_chrome(
        canvas, Rect(margin, top, margin + width, top + box_h), dpi, theme)

    index_color = theme.text_color(scale.style("legend_index"))
    text_color = theme.text_color(scale.style("legend"))
    y = top + gap
    for i, memory in enumerate(entries, start=1):
        px, py = pin_xy[memory["id"]]
        draw.text((px + pin_r + gap / 2, py - pin_r), str(i),
                  font=index_font, fill=index_color)
        label = memory.get("name") or _format_date(memory.get("date", ""))
        text = strip_unsupported(f"{i}. {label}", font)
        draw.text((margin + gap, y), text, font=font, fill=text_color)
        y += line_h


# ── Trip summary card ─────────────────────────────────────────────────────────
# One card for the whole poster (not one per memory): what the trip *was* —
# its title, the period it covered, and its totals.

def _trip_summary_blocks(
    project: Any, span: Tuple[Optional[str], Optional[str]],
    title_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Content blocks for the trip summary card.

    Deliberately the same block vocabulary a memory card uses, so the summary
    is measured, styled and unit-formatted by exactly the same code: the title
    is a ``name``, the period a ``date``, and the totals ``distance``/
    ``elevation`` (which ``card_layout`` renders as its usual km/m stat row).

    Totals come from ``repo_core._compute_stats``, which already sums across
    *all* of the project's activities — unlike ``compute_day_metrics``, which
    is scoped to a single date.

    ``title_text`` overrides the card's title (a poster may cover only part of
    a larger trip, so the trip's own name is not always what belongs on it);
    ``None`` or empty falls back to the project's name, exactly as before this
    override existed.
    """
    start, end = span
    stats = _compute_stats(project)

    blocks: List[Dict[str, Any]] = [
        {"kind": "name", "text": title_text or getattr(project, "name", "") or "Trip"},
    ]
    if start and end:
        blocks.append({
            "kind": "date",
            "text": f"{_format_date(start)} – {_format_date(end)}",
        })
    blocks.append({"kind": "distance", "value_m": stats.get("total_distance_m", 0.0)})
    blocks.append({"kind": "elevation", "value_m": stats.get("total_elevation_m", 0.0)})
    return blocks


# Safe range for the title-scale slider's multiplier on the "hero_title"
# style. Clamped here regardless of what a caller sends — the API layer
# (api/poster.py's PosterRequest validator) already clamps on the way in, but
# this is the renderer's own trust boundary against any other caller (a
# direct render call, a stale/adversarial request_json row).
_TITLE_SCALE_MIN = 0.5
_TITLE_SCALE_MAX = 2.0


def _clamp_title_scale(value: float) -> float:
    return max(_TITLE_SCALE_MIN, min(_TITLE_SCALE_MAX, value))


def _trip_summary_layout(
    project: Any,
    span: Tuple[Optional[str], Optional[str]],
    scale: TypeScale,
    title_text: Optional[str],
) -> Any:
    """Measure the trip summary card's content at *scale*.

    *scale* already carries any "hero_title" override the title-scale slider
    asked for (see ``_compose_poster_image``) — this function just measures,
    it does not decide the scale.
    """
    return layout_card(
        _trip_summary_blocks(project, span, title_text),
        scale, width=card_width_px(scale.dpi),
    )


def _trip_summary_rect(
    layout: Any, target_w: int, target_h: int, dpi: float,
    position: Optional[Dict[str, float]],
) -> Rect:
    """Resolve the trip summary card's rect from a normalized (0-1) position.

    ``position`` is ``{"x": .., "y": ..}``, the fraction of the way across the
    page-margin-inset usable area the card's top-left corner sits at.
    ``(0, 0)`` (and ``None``, for a request that predates this feature) is
    today's fixed top-left corner — byte-for-byte the same rect as before this
    feature existed. Values are clamped to ``[0, 1]`` so a dragged-off-page or
    adversarial position can't push the card outside the margin.
    """
    margin = mm_to_px(_PAGE_MARGIN_MM, dpi)
    px = max(0.0, min(1.0, float((position or {}).get("x", 0.0))))
    py = max(0.0, min(1.0, float((position or {}).get("y", 0.0))))
    max_left = max(margin, target_w - margin - layout.width)
    max_top = max(margin, target_h - margin - layout.height)
    left = margin + px * (max_left - margin)
    top = margin + py * (max_top - margin)
    return Rect(left, top, left + layout.width, top + layout.height)


def _draw_trip_summary(
    canvas: Image.Image,
    rect: Rect,
    layout: Any,
    scale: TypeScale,
    theme: PosterTheme,
) -> None:
    """Draw the trip summary card at its already-resolved *rect*.

    Position, text and scale are all resolved earlier by
    ``_trip_summary_rect``/``_trip_summary_layout`` — before card placement
    runs, so memory cards can be routed around the title (see
    ``_compose_poster_image``) — this just executes the chrome + ops pass
    every other card uses. *scale* must be the same ``TypeScale`` instance
    ``layout`` was measured with, or drawing will use different font sizes
    than the positions were measured for.
    """
    _draw_card_chrome(canvas, rect, scale.dpi, theme)
    _draw_card_ops(canvas, rect, layout, scale, theme)


# ── Composition (shared by the full render and the low-res preview) ─────────

def _compose_poster_image(
    project_id: int,
    user_info_id: int,
    request: Dict[str, Any],
    target_w: int,
    target_h: int,
    *,
    dpi: float,
    tile_fetcher: Optional[TileFetcher] = None,
    basemap_optional: bool = False,
    max_tiles: Optional[int] = None,
    progress: Optional[ProgressFn] = None,
) -> Tuple[Image.Image, Optional[str]]:
    """Build the composited poster: basemap, route, pins, content-sized
    non-overlapping cards, a corner trip summary card, and an overflow legend.

    ``dpi`` drives every physical dimension (type, card size, line widths), so
    the preview and the full render are the same design at two resolutions.

    Returns ``(image, warning)``. A basemap failure normally propagates — a
    poster without its map is not a useful output, so the job must fail with
    the underlying error rather than silently rendering on grey (issue #14
    feedback). With ``basemap_optional`` (the preview), the failure is caught
    and returned as *warning* so the client can say so plainly.

    ``basemap_optional`` also puts a single wall-clock *deadline* on the
    whole call, not just the basemap fetch: card rendering (drop-shadow
    blur, photo decode — see ``_draw_card_chrome``/``_paste_cover``) is real
    CPU/IO work too, and a preview canvas at low DPI can fit far more tiny
    cards than a first look at "the tile budget is small" suggests. Past the
    deadline, remaining cards are pushed to the legend instead of drawn
    (issue #14: a preview that quietly ran past the client's own timeout,
    even after the basemap alone was capped, left the user with no way to
    tell running from hung from failed).
    """
    bounds = request["bounds"]
    config = request.get("config", {})
    memories: List[Dict[str, Any]] = request.get("memories", [])
    scale = TypeScale(dpi)
    theme = get_theme(config.get("theme"))
    warning: Optional[str] = None
    deadline = time.monotonic() + _PREVIEW_TOTAL_BUDGET_S if basemap_optional else None

    _notify(progress, "fetching basemap")
    kwargs = {"tile_fetcher": tile_fetcher}
    if max_tiles is not None:
        kwargs["max_tiles"] = max_tiles
    if basemap_optional:
        kwargs["deadline"] = deadline
        kwargs["tile_timeout"] = _PREVIEW_TILE_TIMEOUT_S
    try:
        canvas = render_basemap(bounds, target_w, target_h, **kwargs)
    except Exception as exc:
        if not basemap_optional:
            raise
        _log.warning("Preview basemap unavailable: %s", exc)
        warning = f"Map imagery unavailable: {exc}"
        canvas = Image.new("RGB", (target_w, target_h), _MISSING_BASEMAP_COLOR)

    # RGBA throughout so card shadows can be alpha-composited; flattened on the
    # way out.
    canvas = canvas.convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    projector = _Projector(bounds, target_w, target_h)

    _notify(progress, "loading project")
    with get_session() as sess:
        project = _repo.get_project_by_id(sess, project_id)
        owner_row = sess.get(DBProject, project_id)
    # The trip's photos live in its owner's folder, whoever asks for the poster.
    user_id = str(owner_row.user_info_id) if owner_row is not None else str(user_info_id)
    memories = _trip_memories(memories, project)

    route_lines: List[List[Tuple[float, float]]] = []
    if project is not None:
        _notify(progress, "plotting route")
        route_lines = _project_route(project, projector)
        _draw_route(canvas, route_lines, dpi,
                    supersample=_ROUTE_SUPERSAMPLE if basemap_optional else 1)
    route = RouteIndex(route_lines, (target_w, target_h))

    _notify(progress, "measuring cards")
    width = card_width_px(dpi)
    # One span for the whole trip, computed once rather than per memory — it
    # feeds both every card's "Day N" badge and the trip summary card.
    trip_span = _trip_span(project) if project is not None else (None, None)
    pin_xy: Dict[Any, Tuple[float, float]] = {}
    layouts: Dict[Any, Any] = {}
    pins: List[PinSpec] = []
    content_less: List[Dict[str, Any]] = []
    placeholder_height = mm_to_px(_PREVIEW_CARD_HEIGHT_MM, dpi)
    for memory in memories:
        x, y = projector.project(memory["lon"], memory["lat"])
        pin_xy[memory["id"]] = (x, y)
        metrics = compute_day_metrics(project, memory["date"]) if project is not None else _EMPTY_METRICS
        blocks = assemble_card_content(
            config, memory, metrics,
            day_number=_day_number(memory.get("date"), trip_span[0]),
        )
        if not blocks:
            # Nothing to show. Placing this card anyway drew an empty rounded
            # box with a leader line pointing at it — so it never enters card
            # placement at all and goes straight to the legend, exactly like a
            # memory whose card could not be fitted. Its pin is still drawn.
            content_less.append(memory)
            continue
        if basemap_optional:
            # See _PREVIEW_CARD_HEIGHT_MM: the preview never measures real
            # content, so it never resolves or decodes a photo either.
            layout = CardLayout(width=width, height=placeholder_height)
        else:
            layout = layout_card(
                blocks, scale, width=width,
                photo_path=_photo_resolver(user_id, memory["id"]),
            )
        layouts[memory["id"]] = layout
        pins.append(PinSpec(id=memory["id"], x=x, y=y,
                            sort_key=memory.get("date", ""),
                            size=(layout.width, layout.height)))

    # The title/trip-summary card's placement must be resolved before card
    # placement runs, so memory cards can be routed around it exactly like
    # they already are around the route and each other (see place_cards'/
    # place_cards_perimeter's `obstacles`). It is drawn later, once placement
    # is done, using this same layout/rect/scale so measurement and drawing
    # never disagree.
    show_trip_summary = project is not None and config.get("trip_summary", True)
    title_layout: Optional[Any] = None
    title_rect: Optional[Rect] = None
    title_scale = scale
    if show_trip_summary:
        title_factor = _clamp_title_scale(request.get("title_scale", 1.0))
        if title_factor != 1.0:
            title_scale = TypeScale(dpi, overrides={"hero_title": title_factor})
        title_layout = _trip_summary_layout(
            project, trip_span, title_scale, request.get("title_text"))
        title_rect = _trip_summary_rect(
            title_layout, target_w, target_h, dpi, request.get("title_position"))

    _notify(progress, "placing cards")
    # "perimeter" is an opt-in prototype (src/poster/perimeter_placement.py);
    # anything else keeps the radial search that has always been the default.
    place = (place_cards_perimeter if config.get("layout") == "perimeter"
             else place_cards)
    placements = place(
        pins, (target_w, target_h), route=route,
        margin=mm_to_px(_PAGE_MARGIN_MM, dpi),
        gutter=mm_to_px(_CARD_GUTTER_MM, dpi),
        obstacles=[title_rect] if title_rect is not None else (),
    )

    _notify(progress, "rendering cards")
    memories_by_id = {m["id"]: m for m in memories}
    legend_entries: List[Dict[str, Any]] = list(content_less)
    budget_skipped = 0

    for placement in placements:
        memory = memories_by_id.get(placement.pin_id)
        if memory is None:
            continue
        # Drop-shadow blur + photo decode (_draw_card_chrome/_paste_cover) are
        # real per-card CPU/IO cost — a low-DPI preview canvas fits far more
        # tiny cards than that sounds like it should allow, and dozens of them
        # is exactly what ran the preview past the client's own timeout even
        # with the basemap alone capped (issue #14). Once the deadline is
        # gone, remaining cards degrade to legend rows instead.
        over_budget = deadline is not None and time.monotonic() > deadline
        if placement.placed and placement.card_rect is not None and not over_budget:
            _draw_leader(draw, *pin_xy[placement.pin_id], placement.anchor, dpi)
            _draw_card(canvas, placement.card_rect, layouts[placement.pin_id],
                       scale, theme)
        else:
            legend_entries.append(memory)
            if placement.placed and over_budget:
                budget_skipped += 1

    # Pins last, so a leader line never runs over the pin it points from.
    for pin_id, (x, y) in pin_xy.items():
        _draw_pin(ImageDraw.Draw(canvas), x, y, dpi)

    if show_trip_summary:
        _draw_trip_summary(canvas, title_rect, title_layout, title_scale, theme)

    if legend_entries:
        _draw_legend(canvas, legend_entries, pin_xy, scale, target_w, target_h, theme)

    if budget_skipped:
        budget_note = (
            f"Preview simplified: {budget_skipped} card(s) shown in the "
            "legend instead of on the map (time budget)."
        )
        warning = f"{warning}; {budget_note}" if warning else budget_note

    return canvas.convert("RGB"), warning


# ── Entry points ───────────────────────────────────────────────────────────────

def render_poster(
    job_id: int,
    user_info_id: int,
    project_id: int,
    request: Dict[str, Any],
    poster_dir: Path,
    progress: ProgressFn,
    *,
    tile_fetcher: Optional[TileFetcher] = None,
) -> Tuple[Path, Path]:
    """Render the poster for one job and save it as PNG + PDF.

    Takes plain scalar job fields (``job_id``/``user_info_id``/``project_id``)
    rather than a live ``DBPosterJob`` row: ``poster_job_runner`` only holds
    that row inside a short ``get_session()`` block, and this render can take a
    while (tile fetching, compositing) — it must not hold a DB session open for
    that whole duration, so it opens its own short-lived session only where it
    needs one.

    ``progress`` is called with a short stage label at each major step, for
    ``poster_job_runner`` to persist onto ``job.stage``. ``tile_fetcher`` is
    forwarded to ``render_basemap``; production leaves it ``None`` (real Mapbox
    tiles), tests inject a fake to avoid network calls.
    """
    orientation = request.get("orientation", "landscape")
    paper_size = request.get("paper_size", "A0")
    target_w, target_h = _target_size(orientation, paper_size=paper_size)

    canvas, _ = _compose_poster_image(
        project_id, user_info_id, request, target_w, target_h,
        dpi=_DPI, tile_fetcher=tile_fetcher, basemap_optional=False, progress=progress,
    )

    _notify(progress, "encoding pdf")
    png_path = poster_dir / "poster.png"
    pdf_path = poster_dir / "poster.pdf"
    canvas.save(str(png_path), "PNG")
    canvas.save(str(pdf_path), "PDF",
                resolution=_pdf_resolution(target_w, orientation, paper_size))

    return png_path, pdf_path


def render_poster_preview(
    project_id: int,
    user_info_id: int,
    request: Dict[str, Any],
    *,
    max_dimension: int = _PREVIEW_MAX_DIMENSION,
    tile_fetcher: Optional[TileFetcher] = None,
) -> Tuple[bytes, Optional[str]]:
    """Fast, low-resolution preview of the real poster.

    Same basemap, type scale, card measurement and placement as the full
    render, just at a small size — so what the preview shows is what the poster
    will look like, only smaller. Previously this always skipped the basemap
    and painted flat grey, which is why the map "looked grey" no matter what
    the token or network were doing.

    The tile budget is deliberately tiny (``_PREVIEW_MAX_TILES``), so the
    preview stays sub-second and cheap; at preview size the chosen zoom needs
    only a handful of tiles anyway. If the basemap can't be fetched, the
    preview still renders (on flat grey) and returns a warning describing why,
    rather than failing outright or quietly implying the poster will be grey.

    The whole call — basemap fetch AND card rendering — also carries a single
    wall-clock deadline (``_PREVIEW_TOTAL_BUDGET_S``, see
    ``_compose_poster_image``): a low-DPI preview canvas can fit far more tiny
    cards than "the tile budget is small" suggests, and each one's drop-shadow
    blur/photo decode is real work, so a tile-count cap alone didn't stop a
    real preview from running past the client's own timeout (issue #14).
    Past the deadline, cards degrade to legend rows instead of being drawn.

    Returns ``(png_bytes, warning_or_None)``.
    """
    orientation = request.get("orientation", "landscape")
    paper_size = request.get("paper_size", "A0")
    real_w, real_h = _target_size(orientation, paper_size=paper_size)
    ratio = max_dimension / max(real_w, real_h)
    preview_w = max(1, round(real_w * ratio))
    preview_h = max(1, round(real_h * ratio))

    canvas, warning = _compose_poster_image(
        project_id, user_info_id, request, preview_w, preview_h,
        dpi=_DPI * ratio, tile_fetcher=tile_fetcher, basemap_optional=True,
        max_tiles=_PREVIEW_MAX_TILES, progress=None,
    )
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue(), warning

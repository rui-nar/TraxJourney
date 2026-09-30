"""Everything a trip video draws over the basemap (docs/TRIP_VIDEO_PLAN.md, U5):
the faint whole route, the travelled route in each mode's colour, the marker
(mode icon in a halo), the speed badge, per-mode distance counters, the date
ticker, and the title and end cards.

Sizes scale with the frame height, so 720p and 1080p look the same. Colours
are the client's (flutter_client design_tokens.dart), so a ride is the same
blue in the app, on the poster and in the video.
"""
from __future__ import annotations

import bisect
import colorsys
import functools
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.poster.typography import FontStack, load_emoji_face, load_face, split_runs
from src.video.camera import (
    TILE_SIZE,
    Shot,
    frame_count,
    lonlat_to_world,
    overview,
    world_to_lonlat,
)
from src.video.timeline import FrameState, Timeline

Size = Tuple[int, int]
RGB = Tuple[int, int, int]

ICON_DIR = Path(__file__).resolve().parents[2] / "assets" / "video" / "icons"

# design_tokens.dart: kColorRide/Run/Hike/Other, kColorFlight/Train/Bus/Boat,
# kColorAlt as the fallback.
MODE_COLORS: Dict[str, RGB] = {
    "ride": (0x4F, 0xC3, 0xF7), "run": (0xEF, 0x53, 0x50),
    "hike": (0x66, 0xBB, 0x6A), "other": (0xAB, 0x47, 0xBC),
    "flight": (0x42, 0xA5, 0xF5), "train": (0x8D, 0x6E, 0x63),
    "bus": (0xFF, 0xB3, 0x00), "boat": (0x26, 0xC6, 0xDA),
}
FALLBACK_COLOR: RGB = (0xFF, 0xA7, 0x26)

MODE_LABELS: Dict[str, str] = {
    "ride": "Ride", "run": "Run", "hike": "Walk & hike", "other": "Other",
    "flight": "Flight", "train": "Train", "bus": "Bus", "boat": "Boat",
}

_FAINT: RGB = (255, 255, 255)
_FAINT_ALPHA = 96
_CASING: RGB = (20, 24, 32)
_PANEL = (12, 14, 20, 150)
_TEXT: RGB = (255, 255, 255)
_MUTED: RGB = (200, 205, 214)
_SCRIM_ALPHA = 150

# The route is drawn this many times larger over its bounding box and
# box-filtered down, so its edges get real coverage instead of a staircase
# (D5, as poster_renderer._draw_route). Set at gate G1 (docs/VIDEO.md); 2-4
# is the allowed range, the memory is ROUTE_SS² × the route box (≈70 MB at
# 3× for an overview at 1080p).
ROUTE_SS = 3
# Only the cells of this grid (frame px) that the route crosses are filtered
# down and pasted: zoomed in, the route box is most of the frame but the route
# a thin line through it. Must exceed the route's half-width.
_ROUTE_CELL = 32
# The marker sprite and the HUD panels' rounded corners are drawn at 4× and
# scaled down.
_SPRITE_SS = 4

# A card fades over this long: the title out as it ends, the end card in.
CARD_FADE_S = 0.5

# The HUD panels, and the corners they sit in outside overview mode. In
# overview mode the whole trip is on screen for the whole video, so the panels
# go, once for the whole video, to the corners the route leaves clearest
# (docs/reviews/VIDEO_CAMERA_QUALITY_PLAN.md F1-3).
_HUD_ROLES = ("date", "speed", "counters")
_DEFAULT_HUD = ("tl", "bl", "br")
_CORNERS = ("tl", "bl", "br", "tr")

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def mode_color(mode: str) -> RGB:
    return MODE_COLORS.get(mode, FALLBACK_COLOR)


def format_km(km: float) -> str:
    return f"{km:.1f} km" if km < 10 else f"{km:,.0f} km"


def format_speed(kmh: float) -> str:
    return f"{kmh:.1f} km/h" if kmh < 10 else f"{kmh:,.0f} km/h"


def _decimate(points: Sequence[Tuple[float, float]], threshold: float) -> List[int]:
    """Indices of the points kept when a point that hasn't moved *threshold*
    from the last kept one is dropped (``poster_renderer._decimate_pixels``,
    returning indices so a leg can be cut part-way)."""
    if len(points) < 3:
        return list(range(len(points)))
    keep = [0]
    lx, ly = points[0]
    for i in range(1, len(points) - 1):
        x, y = points[i]
        if abs(x - lx) >= threshold or abs(y - ly) >= threshold:
            keep.append(i)
            lx, ly = x, y
    keep.append(len(points) - 1)
    return keep


class _Route:
    """The legs in world units, decimated once per integer zoom they are
    drawn at."""

    def __init__(self, timeline: Timeline) -> None:
        self.legs = timeline.legs
        self.world = [[lonlat_to_world(lon, lat) for lon, lat in leg.coords]
                      for leg in self.legs]
        self.bbox = [(min(p[0] for p in pts), min(p[1] for p in pts),
                      max(p[0] for p in pts), max(p[1] for p in pts)) for pts in self.world]
        self._levels: Dict[int, List[List[int]]] = {}
        # km each leg's mode had covered before it: the marker's fraction of
        # its leg is read back from the per-mode counters.
        before: Dict[str, float] = {}
        self.km_before: List[float] = []
        for leg in self.legs:
            self.km_before.append(before.get(leg.mode, 0.0))
            before[leg.mode] = before.get(leg.mode, 0.0) + leg.km

    def kept(self, level: int) -> List[List[int]]:
        out = self._levels.get(level)
        if out is None:
            scale = TILE_SIZE * 2 ** level
            out = [_decimate([(x * scale, y * scale) for x, y in pts], 1.5)
                   for pts in self.world]
            self._levels[level] = out
        return out

    def fraction(self, n: int, state: FrameState) -> float:
        leg = self.legs[n]
        if leg.km <= 0:
            return 1.0
        done = state.km_by_mode.get(leg.mode, 0.0) - self.km_before[n]
        return min(1.0, max(0.0, done / leg.km))


def _font(weight: str, px: int) -> ImageFont.ImageFont:
    return load_face(weight, max(6, px))


def _on_light(rgb: RGB) -> RGB:
    """*rgb* darkened to 65% of its lightness, for a glyph on white — the
    client's ``iconBoxFg`` (design_tokens.dart) in light mode."""
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, l * 0.65, s))


@functools.lru_cache(maxsize=8)
def _icon_source(mode: str) -> Image.Image:
    """The mode's 512 px glyph mask (the PNG's alpha)."""
    path = ICON_DIR / f"{mode}.png"
    if not path.exists():
        path = ICON_DIR / "other.png"
    with Image.open(path) as img:
        return img.getchannel("A")


@functools.lru_cache(maxsize=64)
def _icon(mode: str, px: int, color: RGB) -> Image.Image:
    """The mode's icon, *px* square, in *color*: downsized from its 512 px
    source once per size and colour."""
    mask = _icon_source(mode).resize((px, px), Image.LANCZOS)
    icon = Image.new("RGBA", (px, px), color + (0,))
    icon.putalpha(mask)
    return icon


@functools.lru_cache(maxsize=16)
def _marker(mode: str, d: int) -> Image.Image:
    """The marker sprite: a soft halo, a white disc ringed in the mode colour,
    the mode icon inside. Drawn 4×, icon included, and scaled down for smooth
    edges."""
    ss = _SPRITE_SS
    halo = d * 1.45
    side = int(round(halo)) | 1
    big = Image.new("RGBA", (side * ss, side * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    c = side * ss / 2
    draw.ellipse((c - halo * ss / 2, c - halo * ss / 2, c + halo * ss / 2, c + halo * ss / 2),
                 fill=(255, 255, 255, 80))
    r = d * ss / 2
    ring = max(1, round(d / 12)) * ss
    draw.ellipse((c - r, c - r, c + r, c + r), fill=mode_color(mode) + (255,))
    draw.ellipse((c - r + ring, c - r + ring, c + r - ring, c + r - ring), fill=(255, 255, 255, 255))
    icon = _icon(mode, max(4, round(d * 0.62)) * ss, _on_light(mode_color(mode)))
    big.alpha_composite(icon, ((big.width - icon.width) // 2, (big.height - icon.height) // 2))
    return big.resize((side, side), Image.LANCZOS)


@functools.lru_cache(maxsize=64)
def _panel_patch(w: int, h: int, radius: int) -> Image.Image:
    """A HUD panel's rounded rectangle, drawn 4× and scaled down."""
    ss = _SPRITE_SS
    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1),
                                          radius=radius * ss, fill=_PANEL)
    return big.resize((w, h), Image.BOX)


def _stack(weight: str, px: int) -> FontStack:
    """A text face with the emoji face as fallback, as the poster's cards
    use, so a trip called "Japan 🇯🇵" keeps its flag."""
    faces = [_font(weight, px)]
    scales = [1.0]
    loaded = load_emoji_face(px)
    if loaded is not None:
        face, native = loaded
        faces.append(face)
        scales.append(px / native if native else 1.0)
    return FontStack(faces=tuple(faces), size_px=px, scales=tuple(scales))


def _text_width(text: str, stack: FontStack) -> float:
    return sum(run.advance for run in split_runs(text, stack))


def _draw_text(img: Image.Image, xy: Tuple[float, float], text: str, stack: FontStack,
               fill: RGB) -> None:
    """Draw *text* run by run, emoji runs rendered at their face's native
    size and scaled down (see ``poster_renderer._draw_scaled_run``)."""
    x, y = xy
    draw = ImageDraw.Draw(img)
    ascent = stack.primary.getmetrics()[0]
    for run in split_runs(text, stack):
        if not run.scaled:
            draw.text((x, y), run.text, font=run.face, fill=fill)
        else:
            width = max(1, round(ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(
                run.text, font=run.face)))
            a, dsc = run.face.getmetrics()
            tile = Image.new("RGBA", (width, max(1, a + dsc)), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((0, 0), run.text, font=run.face, embedded_color=True)
            tile = tile.resize((max(1, round(tile.width * run.scale)),
                                max(1, round(tile.height * run.scale))), Image.LANCZOS)
            dy = max(0, ascent - round(a * run.scale))
            img.alpha_composite(tile, (round(x), round(y) + dy))
        x += run.advance


def _fit_stack(text: str, weight: str, px: int, max_width: float) -> FontStack:
    """The largest stack not above *px* in which *text* fits *max_width*."""
    while True:
        stack = _stack(weight, px)
        if px <= 8 or _text_width(text, stack) <= max_width:
            return stack
        px = int(px * 0.9)


def _date_range(timeline: Timeline) -> str:
    dates = [leg.date for leg in timeline.legs if leg.date is not None]
    if not dates:
        return ""
    a, b = min(dates), max(dates)

    def fmt(d, year=True):
        return f"{d.day} {_MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")

    if a == b:
        return fmt(a)
    return f"{fmt(a, a.year != b.year)} – {fmt(b)}"


def _modes_in_order(timeline: Timeline) -> List[str]:
    seen: List[str] = []
    for leg in timeline.legs:
        if leg.mode not in seen:
            seen.append(leg.mode)
    return seen


def _route_cells(lines: Sequence[Sequence[Tuple[float, float]]], box: Size,
                 ss: int) -> List[Tuple[int, int, int, int]]:
    """The parts of the route box the route may touch, as (left, right, top,
    bottom) runs of :data:`_ROUTE_CELL` cells in box pixels. *lines* are in
    the box's ``ss``× coordinates. Each line is drawn 3 cells wide on a grid
    of cells and grown by one more cell each way, so a cell the route only
    grazes is kept."""
    t = _ROUTE_CELL
    cols, rows = -(-box[0] // t), -(-box[1] // t)
    grid = Image.new("L", (cols, rows), 0)
    draw = ImageDraw.Draw(grid)
    k = 1 / (t * ss)
    for line in lines:
        cells = [(x * k, y * k) for x, y in line]
        draw.line(cells, fill=255, width=3)
        draw.point(cells, fill=255)
    grid = grid.filter(ImageFilter.MaxFilter(3))
    data = grid.tobytes()
    runs = []
    for r in range(rows):
        row = data[r * cols:(r + 1) * cols]
        c = 0
        while c < cols:
            if not row[c]:
                c += 1
                continue
            start = c
            while c < cols and row[c]:
                c += 1
            runs.append((start * t, min(c * t, box[0]), r * t, min((r + 1) * t, box[1])))
    return runs


@dataclass(frozen=True)
class _Card:
    rgb: Image.Image
    alpha: Image.Image


class Overlay:
    """Draws one frame's overlay onto its basemap. Built once per render."""

    def __init__(self, timeline: Timeline, size: Size, title: str,
                 camera: str = "variable") -> None:
        self.timeline = timeline
        self.size = size
        w, h = size
        self.route = _Route(timeline)
        self.modes = _modes_in_order(timeline)
        self.faint_w = max(1, round(h / 270))
        self.line_w = max(2, round(h / 160))
        self.casing_w = max(1, round(h / 540))
        self.marker_d = max(10, round(h * 0.075))
        self.margin = max(4, round(h * 0.03))
        self.hud_px = max(8, round(h * 0.032))
        self.hud = _stack("bold", self.hud_px)
        self.hud_small = _stack("regular", max(7, round(h * 0.027)))
        # Each panel's corner, and whether the route and marker are drawn over
        # the panels (overview mode, when no layout keeps clear of them).
        self.hud_corners: Dict[str, str] = dict(zip(_HUD_ROLES, _DEFAULT_HUD))
        self.route_over_hud = False
        if camera == "overview":
            self.hud_corners, self.route_over_hud = self._overview_hud()
        self.title_card = self._title_card(title)
        self.end_card = self._end_card(title)

    # ── route ────────────────────────────────────────────────────────────────

    def _visible(self, i: int, shot: Shot, scale: float, pad: float) -> bool:
        x0, y0, x1, y1 = self.route.bbox[i]
        cx, cy = lonlat_to_world(shot.lon, shot.lat)
        hw, hh = (self.size[0] / 2 + pad) / scale, (self.size[1] / 2 + pad) / scale
        return x1 >= cx - hw and x0 <= cx + hw and y1 >= cy - hh and y0 <= cy + hh

    def _lines(self, shot: Shot, state: FrameState):
        """(faint lines, travelled lines by mode) in frame pixels."""
        scale = TILE_SIZE * 2 ** shot.zoom
        cx, cy = lonlat_to_world(shot.lon, shot.lat)
        ox, oy = self.size[0] / 2 - cx * scale, self.size[1] / 2 - cy * scale
        level = max(0, min(22, int(shot.zoom)))
        kept = self.route.kept(level)
        if state.kind == "title":
            current = 0
        elif state.kind == "end":
            current = len(self.route.legs)
        else:
            current = state.leg_index

        faint, travelled = [], []
        pad = self.line_w * 2
        for i, pts in enumerate(self.route.world):
            if not self._visible(i, shot, scale, pad):
                continue
            idx = kept[i]
            line = [(pts[k][0] * scale + ox, pts[k][1] * scale + oy) for k in idx]
            faint.append(line)
            if i < current:
                travelled.append((self.route.legs[i].mode, line))
            elif i == current and state.kind == "clip":
                leg = self.route.legs[i]
                target = self.route.fraction(i, state) * leg.cum_km[-1]
                cut = bisect.bisect_left(leg.cum_km, target)
                part = [p for k, p in zip(idx, line) if k < cut]
                mx, my = lonlat_to_world(state.lon, state.lat)
                part.append((mx * scale + ox, my * scale + oy))
                if len(part) >= 2:
                    travelled.append((leg.mode, part))
        return faint, travelled

    def _draw_route(self, frame: Image.Image, shot: Shot, state: FrameState) -> None:
        """One colour layer and one coverage mask for the whole route, drawn
        :data:`ROUTE_SS` times larger and box-filtered down for anti-aliasing,
        pasted in one go — all over the route's bounding box only. The colour
        layer is drawn a pixel wider than the mask so the partly covered edge
        never picks up its background."""
        faint, travelled = self._lines(shot, state)
        if not faint:
            return
        outer = self.line_w + 2 * self.casing_w
        pad = outer + 4
        xs = [x for line in faint for x, _ in line]
        ys = [y for line in faint for _, y in line]
        x0, y0 = max(0, int(min(xs)) - pad), max(0, int(min(ys)) - pad)
        x1 = min(self.size[0], int(max(xs)) + pad + 1)
        y1 = min(self.size[1], int(max(ys)) + pad + 1)
        if x1 <= x0 or y1 <= y0:
            return

        ss = ROUTE_SS
        # Frame pixel (i, j) is the ss × ss block from (i·ss, j·ss): a frame
        # coordinate maps to the centre of its block.
        c = (ss - 1) / 2

        def scaled(line):
            return [((x - x0) * ss + c, (y - y0) * ss + c) for x, y in line]

        box = (x1 - x0, y1 - y0)
        big = (box[0] * ss, box[1] * ss)
        # The colour layer is left unfilled (filling an RGB layer this size
        # costs more than drawing the whole route): every colour is drawn a
        # pixel wider than its coverage, the faint line's too, so what lies
        # outside it never shows.
        color = Image.new("RGB", big, None)
        mask = Image.new("L", big, 0)
        cd, md = ImageDraw.Draw(color), ImageDraw.Draw(mask)
        faint = [scaled(line) for line in faint]
        for line in faint:
            cd.line(line, fill=_FAINT, width=(self.faint_w + 2) * ss)
        for line in faint:
            md.line(line, fill=_FAINT_ALPHA, width=self.faint_w * ss)
        travelled = [(mode, scaled(line)) for mode, line in travelled]
        for _, line in travelled:
            cd.line(line, fill=_CASING, width=(outer + 2) * ss, joint="curve")
            md.line(line, fill=255, width=outer * ss, joint="curve")
        for mode, line in travelled:
            cd.line(line, fill=mode_color(mode), width=self.line_w * ss, joint="curve")
        # reduce() is the box filter at an integer factor, about 3× faster
        # than resize(BOX) at these sizes.
        lines = faint + [line for _, line in travelled]
        for a, b, top, bottom in _route_cells(lines, box, ss):
            area = (a * ss, top * ss, b * ss, bottom * ss)
            frame.paste(color.reduce(ss, area), (x0 + a, y0 + top), mask.reduce(ss, area))

    # ── marker and HUD ───────────────────────────────────────────────────────

    def _draw_marker(self, frame: Image.Image, shot: Shot, state: FrameState) -> None:
        scale = TILE_SIZE * 2 ** shot.zoom
        cx, cy = lonlat_to_world(shot.lon, shot.lat)
        mx, my = lonlat_to_world(state.lon, state.lat)
        x = self.size[0] / 2 + (mx - cx) * scale
        y = self.size[1] / 2 + (my - cy) * scale
        sprite = _marker(state.mode, self.marker_d)
        frame.paste(sprite, (round(x - sprite.width / 2), round(y - sprite.height / 2)), sprite)

    def _panel_size(self, rows: Sequence[Tuple[Optional[str], str, FontStack]]):
        """(width, height, padding, gap, row heights) of a panel of *rows*."""
        pad = max(3, round(self.hud_px * 0.45))
        gap = max(2, round(self.hud_px * 0.3))
        heights = [round(s.size_px * 1.25) for _, _, s in rows]
        widths = [(h + gap if icon else 0) + _text_width(text, s)
                  for (icon, text, s), h in zip(rows, heights)]
        pw = round(max(widths)) + 2 * pad
        ph = sum(heights) + gap * (len(rows) - 1) + 2 * pad
        return pw, ph, pad, gap, heights

    def _panel_rect(self, corner: str, pw: int, ph: int) -> Tuple[int, int, int, int]:
        """(left, top, right, bottom) of a *pw* × *ph* panel in *corner*."""
        w, h = self.size
        m = self.margin
        px = m if corner[1] == "l" else w - m - pw
        py = m if corner[0] == "t" else h - m - ph
        return px, py, px + pw, py + ph

    def _panel(self, frame: Image.Image, rows: Sequence[Tuple[Optional[str], str, FontStack]],
               anchor: str) -> None:
        """A translucent rounded panel of rows (optional icon, text), at the
        frame corner *anchor* ("tl", "tr", "bl", "br")."""
        pw, ph, pad, gap, heights = self._panel_size(rows)
        patch = _panel_patch(pw, ph, pad).copy()
        y = pad
        for (icon, text, stack), h in zip(rows, heights):
            x = pad
            if icon:
                ic = _icon(icon, h, mode_color(icon))
                patch.alpha_composite(ic, (x, y))
                x += h + gap
            _draw_text(patch, (x, y + (h - stack.size_px * 1.15) / 2), text, stack, _TEXT)
            y += h + gap
        frame.paste(patch, self._panel_rect(anchor, pw, ph)[:2], patch)

    def _hud_rows(self, state: FrameState) -> Dict[str, list]:
        """Each HUD panel's rows in *state*; a panel without rows isn't drawn."""
        return {
            "date": [(None, state.date_label, self.hud)] if state.date_label else [],
            "speed": [(state.mode, format_speed(state.speed_kmh), self.hud)],
            "counters": [(m, format_km(state.km_by_mode[m]), self.hud_small)
                         for m in self.modes if state.km_by_mode.get(m, 0.0) >= 0.05],
        }

    def _draw_hud(self, frame: Image.Image, state: FrameState) -> None:
        for role, rows in self._hud_rows(state).items():
            if rows:
                self._panel(frame, rows, self.hud_corners[role])

    # ── overview HUD layout ──────────────────────────────────────────────────

    def _clip_states(self) -> List[FrameState]:
        """Every clip frame's state, sampled as ``renderer.FrameRenderer``
        samples them."""
        fps = self.timeline.fps
        states = (self.timeline.sample(n / fps) for n in range(frame_count(self.timeline, fps)))
        return [s for s in states if s.kind == "clip"]

    def _panel_extents(self, states: Sequence[FrameState]) -> Dict[str, Tuple[int, int]]:
        """Each panel's largest width and height over *states*: anchored in
        one corner, every frame's panel lies inside it. (0, 0) if never drawn."""
        out: Dict[str, Tuple[int, int]] = {role: (0, 0) for role in _HUD_ROLES}
        seen = set()
        for state in states:
            for role, rows in self._hud_rows(state).items():
                key = (role, tuple((icon, text) for icon, text, _ in rows))
                if not rows or key in seen:
                    continue
                seen.add(key)
                pw, ph = self._panel_size(rows)[:2]
                out[role] = (max(out[role][0], pw), max(out[role][1], ph))
        return out

    def hud_rects(self) -> Dict[str, Tuple[int, int, int, int]]:
        """Each panel's (left, top, right, bottom) extent over the whole video,
        in its corner: every clip frame's panel lies inside it."""
        extents = self._panel_extents(self._clip_states())
        return {role: self._panel_rect(self.hud_corners[role], *extents[role])
                for role in _HUD_ROLES}

    def overview_shot(self) -> Shot:
        """The overview camera's one shot, as ``camera.camera_path`` makes it."""
        x, y, z = overview(self.timeline, self.size)
        return Shot(*world_to_lonlat(x, y), z, False)

    def _overview_hud(self) -> Tuple[Dict[str, str], bool]:
        """The panels' corners in overview mode, chosen once for the whole
        video so they never jump: the three corners whose panels cover least
        of the whole route and of every clip frame's marker at the overview
        shot, in pixels. Ties go to the layout moving fewest panels from their
        usual corners, so today's layout wins whenever it is as clear as any.
        When every layout covers some, the route and the marker are drawn over
        the panels (the second value)."""
        shot = self.overview_shot()
        scale = TILE_SIZE * 2 ** shot.zoom
        cx, cy = lonlat_to_world(shot.lon, shot.lat)
        ox, oy = self.size[0] / 2 - cx * scale, self.size[1] / 2 - cy * scale
        footprint = Image.new("L", self.size, 0)
        draw = ImageDraw.Draw(footprint)
        outer = self.line_w + 2 * self.casing_w
        for pts in self.route.world:
            line = [(x * scale + ox, y * scale + oy) for x, y in pts]
            draw.line(line, fill=255, width=outer, joint="curve")
            for x, y in (line[0], line[-1]):
                draw.ellipse((x - outer / 2, y - outer / 2, x + outer / 2, y + outer / 2), fill=255)
        states = self._clip_states()
        r = self.marker_d / 2
        for x, y in {(round(mx * scale + ox), round(my * scale + oy))
                     for mx, my in (lonlat_to_world(s.lon, s.lat) for s in states)}:
            draw.ellipse((x - r, y - r, x + r, y + r), fill=255)

        extents = self._panel_extents(states)

        def covered(role: str, corner: str) -> int:
            if not all(extents[role]):
                return 0
            rect = self._panel_rect(corner, *extents[role])
            return sum(footprint.crop(rect).histogram()[1:])

        cost = {(role, c): covered(role, c) for role in _HUD_ROLES for c in _CORNERS}

        def rank(layout):
            return (sum(cost[pair] for pair in zip(_HUD_ROLES, layout)),
                    sum(c != d for c, d in zip(layout, _DEFAULT_HUD)))

        best = min(itertools.permutations(_CORNERS, len(_HUD_ROLES)), key=rank)
        return dict(zip(_HUD_ROLES, best)), rank(best)[0] > 0

    # ── cards ────────────────────────────────────────────────────────────────

    def _card_base(self) -> Image.Image:
        return Image.new("RGBA", self.size, (0, 0, 0, _SCRIM_ALPHA))

    def _centered(self, card: Image.Image, y: float, text: str, stack: FontStack,
                  fill: RGB) -> None:
        _draw_text(card, ((self.size[0] - _text_width(text, stack)) / 2, y), text, stack, fill)

    def _title_card(self, title: str) -> _Card:
        w, h = self.size
        card = self._card_base()
        big = _fit_stack(title, "bold", round(h * 0.085), w * 0.85)
        sub_text = " · ".join(t for t in (
            _date_range(self.timeline),
            format_km(sum(self.timeline.km_totals.values()))) if t)
        sub = _stack("regular", round(h * 0.04))
        block = big.size_px * 1.3 + sub.size_px * 1.2
        y = (h - block) / 2
        self._centered(card, y, title, big, _TEXT)
        self._centered(card, y + big.size_px * 1.3, sub_text, sub, _MUTED)
        return _Card(card.convert("RGB"), card.getchannel("A"))

    def _end_card(self, title: str) -> _Card:
        w, h = self.size
        card = self._card_base()
        head = _fit_stack(title, "bold", round(h * 0.06), w * 0.85)
        row_px = round(h * 0.045)
        row = _stack("regular", row_px)
        totals = self.timeline.km_totals
        rows = [(m, f"{MODE_LABELS.get(m, m.title())}  {format_km(totals[m])}")
                for m in self.modes if totals.get(m, 0.0) >= 0.05]
        line = round(row_px * 1.5)
        foot = _stack("regular", round(h * 0.03))
        block = head.size_px * 1.6 + line * len(rows) + foot.size_px * 2
        y = (h - block) / 2
        self._centered(card, y, title, head, _TEXT)
        y += head.size_px * 1.6
        for mode, text in rows:
            tw = row_px + row_px * 0.4 + _text_width(text, row)
            x = (w - tw) / 2
            ic = _icon(mode, row_px, mode_color(mode))
            card.alpha_composite(ic, (round(x), round(y)))
            _draw_text(card, (x + row_px * 1.4, y - row_px * 0.1), text, row, _TEXT)
            y += line
        self._centered(card, y + foot.size_px, "TraxJourney", foot, _MUTED)
        return _Card(card.convert("RGB"), card.getchannel("A"))

    def _card_alpha(self, state: FrameState) -> Tuple[Optional[_Card], float]:
        tl = self.timeline
        if state.kind == "title":
            return self.title_card, min(1.0, (tl.title_s - state.t) / CARD_FADE_S)
        if state.kind == "end":
            return self.end_card, min(1.0, (state.t - tl.clips_end_s) / CARD_FADE_S)
        return None, 0.0

    @staticmethod
    def _paste_card(frame: Image.Image, card: _Card, a: float) -> None:
        if a <= 0:
            return
        mask = card.alpha if a >= 1 else card.alpha.point([round(v * a) for v in range(256)])
        frame.paste(card.rgb, (0, 0), mask)

    # ── the frame ────────────────────────────────────────────────────────────

    def draw(self, frame: Image.Image, shot: Shot, state: FrameState) -> Image.Image:
        """Draw everything over *frame* (the basemap, RGB, modified in place)."""
        if state.kind == "clip" and self.route_over_hud:
            self._draw_hud(frame, state)
            self._draw_route(frame, shot, state)
            self._draw_marker(frame, shot, state)
        elif state.kind == "clip":
            self._draw_route(frame, shot, state)
            self._draw_marker(frame, shot, state)
            self._draw_hud(frame, state)
        else:
            self._draw_route(frame, shot, state)
        card, a = self._card_alpha(state)
        if card is not None:
            self._paste_card(frame, card, a)
        return frame

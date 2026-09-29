"""Trip video overlay drawing quality (docs/VIDEO_CAMERA_QUALITY_PLAN.md, U4):
the route is anti-aliased by supersampling, the mode icons come from 512 px
sources, and the overlay needs nothing at runtime but Pillow.

The overview timing runs only with ``VIDEO_BENCH=1``::

    VIDEO_BENCH=1 pytest tests/test_video_overlay.py -k overview -s
"""
from __future__ import annotations

import ast
import math
import os
import sys
import time
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from src.models.project import Project, ProjectItem
from src.video import overlay as overlay_module
from src.video.camera import (
    TILE_SIZE,
    Shot,
    camera_path,
    lonlat_to_world,
    overview,
    world_to_lonlat,
)
from src.video.legs import build_legs
from src.video.overlay import ICON_DIR, MODE_COLORS, Overlay
from src.video.timeline import build_timeline
from tests.test_video_renderer import _activity, _benchmark_trip

SMALL = (320, 180)


# ── edge straightness ────────────────────────────────────────────────────────

def _segment_overlay(angle_deg: float):
    """An overlay of one straight ride at *angle_deg* below the horizontal on
    screen, the end-card state (the whole route travelled) and a shot that
    frames it across 150 px at 320×180."""
    lat0, lon0 = 0.0, 10.0
    x0, y0 = lonlat_to_world(lon0, lat0)
    run = 0.001                                   # world units along x
    x1, y1 = x0 + run, y0 + run * math.tan(math.radians(angle_deg))
    lon1, lat1 = world_to_lonlat(x1, y1)
    points = [(lat0 + (lat1 - lat0) * k, lon0 + (lon1 - lon0) * k)
              for k in range(2)]
    project = Project(name="Line", activities=[
        _activity(1, "Ride", date(2026, 5, 1), points, 5_000.0, 1200)],
        items=[ProjectItem(item_type="activity", activity_id=1)])
    tl = build_timeline(build_legs(project), 30.0)
    zoom = math.log2(150 / (run * TILE_SIZE))
    shot = Shot(*world_to_lonlat((x0 + x1) / 2, (y0 + y1) / 2), zoom, False)
    return Overlay(tl, SMALL, "Line"), shot, tl.sample(tl.total_s)


def _coverage(angle_deg: float):
    """The route's coverage per pixel, read back from drawing it over black
    and over white: out = a·colour + (1 − a)·background."""
    ov, shot, state = _segment_overlay(angle_deg)
    black = Image.new("RGB", SMALL, (0, 0, 0))
    white = Image.new("RGB", SMALL, (255, 255, 255))
    ov._draw_route(black, shot, state)
    ov._draw_route(white, shot, state)
    b, w = black.getchannel("G").load(), white.getchannel("G").load()
    return [[1 - (w[x, y] - b[x, y]) / 255 for x in range(SMALL[0])] for y in range(SMALL[1])]


def _edge_rms(angle_deg: float) -> float:
    """RMS distance (px) of the stroke's left edge from a straight line. In
    each row of the middle of the segment, the edge's sub-pixel position is
    the row's stroke centre minus the coverage left of it (the interior is
    fully covered); a least-squares line is fitted through those positions."""
    rows = []
    for y, row in enumerate(_coverage(angle_deg)):
        solid = [x for x, a in enumerate(row) if a > 0.5]
        if solid:
            rows.append((y, row, (solid[0] + solid[-1]) // 2))
    rows = rows[len(rows) // 5: -len(rows) // 5]   # clear of the round caps
    ys = [float(y) for y, _, _ in rows]
    es = [xc - sum(row[:xc]) for _, row, xc in rows]
    n = len(ys)
    my, me = sum(ys) / n, sum(es) / n
    slope = sum((y - my) * (e - me) for y, e in zip(ys, es)) / sum((y - my) ** 2 for y in ys)
    return math.sqrt(sum((e - me - slope * (y - my)) ** 2 for y, e in zip(ys, es)) / n)


def test_a_diagonal_route_edge_is_straight():
    """A 45° segment's edge, sampled row by row from the coverage mask, lies
    on a straight line. An exact pixel diagonal is the one slope a hard
    staircase already follows row by row, so the segment is a few degrees off
    it (at 43°), as a real route's always are.

    Measured in the Linux image: before #518 (a hard mask, box-blurred) the
    RMS residual was 0.246 px, which fails; supersampled at ROUTE_SS = 3 it
    is 0.076 px.
    """
    assert _edge_rms(43.0) < 0.12


def test_filtering_only_the_cells_the_route_crosses_changes_no_pixel(monkeypatch):
    """The route is filtered down and pasted cell by cell, only where it may
    be: the frames match filtering the whole route box, zoomed in (where the
    box is most of the frame) and over the whole trip."""
    size = (640, 360)
    tl = build_timeline(build_legs(_benchmark_trip()), 60.0)
    shots = camera_path(tl, 30, size)
    x, y, z = overview(tl, size)
    still = Shot(*world_to_lonlat(x, y), z, False)
    ov = Overlay(tl, size, "Trip")
    base = Image.new("RGB", size, (90, 110, 90))
    cases = [(shots[n], tl.sample(n / 30)) for n in range(40, len(shots), 97)]
    cases += [(still, tl.sample(tl.total_s * k / 7)) for k in range(8)]

    def frames():
        out = []
        for shot, state in cases:
            frame = base.copy()
            ov._draw_route(frame, shot, state)
            out.append(frame.tobytes())
        return out

    by_cell = frames()
    monkeypatch.setattr(overlay_module, "_ROUTE_CELL", 10 ** 6)
    whole_box = frames()
    assert by_cell == whole_box
    assert any(frame != base.tobytes() for frame in by_cell)


# ── icons and dependencies ───────────────────────────────────────────────────

def test_icons_are_loaded_from_512_px_sources():
    for mode in MODE_COLORS:
        with Image.open(ICON_DIR / f"{mode}.png") as img:
            assert img.size == (512, 512)
            assert img.mode == "RGBA"
    overlay_module._icon_source.cache_clear()
    assert overlay_module._icon_source("ride").size == (512, 512)
    icon = overlay_module._icon("ride", 37, MODE_COLORS["ride"])
    assert icon.size == (37, 37)
    # Tinted: the glyph is its alpha, every pixel the requested colour.
    assert [icon.getchannel(k).getextrema() for k in "RGB"] == [
        (c, c) for c in MODE_COLORS["ride"]]
    assert icon.getchannel("A").getextrema() == (0, 255)


def test_an_unknown_mode_falls_back_to_the_other_icon():
    a = overlay_module._icon("sledge", 24, (1, 2, 3))
    b = overlay_module._icon("other", 24, (1, 2, 3))
    assert a.tobytes() == b.tobytes()


def test_the_overlay_imports_nothing_at_runtime_but_pillow():
    tree = ast.parse(Path(overlay_module.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add(node.module.split(".")[0])
    third_party = roots - set(sys.stdlib_module_names) - {"__future__", "src"}
    assert third_party == {"PIL"}


# ── overview timing ──────────────────────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("VIDEO_BENCH"), reason="manual benchmark: VIDEO_BENCH=1")
def test_overlay_timing_at_the_overview_1080p():
    """``Overlay.draw`` on its own at 1080p, over a still overview shot of the
    benchmark trip — the worst case for the route box (~85% of the frame) —
    at 120 states spread over the video. Reports ms per frame."""
    size = (1920, 1080)
    tl = build_timeline(build_legs(_benchmark_trip()), 60.0)
    x, y, z = overview(tl, size)
    shot = Shot(*world_to_lonlat(x, y), z, False)
    ov = Overlay(tl, size, "Two weeks in France & Italy")
    base = Image.new("RGB", size, (90, 110, 90))
    states = [tl.sample(tl.total_s * k / 120) for k in range(120)]
    ov.draw(base.copy(), shot, states[60])          # warm the caches
    elapsed = 0.0
    for state in states:
        frame = base.copy()
        start = time.perf_counter()
        ov.draw(frame, shot, state)
        elapsed += time.perf_counter() - start
    print(f"\nBENCH overlay at overview 1080p: {elapsed / len(states) * 1000:.1f} ms/frame")

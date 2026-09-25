"""Tests for src/poster/poster_renderer.py (issue #14, Unit E).

Two groups:
  - ``assemble_card_content``: pure Pillow-free unit tests over config-flag /
    memory / metrics combinations.
  - A golden-path test of the full ``render_poster`` entry point, using an
    in-memory SQLite DB (mirrors tests/test_poster_api.py's fixture pattern)
    and a fake ``tile_fetcher`` (mirrors tests/test_tile_stitcher.py's
    pattern) so no real network/Mapbox call happens.
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime

import pytest
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
import src.poster.tile_stitcher as tile_stitcher
from models.project_db import DBMemory, DBProject, DBProjectItem
from models.user import UserInfo
from src.exceptions.errors import APIError
from src.poster.card_layout import card_width_px, layout_card, mm_to_px
from src.poster.card_placement import Rect
from src.models.journal import JournalEntry
from src.models.memory import Memory
from src.models.project import Project
from src.poster.poster_renderer import (
    _MISSING_BASEMAP_COLOR,
    _PAGE_MARGIN_MM,
    _PIN_COLOR,
    _PREVIEW_MAX_DIMENSION,
    _PREVIEW_MAX_TILES,
    _PREVIEW_TOTAL_BUDGET_S,
    _ROUTE_SUPERSAMPLE,
    _Projector,
    _day_number,
    _decimate_pixels,
    _draw_card,
    _draw_card_chrome,
    _draw_legend,
    _draw_route,
    _paste_cover,
    _pdf_resolution,
    _target_size,
    _trip_span,
    _trip_summary_blocks,
    assemble_card_content,
    render_poster,
    render_poster_preview,
)
from src.poster.theme import ACCENT, DARK_THEME, LIGHT_THEME, get_theme
from src.poster.typography import TYPE_SCALE, TypeScale

_PARIS_BOUNDS = {"north": 48.9, "south": 48.8, "east": 2.4, "west": 2.3}


def _solid_tile(color=(120, 140, 160), size=(512, 512)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _fake_tile_fetcher(z, x, y):
    return _solid_tile()


# ── assemble_card_content ────────────────────────────────────────────────────

_MEMORY = {
    "id": 1,
    "lat": 48.85,
    "lon": 2.35,
    "date": "2024-06-01",
    "name": "Day 1",
    "description": "Arrived in Paris",
    "photo_uuids": ["abc", "def"],
}

_METRICS = {
    "distance_m": 12_345.0,
    "elevation_m": 210.0,
    "encounter_count": 2,
    "counters": [{"name": "Coffees", "value": 3}],
    "tag_pie": {"scenic": 5_000.0, "urban": 2_000.0},
}

_ALL_FLAGS = {
    "distance": True, "elevation": True, "hero_photo": True, "all_photos": True,
    "memory_text": True, "counters": True, "tag_pie": True, "encounters": True,
}
_NO_FLAGS = {k: False for k in _ALL_FLAGS}


def test_all_flags_produce_every_block_kind():
    blocks = assemble_card_content(_ALL_FLAGS, _MEMORY, _METRICS)
    kinds = [b["kind"] for b in blocks]
    assert kinds == [
        "name", "date", "description", "hero_photo", "photos",
        "distance", "elevation", "counters", "tag_pie", "encounters",
    ]


def test_no_flags_produce_no_blocks():
    assert assemble_card_content(_NO_FLAGS, _MEMORY, _METRICS) == []


def test_memory_text_off_still_shows_the_title():
    """Regression: ``name`` used to be gated by ``memory_text`` alongside the
    date and description, so a card built from distance/counters alone rendered
    as an anonymous stats box with no way to tell which memory it belonged to.
    The title now shows on every card that has any content at all."""
    config = {**_ALL_FLAGS, "memory_text": False}
    kinds = [b["kind"] for b in assemble_card_content(config, _MEMORY, _METRICS)]
    assert kinds[0] == "name"
    assert "date" not in kinds
    assert "description" not in kinds


def test_memory_text_off_shows_the_title_on_a_stats_only_card():
    config = {**_NO_FLAGS, "distance": True, "counters": True}
    blocks = assemble_card_content(config, _MEMORY, _METRICS)
    assert [b["kind"] for b in blocks] == ["name", "distance", "counters"]
    assert blocks[0]["text"] == "Day 1"


def test_a_title_alone_is_not_content_enough_for_a_card():
    """The title is prepended to a card that has content; it never *creates*
    one on its own, or every flag-less memory would still get an all-but-empty
    box (bug 1)."""
    assert assemble_card_content(_NO_FLAGS, _MEMORY, _METRICS) == []


def test_memory_text_on_but_no_description_omits_description_block():
    memory = {**_MEMORY, "description": None}
    config = {**_NO_FLAGS, "memory_text": True}
    blocks = assemble_card_content(config, memory, _METRICS)
    # The date rides along with the memory text; only the description drops out.
    assert [b["kind"] for b in blocks] == ["name", "date"]


def test_hero_photo_uses_only_first_uuid():
    config = {**_NO_FLAGS, "hero_photo": True}
    blocks = assemble_card_content(config, _MEMORY, _METRICS)
    assert blocks == [
        {"kind": "name", "text": "Day 1"},
        {"kind": "hero_photo", "uuid": "abc"},
    ]


# ── The "Day N" badge ─────────────────────────────────────────────────────────

def test_day_badge_leads_every_card_with_content():
    config = {**_NO_FLAGS, "distance": True}
    blocks = assemble_card_content(config, _MEMORY, _METRICS, day_number=4)
    assert [b["kind"] for b in blocks] == ["day_badge", "name", "distance"]
    assert blocks[0] == {"kind": "day_badge", "day_number": 4}


def test_day_badge_precedes_the_title_with_all_flags_on():
    blocks = assemble_card_content(_ALL_FLAGS, _MEMORY, _METRICS, day_number=1)
    assert [b["kind"] for b in blocks][:3] == ["day_badge", "name", "date"]


def test_day_badge_does_not_give_an_empty_card_content():
    """A day number is not content: with nothing enabled the memory still gets
    no card at all."""
    assert assemble_card_content(_NO_FLAGS, _MEMORY, _METRICS, day_number=3) == []


def test_no_day_badge_when_the_day_number_is_unknown():
    config = {**_NO_FLAGS, "distance": True}
    blocks = assemble_card_content(config, _MEMORY, _METRICS, day_number=None)
    assert [b["kind"] for b in blocks] == ["name", "distance"]


def test_day_badge_lays_out_as_text_above_the_title():
    scale = TypeScale(150.0)
    layout = layout_card(
        [{"kind": "day_badge", "day_number": 7}, {"kind": "name", "text": "Col du Galibier"}],
        scale,
    )
    texts = [op for op in layout.ops if hasattr(op, "text")]
    assert texts[0].text.upper().startswith("DAY 7")
    assert texts[0].style == "day_badge"
    assert texts[0].y < texts[1].y  # the badge sits above the title


def test_hero_photo_and_all_photos_skipped_when_no_photos():
    memory = {**_MEMORY, "photo_uuids": []}
    config = {**_ALL_FLAGS}
    kinds = [b["kind"] for b in assemble_card_content(config, memory, _METRICS)]
    assert "hero_photo" not in kinds
    assert "photos" not in kinds


def test_counters_and_tag_pie_skipped_when_empty_even_if_enabled():
    metrics = {**_METRICS, "counters": [], "tag_pie": {}}
    config = {**_NO_FLAGS, "counters": True, "tag_pie": True}
    assert assemble_card_content(config, _MEMORY, metrics) == []


def test_encounters_block_present_even_when_count_is_zero():
    """Unlike counters/tag_pie, encounters has no "empty" sentinel to skip on —
    a count of 0 is still meaningful information for the card."""
    metrics = {**_METRICS, "encounter_count": 0}
    config = {**_NO_FLAGS, "encounters": True}
    assert assemble_card_content(config, _MEMORY, metrics) == [
        {"kind": "name", "text": "Day 1"},
        {"kind": "encounters", "count": 0},
    ]


def test_distance_and_elevation_carry_raw_metric_values():
    config = {**_NO_FLAGS, "distance": True, "elevation": True}
    blocks = assemble_card_content(config, _MEMORY, _METRICS)
    assert blocks == [
        {"kind": "name", "text": "Day 1"},
        {"kind": "distance", "value_m": 12_345.0},
        {"kind": "elevation", "value_m": 210.0},
    ]


# ── Resolution helpers ────────────────────────────────────────────────────────

def test_target_size_landscape_matches_a0_at_150dpi():
    w, h = _target_size("landscape")
    assert (w, h) == (7022, 4967)


def test_target_size_defaults_to_a0_when_paper_size_omitted():
    assert _target_size("landscape") == _target_size("landscape", paper_size="A0")


def test_target_size_portrait_is_landscape_transposed():
    assert _target_size("portrait") == _target_size("landscape")[::-1]


def test_pdf_resolution_is_close_to_150_dpi_for_both_orientations():
    w, h = _target_size("landscape")
    assert _pdf_resolution(w, "landscape") == pytest.approx(150.0, abs=0.1)
    w, h = _target_size("portrait")
    assert _pdf_resolution(w, "portrait") == pytest.approx(150.0, abs=0.1)


# Every physical drawing metric (card widths, margins, type scale, route
# width, ...) stays exactly as-is regardless of paper size — paper size only
# changes the output canvas's target pixel dimensions and hence the PDF's
# physical page size. Expected px values from ISO 216 short/long edge (mm) *
# 150 DPI, rounded.
@pytest.mark.parametrize("paper_size, landscape_px, portrait_px", [
    ("A0", (7022, 4967), (4967, 7022)),
    ("A1", (4967, 3508), (3508, 4967)),
    ("A2", (3508, 2480), (2480, 3508)),
    ("A3", (2480, 1754), (1754, 2480)),
    ("A4", (1754, 1240), (1240, 1754)),
])
def test_target_size_for_every_paper_size(paper_size, landscape_px, portrait_px):
    assert _target_size("landscape", paper_size=paper_size) == landscape_px
    assert _target_size("portrait", paper_size=paper_size) == portrait_px


@pytest.mark.parametrize("paper_size", ["A0", "A1", "A2", "A3", "A4"])
def test_pdf_resolution_is_close_to_150_dpi_for_every_paper_size(paper_size):
    for orientation in ("landscape", "portrait"):
        w, h = _target_size(orientation, paper_size=paper_size)
        assert _pdf_resolution(w, orientation, paper_size) == pytest.approx(150.0, abs=0.1)


def test_pdf_resolution_defaults_to_a0_when_paper_size_omitted():
    w, _ = _target_size("landscape")
    assert _pdf_resolution(w, "landscape") == _pdf_resolution(w, "landscape", "A0")


# ── _Projector ────────────────────────────────────────────────────────────────
# Regression coverage for a real bug found during manual end-to-end testing:
# _Projector subtracted crop_rect_for_bounds' offset (relative to the stitched
# tile canvas's own origin) directly from lonlat_to_pixel's *absolute*
# world-pixel coordinates, without first subtracting the tile canvas's origin
# (x_min * tile_size). For any bounding box far from the (0, 0) world-pixel
# origin — i.e. almost any real trip — this placed every projected pin/route
# point hundreds of thousands of pixels off-canvas, so every memory silently
# overflowed to the legend and nothing but the legend text ever appeared in
# the rendered image. The existing golden-path test below never caught this
# because it only checked file existence/size, not pixel content.

# A multi-country-scale box (unlike the tight ~0.1°-square _PARIS_BOUNDS used
# elsewhere in this file) — this is the shape of box that actually exposed the
# bug in manual testing, since the world-pixel origin offset grows with how
# far the box's tile range sits from world-pixel (0, 0).
_FRANCE_BOUNDS = {"north": 49.0, "south": 45.5, "east": 5.0, "west": 2.0}


@pytest.mark.parametrize("bounds", [_PARIS_BOUNDS, _FRANCE_BOUNDS])
@pytest.mark.parametrize("target_w, target_h", [(7022, 4967), (4967, 7022)])
def test_projector_places_points_within_bounds_inside_the_canvas(bounds, target_w, target_h):
    projector = _Projector(bounds, target_w, target_h)
    # Points strictly inside the bounds (not exactly on an edge) should
    # project well within the canvas, not thousands of pixels outside it.
    mid_lon = (bounds["west"] + bounds["east"]) / 2
    mid_lat = (bounds["south"] + bounds["north"]) / 2
    for lon, lat in [
        (mid_lon, mid_lat),
        (bounds["west"] + 0.01, bounds["north"] - 0.01),  # near NW corner
        (bounds["east"] - 0.01, bounds["south"] + 0.01),  # near SE corner
    ]:
        x, y = projector.project(lon, lat)
        assert -1 <= x <= target_w + 1, f"x={x} escaped canvas width {target_w}"
        assert -1 <= y <= target_h + 1, f"y={y} escaped canvas height {target_h}"


def test_projector_orders_points_geographically_nw_before_se():
    """A point near the bounds' NW corner should project to a smaller (x, y)
    than one near the SE corner — catches an origin computed with the wrong
    sign/axis, not just one that's merely out of range."""
    projector = _Projector(_FRANCE_BOUNDS, 7022, 4967)
    nw_x, nw_y = projector.project(_FRANCE_BOUNDS["west"] + 0.1, _FRANCE_BOUNDS["north"] - 0.1)
    se_x, se_y = projector.project(_FRANCE_BOUNDS["east"] - 0.1, _FRANCE_BOUNDS["south"] + 0.1)
    assert nw_x < se_x
    assert nw_y < se_y


# ── _decimate_pixels ─────────────────────────────────────────────────────────
# A real multi-week trip's full-resolution GPS track can be hundreds of
# thousands of points; projecting and then RouteIndex-bucketing all of them
# — for both the full render and, worse, the tiny preview canvas — was real,
# unbudgeted per-render cost that kept the preview timing out even after the
# card-measurement and basemap-fetch budgets were fixed (issue #14 follow-up).

def test_keeps_first_and_last_points_even_when_close_together():
    assert _decimate_pixels([(0.0, 0.0), (0.2, 0.2)]) == [(0.0, 0.0), (0.2, 0.2)]


def test_drops_points_that_have_not_moved_a_full_pixel():
    points = [(0.0, 0.0), (0.3, 0.1), (0.6, 0.2), (5.0, 5.0)]
    assert _decimate_pixels(points, threshold=1.0) == [(0.0, 0.0), (5.0, 5.0)]


def test_keeps_points_that_each_move_a_full_pixel():
    points = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]
    assert _decimate_pixels(points, threshold=1.0) == points


def test_a_dense_synthetic_track_collapses_to_a_small_fraction_of_its_points():
    """The actual regression: a real 120-activity trip is documented (see
    api/geo.py) at ~500k points; decimated to a small preview canvas's pixel
    resolution, the point count and therefore RouteIndex's segment count
    should drop by orders of magnitude, not merely somewhat."""
    import random

    rng = random.Random(20260824)
    points = []
    x, y = 0.0, 0.0
    for _ in range(50_000):
        x += rng.uniform(-0.05, 0.05)
        y += rng.uniform(-0.05, 0.05)
        points.append((x, y))

    decimated = _decimate_pixels(points, threshold=1.0)
    assert len(decimated) < len(points) / 20, (
        f"only {len(points)} -> {len(decimated)}, expected a much larger drop")


# ── _draw_route (anti-aliasing) ──────────────────────────────────────────────
# At the preview's much lower effective DPI, the route's physical width floors
# to a literal 1px line (see render_poster_preview's docstring), and plain
# ImageDraw is never anti-aliased — so every diagonal segment came out as a
# hard on/off staircase. supersample=1 (the full render's path) is unchanged;
# supersample>1 (the preview's path) draws onto a larger scratch layer and
# box-filters it back down, which should leave real, partial-coverage alpha
# values along a diagonal edge instead of pure 0/255.

_DIAGONAL = [(10.0, 10.0), (190.0, 150.0)]
# 19.2 DPI matches the actual preview's effective DPI at A0 (measured from
# _PREVIEW_MAX_DIMENSION / _target_size("landscape")), where the route floors
# to a 1px line -- the exact case this fix targets. 50 DPI is only used below
# where a test needs the line a couple of px wide (e.g. to see a fully-opaque
# core survive AA) -- a thin 1px diagonal stroke legitimately never reaches
# 100% coverage in any single output pixel once anti-aliased, since the
# stroke itself is narrower than the box filter's downsampling block.


def _diagonal_alpha_values(supersample: int, dpi: float = 19.2) -> set:
    canvas = Image.new("RGBA", (200, 160), (0, 0, 0, 0))
    _draw_route(canvas, [_DIAGONAL], dpi=dpi, supersample=supersample)
    pixels = canvas.load()
    return {pixels[x, y][3] for x in range(canvas.width) for y in range(canvas.height)
            if pixels[x, y][3] > 0}


def test_supersample_1_leaves_a_hard_edged_line():
    """The full render's path (supersample=1) is exactly the old behaviour:
    a plain ImageDraw line is either fully opaque or fully transparent."""
    alphas = _diagonal_alpha_values(supersample=1)
    assert alphas == {255}


def test_supersampling_produces_intermediate_alpha_on_a_diagonal():
    """The preview's path (supersample>1), at the preview's actual effective
    DPI: a diagonal edge must show partial coverage, not just on/off -- that
    partial coverage *is* the anti-aliasing."""
    alphas = _diagonal_alpha_values(supersample=_ROUTE_SUPERSAMPLE)
    assert any(0 < a < 255 for a in alphas), (
        f"expected intermediate alpha values, got only {sorted(alphas)}")


def test_supersampling_keeps_the_route_visible_at_full_opacity_somewhere():
    """With a couple of px of width to work with, the line's core should still
    hit full coverage -- supersampling must not thin the whole stroke into
    translucency."""
    alphas = _diagonal_alpha_values(supersample=_ROUTE_SUPERSAMPLE, dpi=50.0)
    assert 255 in alphas


def test_supersampling_does_not_shift_the_line_s_position():
    """Anti-aliasing should soften the edge, not move the route: the two
    renders' opaque cores should occupy essentially the same pixels."""
    def opaque_pixels(supersample):
        canvas = Image.new("RGBA", (200, 160), (0, 0, 0, 0))
        _draw_route(canvas, [_DIAGONAL], dpi=50.0, supersample=supersample)
        pixels = canvas.load()
        return {(x, y) for x in range(canvas.width) for y in range(canvas.height)
                if pixels[x, y][3] == 255}

    hard = opaque_pixels(1)
    soft = opaque_pixels(_ROUTE_SUPERSAMPLE)
    assert hard and soft
    # Every fully-opaque pixel in the anti-aliased version should be at most a
    # pixel away from some fully-opaque pixel in the hard-edged version.
    for x, y in soft:
        assert any((x + dx, y + dy) in hard
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1)), (
            f"AA pixel {(x, y)} has no hard-edged neighbour — line moved")


# ── Golden-path render_poster test ───────────────────────────────────────────

@pytest.fixture
def project_id(monkeypatch):
    """Seed a bare-minimum project in an in-memory SQLite DB and monkeypatch
    models.db.engine to point at it, mirroring tests/test_poster_api.py's
    fixture pattern. Returns the project's DB id."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="A", email="a@e.com")
        sess.add(user); sess.commit(); sess.refresh(user)
        proj = DBProject(user_info_id=user.id, name="My Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        # The memories the request bodies below pin: a poster only draws
        # memories of its own trip.
        for pos, (mid, day) in enumerate(((1, "2024-06-01"), (2, "2024-06-02"))):
            sess.add(DBMemory(id=mid, project_id=proj.id, date=day,
                              photos_json=json.dumps(["abc", "def"])))
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="memory", memory_id=mid))
        sess.commit()
        return proj.id


_BODY = {
    "bounds": _PARIS_BOUNDS,
    "orientation": "landscape",
    "config": {
        "distance": True, "elevation": False, "hero_photo": True,
        "all_photos": False, "memory_text": True, "counters": False,
        "tag_pie": False, "encounters": False,
    },
    "memories": [
        {"id": 1, "lat": 48.85, "lon": 2.35, "date": "2024-06-01",
         "name": "Day 1", "description": "Arrived", "photo_uuids": []},
        {"id": 2, "lat": 48.86, "lon": 2.36, "date": "2024-06-02",
         "name": "Day 2", "description": "Explored", "photo_uuids": []},
    ],
}


def test_render_poster_writes_png_and_pdf_at_expected_size(tmp_path, project_id):
    stages: list[str] = []

    png_path, pdf_path = render_poster(
        job_id=1,
        user_info_id=1,
        project_id=project_id,
        request=_BODY,
        poster_dir=tmp_path,
        progress=stages.append,
        tile_fetcher=_fake_tile_fetcher,
    )

    assert png_path.exists() and png_path.stat().st_size > 0
    assert pdf_path.exists() and pdf_path.stat().st_size > 0
    assert png_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert pdf_path.read_bytes()[:5] == b"%PDF-"

    with Image.open(png_path) as img:
        assert img.size == _target_size("landscape")
        # At least one pin should actually land on the canvas (regression
        # coverage for the _Projector origin bug above: a mis-projected pin
        # draws off-canvas and is silently a no-op, leaving the image with no
        # pin-colored pixels anywhere).
        pixels = img.load()
        w, h = img.size
        assert any(
            pixels[x, y][:3] == _PIN_COLOR
            for x in range(0, w, 8)
            for y in range(0, h, 8)
        ), "no pin-colored pixel found anywhere in the rendered poster"

    # Some stage progress should have reached the caller beyond a single
    # "rendering" label.
    assert len(stages) >= 3


def test_render_poster_portrait_orientation_transposes_size(tmp_path, project_id):
    body = {**_BODY, "orientation": "portrait"}
    png_path, _ = render_poster(
        job_id=2,
        user_info_id=1,
        project_id=project_id,
        request=body,
        poster_dir=tmp_path,
        progress=lambda s: None,
        tile_fetcher=_fake_tile_fetcher,
    )
    with Image.open(png_path) as img:
        assert img.size == _target_size("portrait")


def test_render_poster_honors_a_non_default_paper_size(tmp_path, project_id):
    """paper_size only changes the canvas/PDF page size — every physical
    drawing metric (card widths, margins, type scale, ...) stays as-is, so
    this only asserts on the output dimensions, not on layout."""
    body = {**_BODY, "paper_size": "A4"}
    png_path, pdf_path = render_poster(
        job_id=3,
        user_info_id=1,
        project_id=project_id,
        request=body,
        poster_dir=tmp_path,
        progress=lambda s: None,
        tile_fetcher=_fake_tile_fetcher,
    )
    with Image.open(png_path) as img:
        assert img.size == _target_size("landscape", paper_size="A4")
        assert img.size != _target_size("landscape")  # not A0
    # Pillow can't re-open a saved PDF as an image to check its physical
    # page size directly, so assert the resolution passed to save() (the
    # actual mechanism render_poster uses to fix the PDF's page size) is
    # still ~150 DPI for A4, same as it is for A0.
    assert pdf_path.read_bytes()[:5] == b"%PDF-"
    w_px, _ = _target_size("landscape", paper_size="A4")
    assert _pdf_resolution(w_px, "landscape", "A4") == pytest.approx(150.0, abs=0.1)


def test_render_poster_defaults_to_a0_when_paper_size_omitted(tmp_path, project_id):
    """A request with no paper_size at all (an old client) must render
    byte-for-byte the same size as an explicit 'A0' — the regression bar."""
    png_path, _ = render_poster(
        job_id=4,
        user_info_id=1,
        project_id=project_id,
        request=_BODY,  # no paper_size key
        poster_dir=tmp_path,
        progress=lambda s: None,
        tile_fetcher=_fake_tile_fetcher,
    )
    with Image.open(png_path) as img:
        assert img.size == _target_size("landscape", paper_size="A0")


def test_render_poster_raises_when_mapbox_token_missing(tmp_path, project_id, monkeypatch):
    """No tile_fetcher injected and no MAPBOX_TOKEN configured -> the render
    must fail with an actionable error, not silently complete on a grey
    background (issue #14 feedback, point 1 — the silent fallback made a
    misconfigured token undiagnosable)."""
    monkeypatch.setattr(tile_stitcher, "_mapbox_token", lambda: "")
    with pytest.raises(APIError, match="MAPBOX_TOKEN"):
        render_poster(
            job_id=3,
            user_info_id=1,
            project_id=project_id,
            request=_BODY,
            poster_dir=tmp_path,
            progress=lambda s: None,
        )


def test_render_poster_propagates_tile_fetch_failure(tmp_path, project_id):
    """A tile fetch that fails mid-render (bad token, Mapbox outage) must
    propagate with the underlying error message intact so the job runner can
    record it on the job row."""
    def _failing_fetcher(z, x, y):
        raise APIError("Mapbox tile fetch failed (401): Not Authorized")

    with pytest.raises(APIError, match="Not Authorized"):
        render_poster(
            job_id=4,
            user_info_id=1,
            project_id=project_id,
            request=_BODY,
            poster_dir=tmp_path,
            progress=lambda s: None,
            tile_fetcher=_failing_fetcher,
        )


# ── render_poster_preview ─────────────────────────────────────────────────────
# Fast, synchronous, low-res layout preview: never touches the network (no
# tile_fetcher needed, unlike render_poster's golden-path tests above), and
# is small/quick enough to check on every call.

def test_preview_returns_a_small_png_preserving_a0_aspect_ratio(project_id):
    png_bytes, _ = render_poster_preview(project_id, 1, _BODY)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    with Image.open(io.BytesIO(png_bytes)) as img:
        w, h = img.size
        # Longest side bounded by _PREVIEW_MAX_DIMENSION, real A0 aspect
        # ratio preserved (within a pixel of rounding).
        assert max(w, h) == _PREVIEW_MAX_DIMENSION
        real_w, real_h = _target_size("landscape")
        assert abs(w / h - real_w / real_h) < 0.01


def test_preview_portrait_is_taller_than_wide(project_id):
    body = {**_BODY, "orientation": "portrait"}
    png_bytes, _ = render_poster_preview(project_id, 1, body)
    with Image.open(io.BytesIO(png_bytes)) as img:
        w, h = img.size
        assert h > w


def test_preview_honors_a_non_default_paper_size(project_id):
    """All ISO A-series sizes share the same aspect ratio, so the preview's
    shape doesn't change — this instead guards that render_poster_preview
    actually reads paper_size at all (a regression here would silently keep
    behaving as A0 for every request)."""
    body = {**_BODY, "paper_size": "A3"}
    png_bytes, _ = render_poster_preview(project_id, 1, body)
    with Image.open(io.BytesIO(png_bytes)) as img:
        w, h = img.size
        assert max(w, h) == _PREVIEW_MAX_DIMENSION
        real_w, real_h = _target_size("landscape", paper_size="A3")
        assert abs(w / h - real_w / real_h) < 0.01


def test_preview_renders_the_real_basemap(project_id):
    """The preview shows the *actual* map, not a grey stand-in.

    It used to hard-skip the basemap and paint flat grey, so the map "looked
    grey" in the preview no matter what the token or network were doing —
    which is exactly what issue #14's feedback reported.
    """
    calls = []

    def fetcher(z, x, y):
        calls.append((z, x, y))
        return _solid_tile(color=(11, 222, 33))

    png_bytes, warning = render_poster_preview(
        project_id, 1, _BODY, tile_fetcher=fetcher)

    assert calls, "preview did not fetch any basemap tiles"
    assert warning is None
    with Image.open(io.BytesIO(png_bytes)) as img:
        pixels = img.convert("RGB").load()
        w, h = img.size
        assert any(
            pixels[x, y] == (11, 222, 33)
            for x in range(0, w, 3) for y in range(0, h, 3)
        ), "the fetched basemap does not appear in the preview"


def test_preview_uses_a_small_tile_budget(project_id):
    """The preview must stay cheap: a handful of tiles, not a print run."""
    calls = []

    def fetcher(z, x, y):
        calls.append((z, x, y))
        return _solid_tile()

    render_poster_preview(project_id, 1, _BODY, tile_fetcher=fetcher)
    assert len(calls) <= _PREVIEW_MAX_TILES, (
        f"preview fetched {len(calls)} tiles, over its {_PREVIEW_MAX_TILES} budget"
    )


def test_preview_degrades_with_a_warning_when_the_basemap_fails(project_id):
    """A basemap failure must not fail the preview outright, and must not be
    silent either — the client needs something to show the user."""
    def boom(z, x, y):
        raise APIError("MAPBOX_TOKEN is not configured")

    png_bytes, warning = render_poster_preview(project_id, 1, _BODY, tile_fetcher=boom)

    with Image.open(io.BytesIO(png_bytes)) as img:  # still a valid image
        assert img.size[0] > 0
    assert warning is not None
    assert "MAPBOX_TOKEN" in warning


def test_preview_degrades_with_a_warning_when_the_basemap_is_too_slow(
    project_id, monkeypatch
):
    """A tile fetch that never errors but runs past the preview's wall-clock
    budget must degrade the same way an outright failure does, rather than
    running until the *client's* HTTP timeout kills the connection (issue
    #14: "the preview failed with a timeout 30s")."""
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "src.poster.tile_stitcher.time.monotonic", lambda: clock["t"]
    )

    def slow_but_successful(z, x, y):
        clock["t"] += _PREVIEW_TOTAL_BUDGET_S + 1.0
        return _solid_tile()

    png_bytes, warning = render_poster_preview(
        project_id, 1, _BODY, tile_fetcher=slow_but_successful
    )

    with Image.open(io.BytesIO(png_bytes)) as img:
        assert img.size[0] > 0
    assert warning is not None
    assert "time budget" in warning


def test_preview_degrades_cards_to_legend_when_over_time_budget(
    project_id, monkeypatch
):
    """A tile-count/basemap budget alone isn't enough: drop-shadow blur and
    photo decode (_draw_card_chrome/_paste_cover) are real per-card cost, and
    a low-DPI preview canvas fits far more tiny cards than that sounds like
    it should allow. That's what actually ran a real preview past the
    client's own timeout even with the basemap alone capped (issue #14
    follow-up) — the wall-clock budget must cover card rendering too, same
    degrade-to-legend behaviour as an outright failure."""
    import src.poster.poster_renderer as renderer_module

    clock = {"t": 0.0}
    monkeypatch.setattr(
        "src.poster.tile_stitcher.time.monotonic", lambda: clock["t"]
    )

    real_place_cards = renderer_module.place_cards

    def place_cards_then_blow_the_budget(*args, **kwargs):
        # Real placement runs on the un-mocked clock (basemap fetch didn't
        # advance it), so this captures a normal placement — then the clock
        # jumps past budget right before card rendering starts, so every
        # card that *would* have drawn is deterministically over budget
        # regardless of how many placement geometry happens to produce.
        result = real_place_cards(*args, **kwargs)
        clock["t"] += _PREVIEW_TOTAL_BUDGET_S + 1.0
        return result

    monkeypatch.setattr(renderer_module, "place_cards", place_cards_then_blow_the_budget)

    calls = []
    real_draw_card = renderer_module._draw_card
    monkeypatch.setattr(
        renderer_module, "_draw_card",
        lambda *a, **k: (calls.append(1), real_draw_card(*a, **k))[-1],
    )

    png_bytes, warning = render_poster_preview(
        project_id, 1, _BODY, tile_fetcher=_fake_tile_fetcher
    )

    with Image.open(io.BytesIO(png_bytes)) as img:  # still a valid image
        assert img.size[0] > 0
    assert calls == [], "a card was drawn after the budget was already spent"
    assert warning is not None
    assert "time budget" in warning
    assert "legend" in warning


def test_preview_never_measures_or_decodes_real_card_content(project_id, monkeypatch):
    """Real measurement (font metrics, and especially photo decode via
    _photo_resolver) is per-memory cost that scales with trip size and photo
    count — for a real trip with real photos this alone ran the preview past
    the client's timeout before it ever reached placement or drawing, even
    with the wall-clock budget from the test above already in place (issue
    #14 follow-up: "the preview keeps failing on real data"). The preview
    must never resolve or measure a memory's photos, however many it has.

    (The trip-summary card's own layout_card call is unaffected and expected
    — it is text-only, no photo decode, and cheap regardless of trip size.)"""
    import src.poster.poster_renderer as renderer_module

    def boom(*args, **kwargs):
        raise AssertionError("preview must not resolve/decode a memory's photos")

    monkeypatch.setattr(renderer_module, "_photo_resolver", boom)

    calls = []
    real_layout_card = renderer_module.layout_card
    monkeypatch.setattr(
        renderer_module, "layout_card",
        lambda *a, **k: (calls.append(1), real_layout_card(*a, **k))[-1],
    )

    body = {
        **_BODY,
        "config": {**_BODY["config"], "hero_photo": True, "all_photos": True},
        "memories": [
            {**m, "photo_uuids": ["abc", "def"]} for m in _BODY["memories"]
        ],
    }
    png_bytes, _ = render_poster_preview(project_id, 1, body, tile_fetcher=_fake_tile_fetcher)
    with Image.open(io.BytesIO(png_bytes)) as img:
        assert img.size[0] > 0
    # The one remaining call is the trip-summary card's own — text-only, no
    # photo decode, unaffected by this guard. Neither memory card reached
    # layout_card at all.
    assert len(calls) == 1, "a memory card was measured with real content in preview"


def test_full_render_still_measures_real_card_content(project_id, tmp_path, monkeypatch):
    """The counterpart: the preview-only guard above must not silently turn
    into a global regression that skips real measurement for the full render
    too — a real poster's cards must still show their real content."""
    import src.poster.poster_renderer as renderer_module

    calls = []
    real_layout_card = renderer_module.layout_card
    monkeypatch.setattr(
        renderer_module, "layout_card",
        lambda *a, **k: (calls.append(1), real_layout_card(*a, **k))[-1],
    )
    render_poster(
        job_id=1, user_info_id=1, project_id=project_id, request=_BODY,
        poster_dir=tmp_path, progress=lambda stage: None, tile_fetcher=_fake_tile_fetcher,
    )
    assert calls, "the full render must still measure real card content"


def test_preview_draws_the_route_with_supersampling(project_id, monkeypatch):
    """render_poster_preview must be the caller that actually asks for
    anti-aliasing -- the unit tests above only prove _draw_route's own
    behaviour, not that the preview requests it."""
    import src.poster.poster_renderer as renderer_module

    calls = []
    real_draw_route = renderer_module._draw_route
    monkeypatch.setattr(
        renderer_module, "_draw_route",
        lambda *a, **k: (calls.append(k.get("supersample")), real_draw_route(*a, **k))[-1],
    )
    render_poster_preview(project_id, 1, _BODY, tile_fetcher=_fake_tile_fetcher)
    assert calls == [_ROUTE_SUPERSAMPLE]


def test_full_render_draws_the_route_without_supersampling(project_id, tmp_path, monkeypatch):
    """The counterpart: the full render must keep the old, unsupersampled
    path -- see _draw_route's docstring for why (memory cost at A0)."""
    import src.poster.poster_renderer as renderer_module

    calls = []
    real_draw_route = renderer_module._draw_route
    monkeypatch.setattr(
        renderer_module, "_draw_route",
        lambda *a, **k: (calls.append(k.get("supersample")), real_draw_route(*a, **k))[-1],
    )
    render_poster(
        job_id=1, user_info_id=1, project_id=project_id, request=_BODY,
        poster_dir=tmp_path, progress=lambda stage: None, tile_fetcher=_fake_tile_fetcher,
    )
    assert calls == [1]


def test_full_render_still_fails_hard_when_the_basemap_fails(project_id, tmp_path):
    """Unlike the preview, the real poster must never be produced on grey:
    a poster without its map is not a useful output (issue #14, PR 1)."""
    def boom(z, x, y):
        raise APIError("Mapbox unreachable")

    with pytest.raises(APIError):
        render_poster(
            job_id=1, user_info_id=1, project_id=project_id, request=_BODY,
            poster_dir=tmp_path, progress=lambda stage: None, tile_fetcher=boom,
        )


def test_preview_shows_pins_scaled_down_but_still_visible(project_id):
    """Regression coverage mirroring the golden-path pin-pixel check above,
    at preview scale — pins/cards must still be visible, just smaller."""
    png_bytes, _ = render_poster_preview(project_id, 1, _BODY)
    with Image.open(io.BytesIO(png_bytes)) as img:
        pixels = img.load()
        w, h = img.size
        assert any(
            pixels[x, y][:3] == _PIN_COLOR
            for x in range(0, w)
            for y in range(0, h)
        ), "no pin-colored pixel found anywhere in the preview"


# ── config.layout dispatch ───────────────────────────────────────────────────
# The perimeter layout (src/poster/perimeter_placement.py) has its own unit
# tests; all that is checked here is that the config flag reaches the right
# function and that leaving it out keeps today's radial placement.

def _record_placement_calls(monkeypatch):
    """Wrap both placement functions so we can see which one the renderer used."""
    calls: list[str] = []

    def wrap(name, fn):
        def wrapped(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)
        return wrapped

    import src.poster.poster_renderer as renderer
    monkeypatch.setattr(renderer, "place_cards", wrap("radial", renderer.place_cards))
    monkeypatch.setattr(renderer, "place_cards_perimeter",
                        wrap("perimeter", renderer.place_cards_perimeter))
    return calls


def test_layout_defaults_to_the_radial_placement(project_id, monkeypatch):
    calls = _record_placement_calls(monkeypatch)
    render_poster_preview(project_id, 1, _BODY)  # config carries no "layout"
    assert calls == ["radial"]


def test_layout_perimeter_dispatches_to_the_perimeter_placement(project_id, monkeypatch):
    calls = _record_placement_calls(monkeypatch)
    body = {**_BODY, "config": {**_BODY["config"], "layout": "perimeter"}}
    render_poster_preview(project_id, 1, body)
    assert calls == ["perimeter"]


# ── _paste_cover failure logging (issue #205, Unit D) ────────────────────────
# _paste_cover used to swallow a failed photo open/decode with a bare
# `except Exception: return`, leaving a blank card slot with nothing in the
# logs to explain a "my poster is missing a photo" report.

def test_paste_cover_logs_warning_and_leaves_blank_slot_on_corrupt_photo(tmp_path, caplog):
    bad_photo = tmp_path / "corrupt.jpg"
    bad_photo.write_bytes(b"not a real image")

    canvas = Image.new("RGB", (200, 200), (10, 20, 30))
    before = canvas.copy()

    with caplog.at_level(logging.WARNING, logger="src.poster.poster_renderer"):
        _paste_cover(canvas, bad_photo, (10, 10, 50, 50))  # must not raise

    # Behavior unchanged: nothing pasted, canvas untouched (blank slot).
    assert canvas.tobytes() == before.tobytes()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log line for the failed cover-photo paste"
    assert any(str(bad_photo) in r.getMessage() for r in warnings)
    # .exception()-style call: traceback must be captured, not just the message.
    assert warnings[0].exc_info is not None


# ── Theming (light/dark card chrome) ─────────────────────────────────────────
# The poster's cards, legend and stat panels follow the app's own floating-card
# treatment (SelectionStatsOverlay in map_panel.dart) rather than being
# hard-coded white. These cover the token values themselves, the role-based
# text colours, and that the theme actually reaches every drawing function.

def _nearest(pixel, *candidates):
    """Which of *candidates* the (r, g, b) *pixel* is closest to."""
    return min(candidates,
               key=lambda c: sum((a - b) ** 2 for a, b in zip(pixel[:3], c)))


class TestThemeSelection:
    def test_light_and_dark_are_selected_by_name(self):
        assert get_theme("light") is LIGHT_THEME
        assert get_theme("dark") is DARK_THEME

    def test_missing_or_unknown_theme_falls_back_to_dark(self):
        """The renderer replays a stored request JSON that may predate the
        field, so an absent/garbage value must not raise."""
        assert get_theme(None) is DARK_THEME
        assert get_theme("") is DARK_THEME
        assert get_theme("sepia") is DARK_THEME

    def test_token_values_match_the_app_surfaces(self):
        assert LIGHT_THEME.card_bg == (0xFF, 0xFF, 0xFF)
        assert DARK_THEME.card_bg == (0x1B, 0x28, 0x38)
        assert LIGHT_THEME.primary_text == (0x33, 0x41, 0x55)   # slate-700
        assert LIGHT_THEME.muted_text == (0x94, 0xA3, 0xB8)     # slate-400
        assert DARK_THEME.primary_text == (0xCB, 0xD5, 0xE1)    # slate-300
        assert DARK_THEME.muted_text == (0x64, 0x74, 0x8B)      # slate-500

    def test_surfaces_are_translucent_over_the_basemap(self):
        for theme in (LIGHT_THEME, DARK_THEME):
            assert theme.card_fill == (*theme.card_bg, 240)  # ~94%

    def test_shadow_drops_downwards_and_is_softer_in_light_mode(self):
        assert LIGHT_THEME.shadow_fill == (0x0F, 0x22, 0x36, 0x2E)
        assert DARK_THEME.shadow_fill == (0x00, 0x00, 0x00, 0x99)
        for theme in (LIGHT_THEME, DARK_THEME):
            assert theme.shadow_offset_mm > 0  # downwards, like kShadow2's (0, 8)
            assert theme.shadow_blur_mm > theme.shadow_offset_mm

    def test_geometry_is_identical_in_both_themes(self):
        """Only colour is theme-dependent; a card is the same shape either way."""
        assert LIGHT_THEME.radius_mm == DARK_THEME.radius_mm
        assert LIGHT_THEME.shadow_blur_mm == DARK_THEME.shadow_blur_mm
        assert LIGHT_THEME.shadow_offset_mm == DARK_THEME.shadow_offset_mm


class TestThemedTextColors:
    def test_roles_resolve_to_the_theme_s_own_colors(self):
        for theme in (LIGHT_THEME, DARK_THEME):
            assert theme.text_color(TYPE_SCALE["body"]) == theme.primary_text
            assert theme.text_color(TYPE_SCALE["stat_value"]) == theme.primary_text
            assert theme.text_color(TYPE_SCALE["label"]) == theme.muted_text

    def test_the_date_accent_is_the_brand_orange_in_both_themes(self):
        """The accent is a brand colour, not a surface-dependent one."""
        assert ACCENT == (0xFC, 0x4C, 0x02)
        assert LIGHT_THEME.text_color(TYPE_SCALE["date"]) == ACCENT
        assert DARK_THEME.text_color(TYPE_SCALE["date"]) == ACCENT
        assert DARK_THEME.text_color(TYPE_SCALE["legend_index"]) == ACCENT

    def test_a_style_with_no_theme_role_keeps_its_own_color(self):
        """The pin index is drawn over a pin, not over a card surface."""
        assert DARK_THEME.text_color(TYPE_SCALE["pin_index"]) == (255, 255, 255)
        assert LIGHT_THEME.text_color(TYPE_SCALE["pin_index"]) == (255, 255, 255)


class TestThemedDrawing:
    BACKGROUND = (200, 60, 140)  # deliberately unlike either card surface

    def _canvas(self, size=(520, 340)):
        return Image.new("RGBA", size, (*self.BACKGROUND, 255))

    @pytest.mark.parametrize("theme", [LIGHT_THEME, DARK_THEME])
    def test_card_chrome_paints_the_theme_s_surface(self, theme):
        canvas = self._canvas()
        _draw_card_chrome(canvas, Rect(60, 50, 460, 290), 150.0, theme)
        centre = canvas.load()[260, 170]
        assert _nearest(centre, theme.card_bg, self.BACKGROUND) == theme.card_bg

    @pytest.mark.parametrize("theme", [LIGHT_THEME, DARK_THEME])
    def test_the_surface_lets_the_basemap_show_through(self, theme):
        """94%, not 100% — a fully opaque rounded_rectangle would replace the
        pixels outright and the map would vanish under every card."""
        canvas = self._canvas()
        _draw_card_chrome(canvas, Rect(60, 50, 460, 290), 150.0, theme)
        assert canvas.load()[260, 170][:3] != theme.card_bg

    def test_the_two_themes_produce_different_chrome(self):
        images = []
        for theme in (LIGHT_THEME, DARK_THEME):
            canvas = self._canvas()
            _draw_card_chrome(canvas, Rect(60, 50, 460, 290), 150.0, theme)
            images.append(canvas.tobytes())
        assert images[0] != images[1]

    def test_card_chrome_survives_a_card_at_the_canvas_edge(self):
        """The blurred shadow tile hangs off the canvas; compositing it must
        still work (it is cropped, not clamped)."""
        canvas = self._canvas()
        _draw_card_chrome(canvas, Rect(0, 0, 120, 90), 150.0, DARK_THEME)
        assert _nearest(canvas.load()[60, 45], DARK_THEME.card_bg,
                        self.BACKGROUND) == DARK_THEME.card_bg

    @pytest.mark.parametrize("theme", [LIGHT_THEME, DARK_THEME])
    def test_card_text_is_drawn_in_the_theme_s_primary_color(self, theme):
        scale = TypeScale(150.0)
        layout = layout_card([{"kind": "name", "text": "Day 1"}], scale)
        canvas = self._canvas((layout.width + 200, layout.height + 200))
        _draw_card(canvas, Rect(100, 100, 100 + layout.width, 100 + layout.height),
                   layout, scale, theme)

        pixels = canvas.load()
        w, h = canvas.size
        assert any(pixels[x, y][:3] == theme.primary_text
                   for x in range(w) for y in range(h)), (
            "no pixel of the theme's primary text colour found on the card")

    def test_stat_panel_rules_use_the_theme_s_divider(self):
        scale = TypeScale(150.0)
        blocks = [{"kind": "name", "text": "Day 1"},
                  {"kind": "distance", "value_m": 84_210.0}]
        layout = layout_card(blocks, scale)
        found = {}
        for theme in (LIGHT_THEME, DARK_THEME):
            canvas = self._canvas((layout.width + 200, layout.height + 200))
            _draw_card(canvas, Rect(100, 100, 100 + layout.width, 100 + layout.height),
                       layout, scale, theme)
            pixels = canvas.load()
            w, h = canvas.size
            found[theme.name] = any(pixels[x, y][:3] == theme.divider
                                    for x in range(w) for y in range(h))
        assert found == {"light": True, "dark": True}

    @pytest.mark.parametrize("theme", [LIGHT_THEME, DARK_THEME])
    def test_the_legend_uses_the_same_themed_chrome_as_a_card(self, theme):
        canvas = self._canvas((900, 600))
        entries = [{"id": 7, "name": "Overflowed day", "date": "2024-06-01"}]
        _draw_legend(canvas, entries, {7: (400.0, 300.0)},
                     TypeScale(150.0), 900, 600, theme)

        pixels = canvas.load()
        # Bottom-left corner, where the legend box is anchored.
        assert any(
            _nearest(pixels[x, y], theme.card_bg, self.BACKGROUND) == theme.card_bg
            for x in range(60, 400) for y in range(400, 600)
        ), "the legend box was not painted in the theme's surface colour"

    def test_the_legend_index_is_drawn_in_the_brand_accent(self):
        canvas = self._canvas((900, 600))
        entries = [{"id": 7, "name": "Overflowed day", "date": "2024-06-01"}]
        _draw_legend(canvas, entries, {7: (400.0, 300.0)},
                     TypeScale(150.0), 900, 600, DARK_THEME)
        pixels = canvas.load()
        assert any(pixels[x, y][:3] == ACCENT
                   for x in range(900) for y in range(600))


class TestThemeThreadedThroughTheRender:
    """The theme has to survive the whole path: request config -> renderer ->
    every drawing function. These render the real (small) preview rather than
    poking at internals."""

    def _preview(self, project_id, theme_name):
        body = {**_BODY, "config": {**_BODY["config"], "theme": theme_name}}
        png_bytes, _ = render_poster_preview(project_id, 1, body)
        return Image.open(io.BytesIO(png_bytes)).convert("RGB")

    def test_light_and_dark_requests_render_differently(self, project_id):
        light = self._preview(project_id, "light")
        dark = self._preview(project_id, "dark")
        assert light.tobytes() != dark.tobytes()

    @pytest.mark.parametrize("theme", [LIGHT_THEME, DARK_THEME])
    def test_cards_are_painted_in_the_requested_theme(self, project_id, theme):
        image = self._preview(project_id, theme.name)
        pixels = image.load()
        w, h = image.size
        other = LIGHT_THEME if theme is DARK_THEME else DARK_THEME
        assert any(
            _nearest(pixels[x, y], theme.card_bg, other.card_bg,
                     _MISSING_BASEMAP_COLOR) == theme.card_bg
            for x in range(w) for y in range(h)
        ), f"no {theme.name} card surface found in the rendered poster"

    def test_a_request_without_a_theme_still_renders_dark(self, project_id):
        """Defaulting happens in the renderer as well as in the API model, so
        an older stored job request keeps working."""
        config = {k: v for k, v in _BODY["config"].items() if k != "theme"}
        png_bytes, _ = render_poster_preview(
            project_id, 1, {**_BODY, "config": config})
        default = Image.open(io.BytesIO(png_bytes)).convert("RGB").tobytes()
        assert default == self._preview(project_id, "dark").tobytes()


# ── Trip day numbering ────────────────────────────────────────────────────────
# The app's canonical formula (dayTripNumbering in project_notifier.dart):
# day 1 is the project's explicit trip_start override when set, otherwise the
# earliest dated thing in the trip; rest days in between still count.

def _dated_activity(make_activity, day: str, **kwargs):
    dt = datetime.fromisoformat(f"{day}T09:00:00")
    return make_activity(start_date=dt, start_date_local=dt, **kwargs)


class TestTripDayNumbering:
    def test_day_one_is_the_earliest_activity(self, make_activity):
        project = Project(name="T", activities=[
            _dated_activity(make_activity, "2024-06-05", id=2),
            _dated_activity(make_activity, "2024-06-01", id=1),
        ])
        start, end = _trip_span(project)
        assert (start, end) == ("2024-06-01", "2024-06-05")
        assert _day_number("2024-06-01", start) == 1
        assert _day_number("2024-06-05", start) == 5

    def test_rest_days_between_dated_items_still_count(self, make_activity):
        """Numbering is calendar arithmetic over min/max, not a position in a
        list — a week off in the middle of a trip does not renumber the days
        after it."""
        project = Project(name="T", activities=[
            _dated_activity(make_activity, "2024-06-01", id=1),
            _dated_activity(make_activity, "2024-06-10", id=2),
        ])
        start, _ = _trip_span(project)
        assert _day_number("2024-06-10", start) == 10

    def test_an_explicit_trip_start_overrides_the_earliest_item(self, make_activity):
        project = Project(
            name="T",
            trip_start="2024-05-30",
            activities=[_dated_activity(make_activity, "2024-06-01", id=1)],
        )
        start, _ = _trip_span(project)
        assert start == "2024-05-30"
        assert _day_number("2024-06-01", start) == 3

    def test_memories_and_journal_entries_extend_the_span(self, make_activity):
        project = Project(
            name="T",
            activities=[_dated_activity(make_activity, "2024-06-05", id=1)],
            memories=[Memory(id=1, date="2024-06-02")],
            journal_entries=[JournalEntry(id=1, date="2024-06-09")],
        )
        assert _trip_span(project) == ("2024-06-02", "2024-06-09")

    def test_an_explicit_trip_end_overrides_the_latest_item(self, make_activity):
        project = Project(
            name="T",
            trip_end="2024-06-30",
            activities=[_dated_activity(make_activity, "2024-06-01", id=1)],
        )
        assert _trip_span(project)[1] == "2024-06-30"

    def test_a_trip_with_no_dates_has_no_span_and_no_day_numbers(self):
        assert _trip_span(Project(name="T")) == (None, None)
        assert _day_number("2024-06-01", None) is None
        assert _day_number(None, "2024-06-01") is None
        assert _day_number("not a date", "2024-06-01") is None

    def test_the_badge_reaches_a_rendered_card(self, make_activity):
        """End to end through the renderer's own per-memory assembly: a memory
        on the trip's second day gets a "Day 2" badge."""
        project = Project(name="T", activities=[
            _dated_activity(make_activity, "2024-06-01", id=1),
        ])
        start, _ = _trip_span(project)
        blocks = assemble_card_content(
            _ALL_FLAGS, {**_MEMORY, "date": "2024-06-02"}, _METRICS,
            day_number=_day_number("2024-06-02", start),
        )
        assert blocks[0] == {"kind": "day_badge", "day_number": 2}


# ── Bug 1: a memory with no content must never get a card ────────────────────
# With every flag off, assemble_card_content returns [] — but the pipeline
# still built a PinSpec for it, placed it, and drew an empty rounded card with
# a leader line pointing at nothing. Such a memory now skips card placement
# entirely and goes to the legend, exactly like an overflowed pin.

_BLANK_BODY = {
    **_BODY,
    "config": {**{k: False for k in _ALL_FLAGS}, "theme": "dark"},
}


def _spy_pipeline(monkeypatch):
    """Record the pins offered to placement, the legend entries, and the cards
    actually drawn during one render."""
    import src.poster.poster_renderer as renderer_module

    seen = {"pins": None, "legend": [], "cards": 0}

    real_place = renderer_module.place_cards

    def place_spy(pins, *args, **kwargs):
        seen["pins"] = list(pins)
        return real_place(pins, *args, **kwargs)

    real_legend = renderer_module._draw_legend

    def legend_spy(canvas, entries, *args, **kwargs):
        seen["legend"] = list(entries)
        return real_legend(canvas, entries, *args, **kwargs)

    real_card = renderer_module._draw_card

    def card_spy(*args, **kwargs):
        seen["cards"] += 1
        return real_card(*args, **kwargs)

    monkeypatch.setattr(renderer_module, "place_cards", place_spy)
    monkeypatch.setattr(renderer_module, "_draw_legend", legend_spy)
    monkeypatch.setattr(renderer_module, "_draw_card", card_spy)
    return seen


class TestContentLessMemoriesGetNoCard:
    def test_no_pin_spec_no_card_and_a_legend_entry(self, project_id, monkeypatch):
        seen = _spy_pipeline(monkeypatch)
        render_poster_preview(project_id, 1, _BLANK_BODY,
                              tile_fetcher=_fake_tile_fetcher)

        assert seen["pins"] == [], "a content-less memory reached card placement"
        assert seen["cards"] == 0, "an empty card was drawn"
        assert {m["id"] for m in seen["legend"]} == {1, 2}

    def test_the_pin_marker_is_still_drawn(self, project_id):
        """Only the *card* is skipped — the memory is still a place on the map."""
        png_bytes, _ = render_poster_preview(
            project_id, 1, _BLANK_BODY, tile_fetcher=_fake_tile_fetcher)
        with Image.open(io.BytesIO(png_bytes)) as img:
            pixels = img.convert("RGB").load()
            w, h = img.size
            assert any(pixels[x, y] == _PIN_COLOR
                       for x in range(w) for y in range(h)), (
                "the content-less memory lost its pin marker as well as its card")

    def test_a_memory_with_content_still_gets_a_card(self, project_id, monkeypatch):
        """The counterpart: the skip must key off emptiness, not fire always."""
        seen = _spy_pipeline(monkeypatch)
        render_poster_preview(project_id, 1, _BODY, tile_fetcher=_fake_tile_fetcher)

        assert {p.id for p in seen["pins"]} == {1, 2}
        assert seen["cards"] > 0


# ── The trip summary card ────────────────────────────────────────────────────
# One card for the whole poster: the trip's own title, period and totals.

def _summary_project(make_activity):
    return Project(
        name="Alps 2024",
        activities=[
            _dated_activity(make_activity, "2024-06-01", id=1,
                            distance=84_210.0, total_elevation_gain=1_200.0),
            _dated_activity(make_activity, "2024-06-03", id=2,
                            distance=15_790.0, total_elevation_gain=300.0),
        ],
    )


class TestTripSummaryBlocks:
    def test_it_carries_the_title_period_and_both_totals(self, make_activity):
        project = _summary_project(make_activity)
        blocks = _trip_summary_blocks(project, _trip_span(project))
        by_kind = {b["kind"]: b for b in blocks}

        assert [b["kind"] for b in blocks] == ["name", "date", "distance", "elevation"]
        assert by_kind["name"]["text"] == "Alps 2024"
        assert by_kind["date"]["text"] == "1 Jun 2024 – 3 Jun 2024"
        # Summed across ALL the trip's activities, not one day's worth.
        assert by_kind["distance"]["value_m"] == pytest.approx(100_000.0)
        assert by_kind["elevation"]["value_m"] == pytest.approx(1_500.0)

    def test_the_period_is_omitted_when_the_trip_has_no_dates(self):
        blocks = _trip_summary_blocks(Project(name="Undated"), (None, None))
        assert [b["kind"] for b in blocks] == ["name", "distance", "elevation"]

    def test_an_explicit_trip_start_shows_in_the_period(self, make_activity):
        project = _summary_project(make_activity)
        project.trip_start = "2024-05-30"
        blocks = _trip_summary_blocks(project, _trip_span(project))
        assert blocks[1]["text"] == "30 May 2024 – 3 Jun 2024"

    def test_totals_reuse_the_per_card_km_and_m_formatting(self, make_activity):
        """Same block kinds as a memory card, so the units come out of the same
        code — a summary reading "100 km" while a card reads "100000 m" would
        be the poster contradicting itself."""
        project = _summary_project(make_activity)
        layout = layout_card(_trip_summary_blocks(project, _trip_span(project)),
                             TypeScale(150.0))
        texts = [op.text for op in layout.ops if hasattr(op, "text")]
        assert "100 km" in texts
        assert "1,500 m" in texts
        assert "Alps 2024" in texts


class TestTripSummaryRendering:
    def _summary_calls(self, project_id, monkeypatch, config_extra):
        import src.poster.poster_renderer as renderer_module

        calls = []
        real = renderer_module._draw_trip_summary

        def spy(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(renderer_module, "_draw_trip_summary", spy)
        body = {**_BODY, "config": {**_BODY["config"], **config_extra}}
        render_poster_preview(project_id, 1, body, tile_fetcher=_fake_tile_fetcher)
        return calls

    def test_drawn_when_the_flag_is_on(self, project_id, monkeypatch):
        assert self._summary_calls(project_id, monkeypatch, {"trip_summary": True})

    def test_drawn_by_default_when_the_request_omits_the_flag(self, project_id, monkeypatch):
        """A real feature default, so a stored request predating the field
        still gets the card."""
        assert self._summary_calls(project_id, monkeypatch, {})

    def test_not_drawn_when_the_flag_is_off(self, project_id, monkeypatch):
        assert self._summary_calls(project_id, monkeypatch, {"trip_summary": False}) == []

    def test_it_sits_in_the_top_left_corner_sized_to_its_content(
        self, project_id, monkeypatch
    ):
        """It has no pin and no leader line, so it is pinned to the corner by
        the same page margin the legend uses bottom-left, rather than being
        placed by the pin-based search."""
        import src.poster.poster_renderer as renderer_module

        rects = []
        real_chrome = renderer_module._draw_card_chrome
        real_summary = renderer_module._draw_trip_summary

        def summary_spy(*args, **kwargs):
            def chrome_spy(canvas, rect, dpi, theme):
                rects.append(rect)
                return real_chrome(canvas, rect, dpi, theme)

            monkeypatch.setattr(renderer_module, "_draw_card_chrome", chrome_spy)
            try:
                return real_summary(*args, **kwargs)
            finally:
                monkeypatch.setattr(renderer_module, "_draw_card_chrome", real_chrome)

        monkeypatch.setattr(renderer_module, "_draw_trip_summary", summary_spy)
        render_poster_preview(project_id, 1, _BODY, tile_fetcher=_fake_tile_fetcher)

        assert len(rects) == 1
        rect = rects[0]
        ratio = _PREVIEW_MAX_DIMENSION / max(*_target_size("landscape"))
        dpi = 150.0 * ratio
        margin = mm_to_px(_PAGE_MARGIN_MM, dpi)
        assert (rect.left, rect.top) == (margin, margin)
        assert rect.right - rect.left == card_width_px(dpi)
        assert rect.bottom - rect.top > 0  # sized to content, not a fixed box


class TestTripSummaryTitleOverrides:
    """title_position / title_text / title_scale (title-config feature): the
    trip-summary card's position, text and size are all overridable from the
    top-level request, threaded through before card placement runs so memory
    cards can avoid the resolved rect (see TestTitleAvoidsMemoryCards below)."""

    def _spy_on_summary(self, monkeypatch):
        """Wraps _draw_trip_summary and records every (rect, layout) it was
        called with, still calling through to the real implementation."""
        import src.poster.poster_renderer as renderer_module

        calls = []
        real_summary = renderer_module._draw_trip_summary

        def spy(canvas, rect, layout, scale, theme):
            calls.append((rect, layout))
            return real_summary(canvas, rect, layout, scale, theme)

        monkeypatch.setattr(renderer_module, "_draw_trip_summary", spy)
        return calls

    def test_omitted_title_fields_render_byte_for_byte_identical(self, project_id):
        """The regression bar: a request shaped like it predates this feature
        (no title_position/title_text/title_scale keys at all) must render
        pixel-for-pixel the same as one carrying the new fields at the values
        that are supposed to be their defaults."""
        new_style = {
            **_BODY,
            "title_position": {"x": 0.0, "y": 0.0},
            "title_text": None,
            "title_scale": 1.0,
        }
        old_png, old_warning = render_poster_preview(
            project_id, 1, _BODY, tile_fetcher=_fake_tile_fetcher)
        new_png, new_warning = render_poster_preview(
            project_id, 1, new_style, tile_fetcher=_fake_tile_fetcher)
        assert old_png == new_png
        assert old_warning == new_warning

    def test_title_position_moves_the_card_off_the_corner(self, project_id, monkeypatch):
        calls = self._spy_on_summary(monkeypatch)
        body = {**_BODY, "title_position": {"x": 0.5, "y": 0.5}}
        render_poster_preview(project_id, 1, body, tile_fetcher=_fake_tile_fetcher)

        ratio = _PREVIEW_MAX_DIMENSION / max(*_target_size("landscape"))
        dpi = 150.0 * ratio
        margin = mm_to_px(_PAGE_MARGIN_MM, dpi)
        assert len(calls) == 1
        rect, _layout = calls[0]
        assert rect.left > margin
        assert rect.top > margin

    def test_title_position_is_clamped_to_the_poster(self, project_id, monkeypatch):
        """An out-of-[0,1] position (a stale/adversarial request) must not
        push the card outside the page margin."""
        calls = self._spy_on_summary(monkeypatch)
        body = {**_BODY, "title_position": {"x": 5.0, "y": -5.0}}
        render_poster_preview(project_id, 1, body, tile_fetcher=_fake_tile_fetcher)

        ratio = _PREVIEW_MAX_DIMENSION / max(*_target_size("landscape"))
        dpi = 150.0 * ratio
        margin = mm_to_px(_PAGE_MARGIN_MM, dpi)
        target_w, target_h = [round(d * ratio) for d in _target_size("landscape")]
        rect, _layout = calls[0]
        assert rect.left >= margin and rect.top >= margin
        assert rect.right <= target_w - margin
        assert rect.bottom <= target_h - margin

    def test_title_text_overrides_the_project_name(self, project_id, monkeypatch):
        calls = self._spy_on_summary(monkeypatch)
        body = {**_BODY, "title_text": "Alps Only"}
        render_poster_preview(project_id, 1, body, tile_fetcher=_fake_tile_fetcher)

        _rect, layout = calls[0]
        texts = [op.text for op in layout.ops if hasattr(op, "text")]
        assert "Alps Only" in texts
        assert "My Trip" not in texts  # the project's own name, overridden

    def test_no_title_text_falls_back_to_the_project_name(self, project_id, monkeypatch):
        calls = self._spy_on_summary(monkeypatch)
        render_poster_preview(project_id, 1, _BODY, tile_fetcher=_fake_tile_fetcher)

        _rect, layout = calls[0]
        texts = [op.text for op in layout.ops if hasattr(op, "text")]
        assert "My Trip" in texts

    def test_title_scale_changes_the_rendered_output(self, project_id):
        small_png, _ = render_poster_preview(
            project_id, 1, {**_BODY, "title_scale": 0.5}, tile_fetcher=_fake_tile_fetcher)
        big_png, _ = render_poster_preview(
            project_id, 1, {**_BODY, "title_scale": 2.0}, tile_fetcher=_fake_tile_fetcher)
        assert small_png != big_png

    def test_title_scale_is_clamped_regardless_of_the_caller(self, project_id, monkeypatch):
        """Defence in depth: api/poster.py's validator is the primary trust
        boundary, but a request dict reaching the renderer directly (a test,
        a stale request_json row) must still be clamped, not trusted."""
        import src.poster.poster_renderer as renderer_module
        from src.poster.typography import TYPE_SCALE

        seen_sizes = []
        real_layout = renderer_module._trip_summary_layout

        def layout_spy(project, span, scale, title_text):
            seen_sizes.append(scale.style("hero_title").size_pt)
            return real_layout(project, span, scale, title_text)

        monkeypatch.setattr(renderer_module, "_trip_summary_layout", layout_spy)
        render_poster_preview(
            project_id, 1, {**_BODY, "title_scale": 999.0}, tile_fetcher=_fake_tile_fetcher)

        base = TYPE_SCALE["hero_title"].size_pt
        assert seen_sizes[0] == pytest.approx(base * 2.0)  # clamped to 2.0x, not 999x

    def test_memory_cards_avoid_the_title_rect(self, project_id, monkeypatch):
        """Proves the renderer actually wires the resolved title rect into
        place_cards as an obstacle; the avoidance geometry itself is covered
        directly in tests/test_card_placement.py."""
        import src.poster.poster_renderer as renderer_module

        captured = {}
        real_place = renderer_module.place_cards

        def spy(*args, **kwargs):
            captured["obstacles"] = kwargs.get("obstacles")
            return real_place(*args, **kwargs)

        monkeypatch.setattr(renderer_module, "place_cards", spy)
        render_poster_preview(project_id, 1, _BODY, tile_fetcher=_fake_tile_fetcher)

        assert captured["obstacles"] and len(list(captured["obstacles"])) == 1

    def test_no_title_obstacle_when_trip_summary_is_off(self, project_id, monkeypatch):
        import src.poster.poster_renderer as renderer_module

        captured = {}
        real_place = renderer_module.place_cards

        def spy(*args, **kwargs):
            captured["obstacles"] = kwargs.get("obstacles")
            return real_place(*args, **kwargs)

        monkeypatch.setattr(renderer_module, "place_cards", spy)
        body = {**_BODY, "config": {**_BODY["config"], "trip_summary": False}}
        render_poster_preview(project_id, 1, body, tile_fetcher=_fake_tile_fetcher)

        assert not captured["obstacles"]

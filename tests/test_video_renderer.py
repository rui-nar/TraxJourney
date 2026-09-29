"""Trip video frame renderer and encoder (docs/TRIP_VIDEO_PLAN.md, U5).

Every render uses a fake tile fetcher, so nothing touches Mapbox. The tests
that encode need ``ffmpeg``/``ffprobe``: CI installs them, a dev machine
without them skips those tests (never on CI, where ``CI`` is set).

Golden frames live in ``tests/golden/video/``; regenerate them with
``VIDEO_UPDATE_GOLDEN=1`` after an intended visual change, on Linux (CI's
platform), and look at them before committing.

The 1080p benchmark runs only with ``VIDEO_BENCH=1``::

    VIDEO_BENCH=1 pytest tests/test_video_renderer.py -k benchmark -s
"""
from __future__ import annotations

import functools
import io
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import polyline as polyline_lib
import pytest
from PIL import Image, ImageChops, ImageDraw
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
import src.video.renderer as renderer
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.models.activity import Activity
from src.models.project import ConnectingSegment, Project, ProjectItem, SegmentEndpoint
from src.video import job_runner
from src.video.basemap_bands import (
    PIXEL_RATIO,
    Basemaps,
    band_weights,
    plan_bands,
    view_rect,
)
from src.video.camera import CAMERA_MODES, TILE_SIZE, camera_path, fixed_zoom, lonlat_to_world
from src.video.legs import build_legs
from src.video.renderer import (
    FrameRenderer,
    VideoEncodeError,
    render_timeline,
    render_video,
)
from src.video.timeline import NothingToAnimate, build_timeline

GOLDEN_DIR = Path(__file__).parent / "golden" / "video"
SMALL = (320, 180)

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(
    not _HAVE_FFMPEG and not os.environ.get("CI"),
    reason="ffmpeg/ffprobe not installed (CI installs them and never skips)")


# ── fakes and builders ───────────────────────────────────────────────────────

def tile_color(z: int, x: int, y: int):
    """Each tile's own solid colour, so a pixel says which tile it came from."""
    return ((x * 67 + z * 29) % 200 + 30, (y * 101 + z * 53) % 200 + 30,
            ((x + y) * 37 + z * 11) % 200 + 30)


@functools.lru_cache(maxsize=None)
def fake_tile(z: int, x: int, y: int) -> bytes:
    """A 1× tile: its solid colour, a dark border and a white cross."""
    img = Image.new("RGB", (TILE_SIZE, TILE_SIZE), tile_color(z, x, y))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, TILE_SIZE - 1, TILE_SIZE - 1), outline=(0, 0, 0), width=6)
    draw.line((256, 216, 256, 296), fill=(255, 255, 255), width=6)
    draw.line((216, 256, 296, 256), fill=(255, 255, 255), width=6)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _activity(id, type, day, points, distance, moving_time) -> Activity:
    when = datetime(day.year, day.month, day.day, 9, 0)
    return Activity(
        id=id, name=f"act {id}", type=type, distance=distance,
        moving_time=moving_time, elapsed_time=moving_time,
        total_elevation_gain=0.0, start_date=when, start_date_local=when,
        timezone="UTC", achievement_count=0, kudos_count=0, comment_count=0,
        athlete_count=1, photo_count=0, trainer=False, commute=False,
        manual=False, private=False, flagged=False, average_speed=0.0,
        max_speed=0.0, pr_count=0, total_photo_count=0,
        has_kudoed=False, summary_polyline=polyline_lib.encode(points),
        start_latlng=list(points[0]), end_latlng=list(points[-1]))


RIDE = [(48.85 + 0.01 * math.sin(i / 3), 2.25 + 0.01 * i) for i in range(25)]
HIKE = [(45.76 + 0.004 * i, 4.84 + 0.003 * math.cos(i / 2)) for i in range(20)]


def paris_lyon() -> Project:
    """A ride in Paris, a train to Lyon, a hike there."""
    ride = _activity(1, "Ride", date(2026, 5, 1), RIDE, 20_000.0, 4000)
    hike = _activity(2, "Hike", date(2026, 5, 2), HIKE, 8_000.0, 7200)
    train = ConnectingSegment(id="s1", segment_type="train",
                              start=SegmentEndpoint(48.85, 2.49),
                              end=SegmentEndpoint(45.76, 4.84), date="2026-05-02")
    return Project(name="Paris to Lyon", activities=[ride, hike], items=[
        ProjectItem(item_type="activity", activity_id=1),
        ProjectItem(item_type="segment", segment=train),
        ProjectItem(item_type="activity", activity_id=2)])


@functools.lru_cache(maxsize=None)
def timeline30():
    return build_timeline(build_legs(paris_lyon()), 30.0)


PARIS_WALK_1 = [(48.8566 + 0.0004 * i, 2.3522 + 0.0003 * i) for i in range(15)]
PARIS_WALK_2 = [(48.8606 + 0.0004 * i, 2.3602 + 0.0003 * i) for i in range(15)]
NY_WALK_1 = [(40.7580 + 0.0004 * i, -73.9855 + 0.0003 * i) for i in range(15)]
NY_WALK_2 = [(40.7620 + 0.0004 * i, -73.9775 + 0.0003 * i) for i in range(15)]


def paris_new_york() -> Project:
    """Short walks in Paris, a flight to New York, short walks there. The
    flight is fast at ``fixed``'s zoom Z (docs/VIDEO_CAMERA_QUALITY_PLAN.md
    D3): ``fixed`` flies over it and keeps the walks' Z, ``fixed_strict``
    drops to a lower Z that follows it too."""
    w1 = _activity(1, "Walk", date(2026, 5, 1), PARIS_WALK_1, 800.0, 600)
    w2 = _activity(2, "Walk", date(2026, 5, 1), PARIS_WALK_2, 800.0, 600)
    flight = ConnectingSegment(id="f1", segment_type="flight",
                               start=SegmentEndpoint(48.86, 2.35),
                               end=SegmentEndpoint(40.71, -73.98), date="2026-05-02")
    w3 = _activity(3, "Walk", date(2026, 5, 3), NY_WALK_1, 800.0, 600)
    w4 = _activity(4, "Walk", date(2026, 5, 3), NY_WALK_2, 800.0, 600)
    return Project(name="Paris to New York", activities=[w1, w2, w3, w4], items=[
        ProjectItem(item_type="activity", activity_id=1),
        ProjectItem(item_type="activity", activity_id=2),
        ProjectItem(item_type="segment", segment=flight),
        ProjectItem(item_type="activity", activity_id=3),
        ProjectItem(item_type="activity", activity_id=4)])


@functools.lru_cache(maxsize=None)
def timeline30_ny():
    return build_timeline(build_legs(paris_new_york()), 30.0)


def follow_window(timeline, fps=30, frames=60):
    """*frames* frames inside the first clip, after its fly-in: the camera
    follows the ride."""
    start = int(math.ceil((timeline.clips[0].start_s + 1.0) * fps))
    assert (start + frames) / fps < timeline.clips[0].end_s - 0.7
    return range(start, start + frames)


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=codec_name,pix_fmt,width,height,nb_read_frames,r_frame_rate",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)


# ── bands ────────────────────────────────────────────────────────────────────

def test_integer_zoom_draws_from_one_band():
    """The camera's mode floors are integers: a clip followed at its floor
    must not pay for two bands."""
    assert band_weights(12.0) == [(12, 1.0)]
    assert band_weights(11.5) == [(11, 1.0)]


def test_near_the_next_level_the_bands_cross_fade():
    layers = band_weights(11.9)
    assert [b for b, _ in layers] == [11, 12]
    assert sum(w for _, w in layers) == pytest.approx(1.0)
    # The fade is continuous: it starts at 0 and ends at the next band alone.
    assert band_weights(11.8 + 1e-9)[0] == (11, pytest.approx(1.0))
    assert band_weights(11.9999999)[-1] == (12, pytest.approx(1.0, abs=1e-5))


def test_a_band_cap_draws_higher_zooms_from_it():
    assert band_weights(14.95, max_band=12) == [(12, 1.0)]


def test_every_frame_is_covered_by_its_sheets():
    tl = timeline30()
    shots = camera_path(tl, 30, SMALL)
    plan = plan_bands(shots, SMALL)
    assert len(plan.frames) == len(shots)
    for n, refs in enumerate(plan.frames):
        assert sum(w for _, w in refs) == pytest.approx(1.0)
        for i, _ in refs:
            s = plan.sheets[i]
            assert s.first <= n <= s.last
            world = TILE_SIZE * PIXEL_RATIO * 2 ** s.band
            l, t, r, b = view_rect(shots[n], s.band, SMALL)
            assert s.x0 <= max(0, l) and s.y0 <= max(0, t)
            assert s.x1 >= min(world, r) and s.y1 >= min(world, b)


def test_over_budget_lowers_the_basemap_zoom_not_the_camera():
    tl = timeline30()
    shots = camera_path(tl, 30, (1280, 720))
    free = plan_bands(shots, (1280, 720), max_tiles=10**9)
    budget = free.tiles // 3
    capped = plan_bands(shots, (1280, 720), max_tiles=budget)
    assert free.max_band is None
    assert capped.max_band is not None
    assert capped.tiles <= budget
    assert max(s.band for s in capped.sheets) <= capped.max_band
    # The camera path is the same object: framing never depends on the budget.
    assert camera_path(tl, 30, (1280, 720)) == shots


def test_capped_render_still_renders():
    frames = FrameRenderer(timeline30(), SMALL, "Trip", tile_fetcher=fake_tile, max_tiles=5)
    assert frames.plan.tiles > 0
    assert frames.frame(follow_window(timeline30())[0]).size == SMALL


def test_sheets_are_dropped_after_their_last_frame():
    frames = FrameRenderer(timeline30(), SMALL, "Trip", tile_fetcher=fake_tile)
    most = 0
    for n in range(len(frames)):
        frames.basemap(n)
        live = frames.basemaps._images
        assert all(frames.plan.sheets[i].last > n for i in live)
        most = max(most, len(live))
    assert most <= 6


# ── frames ───────────────────────────────────────────────────────────────────

def test_follow_camera_moves_the_basemap():
    """Frames 0 and 59 of a 2 s follow window show the map at different
    offsets, and each shows the tile the camera says is under a fixed world
    point where the camera says it is."""
    tl = timeline30()
    window = follow_window(tl)
    frames = FrameRenderer(tl, SMALL, "Trip", tile_fetcher=fake_tile)
    first, last = window[0], window[-1]
    imgs = {n: frames.basemap(n) for n in window}
    a, b = imgs[first], imgs[last]
    assert ImageChops.difference(a, b).getbbox() is not None

    band = frames.plan.sheets[frames.plan.frames[first][0][0]].band
    assert [frames.plan.sheets[i].band for i, _ in frames.plan.frames[last]] == [band]
    tiles = 2 ** band

    def screen(n, px, py):
        s = frames.shots[n]
        cx, cy = lonlat_to_world(s.lon, s.lat)
        scale = TILE_SIZE * 2 ** s.zoom
        return SMALL[0] / 2 + (px - cx) * scale, SMALL[1] / 2 + (py - cy) * scale

    def clear(f):  # away from the tile's border and its central cross
        return 0.1 < f < 0.4 or 0.6 < f < 0.9

    # A world point on screen in both frames, well inside one tile's plain
    # colour: searched on a grid around the first frame's centre.
    s0 = frames.shots[first]
    cx, cy = lonlat_to_world(s0.lon, s0.lat)
    scale = TILE_SIZE * 2 ** s0.zoom
    point = None
    for dx in range(-100, 101, 10):
        for dy in range(-60, 61, 10):
            px, py = cx + dx / scale, cy + dy / scale
            inside = all(8 <= x < SMALL[0] - 8 and 8 <= y < SMALL[1] - 8
                         for x, y in (screen(first, px, py), screen(last, px, py)))
            if inside and clear(px * tiles % 1) and clear(py * tiles % 1):
                point = (px, py)
                break
        if point:
            break
    assert point is not None
    px, py = point
    tx, ty = int(px * tiles), int(py * tiles)

    positions = []
    for n, img in ((first, a), (last, b)):
        x, y = screen(n, px, py)
        got = img.getpixel((round(x), round(y)))
        assert max(abs(g - e) for g, e in zip(got, tile_color(band, tx, ty))) <= 3
        positions.append((x, y))
    moved = math.hypot(positions[1][0] - positions[0][0], positions[1][1] - positions[0][1])
    assert moved > 5


def test_frames_are_deterministic():
    tl = timeline30()
    n = follow_window(tl)[10]
    a = FrameRenderer(tl, SMALL, "Trip", tile_fetcher=fake_tile).frame(n)
    b = FrameRenderer(tl, SMALL, "Trip", tile_fetcher=fake_tile).frame(n)
    assert a.tobytes() == b.tobytes()


def _golden_frames():
    tl = timeline30()
    return {"title": 15, "follow": follow_window(tl)[30],
            "end": len(camera_path(tl, 30, SMALL)) - 10}


def _differs(a: Image.Image, b: Image.Image):
    """(mean absolute difference per channel, share of pixels off by > 48)."""
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    hist = diff.convert("L").histogram()
    n = a.width * a.height
    mean = sum(sum(i * c for i, c in enumerate(diff.getchannel(k).histogram()))
               for k in range(3)) / (3 * n)
    return mean, sum(hist[49:]) / n


@pytest.mark.parametrize("name", ["title", "follow", "end"])
def test_frames_match_golden_images(name):
    """Text anti-aliasing and resampling differ slightly between platforms
    and Pillow builds, so the match is within a tolerance: a small mean
    difference and few strongly different pixels — a moved route, marker,
    panel or wrong colour fails it."""
    n = _golden_frames()[name]
    frames = FrameRenderer(timeline30(), SMALL, "Paris to Lyon", tile_fetcher=fake_tile)
    img = frames.frame(n)
    path = GOLDEN_DIR / f"{name}.png"
    if os.environ.get("VIDEO_UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        img.save(path)
    golden = Image.open(path)
    assert golden.size == img.size
    mean, far = _differs(img, golden)
    assert mean <= 2.0, f"{name}: mean difference {mean:.2f}"
    assert far <= 0.01, f"{name}: {far:.2%} of pixels differ strongly"


def test_golden_comparison_catches_a_moved_route():
    """The tolerance is not so loose that a visibly different frame passes."""
    frames = FrameRenderer(timeline30(), SMALL, "Paris to Lyon", tile_fetcher=fake_tile)
    n = _golden_frames()["follow"]
    other = frames.frame(n + 20)
    golden = Image.open(GOLDEN_DIR / "follow.png")
    mean, far = _differs(other, golden)
    assert mean > 2.0 or far > 0.01


# ── the new camera modes (docs/VIDEO_CAMERA_QUALITY_PLAN.md #518 U6) ────────

def test_the_new_york_flight_is_fast_at_fixeds_zoom():
    """Precondition for the goldens below: on this fixture "fixed" flies over
    the flight and keeps the walks' zoom, while "fixed_strict" drops to a
    lower one that follows it too (D3)."""
    tl = timeline30_ny()
    assert fixed_zoom(tl, SMALL, "fixed") > fixed_zoom(tl, SMALL, "fixed_strict")


def _overview_mid_frame(tl):
    return follow_window(tl)[30]


def _overview_end_frame(tl):
    return len(camera_path(tl, 30, SMALL)) - 10


def _ny_walk_frame(tl):
    """A settled frame in the Paris walks' clip (index 0), after its fly-in —
    follow_window's margin, generalised to this fixture's first clip."""
    return int(math.ceil((tl.clips[0].start_s + 1.0) * 30))


def _ny_flight_frame(tl):
    """A settled frame in the flight's clip (index 1), after its fly-in."""
    return int(math.ceil((tl.clips[1].start_s + 1.0) * 30))


# name -> (camera mode, timeline, its title, the frame, the other modes the
# frame must fail the golden comparison in when rendered instead of the
# golden's own mode).
NEW_CAMERA_GOLDENS = {
    "overview_mid": ("overview", timeline30, "Paris to Lyon",
                     _overview_mid_frame, ("variable",)),
    "overview_end": ("overview", timeline30, "Paris to Lyon",
                     _overview_end_frame, ()),
    "fixed_walk": ("fixed", timeline30_ny, "Paris to New York",
                   _ny_walk_frame, ("fixed_strict", "variable")),
    "fixed_strict_flight": ("fixed_strict", timeline30_ny, "Paris to New York",
                            _ny_flight_frame, ("fixed", "variable")),
}


@pytest.mark.parametrize("name", sorted(NEW_CAMERA_GOLDENS))
def test_new_camera_mode_frames_match_golden_images(name):
    """As test_frames_match_golden_images, for the camera modes added by
    #518: overview, fixed and fixed_strict."""
    mode, tl_fn, title, frame_fn, _ = NEW_CAMERA_GOLDENS[name]
    tl = tl_fn()
    n = frame_fn(tl)
    frames = FrameRenderer(tl, SMALL, title, tile_fetcher=fake_tile, camera=mode)
    img = frames.frame(n)
    path = GOLDEN_DIR / f"{name}.png"
    if os.environ.get("VIDEO_UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        img.save(path)
    golden = Image.open(path)
    assert golden.size == img.size
    mean, far = _differs(img, golden)
    assert mean <= 2.0, f"{name}: mean difference {mean:.2f}"
    assert far <= 0.01, f"{name}: {far:.2%} of pixels differ strongly"


@pytest.mark.parametrize("name,wrong_mode", [
    (name, wrong) for name, (*_, wrongs) in NEW_CAMERA_GOLDENS.items() for wrong in wrongs
])
def test_new_golden_comparison_catches_the_wrong_camera_mode(name, wrong_mode):
    """As test_golden_comparison_catches_a_moved_route: the same frame,
    rendered in a different camera mode, must not pass as the golden's own
    mode."""
    mode, tl_fn, title, frame_fn, _ = NEW_CAMERA_GOLDENS[name]
    tl = tl_fn()
    n = frame_fn(tl)
    frames = FrameRenderer(tl, SMALL, title, tile_fetcher=fake_tile, camera=wrong_mode)
    other = frames.frame(n)
    golden = Image.open(GOLDEN_DIR / f"{name}.png")
    mean, far = _differs(other, golden)
    assert mean > 2.0 or far > 0.01


def test_the_overview_end_card_is_identical_in_every_camera_mode():
    """Unlike the other new goldens, the end card has no negative control:
    every mode's cards home on the whole-trip overview (D2, D3), so the card
    itself doesn't depend on the mode. Asserted directly, rather than relying
    on the golden tolerance to hide a difference that shouldn't exist."""
    tl = timeline30()
    n = _overview_end_frame(tl)
    reference = FrameRenderer(tl, SMALL, "Paris to Lyon", tile_fetcher=fake_tile,
                              camera="overview").frame(n).tobytes()
    for mode in CAMERA_MODES:
        frames = FrameRenderer(tl, SMALL, "Paris to Lyon", tile_fetcher=fake_tile, camera=mode)
        assert frames.frame(n).tobytes() == reference, mode


def test_a_frame_across_the_antimeridian_wraps_the_basemap():
    """A Tokyo to Los Angeles flight: a frame whose viewport crosses ±180°
    requests only valid tiles, and shows the world's last tile column west
    of the meridian and its first column east of it."""
    seg = ConnectingSegment(id="f", segment_type="flight", date="2026-05-01",
                            start=SegmentEndpoint(35.55, 139.78),
                            end=SegmentEndpoint(33.94, -118.41))
    tl = build_timeline(build_legs(Project(name="Pacific", items=[
        ProjectItem(item_type="segment", segment=seg)])), 30.0)
    size = (640, 360)
    requested = []

    def fetcher(z, x, y):
        requested.append((z, x, y))
        return fake_tile(z, x, y)

    frames = FrameRenderer(tl, size, "Pacific", tile_fetcher=fetcher)

    def screen(shot, px, py):
        cx, cy = lonlat_to_world(shot.lon, shot.lat)
        scale = TILE_SIZE * 2 ** shot.zoom
        return size[0] / 2 + (px - cx) * scale, size[1] / 2 + (py - cy) * scale

    def on_screen(x, y):
        return 8 <= x < size[0] - 8 and 8 <= y < size[1] - 8

    checked = 0
    for n, shot in enumerate(frames.shots):
        refs = frames.plan.frames[n]
        if len(refs) != 1 or tl.sample(n / tl.fps).kind != "clip":
            continue
        band = frames.plan.sheets[refs[0][0]].band
        tiles = 2 ** band
        cy = lonlat_to_world(shot.lon, shot.lat)[1]
        ty = int(cy * tiles)
        py = (ty + (0.25 if cy * tiles % 1 < 0.5 else 0.75)) / tiles
        west, east = (1 - 0.15 / tiles, py), (1 + 0.15 / tiles, py)
        if not all(on_screen(*screen(shot, *p)) for p in (west, east)):
            continue
        base = frames.basemap(n)
        img = frames.frame(n)
        for (px, _), tx in ((west, tiles - 1), (east, 0)):
            got = base.getpixel(tuple(round(v) for v in screen(shot, px, py)))
            assert max(abs(g - e) for g, e in zip(got, tile_color(band, tx, ty))) <= 3, n
        assert img.size == size
        checked += 1
        if checked == 3:
            break
    assert checked == 3
    assert requested
    assert all(0 <= x < 2 ** z and 0 <= y < 2 ** z for z, x, y in requested)


# ── encoding ─────────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_two_second_render_has_60_frames(tmp_path):
    tl = timeline30()
    out = render_timeline(tl, SMALL, tmp_path / "video.mp4", title="Trip",
                          tile_fetcher=fake_tile, frame_range=follow_window(tl))
    assert out == tmp_path / "video.mp4" and out.exists()
    assert not list(tmp_path.glob("*.part*"))
    probe = ffprobe(out)
    stream = probe["streams"][0]
    assert int(stream["nb_read_frames"]) == 60
    assert float(probe["format"]["duration"]) == pytest.approx(2.0, abs=0.01)
    assert (stream["codec_name"], stream["pix_fmt"]) == ("h264", "yuv420p")
    assert (stream["width"], stream["height"]) == SMALL
    assert stream["r_frame_rate"] == "30/1"


@needs_ffmpeg
def test_mp4_is_faststart(tmp_path):
    """+faststart: the moov atom precedes the media data, so the download
    URL streams."""
    tl = timeline30()
    out = render_timeline(tl, SMALL, tmp_path / "video.mp4", title="Trip",
                          tile_fetcher=fake_tile, frame_range=range(0, 10))
    head = out.read_bytes()
    assert 0 <= head.find(b"moov") < head.find(b"mdat")


def test_ffmpeg_that_stops_reading_is_a_fixed_error(tmp_path, monkeypatch):
    """A broken pipe or a non-zero exit is a VideoEncodeError with a fixed
    message — never ffmpeg's output — which the runner reports as its
    generic render failure. No partial file is left."""
    # A "ffmpeg" that exits at once without reading: Python refusing ffmpeg's
    # arguments.
    monkeypatch.setattr(renderer, "_ffmpeg", lambda: sys.executable)
    out = tmp_path / "video.mp4"
    with pytest.raises(VideoEncodeError) as info:
        render_timeline(timeline30(), SMALL, out, title="Trip", tile_fetcher=fake_tile,
                        frame_range=range(0, 60))
    assert str(info.value) in ("ffmpeg stopped reading frames", "ffmpeg exited with code 2")
    assert not out.exists() and not list(tmp_path.iterdir())
    assert job_runner._reason_for(info.value) == job_runner.REASON_RENDER_FAILED


@needs_ffmpeg
def test_ffmpeg_non_zero_exit_is_a_fixed_error(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "_FFMPEG_ARGS", ("-c:v", "no_such_encoder"))
    out = tmp_path / "video.mp4"
    with pytest.raises(VideoEncodeError) as info:
        render_timeline(timeline30(), SMALL, out, title="Trip", tile_fetcher=fake_tile,
                        frame_range=range(0, 5))
    assert str(info.value) in ("ffmpeg stopped reading frames", "ffmpeg exited with code 8",
                               "ffmpeg exited with code 1")
    assert not out.exists() and not list(tmp_path.iterdir())


def test_missing_ffmpeg_is_a_fixed_error(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer.shutil, "which", lambda name: None)
    with pytest.raises(VideoEncodeError, match="^ffmpeg is not installed$"):
        render_timeline(timeline30(), SMALL, tmp_path / "video.mp4", title="Trip",
                        tile_fetcher=fake_tile, frame_range=range(0, 5))


# ── render_video: the job runner's contract (Convention 5) ──────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}",
                           connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@example.com")
        friend = UserInfo(display_name="Friend", email="friend@example.com")
        sess.add_all([owner, friend]); sess.commit()
        sess.refresh(owner); sess.refresh(friend)
        sess.add(DBActivity(id=101, user_info_id=owner.id, name="ride", type="Ride",
                            distance=20000.0, moving_time=4000, elapsed_time=4200,
                            start_date="2026-05-01T09:00:00Z",
                            start_date_local="2026-05-01T09:00:00Z",
                            summary_polyline=polyline_lib.encode(RIDE),
                            start_latlng_json=json.dumps(list(RIDE[0])),
                            end_latlng_json=json.dumps(list(RIDE[-1]))))
        proj = DBProject(user_info_id=owner.id, name="Ride trip")
        empty = DBProject(user_info_id=owner.id, name="Empty")
        sess.add_all([proj, empty]); sess.commit()
        sess.refresh(proj); sess.refresh(empty)
        sess.add(DBProjectItem(project_id=proj.id, position=0, uid="r-101",
                               item_type="activity", activity_id=101))
        sess.commit()
        return {"owner": owner.id, "friend": friend.id, "trip": proj.id, "empty": empty.id}


@needs_ffmpeg
def test_render_video_renders_the_job(db, tmp_path):
    """Called exactly as job_runner calls it — by a companion, whose trip is
    loaded through its owner — it writes a 30 s MP4 at the requested size,
    reports progress in [0, 1] and returns the path."""
    calls = []
    out = tmp_path / "videos" / "7" / "video.mp4"
    out.parent.mkdir(parents=True)
    written = render_video(
        job_id=7, user_info_id=db["friend"], project_id=db["trip"],
        request={"length_s": 30, "height": 180, "width": 320},
        out_path=out, geometry=None, progress=lambda f, s: calls.append((f, s)),
        tile_fetcher=fake_tile)
    assert written == out and out.exists()
    probe = ffprobe(out)
    assert int(probe["streams"][0]["nb_read_frames"]) == 900
    assert float(probe["format"]["duration"]) == pytest.approx(30.0, abs=0.01)
    fractions = [f for f, _ in calls]
    assert fractions == sorted(fractions) and all(0.0 <= f <= 1.0 for f in fractions)
    assert {s for _, s in calls} >= {"loading trip", "rendering"}
    assert all(isinstance(s, str) for _, s in calls)


def test_render_video_with_nothing_to_animate(db, tmp_path):
    with pytest.raises(NothingToAnimate):
        render_video(job_id=8, user_info_id=db["owner"], project_id=db["empty"],
                     request={"length_s": 30, "height": 180, "width": 320},
                     out_path=tmp_path / "video.mp4", geometry=None,
                     progress=lambda f, s: None, tile_fetcher=fake_tile)
    assert not (tmp_path / "video.mp4").exists()


def test_render_video_for_a_deleted_trip(db, tmp_path):
    with pytest.raises(LookupError):
        render_video(job_id=9, user_info_id=db["owner"], project_id=10_000,
                     request={"length_s": 30, "height": 180, "width": 320},
                     out_path=tmp_path / "video.mp4", geometry=None,
                     progress=lambda f, s: None, tile_fetcher=fake_tile)


# ── benchmark (manual) ───────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _retina_pool(i: int) -> bytes:
    img = Image.effect_noise((2 * TILE_SIZE, 2 * TILE_SIZE), 40).convert("RGB")
    img = Image.blend(img, Image.new("RGB", img.size, tile_color(i, i, i)), 0.6)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _retina_tile(z: int, x: int, y: int) -> bytes:
    """A @2x-sized (1024 px) textured tile, so decoding costs what a real
    one does — from a pool of 16, so the fake holds no more memory than the
    network client would."""
    return _retina_pool((x + 7 * y + 3 * z) % 16)


def _benchmark_trip() -> Project:
    """Two weeks across France and Italy: rides, walks, trains, a bus, a
    ferry and a flight — 24 legs."""
    items, activities = [], []
    lat, lon = 48.85, 2.35
    day = date(2026, 6, 1)
    seq = ["Ride", "train", "Walk", "Ride", "bus", "Run", "train", "Hike", "flight",
           "Ride", "Walk", "boat", "Hike", "train", "Ride", "Walk", "train", "Run",
           "bus", "Hike", "train", "Ride", "Walk", "flight"]
    for i, kind in enumerate(seq):
        d = date.fromordinal(day.toordinal() + i // 2)
        if kind[0].isupper():
            n = 300
            pts = [(lat + 0.0004 * k * math.cos(i), lon + 0.0006 * k + 0.002 * math.sin(k / 9))
                   for k in range(n)]
            activities.append(_activity(1000 + i, kind, d, pts, 25_000.0, 5400))
            items.append(ProjectItem(item_type="activity", activity_id=1000 + i))
            lat, lon = pts[-1]
        else:
            step = {"flight": (-4.0, 6.0), "train": (-0.9, 1.1), "bus": (0.4, 0.5),
                    "boat": (-0.6, -0.8)}[kind]
            end = (lat + step[0] * (1 if i % 4 else -1), lon + step[1])
            items.append(ProjectItem(item_type="segment", segment=ConnectingSegment(
                id=f"s{i}", segment_type=kind, start=SegmentEndpoint(lat, lon),
                end=SegmentEndpoint(*end), date=d.isoformat())))
            lat, lon = end
    return Project(name="Two weeks in France & Italy", items=items, activities=activities)


def _peak_rss_mb():
    """Peak RSS of this process and of the largest child (ffmpeg), in MB."""
    import resource
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    return own, child


@pytest.mark.skipif(not os.environ.get("VIDEO_BENCH"), reason="manual benchmark: VIDEO_BENCH=1")
def test_benchmark_1080p_60s(tmp_path):
    """A 60 s 1080p render of a two-week trip with a fake @2x tile fetcher:
    ms/frame (tile decoding included, network excluded) and peak RSS of the
    renderer and of ffmpeg. Run on Linux (resource module)."""
    tl = build_timeline(build_legs(_benchmark_trip()), 60.0)
    start = time.perf_counter()
    out = render_timeline(tl, (1920, 1080), tmp_path / "video.mp4",
                          title="Two weeks in France & Italy", tile_fetcher=_retina_tile)
    elapsed = time.perf_counter() - start
    frames = len(camera_path(tl, 30, (1920, 1080)))
    ms = elapsed / frames * 1000
    own, child = _peak_rss_mb()
    plan = plan_bands(camera_path(tl, 30, (1920, 1080)), (1920, 1080))
    print(f"\nBENCH 1080p 60 s: {frames} frames, {ms:.1f} ms/frame, "
          f"{len(plan.sheets)} sheets, {plan.tiles} tiles, "
          f"peak RSS renderer {own:.0f} MB, ffmpeg {child:.0f} MB, "
          f"MP4 {out.stat().st_size / 1e6:.1f} MB")
    assert ms <= 150
    assert own + child <= 896

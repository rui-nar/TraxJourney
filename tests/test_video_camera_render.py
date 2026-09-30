"""Camera modes through the renderer, still-camera basemap reuse, and the
encoder defaults (docs/VIDEO_CAMERA_QUALITY_PLAN.md D1, D7, D9; U3).

Reuses ``tests/test_video_renderer.py``'s fakes and DB fixture (``fake_tile``,
``timeline30``, ``SMALL``, ``needs_ffmpeg``, ``db``, the benchmark trip).

The overview benchmark runs only with ``VIDEO_BENCH=1``, like the variable
one it is compared with (D8)::

    VIDEO_BENCH=1 pytest tests/test_video_camera_render.py -k benchmark -s
"""
from __future__ import annotations

import logging
import os
import subprocess
import time

import pytest
from PIL import ImageDraw

import src.video.basemap_bands as basemap_bands
import src.video.renderer as renderer
from src.video.basemap_bands import Basemaps, plan_bands
from src.video.camera import CAMERA_MODES, camera_path
from src.video.legs import build_legs
from src.video.renderer import FrameRenderer, render_timeline, render_video
from src.video.timeline import build_timeline
from tests.test_video_renderer import (  # noqa: F401
    SMALL,
    _benchmark_trip,
    _peak_rss_mb,
    _retina_tile,
    db,
    fake_tile,
    follow_window,
    needs_ffmpeg,
    timeline30,
)


# ── the mode reaches camera_path ─────────────────────────────────────────────

def _spy_camera_path(monkeypatch):
    modes = []

    def spy(timeline, fps, size, mode="variable"):
        modes.append(mode)
        return camera_path(timeline, fps, size, mode)

    monkeypatch.setattr(renderer, "camera_path", spy)
    return modes


def _no_encode(monkeypatch):
    """Stands in for ffmpeg: the FrameRenderer the job would have encoded."""
    seen = []

    def fake_encode(frames, out_path, **kwargs):
        seen.append(frames)
        out_path.write_bytes(b"")
        return out_path

    monkeypatch.setattr(renderer, "encode", fake_encode)
    return seen


@pytest.mark.parametrize("camera", CAMERA_MODES)
def test_the_jobs_camera_reaches_camera_path(db, tmp_path, monkeypatch, camera):
    modes = _spy_camera_path(monkeypatch)
    seen = _no_encode(monkeypatch)
    render_video(job_id=1, user_info_id=db["owner"], project_id=db["trip"],
                 request={"length_s": 30, "height": 180, "width": 320, "camera": camera},
                 out_path=tmp_path / "video.mp4", geometry=None,
                 progress=lambda f, s: None, tile_fetcher=fake_tile)
    assert modes == [camera]
    assert seen[0].camera == camera
    assert tuple(seen[0].shots) == camera_path(seen[0].timeline, 30, (320, 180), camera)


def test_a_row_without_camera_renders_as_variable(db, tmp_path, monkeypatch):
    """Jobs queued before the field existed (a deploy) keep today's camera."""
    modes = _spy_camera_path(monkeypatch)
    _no_encode(monkeypatch)
    render_video(job_id=1, user_info_id=db["owner"], project_id=db["trip"],
                 request={"length_s": 30, "height": 180, "width": 320},
                 out_path=tmp_path / "video.mp4", geometry=None,
                 progress=lambda f, s: None, tile_fetcher=fake_tile)
    assert modes == ["variable"]


@needs_ffmpeg
def test_the_render_summary_names_the_camera(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="src.video.renderer"):
        render_timeline(timeline30(), SMALL, tmp_path / "video.mp4", title="Trip",
                        tile_fetcher=fake_tile, frame_range=range(0, 5), camera="overview")
    [line] = [r.getMessage() for r in caplog.records
              if "video render summary" in r.getMessage()]
    assert "camera=overview " in line


# ── still-camera basemap reuse (D9) ──────────────────────────────────────────

def _count_crops(monkeypatch):
    calls = []
    real = basemap_bands._crop_scaled

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(basemap_bands, "_crop_scaled", counting)
    return calls


def _count_stitches(monkeypatch):
    calls = []
    real = renderer.render_basemap

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(renderer, "render_basemap", counting)
    return calls


def test_an_overview_render_stitches_one_sheet_and_crops_it_once(monkeypatch):
    stitches = _count_stitches(monkeypatch)
    crops = _count_crops(monkeypatch)
    frames = FrameRenderer(timeline30(), SMALL, "Trip", tile_fetcher=fake_tile,
                           camera="overview")
    assert len(set(frames.shots)) == 1
    assert len(frames.plan.sheets) == 1
    for n in range(len(frames)):
        frames.basemap(n)
    assert len(stitches) == 1
    assert len(crops) == 1


def test_a_moving_camera_crops_once_per_distinct_shot(monkeypatch):
    """Variable mode: the crop and scale step runs once per run of equal
    shots and bands (the cards' still frames share one), never more."""
    crops = _count_crops(monkeypatch)
    frames = FrameRenderer(timeline30(), SMALL, "Trip", tile_fetcher=fake_tile)
    keys = [(frames.shots[n], tuple(frames.plan.frames[n])) for n in range(len(frames))]
    expected = sum(len(k[1]) for i, k in enumerate(keys) if i == 0 or keys[i - 1] != k)
    assert expected < sum(len(k[1]) for k in keys)   # the cards do repeat a shot
    for n in range(len(frames)):
        frames.basemap(n)
    assert len(crops) == expected


def test_drawing_on_a_basemap_never_reaches_the_next_frame():
    """The overlay draws in place: whatever it does to frame n's image, frame
    n + 1 of an unchanged shot is still the clean basemap."""
    tl = timeline30()
    shots = camera_path(tl, 30, SMALL, "overview")
    plan = plan_bands(shots, SMALL)
    clean = Basemaps(shots, SMALL, plan, tile_fetcher=fake_tile).frame(1)
    maps = Basemaps(shots, SMALL, plan, tile_fetcher=fake_tile)
    first = maps.frame(0)
    ImageDraw.Draw(first).rectangle((0, 0, SMALL[0], SMALL[1]), fill=(255, 0, 255))
    assert maps.frame(1).tobytes() == clean.tobytes()


def _renders_alone_as_in_sequence(camera, targets):
    tl = timeline30()
    in_sequence = {}
    frames = FrameRenderer(tl, SMALL, "Paris to Lyon", tile_fetcher=fake_tile, camera=camera)
    for n in range(max(targets) + 1):
        img = frames.frame(n)
        if n in targets:
            in_sequence[n] = img.tobytes()
    for n in targets:
        alone = FrameRenderer(tl, SMALL, "Paris to Lyon", tile_fetcher=fake_tile,
                              camera=camera).frame(n)
        assert alone.tobytes() == in_sequence[n], f"{camera} frame {n} accumulated"


def test_overview_frames_do_not_accumulate():
    tl = timeline30()
    last = len(camera_path(tl, 30, SMALL, "overview")) - 10
    _renders_alone_as_in_sequence("overview", [15, follow_window(tl)[30], last])


def test_variable_card_frames_do_not_accumulate():
    tl = timeline30()
    shots = camera_path(tl, 30, SMALL)
    title, end = 15, len(shots) - 10
    # Both are still frames, so they come from a reused basemap.
    assert shots[title] == shots[title - 1] and shots[end] == shots[end - 1]
    _renders_alone_as_in_sequence("variable", [title, end])


# ── encoder defaults (D7, G1) ────────────────────────────────────────────────

@needs_ffmpeg
def test_ffmpeg_gets_crf_20_and_no_tune_by_default(tmp_path, monkeypatch):
    commands = []
    real = subprocess.Popen

    def recording(cmd, *args, **kwargs):
        commands.append(list(cmd))
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(renderer.subprocess, "Popen", recording)
    render_timeline(timeline30(), SMALL, tmp_path / "video.mp4", title="Trip",
                    tile_fetcher=fake_tile, frame_range=range(0, 5))
    [cmd] = commands
    assert cmd[cmd.index("-crf") + 1] == "20"
    assert "-tune" not in cmd
    assert cmd[cmd.index("-pix_fmt", cmd.index("-i")) + 1] == "yuv420p"


# ── benchmark (manual) ───────────────────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("VIDEO_BENCH"), reason="manual benchmark: VIDEO_BENCH=1")
def test_benchmark_1080p_60s_overview(tmp_path):
    """tests/test_video_renderer.py's 1080p benchmark in overview mode (D8:
    compared with the variable-mode figure from before #518)."""
    tl = build_timeline(build_legs(_benchmark_trip()), 60.0)
    start = time.perf_counter()
    out = render_timeline(tl, (1920, 1080), tmp_path / "video.mp4",
                          title="Two weeks in France & Italy", tile_fetcher=_retina_tile,
                          camera="overview")
    elapsed = time.perf_counter() - start
    shots = camera_path(tl, 30, (1920, 1080), "overview")
    ms = elapsed / len(shots) * 1000
    own, child = _peak_rss_mb()
    plan = plan_bands(shots, (1920, 1080))
    print(f"\nBENCH 1080p 60 s overview: {len(shots)} frames, {ms:.1f} ms/frame, "
          f"{len(plan.sheets)} sheets, {plan.tiles} tiles, "
          f"peak RSS renderer {own:.0f} MB, ffmpeg {child:.0f} MB, "
          f"MP4 {out.stat().st_size / 1e6:.1f} MB")
    assert ms <= 150
    assert own + child <= 896

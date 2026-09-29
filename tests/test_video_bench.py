"""Stage-timing instrumentation (``renderer.py``) and the benchmark/frame-dump
CLI (``bench.py``) added for docs/VIDEO_CAMERA_QUALITY_PLAN.md, D4/U1.

Reuses ``tests/test_video_renderer.py``'s fakes and DB fixture rather than
duplicating them (``fake_tile``, ``timeline30``, ``SMALL``, ``needs_ffmpeg``,
``db``).
"""
from __future__ import annotations

import logging
import re
import time

import pytest
from PIL import Image

import src.poster.tile_stitcher as tile_stitcher
import src.video.bench as bench
import src.video.renderer as renderer
from src.video.renderer import FrameRenderer, encode, render_timeline
from tests.test_video_renderer import SMALL, db, fake_tile, needs_ffmpeg, timeline30  # noqa: F401


# ── stage timings (renderer.py) ─────────────────────────────────────────────

@needs_ffmpeg
def test_stage_totals_sum_to_the_measured_wall_time(tmp_path, caplog):
    """The render summary's five per-stage ms/frame figures, multiplied back
    up by the frame count, add up to within 10% of the wall time actually
    measured around the render."""
    tl = timeline30()
    start = time.perf_counter()
    with caplog.at_level(logging.INFO, logger="src.video.renderer"):
        render_timeline(tl, SMALL, tmp_path / "video.mp4", title="Trip",
                        tile_fetcher=fake_tile, frame_range=range(0, 90))
    measured = time.perf_counter() - start

    lines = [r.getMessage() for r in caplog.records if "video render summary" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    frames = int(re.search(r"frames=(\d+)", line).group(1))
    assert frames == 90
    stage_ms = {
        name: float(re.search(rf"{name}_ms=([\d.]+)", line).group(1))
        for name in ("fetch", "stitch", "basemap", "overlay", "write")
    }
    stage_total_s = sum(stage_ms.values()) / 1000 * frames
    assert stage_total_s == pytest.approx(measured, rel=0.10, abs=0.05)


@needs_ffmpeg
def test_ffmpeg_peak_is_its_own_not_the_renderers(tmp_path, caplog):
    """The summary's ``peak_rss_ffmpeg_mb`` is ffmpeg's own peak (read from
    its ``/proc/<pid>/status`` VmHWM), not the renderer's ``RUSAGE_CHILDREN``
    figure (which, on Linux, is at least the renderer's own RSS at fork
    time). For this small 320x180 render ffmpeg needs far less memory than
    the Python renderer, so the two figures must differ, and ffmpeg's must
    be the smaller one."""
    tl = timeline30()
    frames = FrameRenderer(tl, SMALL, "Trip", tile_fetcher=fake_tile)
    # Two warm-up passes first, so the renderer's own RSS is already at (or
    # very near) its peak before the measured pass's ffmpeg is even forked.
    # RUSAGE_CHILDREN, the pre-fix reading, is *at least* the renderer's own
    # RSS at fork time: with a renderer that still had most of its growing
    # ahead of it, that reading would come out comfortably below the
    # renderer's later, bigger, own peak, passing this test even unfixed.
    # Flattening the renderer's growth first — so its RSS is essentially the
    # same at fork time as at its own final peak — is what actually exposes
    # the bug: RUSAGE_CHILDREN then reports (almost) exactly the renderer's
    # own peak, not ffmpeg's.
    encode(frames, tmp_path / "warmup1.mp4", frame_range=range(0, 90))
    encode(frames, tmp_path / "warmup2.mp4", frame_range=range(0, 90))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.video.renderer"):
        encode(frames, tmp_path / "video.mp4", frame_range=range(0, 90))
    lines = [r.getMessage() for r in caplog.records if "video render summary" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    renderer_mb = float(re.search(r"peak_rss_renderer_mb=([\d.]+)", line).group(1))
    ffmpeg_mb = float(re.search(r"peak_rss_ffmpeg_mb=([\d.]+)", line).group(1))
    assert ffmpeg_mb > 0
    assert ffmpeg_mb != renderer_mb
    assert ffmpeg_mb < renderer_mb


def test_encoder_args_default_matches_ffmpeg_args():
    """Neither *crf* nor *tune* passed (or *tune* explicitly "none") is
    exactly today's command line — no behaviour change by default."""
    assert renderer._encoder_args() is renderer._FFMPEG_ARGS
    assert renderer._encoder_args(tune="none") == renderer._FFMPEG_ARGS


def test_encoder_args_override_reaches_the_command_line():
    args = renderer._encoder_args(crf=18, tune="animation")
    assert args != renderer._FFMPEG_ARGS
    assert args[args.index("-crf") + 1] == "18"
    assert args[args.index("-tune") + 1] == "animation"


# ── bench.py ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("camera", ["variable", "overview", "fixed", "fixed_strict"])
def test_bench_renders_in_the_requested_camera_mode(db, tmp_path, monkeypatch, camera):
    """Every camera mode is wired into the renderer (U3): ``--camera``
    reaches the FrameRenderer the bench encodes."""
    captured = {}

    def fake_encode(frames, out_path, **kwargs):
        captured["camera"] = frames.camera
        out_path.write_bytes(b"")
        return out_path

    monkeypatch.setattr(bench, "encode", fake_encode)
    rc = bench.main(["--project", "Ride trip", "--owner", str(db["owner"]),
                     "--length", "10", "--height", "180", "--camera", camera,
                     "--out", str(tmp_path / "out")])
    assert rc == 0
    assert captured == {"camera": camera}


def test_refuses_an_unknown_camera_mode(capsys):
    with pytest.raises(SystemExit) as info:
        bench.main(["--project", "x", "--owner", "1", "--camera", "zoom"])
    assert info.value.code == 2
    assert "zoom" in capsys.readouterr().err


def test_refuses_a_trip_the_owner_cant_see(db, tmp_path, capsys):
    """``--owner`` names the requester: a trip owned by someone else 404s the
    same way ``get_project`` already does for any other caller."""
    rc = bench.main(["--project", "Ride trip", "--owner", str(db["friend"]),
                     "--length", "10", "--height", "180", "--out", str(tmp_path / "out")])
    assert rc == 1
    assert "no trip" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_bench_plumbs_crf_and_tune_to_encode(db, tmp_path, monkeypatch):
    captured = {}

    def fake_encode(frames, out_path, **kwargs):
        captured.update(kwargs)
        out_path.write_bytes(b"")
        return out_path

    monkeypatch.setattr(bench, "encode", fake_encode)
    rc = bench.main(["--project", "Ride trip", "--owner", str(db["owner"]),
                     "--length", "10", "--height", "180", "--crf", "18",
                     "--tune", "animation", "--out", str(tmp_path / "out")])
    assert rc == 0
    assert captured == {"crf": 18, "tune": "animation"}


def test_bench_defaults_pass_no_encoder_overrides(db, tmp_path, monkeypatch):
    captured = {}

    def fake_encode(frames, out_path, **kwargs):
        captured.update(kwargs)
        out_path.write_bytes(b"")
        return out_path

    monkeypatch.setattr(bench, "encode", fake_encode)
    rc = bench.main(["--project", "Ride trip", "--owner", str(db["owner"]),
                     "--length", "10", "--height", "180", "--out", str(tmp_path / "out")])
    assert rc == 0
    assert captured == {}


@needs_ffmpeg
def test_dump_frames_writes_two_pngs_per_frame_of_the_right_size(db, tmp_path, monkeypatch):
    monkeypatch.setattr(tile_stitcher, "_default_tile_fetcher", lambda: fake_tile)
    out_dir = tmp_path / "out"
    rc = bench.main(["--project", "Ride trip", "--owner", str(db["owner"]),
                     "--length", "10", "--height", "180",
                     "--dump-frames", "0,10", "--out", str(out_dir)])
    assert rc == 0
    for n in (0, 10):
        pre = Image.open(out_dir / f"frame_{n:06d}_pre.png")
        post = Image.open(out_dir / f"frame_{n:06d}_post.png")
        assert pre.size == (320, 180)
        assert post.size == (320, 180)

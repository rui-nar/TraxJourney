"""The preview render (docs/VIDEO_PREVIEW_PLAN.md D1, D4, Convention 3; U2):
``render_preview``, its ``FrameRenderer`` and ``bench.py --preview``.

Reuses ``tests/test_video_renderer.py``'s fakes, DB fixture and golden-image
tolerance (``fake_tile``, ``db``, ``_differs``) rather than duplicating them,
as ``tests/test_video_bench.py`` already does.

Every check runs on what the production code built — the shots and states of
``_preview_frame_renderer``, the frames handed to the WebP encoder — and each
negative control feeds the very same check a plausible wrong build, to show
the check can fail. Nothing here touches Mapbox: every render uses a fake
tile fetcher.
"""
from __future__ import annotations

import functools
import logging
import math
import re
from datetime import date

import pytest
from PIL import Image, ImageChops, ImageStat

import src.poster.tile_stitcher as tile_stitcher
import src.video.bench as bench
import src.video.renderer as renderer
from src.models.project import ConnectingSegment, Project, ProjectItem, SegmentEndpoint
from src.video import tile_prefetch
from src.video.camera import CAMERA_MODES, Shot, camera_path, frame_count
from src.video.legs import build_legs
from src.video.renderer import FrameRenderer, render_preview
from src.video.timeline import build_timeline
from tests.test_video_renderer import HIKE, RIDE, _activity, _differs, db, fake_tile  # noqa: F401

TARGET_720 = (1280, 720)
LENGTH_S = 30
PREVIEW_FRAMES = LENGTH_S * 8   # 8 fps, full length (D1)


# ── fixture: one of each mode this plan's camera/pacing math treats
# differently (mode floors, fixed-zoom chase, the overview) ─────────────────

RUN = [(48.845 - 0.0006 * i, 2.49 + 0.0004 * i) for i in range(15)]


def four_leg_trip() -> Project:
    """A ride and a run in Paris, a train to Lyon, a hike there."""
    ride = _activity(1, "Ride", date(2026, 5, 1), RIDE, 20_000.0, 4000)
    run = _activity(2, "Run", date(2026, 5, 1), RUN, 3_000.0, 900)
    train = ConnectingSegment(id="t1", segment_type="train",
                              start=SegmentEndpoint(48.85, 2.49),
                              end=SegmentEndpoint(45.76, 4.84), date="2026-05-02")
    hike = _activity(3, "Hike", date(2026, 5, 2), HIKE, 8_000.0, 7200)
    return Project(name="Four-leg trip", activities=[ride, run, hike], items=[
        ProjectItem(item_type="activity", activity_id=1),
        ProjectItem(item_type="activity", activity_id=2),
        ProjectItem(item_type="segment", segment=train),
        ProjectItem(item_type="activity", activity_id=3)])


@functools.lru_cache(maxsize=None)
def preview_timeline():
    return build_timeline(build_legs(four_leg_trip()), float(LENGTH_S))


def test_the_four_leg_trip_covers_a_hike_a_run_a_ride_and_a_train():
    modes = {leg.mode for leg in preview_timeline().legs}
    assert {"hike", "run", "ride", "train"} <= modes, modes


def _preview_renderer(mode: str) -> FrameRenderer:
    """What ``render_preview`` draws for the four-leg trip at 720p in *mode*."""
    return renderer._preview_frame_renderer(preview_timeline(), TARGET_720, mode,
                                            "Four-leg trip", fake_tile)


def _video_index(k: int) -> int:
    """Preview frame *k*'s video frame, ``round(k × 30 / 8)`` (D1)."""
    return round(k * 30 / 8)


# ── camera (Convention 3) ────────────────────────────────────────────────────

def _camera_mismatches(preview_shots, mode: str):
    """Preview frames whose shot is not video shot ``round(k × 30 / 8)`` of the
    video's own camera path (built at 1280×720 and 30 fps) with the same
    centre and a zoom exactly ``log2(1280 / 320)`` lower."""
    tl = preview_timeline()
    video = camera_path(tl, 30, TARGET_720, mode)
    offset = math.log2(TARGET_720[0] / 320)
    bad = []
    for k, shot in enumerate(preview_shots):
        v = video[_video_index(k)]
        if not (math.isclose(shot.lon, v.lon, abs_tol=1e-9)
                and math.isclose(shot.lat, v.lat, abs_tol=1e-9)
                and math.isclose(shot.zoom, v.zoom - offset, abs_tol=1e-9)):
            bad.append(k)
    return bad


@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_preview_shots_are_the_video_shots_two_zoom_levels_lower(mode):
    frames = _preview_renderer(mode)
    assert len(frames.shots) == PREVIEW_FRAMES
    assert _camera_mismatches(frames.shots, mode) == []


def test_the_camera_check_fails_for_variable_built_at_320x180_or_8_fps():
    """Required for ``variable`` only: ``overview`` is one shot at any fps
    and size, and the fixed modes are invariant under (size ÷ 4, zoom − 2) on
    this trip, so the control can't fail there by construction (review R3-2)."""
    tl = preview_timeline()
    offset = math.log2(TARGET_720[0] / 320)
    # Built at the preview's own size: already framed for 320 px, no offset.
    at_320 = camera_path(tl, 30, renderer.PREVIEW_SIZE, "variable")
    wrong_size = [at_320[_video_index(k)] for k in range(PREVIEW_FRAMES)]
    # Built at the preview's own rate: shot k of an 8 fps path.
    at_8fps = camera_path(tl, 8, TARGET_720, "variable")
    wrong_fps = [Shot(s.lon, s.lat, s.zoom - offset, s.flying) for s in at_8fps]
    assert len(wrong_fps) == PREVIEW_FRAMES
    assert _camera_mismatches(wrong_size, "variable") != []
    assert _camera_mismatches(wrong_fps, "variable") != []


# ── state ────────────────────────────────────────────────────────────────────

def _state_mismatches(states):
    """Preview frames whose marker position, travelled km, date or card phase
    differ from ``timeline.sample(round(k × 30 / 8) / 30)``."""
    tl = preview_timeline()

    def seen(s):
        return (s.lon, s.lat, dict(s.km_by_mode), s.date, s.kind, s.clip_index)

    return [k for k, s in enumerate(states)
            if seen(s) != seen(tl.sample(_video_index(k) / 30))]


@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_preview_state_is_sampled_at_the_video_time(mode):
    frames = _preview_renderer(mode)
    assert len(frames.states) == PREVIEW_FRAMES
    assert _state_mismatches(frames.states) == []
    # The whole state, not just what the check above names.
    tl = preview_timeline()
    assert all(s == tl.sample(_video_index(k) / 30) for k, s in enumerate(frames.states))


def test_the_state_check_fails_when_sampled_at_k_over_30_or_k_over_8():
    tl = preview_timeline()
    assert _state_mismatches([tl.sample(k / 30) for k in range(PREVIEW_FRAMES)]) != []
    assert _state_mismatches([tl.sample(k / 8) for k in range(PREVIEW_FRAMES)]) != []


# ── the frames handed to the WebP encoder ───────────────────────────────────

def _spy_on_webp_encoder(monkeypatch):
    """Record every frame list handed to Pillow's animated WebP writer: the
    pre-encode frames, exactly as ``_write_preview_webp`` drew them."""
    calls = []
    real_save = Image.Image.save

    def save(self, fp, format=None, **params):
        if params.get("save_all"):
            calls.append([self, *params.get("append_images", [])])
        return real_save(self, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", save)
    return calls


def _write(mode, tmp_path, monkeypatch):
    """Write the four-leg trip's WebP in *mode*: (its renderer, its pre-encode
    frames, its path)."""
    calls = _spy_on_webp_encoder(monkeypatch)
    frames = _preview_renderer(mode)
    out = renderer._write_preview_webp(frames, tmp_path / f"{mode}.webp")
    assert len(calls) == 1
    return frames, calls[0], out


# ── pixels ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_every_pre_encode_frame_is_a_direct_render_of_its_shot_and_state(mode, tmp_path,
                                                                         monkeypatch):
    """Frame *k*, as drawn within the whole preview and handed to the encoder,
    matches a direct 320×180 render of just its (shot, state) pair within the
    golden tolerance. Not compared with a downscaled video frame, which differs
    by design (review R2-3)."""
    frames, pre, _ = _write(mode, tmp_path, monkeypatch)
    assert len(pre) == PREVIEW_FRAMES
    for k, image in enumerate(pre):
        assert image.size == renderer.PREVIEW_SIZE
        direct = FrameRenderer(preview_timeline(), renderer.PREVIEW_SIZE, "Four-leg trip",
                               tile_fetcher=fake_tile, camera=mode,
                               shots=(frames.shots[k],), states=(frames.states[k],))
        mean, far = _differs(image, direct.frame(0))
        assert mean <= 2.0, f"{mode} frame {k}: mean difference {mean:.2f}"
        assert far <= 0.01, f"{mode} frame {k}: {far:.2%} of pixels differ strongly"


# ── WebP (review R3-3) ───────────────────────────────────────────────────────

# The codec tolerance between a decoded frame and its own pre-encode frame
# (libwebp 1.6.0 through Pillow 12.3, quality 50, every frame a key frame, in
# the CI Linux image). Measured over every frame of every camera mode on the
# four-leg trip: worst mean difference 2.97 (variable), worst share of pixels
# off by > 48 0.02%. Set with headroom above that. The nearest-frame margin
# measured at the same time was >= 0 in every mode, tightest (0.0001) between
# two pre-encode frames only a few pixels apart. The golden tolerance
# (2.0 / 1%) is not loosened: this is a separate, codec-only bound.
WEBP_CODEC_MEAN_TOLERANCE = 4.0
WEBP_CODEC_FAR_TOLERANCE = 0.005


def _decode(path):
    """Every frame of *path* as (start ms, duration ms, RGB image)."""
    img = Image.open(path)
    assert img.size == renderer.PREVIEW_SIZE
    assert img.info.get("loop") == 0
    out, start = [], 0
    for i in range(img.n_frames):
        img.seek(i)
        img.load()
        duration = img.info["duration"]
        out.append((start, duration, img.convert("RGB")))
        start += duration
    return out


def _check_pacing(decoded):
    """Full length at 8 fps: each frame lasts a positive multiple of 125 ms,
    together exactly the video's length; merged frames only shorten the list."""
    assert len(decoded) <= PREVIEW_FRAMES
    for _, duration, _ in decoded:
        assert duration > 0 and duration % renderer.PREVIEW_FRAME_MS == 0, duration
    assert sum(d for _, d, _ in decoded) == LENGTH_S * 1000


def _distance(a, b) -> float:
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_the_webp_decodes_to_its_pre_encode_frames(mode, tmp_path, monkeypatch):
    """Each decoded frame is nearest to the pre-encode frame at its own start
    time and within the codec tolerance of it; every pre-encode frame libwebp
    merged into a longer one is bit-identical to the one kept, so a merge
    never hides a real change."""
    _, pre, out = _write(mode, tmp_path, monkeypatch)
    assert len(pre) == PREVIEW_FRAMES
    decoded = _decode(out)
    _check_pacing(decoded)

    raw = [image.tobytes() for image in pre]
    # Distinct pre-encode frames, for the nearest-frame search.
    distinct = {}
    for k, data in enumerate(raw):
        distinct.setdefault(data, k)

    for start, duration, image in decoded:
        k = start // renderer.PREVIEW_FRAME_MS
        span = range(k, k + duration // renderer.PREVIEW_FRAME_MS)
        assert all(raw[j] == raw[k] for j in span), f"{mode}: merge over {span}"

        own = _distance(image, pre[k])
        others = min((_distance(image, pre[j]) for data, j in distinct.items()
                      if data != raw[k]), default=math.inf)
        assert own <= others, f"{mode} frame at {start} ms is nearer another frame"
        mean, far = _differs(image, pre[k])
        assert mean <= WEBP_CODEC_MEAN_TOLERANCE, f"{mode} frame {k}: codec mean {mean:.2f}"
        assert far <= WEBP_CODEC_FAR_TOLERANCE, f"{mode} frame {k}: codec far {far:.2%}"


# ── tile prefetching (docs/VIDEO_RENDER_TIME_PLAN.md U2b) ───────────────────

@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_preview_frames_are_bit_identical_with_and_without_prefetching(mode, tmp_path,
                                                                      monkeypatch):
    """The frames handed to the WebP encoder, with ``PREFETCH_THREADS`` at 4
    and at 0 (Conventions). ``fake_tile`` is thread-safe: it counts nothing
    and its cache takes a lock."""
    drawn = {}
    for threads in (4, 0):
        monkeypatch.setattr(tile_prefetch, "PREFETCH_THREADS", threads)
        (tmp_path / str(threads)).mkdir()
        frames, pre, _ = _write(mode, tmp_path / str(threads), monkeypatch)
        assert len(frames._prefetcher.threads) == threads
        assert frames.prefetch_misses == 0
        drawn[threads] = [image.tobytes() for image in pre]
    assert len(drawn[4]) == PREVIEW_FRAMES
    assert drawn[4] == drawn[0]


# ── render_preview: the job runner's contract (Convention 5) ────────────────

def test_render_preview_renders_the_job(db, tmp_path, monkeypatch):
    """Called exactly as job_runner would call it — by a companion — it draws
    240 frames for a 30 s preview and writes a 320×180, looping WebP that
    lasts 30 s at 125 ms steps, reporting progress in [0, 1]."""
    calls = _spy_on_webp_encoder(monkeypatch)
    progress = []
    out = tmp_path / "videos" / "7" / "preview.webp"
    out.parent.mkdir(parents=True)
    written = render_preview(
        job_id=7, user_info_id=db["friend"], project_id=db["trip"],
        request={"length_s": LENGTH_S, "height": 720, "width": 1280, "camera": "variable"},
        out_path=out, geometry=None, progress=lambda f, s: progress.append((f, s)),
        tile_fetcher=fake_tile)
    assert written == out and out.exists()
    assert not list(out.parent.glob("*.part*"))
    assert len(calls) == 1 and len(calls[0]) == PREVIEW_FRAMES
    _check_pacing(_decode(out))
    fractions = [f for f, _ in progress]
    assert fractions == sorted(fractions) and all(0.0 <= f <= 1.0 for f in fractions)
    assert {s for _, s in progress} >= {"loading trip", "rendering"}


def test_render_preview_frames_the_requested_target_size(db, tmp_path, monkeypatch):
    """``width``/``height`` are the video's target resolution: at 1080p the
    shots are the 1920×1080 path's, log2(1920 / 320) lower."""
    seen = {}
    real = renderer._preview_frame_renderer

    def spy(timeline, target_size, camera, title, tile_fetcher=None):
        seen["target"], seen["camera"] = target_size, camera
        seen["frames"] = real(timeline, target_size, camera, title, tile_fetcher)
        return seen["frames"]

    monkeypatch.setattr(renderer, "_preview_frame_renderer", spy)
    render_preview(
        job_id=1, user_info_id=db["owner"], project_id=db["trip"],
        request={"length_s": 10, "height": 1080, "width": 1920, "camera": "fixed"},
        out_path=tmp_path / "p.webp", geometry=None, progress=lambda f, s: None,
        tile_fetcher=fake_tile)
    assert seen["target"] == (1920, 1080) and seen["camera"] == "fixed"
    frames = seen["frames"]
    video = camera_path(frames.timeline, 30, (1920, 1080), "fixed")
    for k, shot in enumerate(frames.shots):
        assert shot.zoom == pytest.approx(video[_video_index(k)].zoom - math.log2(6))


def test_preview_summary_line_carries_kind_preview(db, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="src.video.renderer"):
        render_preview(
            job_id=1, user_info_id=db["owner"], project_id=db["trip"],
            request={"length_s": 10, "height": 720, "width": 1280, "camera": "variable"},
            out_path=tmp_path / "preview.webp", geometry=None, progress=lambda f, s: None,
            tile_fetcher=fake_tile)
    lines = [r.getMessage() for r in caplog.records if "video render summary" in r.getMessage()]
    assert len(lines) == 1
    assert "kind=preview" in lines[0] and "frames=80 " in lines[0]


def test_preview_summary_line_carries_tile_ms_and_prefetch_misses(db, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="src.video.renderer"):
        render_preview(
            job_id=1, user_info_id=db["owner"], project_id=db["trip"],
            request={"length_s": 10, "height": 720, "width": 1280, "camera": "variable"},
            out_path=tmp_path / "preview.webp", geometry=None, progress=lambda f, s: None,
            tile_fetcher=fake_tile)
    lines = [r.getMessage() for r in caplog.records if "video render summary" in r.getMessage()]
    assert len(lines) == 1
    assert re.search(r" tile_ms=[\d.]+ ", lines[0])
    assert " prefetch_misses=0 " in lines[0]


def test_a_failed_encode_leaves_no_partial_file(tmp_path, monkeypatch):
    def broken_save(self, fp, format=None, **params):
        with open(fp, "wb") as f:
            f.write(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", broken_save)
    with pytest.raises(OSError):
        renderer._write_preview_webp(_preview_renderer("overview"), tmp_path / "p.webp")
    assert list(tmp_path.iterdir()) == []


# ── bench.py --preview ───────────────────────────────────────────────────────

def test_bench_preview_writes_the_webp_and_logs_the_summary(db, tmp_path, monkeypatch,
                                                            caplog, capsys):
    monkeypatch.setattr(tile_stitcher, "_default_tile_fetcher", lambda: fake_tile)
    targets = []
    real = bench._preview_frame_renderer
    monkeypatch.setattr(bench, "_preview_frame_renderer",
                        lambda tl, size, *a, **kw: targets.append(size) or real(tl, size, *a, **kw))
    out_dir = tmp_path / "out"
    with caplog.at_level(logging.INFO, logger="src.video.renderer"):
        rc = bench.main(["--project", "Ride trip", "--owner", str(db["owner"]),
                         "--length", "10", "--height", "720", "--camera", "variable",
                         "--preview", "--out", str(out_dir)])
    assert rc == 0
    assert targets == [(1280, 720)]
    img = Image.open(out_dir / "bench.webp")
    assert img.size == renderer.PREVIEW_SIZE
    assert not (out_dir / "bench.mp4").exists()
    lines = [r.getMessage() for r in caplog.records if "video render summary" in r.getMessage()]
    assert len(lines) == 1 and "kind=preview" in lines[0] and "camera=variable" in lines[0]
    assert "output:" in capsys.readouterr().out

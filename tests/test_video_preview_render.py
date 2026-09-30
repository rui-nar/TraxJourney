"""The preview render (docs/VIDEO_PREVIEW_PLAN.md D1, D4, Convention 3; U2):
``render_preview``, its ``FrameRenderer`` and ``bench.py`` support.

Reuses ``tests/test_video_renderer.py``'s fakes, DB fixture and golden-image
tolerance (``fake_tile``, ``db``, ``_differs``) rather than duplicating them,
as ``tests/test_video_bench.py`` already does.

Nothing here touches Mapbox: every render uses a fake tile fetcher.
"""
from __future__ import annotations

import functools
import logging
import math
from datetime import date

import pytest
from PIL import Image

import src.video.bench as bench
import src.video.renderer as renderer
from src.models.project import ConnectingSegment, Project, ProjectItem, SegmentEndpoint
from src.video.camera import CAMERA_MODES, camera_path, frame_count
from src.video.legs import build_legs
from src.video.renderer import FrameRenderer, render_preview
from src.video.timeline import build_timeline
from tests.test_video_renderer import HIKE, RIDE, _activity, _differs, db, fake_tile  # noqa: F401

TARGET_720 = (1280, 720)


# ── fixture: one of each mode this plan's camera/pacing math treats
# differently (mode floors, fixed-zoom chase, the overview) ─────────────────

RUN = [(48.845 - 0.0006 * i, 2.49 + 0.0004 * i) for i in range(15)]


def preview_trip() -> Project:
    """A ride and a run in Paris, a train to Lyon, a hike there."""
    ride = _activity(1, "Ride", date(2026, 5, 1), RIDE, 20_000.0, 4000)
    run = _activity(2, "Run", date(2026, 5, 1), RUN, 3_000.0, 900)
    train = ConnectingSegment(id="t1", segment_type="train",
                              start=SegmentEndpoint(48.85, 2.49),
                              end=SegmentEndpoint(45.76, 4.84), date="2026-05-02")
    hike = _activity(3, "Hike", date(2026, 5, 2), HIKE, 8_000.0, 7200)
    return Project(name="Preview trip", activities=[ride, run, hike], items=[
        ProjectItem(item_type="activity", activity_id=1),
        ProjectItem(item_type="activity", activity_id=2),
        ProjectItem(item_type="segment", segment=train),
        ProjectItem(item_type="activity", activity_id=3)])


@functools.lru_cache(maxsize=None)
def preview_timeline():
    return build_timeline(build_legs(preview_trip()), 30.0)


def _video_frame_index(tl, k: int, shots) -> int:
    """Preview frame *k*'s video shot index, ``round(k × fps / PREVIEW_FPS)``
    (D1), clamped to *shots*'s length as :func:`renderer.preview_frames` does."""
    return min(round(k * tl.fps / renderer.PREVIEW_FPS), len(shots) - 1)


# ── camera (Convention 3) ────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_preview_shots_match_the_video_shot_scaled(mode):
    """Every preview shot's centre is its video shot's, at the target size and
    the timeline's own fps; its zoom is exactly ``log2(target width / 320)``
    lower."""
    tl = preview_timeline()
    shots = camera_path(tl, tl.fps, TARGET_720, mode)
    p_shots, _ = renderer.preview_frames(tl, shots, TARGET_720)
    offset = math.log2(TARGET_720[0] / 320)
    assert len(p_shots) == frame_count(tl, renderer.PREVIEW_FPS)
    for k, shot in enumerate(p_shots):
        video_shot = shots[_video_frame_index(tl, k, shots)]
        assert shot.lon == pytest.approx(video_shot.lon)
        assert shot.lat == pytest.approx(video_shot.lat)
        assert shot.zoom == pytest.approx(video_shot.zoom - offset)


def test_variable_camera_check_fails_if_shots_are_built_at_preview_size_or_fps():
    """The check above requires the video's own camera path, built at the
    target size and the timeline's own fps (D1; review R1-1): shown here to
    fail on ``variable`` if the shots handed to
    :func:`renderer.preview_frames` were built at the preview's own size or
    frame rate instead. Required for ``variable`` only: ``overview`` is one
    shot at any size or fps, and the fixed modes are invariant under
    (size ÷ 4, zoom − 2) on this trip, so this control can't fail for them by
    construction (review R3-2)."""
    tl = preview_timeline()
    correct = camera_path(tl, tl.fps, TARGET_720, "variable")
    wrong_size = camera_path(tl, tl.fps, renderer.PREVIEW_SIZE, "variable")
    wrong_fps = camera_path(tl, renderer.PREVIEW_FPS, TARGET_720, "variable")

    def centres_and_zooms(shots):
        p_shots, _ = renderer.preview_frames(tl, shots, TARGET_720)
        return [(s.lon, s.lat, s.zoom) for s in p_shots]

    reference = centres_and_zooms(correct)
    for wrong in (wrong_size, wrong_fps):
        assert centres_and_zooms(wrong) != reference


# ── state ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_preview_state_matches_the_video_time(mode):
    """Every preview frame's state (marker position, travelled km, date and
    card phase) is ``timeline.sample(round(k × fps / PREVIEW_FPS) / fps)`` —
    the same video time its shot was taken at, not the preview's own clock."""
    tl = preview_timeline()
    shots = camera_path(tl, tl.fps, TARGET_720, mode)
    _, p_states = renderer.preview_frames(tl, shots, TARGET_720)
    for k, state in enumerate(p_states):
        idx = _video_frame_index(tl, k, shots)
        expected = tl.sample(idx / tl.fps)
        assert (state.lon, state.lat) == (expected.lon, expected.lat)
        assert state.km_by_mode == expected.km_by_mode
        assert state.date == expected.date
        assert state.kind == expected.kind


def test_state_check_fails_if_sampled_at_k_over_30_or_k_over_8():
    """Shown here to fail if the state were sampled at the preview's own
    frame number over its own or the video's fps instead of the video shot's
    own time."""
    tl = preview_timeline()
    shots = camera_path(tl, tl.fps, TARGET_720, "variable")
    _, correct = renderer.preview_frames(tl, shots, TARGET_720)
    n = len(correct)

    def positions(states):
        return [(s.lon, s.lat, tuple(sorted(s.km_by_mode.items()))) for s in states]

    reference = positions(correct)
    wrong_30 = positions([tl.sample(k / 30) for k in range(n)])
    wrong_8 = positions([tl.sample(k / 8) for k in range(n)])
    assert wrong_30 != reference
    assert wrong_8 != reference


# ── pixels ───────────────────────────────────────────────────────────────────

def _pixel_check_frames(tl):
    """A few preview frame indices to pixel-check per camera mode: inside the
    title card, mid-clips and inside the end card."""
    n = frame_count(tl, renderer.PREVIEW_FPS)
    return (2, n // 2, n - 3)


@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_preview_pixels_match_a_direct_render_of_the_same_shot_and_state(mode):
    """Preview frame *k*, rendered as part of the whole preview, is pixel
    identical (within the golden tolerance) to a direct 320×180 render of
    just that (shot, state) pair — not to a downscaled video frame, which
    would differ by design (review R2-3). Compared before any WebP encoding."""
    tl = preview_timeline()
    shots = camera_path(tl, tl.fps, TARGET_720, mode)
    p_shots, p_states = renderer.preview_frames(tl, shots, TARGET_720)
    sequence = FrameRenderer(tl, renderer.PREVIEW_SIZE, "Preview trip", tile_fetcher=fake_tile,
                             camera=mode, shots=p_shots, states=p_states)
    for k in _pixel_check_frames(tl):
        direct = FrameRenderer(tl, renderer.PREVIEW_SIZE, "Preview trip", tile_fetcher=fake_tile,
                               camera=mode, shots=(p_shots[k],), states=(p_states[k],))
        mean, far = _differs(sequence.frame(k), direct.frame(0))
        assert mean <= 2.0, f"{mode} frame {k}: mean difference {mean:.2f}"
        assert far <= 0.01, f"{mode} frame {k}: {far:.2%} of pixels differ strongly"


# ── render_preview: the job runner's contract (Convention 5) ────────────────

def test_render_preview_renders_the_job(db, tmp_path):
    """Called exactly as job_runner would call it, it writes a 240-frame,
    320×180, looping animated WebP and reports progress in [0, 1]."""
    calls = []
    out = tmp_path / "videos" / "7" / "preview.webp"
    out.parent.mkdir(parents=True)
    written = render_preview(
        job_id=7, user_info_id=db["friend"], project_id=db["trip"],
        request={"length_s": 30, "height": 720, "width": 1280, "camera": "variable"},
        out_path=out, geometry=None, progress=lambda f, s: calls.append((f, s)),
        tile_fetcher=fake_tile)
    assert written == out and out.exists()
    img = Image.open(out)
    assert img.n_frames == 240
    assert img.size == renderer.PREVIEW_SIZE
    assert img.info.get("loop") == 0
    fractions = [f for f, _ in calls]
    assert fractions == sorted(fractions) and all(0.0 <= f <= 1.0 for f in fractions)
    assert {s for _, s in calls} >= {"loading trip", "rendering"}


def test_preview_summary_line_carries_kind_preview(db, tmp_path, caplog):
    out = tmp_path / "preview.webp"
    with caplog.at_level(logging.INFO, logger="src.video.renderer"):
        render_preview(
            job_id=1, user_info_id=db["owner"], project_id=db["trip"],
            request={"length_s": 10, "height": 720, "width": 1280, "camera": "variable"},
            out_path=out, geometry=None, progress=lambda f, s: None, tile_fetcher=fake_tile)
    lines = [r.getMessage() for r in caplog.records if "video render summary" in r.getMessage()]
    assert len(lines) == 1
    assert "kind=preview" in lines[0]


# ── WebP: decoding and its codec tolerance (review R3-3) ────────────────────

# Measured on the CI Linux image (Pillow's libwebp encoder, quality=50) over
# test_render_preview_renders_the_job's and this test's fixtures: the actual
# mean difference and far-pixel share between a decoded frame and its own
# pre-encode frame. Set with headroom above that measurement; the golden
# tolerance (2.0 / 1%) above is not loosened.
WEBP_CODEC_MEAN_TOLERANCE = 3.0
WEBP_CODEC_FAR_TOLERANCE = 0.03


def test_webp_decodes_to_its_pre_encode_frames_within_the_codec_tolerance(db, tmp_path):
    """The written file decodes with Pillow to the frame count, size, looping
    and pacing it was written with, and every decoded frame is close to the
    frame it was encoded from (positionally — frame *i* of the file to frame
    *i* pre-encode), within a tolerance measured from the codec, not the
    golden tolerance."""
    request = {"length_s": 10, "height": 720, "width": 1280, "camera": "variable"}
    out = tmp_path / "preview.webp"
    written = render_preview(
        job_id=1, user_info_id=db["owner"], project_id=db["trip"], request=request,
        out_path=out, geometry=None, progress=lambda f, s: None, tile_fetcher=fake_tile)
    assert written == out

    # The exact pre-encode frames render_preview drew, rebuilt the same way.
    project = renderer._load_project(db["trip"], db["owner"])
    timeline = renderer.timeline_for_project(project, request["length_s"])
    frames = renderer._preview_frame_renderer(
        timeline, (request["width"], request["height"]), request["camera"], project.name,
        fake_tile)
    pre = [frames.frame(i).convert("RGB") for i in range(len(frames))]

    img = Image.open(out)
    assert img.n_frames == len(pre)
    assert img.size == renderer.PREVIEW_SIZE
    decoded = []
    for i in range(img.n_frames):
        img.seek(i)
        img.load()
        assert img.info["duration"] == renderer.PREVIEW_FRAME_MS
        decoded.append(img.convert("RGB"))
    assert Image.open(out).info.get("loop") == 0

    for i, (d, p) in enumerate(zip(decoded, pre)):
        mean, far = _differs(d, p)
        assert mean <= WEBP_CODEC_MEAN_TOLERANCE, f"frame {i}: codec mean difference {mean:.2f}"
        assert far <= WEBP_CODEC_FAR_TOLERANCE, f"frame {i}: {far:.2%} of pixels differ strongly"


# ── bench.py --preview ───────────────────────────────────────────────────────

def test_bench_preview_flag_writes_a_webp_and_prints_the_summary(db, tmp_path, monkeypatch,
                                                                  capsys):
    captured = {}

    def fake_write(frames, out_path, **kwargs):
        captured["camera"] = frames.camera
        captured["frames"] = len(frames)
        captured["size"] = frames.size
        out_path.write_bytes(b"")
        return out_path

    monkeypatch.setattr(bench, "_write_preview_webp", fake_write)
    rc = bench.main(["--project", "Ride trip", "--owner", str(db["owner"]),
                     "--length", "10", "--height", "720", "--camera", "variable",
                     "--preview", "--out", str(tmp_path / "out")])
    assert rc == 0
    assert captured == {"camera": "variable", "frames": 80, "size": renderer.PREVIEW_SIZE}
    assert "output:" in capsys.readouterr().out

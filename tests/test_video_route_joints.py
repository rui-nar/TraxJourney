"""Round joints as disks (docs/VIDEO_ROUTE_DRAWING_PLAN.md, U2; #525).

``Overlay._draw_route`` draws the travelled route's casing and colour as
plain lines plus a disk at each interior vertex, the size of Pillow's own
round joint, instead of ``joint="curve"``, whose pie slice per vertex was
most of the overlay's time (D4). That changes pixels, within D5's bounds:

- **bounds:** against the pre-U2 drawing, at 1080p on D5's dense trip
  (:func:`~tests.test_video_route_clip.dense_timeline`), per camera mode;
- **control:** with no joints at all, the same comparison exceeds its
  camera's bound by at least 2× on the mean or on the share of pixels that
  differ, so the fixture can tell good joints from none;
- **no pie slices:** a render in every camera mode never calls
  ``ImageDraw.pieslice`` once the overlay is built.

The pre-U2 drawing is :func:`_reference`: today's ``_draw_route`` with every
line but the faint one drawn ``joint="curve"`` and no disks, which is what
the code drew before U2 (checked byte for byte against it on the renderer's
trips and the dense trip when U2 was written).
"""
from __future__ import annotations

import contextlib
import math

import pytest
from PIL import Image, ImageChops, ImageDraw

from src.video import overlay as overlay_module
from src.video.camera import CAMERA_MODES, camera_path
from src.video.renderer import FrameRenderer
from tests.test_video_renderer import fake_tile
from tests.test_video_route_clip import HD, SMALL, _overlay, dense_timeline

_BASE = (90, 110, 90)
# Every this many frames of the dense trip's 300 is compared.
STRIDE = 5

# D5's bounds per camera: mean absolute difference (per channel), share of
# pixels that differ in any channel, PSNR in dB.
FOLLOW = (0.02, 0.001, 48.0)
OVERVIEW = (0.04, 0.0015, 42.0)
BOUNDS = {"variable": FOLLOW, "fixed": FOLLOW, "fixed_strict": FOLLOW, "overview": OVERVIEW}


# ── the drawings compared ────────────────────────────────────────────────────

_line, _ellipse = ImageDraw.ImageDraw.line, ImageDraw.ImageDraw.ellipse


@contextlib.contextmanager
def _patched(line=_line, ellipse=_ellipse):
    ImageDraw.ImageDraw.line, ImageDraw.ImageDraw.ellipse = line, ellipse
    try:
        yield
    finally:
        ImageDraw.ImageDraw.line, ImageDraw.ImageDraw.ellipse = _line, _ellipse


def _curve_line(self, xy, fill=None, width=0, joint=None):
    """Every line but the faint one with Pillow's ``joint="curve"``."""
    if fill not in (overlay_module._FAINT, overlay_module._FAINT_ALPHA):
        joint = "curve"
    return _line(self, xy, fill=fill, width=width, joint=joint)


def _no_ellipse(self, *args, **kwargs):
    pass


def _draw(ov, shot, state) -> Image.Image:
    frame = Image.new("RGB", ov.size, _BASE)
    ov._draw_route(frame, shot, state)
    return frame


def _reference(ov, shot, state) -> Image.Image:
    """The pre-U2 drawing: pie-slice joints, no disks."""
    with _patched(line=_curve_line, ellipse=_no_ellipse):
        return _draw(ov, shot, state)


def _no_joints(ov, shot, state) -> Image.Image:
    """The control: plain lines, no joints at all."""
    with _patched(ellipse=_no_ellipse):
        return _draw(ov, shot, state)


# ── the comparison ───────────────────────────────────────────────────────────

class _Diff:
    """Mean absolute difference, share of pixels that differ and PSNR of
    candidate frames against reference frames, over all of them, as the
    probe measured them (D5)."""

    def __init__(self) -> None:
        self.px = self.abs = self.sq = self.differ = 0

    def add(self, a: Image.Image, b: Image.Image) -> None:
        d = ImageChops.difference(a, b)
        self.px += a.width * a.height
        for band in d.split():
            for v, n in enumerate(band.histogram()):
                self.abs += v * n
                self.sq += v * v * n
        r, g, b_ = d.split()
        h = ImageChops.lighter(ImageChops.lighter(r, g), b_).histogram()
        self.differ += sum(h) - h[0]

    def stats(self):
        mse = self.sq / (3 * self.px)
        psnr = math.inf if mse == 0 else 10 * math.log10(255 ** 2 / mse)
        return self.abs / (3 * self.px), self.differ / self.px, psnr


def _compare(mode: str):
    """The dense trip's video at 1080p in *mode*, every :data:`STRIDE`-th
    frame's route over a flat frame: the disks' and the control's
    (mean, share, PSNR) against the reference, and how many frames were
    compared.

    Only frames with a travelled line are compared. The others (the title
    card's 2.5 s, the first frames of the first leg) draw the faint line
    alone, which has no joints: they would dilute both the disks' difference
    and the control's, making the bounds easier to meet and the control
    harder to tell apart (at 1.9× on overview's mean with them)."""
    tl = dense_timeline()
    ov = _overlay(dense_timeline, HD)
    disks, control = _Diff(), _Diff()
    compared = 0
    for n, shot in enumerate(camera_path(tl, tl.fps, HD, mode)):
        state = tl.sample(n / tl.fps)
        if n % STRIDE or not ov._lines(shot, state)[4]:
            continue
        ref = _reference(ov, shot, state)
        disks.add(_draw(ov, shot, state), ref)
        control.add(_no_joints(ov, shot, state), ref)
        compared += 1
    return disks.stats(), control.stats(), compared


@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_disk_joints_stay_within_the_bounds_and_no_joints_do_not(mode):
    (mean, share, psnr), (c_mean, c_share, c_psnr), compared = _compare(mode)
    b_mean, b_share, b_psnr = BOUNDS[mode]
    measured = (f"{mode}: disks mean {mean:.4f} share {share:.4%} psnr {psnr:.1f}; "
                f"no joints mean {c_mean:.4f} share {c_share:.4%} psnr {c_psnr:.1f}")
    assert compared >= 40, measured
    assert mean <= b_mean and share <= b_share and psnr >= b_psnr, measured
    # The fixture is strong enough to see joints missing (reviews R1-2, R2-1).
    assert c_mean >= 2 * b_mean or c_share >= 2 * b_share, measured


def test_the_reference_draws_pie_slices():
    """:func:`_reference` stands for the pre-U2 drawing only as long as
    Pillow draws its round joints with pie slices."""
    tl = dense_timeline()
    ov = _overlay(dense_timeline, HD)
    shot = camera_path(tl, tl.fps, HD, "variable")[-1]
    calls = []
    pieslice = ImageDraw.ImageDraw.pieslice

    def count(self, *args, **kwargs):
        calls.append(1)
        return pieslice(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ImageDraw.ImageDraw, "pieslice", count)
        _reference(ov, shot, tl.sample(tl.total_s))
    assert len(calls) > 100


# ── no pie slices ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_no_pie_slice_is_drawn_while_frames_are_drawn(mode, monkeypatch):
    """Once the overlay is built (``_overview_hud`` keeps its pie-slice
    joints, in ``__init__``), drawing frames calls no ``pieslice``, at 1080p
    and at the preview's size."""
    tl = dense_timeline()
    for size in (SMALL, HD):
        frames = FrameRenderer(tl, size, "Dense", tile_fetcher=fake_tile, camera=mode)
        try:
            with monkeypatch.context() as mp:
                def refuse(*args, **kwargs):
                    raise AssertionError("pieslice drawn while drawing a frame")
                mp.setattr(ImageDraw.ImageDraw, "pieslice", refuse)
                drawn = [frames.frame(n) for n in range(0, len(frames), 30)]
        finally:
            frames.close()
        assert len(drawn) == 10

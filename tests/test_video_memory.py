"""Memory of the video pipeline per GPS point (docs/VIDEO_PREVIEW_PLAN.md, D11, U2b).

The route's points are stored as flat ``array('d')`` from the legs through the
camera to the overlay, so a trip costs about 50 bytes per point in the Python
heap rather than about 500: at gate G1 a 60 s preview of a 1.59 M-point trip
peaked at 936 MB against the ``worker`` container's 1024 MB.

Each test measures one camera mode and one renderer, from building the legs
through the camera path, the :class:`FrameRenderer` and one frame drawn at
each integer zoom of the path (fake tiles), with :mod:`tracemalloc`: Pillow's
pixel buffers are outside the Python heap and don't count, so the figure is
the route's own cost (and an :class:`ImagePath.Path`'s doubles, which the
overlay's overview layout draws through, are outside it too).

Measured in the Linux image on this trip, 220,000 points: the tuple-based
code before U2b peaked at 391 (variable), 467 (overview), 349 (fixed) and 349
(fixed_strict) bytes per point; U2b's at 112, 81, 81 and 81.
"""
from __future__ import annotations

import gc
import io
import math
import random
import tracemalloc
from datetime import date, datetime

import polyline as polyline_lib
import pytest
from PIL import Image

from src.models.activity import Activity
from src.models.project import Project, ProjectItem
from src.video import camera as camera_mod
from src.video.camera import CAMERA_MODES, TILE_SIZE, camera_path
from src.video.legs import build_legs
from src.video.renderer import FrameRenderer
from src.video.timeline import build_timeline

MAX_BYTES_PER_POINT = 120
POINTS_PER_LEG = 110_000
SIZE = (1280, 720)
LENGTH_S = 60.0


def _tile() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (TILE_SIZE, TILE_SIZE), (90, 110, 130)).save(buf, "PNG")
    return buf.getvalue()


_TILE = _tile()


def _fake_tile(z: int, x: int, y: int) -> bytes:
    """One shared tile, so the fake holds no memory per tile."""
    return _TILE


def _track(rng: random.Random, lat: float, lon: float, n: int):
    """*n* (lat, lon) points 3–10 m apart, the heading drifting as a real
    recording's does."""
    pts = [(lat, lon)]
    heading = rng.uniform(0, 2 * math.pi)
    for _ in range(n - 1):
        heading += rng.gauss(0.0, 0.08)
        step_km = rng.uniform(0.003, 0.010)
        lat += step_km / 111.2 * math.cos(heading)
        lon += step_km / (111.2 * math.cos(math.radians(lat))) * math.sin(heading)
        pts.append((lat, lon))
    return pts


def _activity(id, type, day, points) -> Activity:
    when = datetime(day.year, day.month, day.day, 9, 0)
    return Activity(
        id=id, name=f"act {id}", type=type, distance=len(points) * 6.5,
        moving_time=len(points) * 2, elapsed_time=len(points) * 2,
        total_elevation_gain=0.0, start_date=when, start_date_local=when,
        timezone="UTC", achievement_count=0, kudos_count=0, comment_count=0,
        athlete_count=1, photo_count=0, trainer=False, commute=False,
        manual=False, private=False, flagged=False, average_speed=0.0,
        max_speed=0.0, pr_count=0, total_photo_count=0,
        has_kudoed=False, summary_polyline=polyline_lib.encode(points),
        start_latlng=list(points[0]), end_latlng=list(points[-1]))


def _trip(points_per_leg: int) -> Project:
    """A long ride, then a long hike from where it ended: two legs of the
    same size."""
    rng = random.Random(519)
    ride = _track(rng, 45.0, 6.0, points_per_leg)
    hike = _track(rng, *ride[-1], points_per_leg)
    activities = [_activity(1, "Ride", date(2026, 5, 1), ride),
                  _activity(2, "Hike", date(2026, 5, 2), hike)]
    return Project(name="Big trip", activities=activities,
                   items=[ProjectItem(item_type="activity", activity_id=a.id) for a in activities])


def _clear_caches() -> None:
    for cached in (camera_mod._path, camera_mod._samples, camera_mod._pan_speeds,
                   camera_mod._marker_steps, camera_mod._fixed_zoom):
        cached.cache_clear()
    gc.collect()


def _render(project: Project, mode: str) -> None:
    """Legs, timeline, camera path, renderer, and one frame per integer zoom."""
    timeline = build_timeline(build_legs(project), LENGTH_S)
    camera_path(timeline, timeline.fps, SIZE, mode)
    frames = FrameRenderer(timeline, SIZE, project.name, tile_fetcher=_fake_tile, camera=mode)
    first = {}
    for n, shot in enumerate(frames.shots):
        first.setdefault(max(0, int(shot.zoom)), n)
    for n in sorted(first.values()):
        frames.frame(n)


@pytest.fixture(scope="module")
def big_trip() -> Project:
    return _trip(POINTS_PER_LEG)


@pytest.mark.parametrize("mode", CAMERA_MODES)
def test_the_pipeline_costs_under_120_bytes_per_point(big_trip, mode):
    # Fonts, icons and panels are cached once per process: warm them on a
    # small trip first, so only the route's own cost is measured.
    _render(_trip(200), mode)
    _clear_caches()
    n_points = 2 * POINTS_PER_LEG
    tracemalloc.start()
    try:
        _render(big_trip, mode)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        _clear_caches()
    assert peak / n_points < MAX_BYTES_PER_POINT, f"{mode}: {peak / n_points:.0f} B/point"

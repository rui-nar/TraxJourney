"""Render a trip video to MP4 (docs/TRIP_VIDEO_PLAN.md, U5; Convention 5).

``render_video`` is what ``src.video.job_runner`` calls on the ``video``
worker. It loads the trip, builds its timeline and camera path, and pipes
every frame — basemap (``basemap_bands``) plus overlay (``overlay``) — as raw
RGB into a local ``ffmpeg`` that encodes H.264 yuv420p MP4 with
``+faststart`` (D8).

Failures raise with fixed messages: the runner turns any exception into one of
its fixed user-facing reasons, and a message here never carries geometry or
ffmpeg's own output (which goes to the server log only).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from PIL import Image

from src.poster.tile_stitcher import TileFetcher
from src.utils.logging import get_logger
from src.video.basemap_bands import MAX_TILES, Basemaps, plan_bands
from src.video.camera import Shot, camera_path, frame_count
from src.video.overlay import Overlay
from src.video.timeline import Timeline, timeline_for_project

_log = get_logger(__name__)

Size = Tuple[int, int]
ProgressFn = Callable[[float, str], None]

# The job's progress is written to its row this often.
PROGRESS_EVERY = 30
# x264 picks ~1.5 frame threads per core by default, each buffering frames:
# on a many-core host that alone took ~560 MB at 1080p. Four bound it and
# still keep up with the frames Python draws.
_FFMPEG_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", "4",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart")


class VideoEncodeError(RuntimeError):
    """ffmpeg is missing, stopped reading frames or exited non-zero."""


class FrameRenderer:
    """Frame *n* of *timeline* at *size*: a pure function of both and *n*
    (Convention 3). Frames are cheapest asked for in order."""

    def __init__(self, timeline: Timeline, size: Size, title: str, *,
                 tile_fetcher: Optional[TileFetcher] = None,
                 max_tiles: int = MAX_TILES) -> None:
        self.timeline = timeline
        self.size = (int(size[0]), int(size[1]))
        self.fps = timeline.fps
        self.shots: Sequence[Shot] = camera_path(timeline, self.fps, self.size)
        self.plan = plan_bands(self.shots, self.size, max_tiles)
        if self.plan.max_band is not None:
            _log.info("Video basemap capped at zoom %d to stay within %d tiles (%d planned)",
                      self.plan.max_band, max_tiles, self.plan.tiles)
        self.basemaps = Basemaps(self.shots, self.size, self.plan, tile_fetcher=tile_fetcher)
        self.overlay = Overlay(timeline, self.size, title)

    def __len__(self) -> int:
        return len(self.shots)

    def basemap(self, n: int) -> Image.Image:
        return self.basemaps.frame(n)

    def frame(self, n: int) -> Image.Image:
        state = self.timeline.sample(n / self.fps)
        return self.overlay.draw(self.basemaps.frame(n), self.shots[n], state)


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise VideoEncodeError("ffmpeg is not installed")
    return path


def encode(frames: FrameRenderer, out_path: Path, *, progress: Optional[ProgressFn] = None,
           frame_range: Optional[range] = None) -> Path:
    """Pipe *frame_range* (default: all) of *frames* through ffmpeg into
    *out_path*, written under a temporary name and moved into place only
    when ffmpeg exits 0."""
    w, h = frames.size
    todo = frame_range if frame_range is not None else range(len(frames))
    part = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(frames.fps),
           "-i", "-", "-an", *_FFMPEG_ARGS, "-f", "mp4", str(part)]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        try:
            for i, n in enumerate(todo):
                data = frames.frame(n).tobytes()
                try:
                    proc.stdin.write(data)
                except (BrokenPipeError, OSError):
                    raise VideoEncodeError("ffmpeg stopped reading frames") from None
                if progress is not None and i % PROGRESS_EVERY == 0:
                    progress(0.97 * i / len(todo), "rendering")
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                raise VideoEncodeError("ffmpeg stopped reading frames") from None
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            try:
                proc.stdin.close()  # a dead ffmpeg's pipe, so GC doesn't flush into it
            except (BrokenPipeError, OSError):
                pass
        if code != 0:
            err.seek(0)
            _log.warning("ffmpeg exited with %s: %s", code,
                         err.read()[-2000:].decode("utf-8", "replace"))
            part.unlink(missing_ok=True)
            raise VideoEncodeError(f"ffmpeg exited with code {code}")
    os.replace(part, out_path)
    return out_path


def render_timeline(timeline: Timeline, size: Size, out_path: Path, *, title: str,
                    progress: Optional[ProgressFn] = None,
                    tile_fetcher: Optional[TileFetcher] = None,
                    max_tiles: int = MAX_TILES,
                    frame_range: Optional[range] = None) -> Path:
    """Render *timeline* (or *frame_range* of it) at *size* into *out_path*."""
    if progress is not None:
        progress(0.0, "planning")
    frames = FrameRenderer(timeline, size, title, tile_fetcher=tile_fetcher,
                           max_tiles=max_tiles)
    return encode(frames, out_path, progress=progress, frame_range=frame_range)


def _load_project(project_id: int, requester: int):
    """The trip, loaded through its owner as ``api/video.py``'s ``_load``
    does: the requester may be a companion (D12)."""
    from models.db import get_session
    from models.project_db import DBProject
    from src.project.project_repo import ProjectRepo

    with get_session() as sess:
        row = sess.get(DBProject, project_id)
        if row is None:
            raise LookupError("the trip no longer exists")
        project = ProjectRepo().get_project(sess, row.user_info_id, row.name,
                                            include_elevation=False,
                                            journal_user_id=requester)
    if project is None:
        raise LookupError("the trip no longer exists")
    return project


def render_video(job_id: int, user_info_id: int, project_id: int, request: dict,
                 out_path: Path, geometry: Optional[Dict[int, str]],
                 progress: ProgressFn, *,
                 tile_fetcher: Optional[TileFetcher] = None) -> Path:
    """Render job *job_id*'s video into *out_path* and return it (Convention 5).

    *request* is the job's ``{"length_s", "width", "height"}``; *geometry*
    the consent geometry of an encrypted trip (D2), used for this render
    only. ``NothingToAnimate`` propagates for the runner to report.
    """
    progress(0.0, "loading trip")
    project = _load_project(project_id, user_info_id)
    timeline = timeline_for_project(project, float(request["length_s"]), geometry=geometry)
    size = (int(request["width"]), int(request["height"]))
    _log.info("Video job %s: %d frames at %dx%d, %d clips", job_id,
              frame_count(timeline, timeline.fps), size[0], size[1], len(timeline.clips))
    return render_timeline(timeline, size, out_path, title=project.name,
                           progress=progress, tile_fetcher=tile_fetcher)

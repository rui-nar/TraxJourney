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

import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image

from src.poster.tile_stitcher import TileFetcher, render_basemap
from src.utils.logging import get_logger
from src.video import tile_prefetch
from src.video.basemap_bands import MAX_TILES, Basemaps, plan_bands, plan_requests
from src.video.camera import Shot, camera_path, frame_count
from src.video.overlay import Overlay
from src.video.timeline import FrameState, Timeline, timeline_for_project

try:
    import resource  # Linux/macOS only; not on Windows dev machines.
except ImportError:
    resource = None  # type: ignore[assignment]

_log = get_logger(__name__)

Size = Tuple[int, int]
ProgressFn = Callable[[float, str], None]

# The job's progress is written to its row this often.
PROGRESS_EVERY = 30
# docs/VIDEO_CAMERA_QUALITY_PLAN.md D7, chosen at gate G1 (docs/VIDEO.md):
# sharper route lines and map labels than crf 23, for files ~1.5x larger;
# ``-tune animation`` softened the map imagery, so no tune.
_DEFAULT_CRF = 20
_DEFAULT_TUNE: Optional[str] = None
# x264 picks ~1.5 frame threads per core by default, each buffering frames:
# on a many-core host that alone took ~560 MB at 1080p. Four bound it and
# still keep up with the frames Python draws. This is _encoder_args()'s
# result at the defaults above, kept as its own name (rather than computed)
# so a test can still monkeypatch the whole command line.
_FFMPEG_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", str(_DEFAULT_CRF), "-threads", "4",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart")


def _encoder_args(crf: int = _DEFAULT_CRF, tune: Optional[str] = _DEFAULT_TUNE) -> Tuple[str, ...]:
    """The libx264 flags for one encode (D4/U1): *crf* and *tune* are
    overridable (the bench CLI's ``--crf``/``--tune``). At the defaults
    this returns :data:`_FFMPEG_ARGS` itself (unchanged, and still the name a
    test overrides to break the encoder). ``tune`` of ``None`` or ``"none"``
    omits ``-tune`` entirely."""
    if crf == _DEFAULT_CRF and (tune is None or tune == "none"):
        return _FFMPEG_ARGS
    args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]
    if tune and tune != "none":
        args += ["-tune", tune]
    args += ["-threads", "4", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    return tuple(args)


class VideoEncodeError(RuntimeError):
    """ffmpeg is missing, stopped reading frames or exited non-zero."""


@dataclass
class StageTimes:
    """Wall time spent per render stage (D4/U1), accumulated across every
    frame of one job. ``fetch`` and ``stitch`` never double-count: ``stitch``
    is a sheet build's own time net of any ``fetch`` calls it made, and
    ``basemap`` is a frame's basemap lookup net of any ``fetch``/``stitch``
    a new sheet needed."""
    fetch: float = 0.0
    stitch: float = 0.0
    basemap: float = 0.0
    overlay: float = 0.0
    write: float = 0.0

    def add(self, stage: str, seconds: float) -> None:
        setattr(self, stage, getattr(self, stage) + max(seconds, 0.0))

    @property
    def total(self) -> float:
        return self.fetch + self.stitch + self.basemap + self.overlay + self.write


class _LazyDefaultFetcher:
    """The real network client, built on the first tile, exactly as
    ``render_basemap`` would build it itself, so a render that never needs a
    tile never needs ``MAPBOX_TOKEN``. It holds no reference to the renderer:
    the prefetcher's pool threads hold it, and must not keep a dropped
    renderer alive (docs/VIDEO_RENDER_TIME_PLAN.md D7). The pool calls it
    from several threads, so the client is built under a lock, once."""

    def __init__(self) -> None:
        self._fetcher: Optional[TileFetcher] = None
        self._lock = threading.Lock()

    def __call__(self, z: int, x: int, y: int) -> bytes:
        if self._fetcher is None:
            with self._lock:
                if self._fetcher is None:
                    from src.poster import tile_stitcher
                    self._fetcher = tile_stitcher._default_tile_fetcher()
        return self._fetcher(z, x, y)


class _TimedFetcher:
    """Times every tile fetch into *timings* (Do 1). Once the frame loop
    starts, *fetcher* is the renderer's :class:`TilePrefetcher`, so ``fetch``
    is the time the frame loop waits for tiles, not network time
    (docs/VIDEO_RENDER_TIME_PLAN.md D9)."""

    def __init__(self, fetcher: TileFetcher, timings: StageTimes) -> None:
        self.fetcher = fetcher
        self._timings = timings

    def __call__(self, z: int, x: int, y: int) -> bytes:
        start = time.perf_counter()
        try:
            return self.fetcher(z, x, y)
        finally:
            self._timings.add("fetch", time.perf_counter() - start)


class _TimedRender:
    """Times a sheet build (``render_basemap``) into *timings* (Do 1), net of
    any tile fetching it did through :class:`_TimedFetcher` — the remainder
    is decode/paste/resize (sheet stitching)."""

    def __init__(self, render: Callable[..., Image.Image], timings: StageTimes) -> None:
        self._render = render
        self._timings = timings

    def __call__(self, *args, **kwargs) -> Image.Image:
        fetch_before = self._timings.fetch
        start = time.perf_counter()
        img = self._render(*args, **kwargs)
        elapsed = time.perf_counter() - start
        self._timings.add("stitch", elapsed - (self._timings.fetch - fetch_before))
        return img


def _renderer_peak_rss_mb() -> float:
    """Peak RSS of this process, in MB — 0 on a platform without the
    ``resource`` module (Windows dev machines; the video worker runs on
    Linux)."""
    if resource is None:
        return 0.0
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _read_ffmpeg_vmhwm_kb(pid: int) -> Optional[int]:
    """ffmpeg's own peak resident set size (kB), read from its live
    ``/proc/<pid>/status`` (Linux only). ``VmHWM`` is a high-water mark
    tracked by the kernel and reset by ``exec``, unlike
    ``RUSAGE_CHILDREN.ru_maxrss`` which, on Linux, is at least as large as
    whatever this process (the parent) was resident at *its* peak before
    ``fork`` — a forked child's accounting starts from the parent's own
    figure. Must be called while ffmpeg is still alive: its ``mm`` — and so
    ``VmHWM`` — goes away once it exits, so this has to run before it is
    reaped by ``wait()``. Returns ``None`` if unreadable (no ``/proc``, or
    the process is already gone)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _ffmpeg_peak_rss_mb(peak_kb: Optional[int]) -> float:
    """ffmpeg's own peak RSS in MB, from the highest ``VmHWM`` sample seen
    while it was alive. Falls back to ``RUSAGE_CHILDREN`` (which, as noted on
    :func:`_read_ffmpeg_vmhwm_kb`, is the *renderer's* pre-fork RSS at a
    minimum, not ffmpeg's own peak) only when ``/proc`` wasn't available,
    e.g. on Windows or macOS dev machines — the video worker itself runs on
    Linux, where *peak_kb* is always set."""
    if peak_kb is not None:
        return peak_kb / 1024
    if resource is None:
        return 0.0
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024


def _log_render_summary(*, kind: str = "video", camera: str, frames: int, elapsed: float,
                        stages: StageTimes, sheets: int, tiles: int, renderer_mb: float,
                        ffmpeg_mb: float, tile_ms: float = 0.0, prefetch_misses: int = 0) -> None:
    """One INFO line per job: where the time went (Do 1). *kind* is
    ``"video"`` for a full render, ``"preview"`` for U2's animated WebP
    (#519 D1), which has no ffmpeg step (*ffmpeg_mb* is 0). *tile_ms* is the
    mean network time per tile, measured on the prefetcher's threads, and
    *prefetch_misses* the tiles the frame loop had to fetch itself
    (docs/VIDEO_RENDER_TIME_PLAN.md D9)."""
    per_frame = (lambda s: s / frames * 1000) if frames else (lambda s: 0.0)
    _log.info(
        "video render summary: kind=%s camera=%s frames=%d elapsed_s=%.2f ms_per_frame=%.1f "
        "fetch_ms=%.2f stitch_ms=%.2f basemap_ms=%.2f overlay_ms=%.2f write_ms=%.2f "
        "sheets=%d tiles=%d tile_ms=%.1f prefetch_misses=%d "
        "peak_rss_renderer_mb=%.0f peak_rss_ffmpeg_mb=%.0f",
        kind, camera, frames, elapsed, per_frame(elapsed),
        per_frame(stages.fetch), per_frame(stages.stitch), per_frame(stages.basemap),
        per_frame(stages.overlay), per_frame(stages.write),
        sheets, tiles, tile_ms, prefetch_misses, renderer_mb, ffmpeg_mb,
    )


class FrameRenderer:
    """Frame *n* of *timeline* at *size* in *camera* mode: a pure function
    of those and *n* (Convention 3). Frames are cheapest asked for in order.

    *shots*/*states*, given together, override the camera path this would
    otherwise build itself and the state ``timeline.sample(n / fps)`` would
    otherwise sample: frame *n* then simply draws ``shots[n]``/``states[n]``
    (U2's preview render, whose frame *n* is a different video frame's shot
    and state, not the *n*-th of its own path — review R2-2). *camera* still
    names the mode they were built in, for the render summary and the
    overlay's overview HUD layout.

    Tiles are fetched ahead of the frames through a :class:`TilePrefetcher`
    (docs/VIDEO_RENDER_TIME_PLAN.md D2, D7), created on the first
    :meth:`frame` (or :meth:`basemap`) call with the plan's requests from that
    frame on. :meth:`close` stops it; a renderer dropped without ``close()``
    stops it when it is collected. Once closed, tiles are fetched
    synchronously and the prefetcher is never restarted."""

    def __init__(self, timeline: Timeline, size: Size, title: str, *,
                 tile_fetcher: Optional[TileFetcher] = None,
                 max_tiles: int = MAX_TILES, camera: str = "variable",
                 shots: Optional[Sequence[Shot]] = None,
                 states: Optional[Sequence[FrameState]] = None) -> None:
        self.timeline = timeline
        self.size = (int(size[0]), int(size[1]))
        self.fps = timeline.fps
        self.camera = camera
        self.shots: Sequence[Shot] = (tuple(shots) if shots is not None
                                      else camera_path(timeline, self.fps, self.size, camera))
        self.states: Optional[Sequence[FrameState]] = tuple(states) if states is not None else None
        self.plan = plan_bands(self.shots, self.size, max_tiles)
        if self.plan.max_band is not None:
            _log.info("Video basemap capped at zoom %d to stay within %d tiles (%d planned)",
                      self.plan.max_band, max_tiles, self.plan.tiles)
        self.timings = StageTimes()
        # The raw fetcher the prefetcher's threads call: never anything that
        # refers to this renderer (D7, review R2-2).
        self._raw_fetcher: TileFetcher = (tile_fetcher if tile_fetcher is not None
                                          else _LazyDefaultFetcher())
        self._timed_fetcher = _TimedFetcher(self._raw_fetcher, self.timings)
        self._prefetcher: Optional[tile_prefetch.TilePrefetcher] = None
        self._closed = False
        self.basemaps = Basemaps(self.shots, self.size, self.plan,
                                 tile_fetcher=self._timed_fetcher,
                                 render=_TimedRender(render_basemap, self.timings))
        self.overlay = Overlay(timeline, self.size, title, camera)

    def __len__(self) -> int:
        return len(self.shots)

    def _start(self, n: int) -> None:
        """Create the prefetcher on the first frame asked for, *n*: it
        fetches the plan's tiles in first-use order from frame *n* on. Its
        threads start on its first tile. After :meth:`close`, it is created
        without threads, so tiles are fetched synchronously but still counted
        for the summary."""
        if self._prefetcher is not None:
            return
        requests = plan_requests(replace(self.plan, frames=self.plan.frames[n:]))
        threads = 0 if self._closed else tile_prefetch.PREFETCH_THREADS
        prefetcher = tile_prefetch.TilePrefetcher(self._raw_fetcher, requests, threads=threads,
                                                  window=tile_prefetch.PREFETCH_WINDOW)
        # The prefetcher's own bound close(), which holds no reference to
        # this renderer, so the renderer can still be collected (review R2-2).
        weakref.finalize(self, prefetcher.close)
        self._prefetcher = prefetcher
        self._timed_fetcher.fetcher = prefetcher

    def close(self) -> None:
        """Stop prefetching: queued fetches are dropped and in-flight ones are
        not waited for (D7). Idempotent; frames drawn after it fetch their
        tiles synchronously."""
        self._closed = True
        if self._prefetcher is not None:
            self._prefetcher.close()

    @property
    def tile_ms(self) -> float:
        """Mean network time per tile fetched so far, in ms (D9)."""
        p = self._prefetcher
        return p.net_seconds / p.fetched * 1000 if p is not None and p.fetched else 0.0

    @property
    def prefetch_misses(self) -> int:
        """Tiles the frame loop fetched itself because none was pending (D4)."""
        return self._prefetcher.misses if self._prefetcher is not None else 0

    def basemap(self, n: int) -> Image.Image:
        self._start(n)
        return self.basemaps.frame(n)

    def frame(self, n: int) -> Image.Image:
        self._start(n)
        state = self.states[n] if self.states is not None else self.timeline.sample(n / self.fps)
        fetch_before, stitch_before = self.timings.fetch, self.timings.stitch
        start = time.perf_counter()
        base = self.basemaps.frame(n)
        basemap_elapsed = (time.perf_counter() - start
                           - (self.timings.fetch - fetch_before)
                           - (self.timings.stitch - stitch_before))
        self.timings.add("basemap", basemap_elapsed)
        start = time.perf_counter()
        out = self.overlay.draw(base, self.shots[n], state)
        self.timings.add("overlay", time.perf_counter() - start)
        return out


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise VideoEncodeError("ffmpeg is not installed")
    return path


def encode(frames: FrameRenderer, out_path: Path, *, progress: Optional[ProgressFn] = None,
           frame_range: Optional[range] = None, crf: int = _DEFAULT_CRF,
           tune: Optional[str] = _DEFAULT_TUNE) -> Path:
    """Pipe *frame_range* (default: all) of *frames* through ffmpeg into
    *out_path*, written under a temporary name and moved into place only
    when ffmpeg exits 0. *crf*/*tune* override the encoder (D4/U1); their
    defaults are G1's choice (D7), :data:`_FFMPEG_ARGS`."""
    w, h = frames.size
    todo = frame_range if frame_range is not None else range(len(frames))
    part = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(frames.fps),
           "-i", "-", "-an", *_encoder_args(crf, tune), "-f", "mp4", str(part)]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        ffmpeg_peak_kb: Optional[int] = None
        start = time.perf_counter()
        try:
            for i, n in enumerate(todo):
                data = frames.frame(n).tobytes()
                write_start = time.perf_counter()
                try:
                    proc.stdin.write(data)
                except (BrokenPipeError, OSError):
                    raise VideoEncodeError("ffmpeg stopped reading frames") from None
                frames.timings.add("write", time.perf_counter() - write_start)
                if progress is not None and i % PROGRESS_EVERY == 0:
                    progress(0.97 * i / len(todo), "rendering")
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                raise VideoEncodeError("ffmpeg stopped reading frames") from None
            # ffmpeg keeps encoding buffered frames and (with +faststart)
            # rewrites the file for its moov atom after stdin closes, so its
            # peak can still grow here. Sample VmHWM (a high-water mark, so
            # every read before exit is enough) until it exits; proc.wait()
            # would reap it first and its /proc entry would be gone.
            while True:
                kb = _read_ffmpeg_vmhwm_kb(proc.pid)
                if kb is not None:
                    ffmpeg_peak_kb = kb if ffmpeg_peak_kb is None else max(ffmpeg_peak_kb, kb)
                if proc.poll() is not None:
                    break
                time.sleep(0.01)
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            try:
                proc.stdin.close()  # a dead ffmpeg's pipe, so GC doesn't flush into it
            except (BrokenPipeError, OSError):
                pass
            frames.close()  # stop prefetching, whatever ended the frame loop (D7)
        if code != 0:
            err.seek(0)
            _log.warning("ffmpeg exited with %s: %s", code,
                         err.read()[-2000:].decode("utf-8", "replace"))
            part.unlink(missing_ok=True)
            raise VideoEncodeError(f"ffmpeg exited with code {code}")
        elapsed = time.perf_counter() - start
        renderer_mb = _renderer_peak_rss_mb()
        ffmpeg_mb = _ffmpeg_peak_rss_mb(ffmpeg_peak_kb)
        _log_render_summary(camera=frames.camera, frames=len(todo), elapsed=elapsed, stages=frames.timings,
                            sheets=len(frames.plan.sheets), tiles=frames.plan.tiles,
                            renderer_mb=renderer_mb, ffmpeg_mb=ffmpeg_mb,
                            tile_ms=frames.tile_ms, prefetch_misses=frames.prefetch_misses)
    os.replace(part, out_path)
    return out_path


def render_timeline(timeline: Timeline, size: Size, out_path: Path, *, title: str,
                    progress: Optional[ProgressFn] = None,
                    tile_fetcher: Optional[TileFetcher] = None,
                    max_tiles: int = MAX_TILES,
                    frame_range: Optional[range] = None,
                    crf: int = _DEFAULT_CRF, tune: Optional[str] = _DEFAULT_TUNE,
                    camera: str = "variable") -> Path:
    """Render *timeline* (or *frame_range* of it) at *size* into *out_path*,
    in *camera* mode."""
    if progress is not None:
        progress(0.0, "planning")
    frames = FrameRenderer(timeline, size, title, tile_fetcher=tile_fetcher,
                           max_tiles=max_tiles, camera=camera)
    return encode(frames, out_path, progress=progress, frame_range=frame_range, crf=crf, tune=tune)


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

    *request* is the job's ``{"length_s", "width", "height", "camera"}``;
    a row written before ``camera`` existed has no such key and renders in
    ``"variable"`` mode. *geometry* the consent geometry of an encrypted trip
    (D2), used for this render only. ``NothingToAnimate`` propagates for the runner to report.
    """
    progress(0.0, "loading trip")
    project = _load_project(project_id, user_info_id)
    timeline = timeline_for_project(project, float(request["length_s"]), geometry=geometry)
    size = (int(request["width"]), int(request["height"]))
    camera = request.get("camera", "variable")
    _log.info("Video job %s: %d frames at %dx%d, %d clips, %s camera", job_id,
              frame_count(timeline, timeline.fps), size[0], size[1], len(timeline.clips),
              camera)
    return render_timeline(timeline, size, out_path, title=project.name,
                           progress=progress, tile_fetcher=tile_fetcher, camera=camera)


# ── preview render (docs/VIDEO_PREVIEW_PLAN.md D1, D4, Convention 3; U2) ────

PREVIEW_FPS = 8
PREVIEW_SIZE: Size = (320, 180)
# Pillow's ``duration`` is milliseconds per frame; 1000 / PREVIEW_FPS exactly.
PREVIEW_FRAME_MS = round(1000 / PREVIEW_FPS)
PREVIEW_QUALITY = 50   # D4: lossy, small enough to show inline without a CDN
# Every frame a key frame (libwebp's kmin=0, kmax=1). Between key frames,
# libwebp's lossy animation encoder keeps the previous frame wherever the new
# one differs by less than a quality-derived margin (about 10 levels per
# channel at quality 50), so a slow pan could decode nearer the frame before
# it than its own. Key frames only depend on their own frame; measured on the
# test trip at 720p framing, 30 s grows from about 400 to 575 KB and the
# encode time barely moves. Bit-identical frames (the title and end cards)
# are still merged into one longer frame.
PREVIEW_KEY_FRAMES = {"kmin": 0, "kmax": 1}


def preview_zoom_offset(target_width: int) -> float:
    """How much lower a preview shot's zoom is than the video's own frame at
    *target_width* (Convention 3): ``log2(target_width / 320)``, so a 320 px
    preview frame shows the same geographic area as the video's."""
    return math.log2(target_width / PREVIEW_SIZE[0])


def preview_frames(timeline: Timeline, shots: Sequence[Shot],
                   target_size: Size) -> Tuple[Tuple[Shot, ...], Tuple[FrameState, ...]]:
    """The preview's own (shot, state) pair per frame (D1): preview frame *n*
    takes *shots* — the video's own camera path, built at *target_size* and
    the timeline's own fps — index ``round(n * fps / PREVIEW_FPS)``, its zoom
    lowered by :func:`preview_zoom_offset`, and the trip state at that same
    shot's video time, ``timeline.sample(index / fps)``.

    *shots* must be built at *target_size* and the timeline's own fps: built
    at the preview's own size or frame rate, the camera would frame clips
    differently, because its mode floors and per-frame thresholds are counted
    on the video's own clock (D1; review R1-1)."""
    fps = timeline.fps
    ratio = fps / PREVIEW_FPS
    n_preview = frame_count(timeline, PREVIEW_FPS)
    offset = preview_zoom_offset(target_size[0])
    out_shots: List[Shot] = []
    out_states: List[FrameState] = []
    for n in range(n_preview):
        idx = min(round(n * ratio), len(shots) - 1)
        shot = shots[idx]
        out_shots.append(Shot(shot.lon, shot.lat, shot.zoom - offset, shot.flying))
        out_states.append(timeline.sample(idx / fps))
    return tuple(out_shots), tuple(out_states)


def _preview_frame_renderer(timeline: Timeline, target_size: Size, camera: str, title: str,
                            tile_fetcher: Optional[TileFetcher] = None) -> FrameRenderer:
    """The :class:`FrameRenderer` that draws every preview frame, at
    :data:`PREVIEW_SIZE`, through the video's own camera path and states
    (:func:`preview_frames`) — the same basemap and overlay machinery a full
    video uses. Shared by :func:`render_preview` and the bench's
    ``--preview``."""
    shots = camera_path(timeline, timeline.fps, target_size, camera)
    p_shots, p_states = preview_frames(timeline, shots, target_size)
    return FrameRenderer(timeline, PREVIEW_SIZE, title, tile_fetcher=tile_fetcher,
                         camera=camera, shots=p_shots, states=p_states)


def _write_preview_webp(frames: FrameRenderer, out_path: Path, *,
                        progress: Optional[ProgressFn] = None) -> Path:
    """Write every frame of *frames* into an animated WebP at *out_path*
    (D4), under a temporary name, moved into place only once Pillow has saved
    it. Pillow's animated WebP writer takes every frame as an in-memory list
    (``save_all``/``append_images``): there is no streaming API to pipe frames
    into as they are drawn, unlike ``encode()``'s ffmpeg pipe. Frames are
    buffered here instead, bounded to at most one preview's worth — about
    124 MB for a 90 s preview at 8 fps, 320×180 RGB (720 frames).

    libwebp merges consecutive bit-identical frames (the title and end
    cards' plateaus) into one longer frame, so the file may hold fewer
    frames than were drawn; each lasts a multiple of
    :data:`PREVIEW_FRAME_MS` and together they last exactly as long.
    The summary's ``write_ms`` is the WebP encode."""
    n = len(frames)
    part = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    start = time.perf_counter()
    images: List[Image.Image] = []
    try:
        for i in range(n):
            images.append(frames.frame(i))
            if progress is not None and i % PROGRESS_EVERY == 0:
                progress(0.97 * i / n, "rendering")
    finally:
        frames.close()  # stop prefetching, whatever ended the frame loop (D7)
    write_start = time.perf_counter()
    try:
        images[0].save(part, format="WEBP", save_all=True, append_images=images[1:],
                       duration=PREVIEW_FRAME_MS, loop=0, quality=PREVIEW_QUALITY,
                       **PREVIEW_KEY_FRAMES)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    frames.timings.add("write", time.perf_counter() - write_start)
    os.replace(part, out_path)
    elapsed = time.perf_counter() - start
    renderer_mb = _renderer_peak_rss_mb()
    _log_render_summary(kind="preview", camera=frames.camera, frames=n, elapsed=elapsed,
                        stages=frames.timings, sheets=len(frames.plan.sheets),
                        tiles=frames.plan.tiles, renderer_mb=renderer_mb, ffmpeg_mb=0.0,
                        tile_ms=frames.tile_ms, prefetch_misses=frames.prefetch_misses)
    return out_path


def render_preview(job_id: int, user_info_id: int, project_id: int, request: dict,
                   out_path: Path, geometry: Optional[Dict[int, str]],
                   progress: ProgressFn, *,
                   tile_fetcher: Optional[TileFetcher] = None) -> Path:
    """Render job *job_id*'s preview into *out_path* and return it, an
    animated WebP of the video's own camera path sampled at
    :data:`PREVIEW_FPS` and drawn at :data:`PREVIEW_SIZE` (Convention 5; D1).

    *request* is the job's ``{"length_s", "width", "height", "camera"}`` —
    ``width``/``height`` are the *video's* target resolution (720p or 1080p),
    whose camera path this samples; the preview itself is always drawn at
    :data:`PREVIEW_SIZE`. *geometry* is the consent geometry of an encrypted
    trip (D2), used for this render only. ``NothingToAnimate`` propagates for
    the runner to report.
    """
    progress(0.0, "loading trip")
    project = _load_project(project_id, user_info_id)
    timeline = timeline_for_project(project, float(request["length_s"]), geometry=geometry)
    target_size = (int(request["width"]), int(request["height"]))
    camera = request.get("camera", "variable")
    _log.info("Video preview job %s: %d frames at %dx%d framed as %dx%d, %d clips, %s camera",
              job_id, frame_count(timeline, PREVIEW_FPS), PREVIEW_SIZE[0], PREVIEW_SIZE[1],
              target_size[0], target_size[1], len(timeline.clips), camera)
    frames = _preview_frame_renderer(timeline, target_size, camera, project.name, tile_fetcher)
    return _write_preview_webp(frames, out_path, progress=progress)

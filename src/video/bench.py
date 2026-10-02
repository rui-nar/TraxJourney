"""Benchmark a real trip video render on the server, in any camera mode, with
candidate encoder settings — and dump frames for the sharpness comparison
(docs/VIDEO_CAMERA_QUALITY_PLAN.md, D4; U1)::

    python -m src.video.bench --project "Tour de France" --owner 3 \\
        --length 90 --height 1080 [--camera variable] [--crf 20] \\
        [--tune animation] [--dump-frames 450,1350,2250] [--out DIR] \\
        [--profile FILE]

Reads the database named by ``DATABASE_URL`` and fetches basemap tiles with
the server's ``MAPBOX_TOKEN``, exactly like a real render does — nothing here
is faked. Prints the same one-line render summary the renderer logs (frames,
setup time, ms per frame per stage, sheets, tiles, peak RSS). ``--profile
FILE`` runs the render under ``cProfile``, dumps its stats to *FILE* and
prints the top 30 functions by own time (D10, #517) — a profiled run's own
timings are inflated, so the gate's figures come from a run without it.

Run this on the server in its **own** container, never inside the live
worker, so a benchmark run can neither starve nor be starved by a user's
render::

    docker compose run --rm worker-video python -m src.video.bench \\
        --project "Tour de France" --owner 3 --length 90 --height 1080

See docs/VIDEO.md, "Profiling on the server".
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp
from typing import List, Optional, Tuple

from src.utils.logging import configure_logging, env_level
from src.video.camera import CAMERA_MODES
from src.video.renderer import (
    FrameRenderer,
    _ffmpeg,
    _preview_frame_renderer,
    _write_preview_webp,
    encode,
)
from src.video.timeline import NothingToAnimate, timeline_for_project


def _frame_size(height: int) -> Tuple[int, int]:
    """16:9 (api/video.py's ``WIDTH_FOR_HEIGHT`` ratio) for an arbitrary height."""
    return (round(height * 16 / 9), height)


def _parse_frame_numbers(value: str) -> List[int]:
    return [int(v) for v in value.split(",") if v.strip()]


def _print_profile(profiler: cProfile.Profile, path: str) -> None:
    """Dump *profiler*'s stats to *path* and print the top 30 functions by
    own time (``tottime``), after the render summary line (D10). Profiling
    inflates every timing: the gate's ms-per-frame figures come from a run
    without ``--profile`` (docs/VIDEO.md)."""
    profiler.dump_stats(path)
    pstats.Stats(profiler).sort_stats("tottime").print_stats(30)


def _dump_decoded_frame(video_path: Path, n: int, out_dir: Path) -> Path:
    """Frame *n* of *video_path*, decoded back with ffmpeg, as a PNG — the
    "after encoding" half of a ``--dump-frames`` pair."""
    out = out_dir / f"frame_{n:06d}_post.png"
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path),
         "-vf", f"select=eq(n\\,{n})", "-vframes", "1", str(out)],
        check=True)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.video.bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="trip name")
    ap.add_argument("--owner", required=True, type=int, help="owner's user_info id")
    ap.add_argument("--length", type=float, default=60.0, help="video length in seconds")
    ap.add_argument("--height", type=int, default=720, help="video height in pixels (width: 16:9)")
    ap.add_argument("--camera", choices=CAMERA_MODES, default="variable")
    ap.add_argument("--preview", action="store_true",
                    help="render the low-resolution animated WebP preview (docs/VIDEO_PREVIEW_PLAN.md "
                         "D1) instead of the MP4; --crf/--tune/--dump-frames don't apply")
    ap.add_argument("--crf", type=int, default=None,
                    help="override the encoder's -crf (default: the renderer's, 20)")
    ap.add_argument("--tune", choices=("animation", "none"), default=None,
                    help="override libx264's -tune (default: the renderer's, none)")
    ap.add_argument("--dump-frames", default=None,
                    help="comma-separated frame numbers to dump before and after encoding")
    ap.add_argument("--out", default=None,
                    help="directory for the video and dumped frames (default: a fresh temp dir)")
    ap.add_argument("--profile", default=None, metavar="FILE",
                    help="profile the render under cProfile, dump stats to FILE and print the "
                         "top 30 functions by own time; profiled timings are inflated, so the "
                         "gate's ms-per-frame figures come from a run without this flag")
    args = ap.parse_args(argv)

    configure_logging(level=env_level())

    # Imported here, not at the top: models.db builds its engine on import
    # (src.video.__main__'s convention).
    from models.db import get_session
    from src.project.project_repo import ProjectRepo

    with get_session() as sess:
        project = ProjectRepo().get_project(sess, args.owner, args.project, include_elevation=False)
    if project is None:
        print(f"no trip {args.project!r} for owner {args.owner}", file=sys.stderr)
        return 1

    try:
        timeline = timeline_for_project(project, args.length)
    except NothingToAnimate:
        print("nothing to animate", file=sys.stderr)
        return 1

    out_dir = Path(args.out) if args.out else Path(mkdtemp(prefix="video_bench_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    # tile_fetcher left at its default (None) in every branch below: the
    # renderer builds the real MAPBOX_TOKEN client itself, lazily, on the
    # first tile it needs.
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()

    if args.preview:
        # The video's own camera path is built at --height's target
        # resolution (D1); the preview itself is always 320x180 at 8 fps.
        frames = _preview_frame_renderer(timeline, _frame_size(args.height), args.camera,
                                         project.name)
        _write_preview_webp(frames, out_dir / "bench.webp")
        if profiler:
            profiler.disable()
            _print_profile(profiler, args.profile)
        print(f"output: {out_dir}")
        return 0

    video_path = out_dir / "bench.mp4"

    # Only override the encoder when asked: this is what keeps a bare
    # ``--crf``/``--tune``-less run at the renderer's own settings (Scope).
    encode_kwargs = {}
    if args.crf is not None:
        encode_kwargs["crf"] = args.crf
    if args.tune is not None:
        encode_kwargs["tune"] = args.tune

    frames = FrameRenderer(timeline, _frame_size(args.height), project.name,
                           camera=args.camera)
    encode(frames, video_path, **encode_kwargs)
    if profiler:
        profiler.disable()
        _print_profile(profiler, args.profile)

    frame_numbers = _parse_frame_numbers(args.dump_frames) if args.dump_frames else []
    for n in frame_numbers:
        frames.frame(n).save(out_dir / f"frame_{n:06d}_pre.png")
    for n in frame_numbers:
        _dump_decoded_frame(video_path, n, out_dir)

    print(f"output: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Print a trip's video timeline: its clips, their screen time and the legs
merged into each.

    python -m src.video --project "Tour de France" --owner 3 --length 60

Reads the database named by ``DATABASE_URL``, like the app does.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from src.video.pacing import max_clips
from src.video.timeline import NothingToAnimate, Timeline, timeline_for_project


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h"


def format_timeline(timeline: Timeline) -> str:
    """The allocation of *timeline* as plain text, one clip per block."""
    lines: List[str] = [
        f"{timeline.total_s:g} s video: title {timeline.title_s:g} s, "
        f"{len(timeline.clips)} clips (max {max_clips(timeline.total_s)}) "
        f"from {len(timeline.legs)} legs, end {timeline.end_s:g} s",
    ]
    for c in timeline.clips:
        lines.append(
            f"clip {c.index:>3}  {c.start_s:7.2f}-{c.end_s:7.2f} s  "
            f"{c.duration_s:5.2f} s  {c.clip.mode:<6}  real {_hours(c.clip.real_s)}  "
            f"{len(c.clip.legs)} leg(s)")
        for sub in c.subs:
            leg = sub.leg
            lines.append(
                f"    {leg.date or '----------'}  {leg.mode:<6} {leg.km:8.1f} km  "
                f"{_hours(leg.real_s):>8}  {leg.speed_kmh:6.1f} km/h  "
                f"{sub.end_s - sub.start_s:5.2f} s{'  (instant)' if sub.instant else ''}  "
                f"{leg.label}")
    for s in timeline.skipped:
        lines.append(f"skipped {s.kind} {s.ref}: {s.reason}")
    totals = ", ".join(f"{m} {km:.1f} km" for m, km in timeline.km_totals.items())
    lines.append(f"totals: {totals}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.video", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="trip name")
    ap.add_argument("--owner", required=True, type=int, help="owner's user_info id")
    ap.add_argument("--length", type=float, default=60.0, help="video length in seconds")
    args = ap.parse_args(argv)

    # Imported here, not at the top: models.db builds its engine on import.
    from models.db import get_session
    from src.project.project_repo import ProjectRepo

    with get_session() as sess:
        project = ProjectRepo().get_project(sess, args.owner, args.project,
                                            include_elevation=False)
    if project is None:
        print(f"no trip {args.project!r} for owner {args.owner}", file=sys.stderr)
        return 1
    try:
        timeline = timeline_for_project(project, args.length)
    except NothingToAnimate:
        print("nothing to animate", file=sys.stderr)
        return 1
    print(format_timeline(timeline))
    return 0


if __name__ == "__main__":
    sys.exit(main())

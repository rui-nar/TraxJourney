"""Where a video job's files live (docs/TRIP_VIDEO_PLAN.md, Convention 1).

Everything a job writes sits in ``data/users/<uid>/videos/<job_id>/``: the
rendered ``video.mp4`` and, for an encrypted trip whose owner consented, the
transient ``geometry.json`` of decrypted activity lines (D2). Keeping both
under the user's directory means account deletion's single ``rmtree`` removes
them (D6); the billing storage reconcile skips ``videos/`` (D13).

No other module builds these paths by hand. ``_DATA_DIR`` is read on every
call so tests can point it at a temporary directory.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Dict, Optional

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

GEOMETRY_FILE = "geometry.json"
RESULT_FILE = "video.mp4"


def videos_root(user_info_id: int) -> Path:
    return _DATA_DIR / "users" / str(user_info_id) / "videos"


def video_dir(user_info_id: int, job_id: int) -> Path:
    return videos_root(user_info_id) / str(job_id)


def geometry_path(user_info_id: int, job_id: int) -> Path:
    return video_dir(user_info_id, job_id) / GEOMETRY_FILE


def result_path(user_info_id: int, job_id: int) -> Path:
    return video_dir(user_info_id, job_id) / RESULT_FILE


def write_job_geometry(user_info_id: int, job_id: int, geometry: Dict[int, str]) -> Path:
    """Write the consent geometry for one job, readable by the server user only.

    The file is created with mode 0600 from the start rather than chmod-ed
    after, so there is no moment at which it is readable by others.
    """
    path = geometry_path(user_info_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({str(k): v for k, v in geometry.items()}, fh)
    return path


def read_job_geometry(user_info_id: int, job_id: int) -> Optional[Dict[int, str]]:
    """The job's consent geometry, or None when it has none."""
    path = geometry_path(user_info_id, job_id)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None
    return {int(k): v for k, v in raw.items()}


def delete_job_geometry(job_id: int) -> None:
    """Remove *job_id*'s ``geometry.json`` wherever it is. Idempotent.

    Found by job id alone, without the job row, so every terminal path can
    call it — including one where the row is gone or unreadable.
    """
    for path in (_DATA_DIR / "users").glob(f"*/videos/{int(job_id)}/{GEOMETRY_FILE}"):
        path.unlink(missing_ok=True)


def delete_job_files(user_info_id: int, job_id: int) -> None:
    """Remove the job's whole directory: the MP4, a partial one, and the
    geometry. Idempotent."""
    shutil.rmtree(video_dir(user_info_id, job_id), ignore_errors=True)


def stray_geometry_files():
    """``(job_id, path)`` for every ``geometry.json`` on disk, for the sweep."""
    for path in (_DATA_DIR / "users").glob(f"*/videos/*/{GEOMETRY_FILE}"):
        try:
            yield int(path.parent.name), path
        except ValueError:
            continue

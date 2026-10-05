#!/usr/bin/env python
"""Turn upright the stored thumbnails of memory and journal photos (#511).

A phone stores a portrait photo with an EXIF orientation tag instead of
rotated pixels. Thumbnails made before #511 ignored that tag, so they are
sideways. New uploads get an upright thumbnail (``src/utils/photo_store.py``);
this script regenerates the old ones through that same code and corrects each
owner's counted storage by the size difference. Originals are never touched.

Only ``users/<id>/memories/<id>/`` and ``users/<id>/journal/<id>/`` are
walked, never people's avatars. A photo is a candidate when its original
carries an orientation tag other than 1. Its thumbnail is rewritten only when
it differs from the upright one, so running this again rewrites nothing: the
original keeps its tag, so the tag alone cannot tell a fixed thumbnail from a
sideways one. An unreadable original is listed and skipped; the run goes on.

Counted storage is written through the app's own database connection, so run
it with the same environment as the server (``DATABASE_URL``). A dry run does
not touch the database.

NOT SAFE WITH THE API RUNNING: stop it first. ``--apply`` refuses to run
without ``--api-stopped``.

DRY-RUN BY DEFAULT — prints what would change and changes nothing.

Usage:
    python scripts/backfill_thumbnail_orientation.py --data-dir data
    python scripts/backfill_thumbnail_orientation.py --data-dir data --apply --api-stopped
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# Allow running as a plain script: put the project root on sys.path so the
# `src` package imports (same convention as scripts/reorder_polarsteps_memory_photos.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageChops, ImageStat  # noqa: E402

from src.billing.usage import record_delta  # noqa: E402
from src.utils.photo_paths import is_photo_name, photo_file  # noqa: E402
from src.utils.photo_store import InvalidPhoto, _thumbnail  # noqa: E402

#: Folders whose photos are walked. ``people`` (avatars) is left out on purpose.
KINDS = ("memories", "journal")

#: EXIF tag holding the orientation.
_ORIENTATION = 0x0112

#: Largest mean absolute per-channel difference (0-255) between the thumbnail
#: on disk and the upright one, both decoded from a JPEG written the way
#: ``write_photo_files`` writes it, for which the thumbnail counts as already
#: upright. The same pipeline gives 0; this only absorbs a JPEG encoder that
#: changed between the two writes. A sideways photo is far above it (12 and up
#: on real photos turned 180°); a picture so uniform that turning it changes
#: nothing scores under it and is left alone, which is harmless.
UPRIGHT_MAX_DIFFERENCE = 2.0


@dataclass
class Report:
    scanned: int = 0
    candidates: int = 0
    already_upright: int = 0
    rewritten: List[Path] = field(default_factory=list)
    skipped: List[Tuple[Path, str]] = field(default_factory=list)


def _encode(img: Image.Image) -> bytes:
    """The thumbnail as ``write_photo_files`` saves it."""
    img.info.clear()
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _mean_difference(a: Image.Image, b: Image.Image) -> float:
    means = ImageStat.Stat(ImageChops.difference(a, b)).mean
    return sum(means) / len(means)


def _is_same_picture(new_jpeg: bytes, thumb: Path) -> bool:
    new = Image.open(io.BytesIO(new_jpeg)).convert("RGB")
    with Image.open(thumb) as stored:
        old = stored.convert("RGB")
    return old.size == new.size and _mean_difference(old, new) <= UPRIGHT_MAX_DIFFERENCE


def _write_atomically(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _photos(data_dir: Path):
    """(user id, original, thumbnail) of every memory and journal photo that
    has both files."""
    users = data_dir / "users"
    if not users.is_dir():
        return
    for user_dir in sorted(users.iterdir()):
        if not user_dir.is_dir() or not user_dir.name.isdigit():
            continue
        for kind in KINDS:
            kind_dir = user_dir / kind
            if not kind_dir.is_dir():
                continue
            for folder in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
                for original in sorted(folder.glob("*.jpg")):
                    if not is_photo_name(original.stem):
                        continue
                    thumb = photo_file(folder, original.stem, "_thumb")
                    if thumb is not None and thumb.is_file():
                        yield int(user_dir.name), original, thumb


def _orientation(original: Path) -> Optional[int]:
    """The original's orientation tag, read from its header (no decode)."""
    with Image.open(original) as img:
        return img.getexif().get(_ORIENTATION)


def backfill(data_dir: Path, apply: bool) -> Report:
    report = Report()
    for user_id, original, thumb in _photos(data_dir):
        report.scanned += 1
        try:
            orientation = _orientation(original)
        except Exception as exc:  # any unreadable original: list it, go on
            report.skipped.append((original, f"original unreadable ({exc})"))
            continue
        if orientation in (None, 1):
            continue
        report.candidates += 1
        try:
            new_jpeg = _encode(_thumbnail(original.read_bytes()))
        except (InvalidPhoto, OSError) as exc:
            report.skipped.append((original, f"original unreadable ({exc})"))
            continue
        try:
            same = _is_same_picture(new_jpeg, thumb)
        except Exception as exc:  # a broken thumbnail is not ours to guess at
            report.skipped.append((thumb, f"thumbnail unreadable ({exc})"))
            continue
        if same:
            report.already_upright += 1
            continue
        if apply:
            old_size = thumb.stat().st_size
            _write_atomically(thumb, new_jpeg)
            record_delta(user_id, len(new_jpeg) - old_size)
        report.rewritten.append(thumb)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="Path to the data/ dir holding user photo files")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument("--api-stopped", action="store_true",
                    help="Confirm the API is stopped; required with --apply")
    args = ap.parse_args(argv)

    if args.apply and not args.api_stopped:
        print("ERROR: --apply rewrites thumbnails and counted storage behind the API's "
              "back. Stop the API first, then re-run with --apply --api-stopped.",
              file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Thumbnail orientation backfill [{mode}] — {data_dir} ===")
    report = backfill(data_dir, args.apply)

    verb = "rewritten" if args.apply else "to rewrite"
    for path in report.rewritten:
        print(f"{verb}: {path}")
    for path, reason in report.skipped:
        print(f"skipped: {path}: {reason}")
    print(f"scanned {report.scanned} / candidates {report.candidates} / "
          f"already upright {report.already_upright} / {verb} {len(report.rewritten)} / "
          f"skipped {len(report.skipped)}")
    if not args.apply and report.rewritten:
        print("Dry run: nothing written. Stop the API, then re-run with --apply --api-stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Remove duplicate photos from Polarsteps-imported memories (#566).

A Polarsteps re-import, or a step that served one photo twice, could store the
same photo several times in a memory. New imports skip a photo the memory
already holds, by the sha256 of its bytes kept in ``photo_order_json``
(``api/photo_order.py``); this script cleans the memories imported before that.

Only memories with a ``polarsteps_step_id`` are walked: in a manual memory the
owner may have uploaded the same photo twice on purpose. ``--project ID``
(repeatable) narrows the run. In each memory, two photos are duplicates when
their full-resolution files have the same sha256 (the import writes that file
byte for byte as downloaded). The first in ``photos_json`` order is kept, the
others are removed with their thumbnail and share copy, and each owner's
counted storage drops by what was removed. A listed photo whose file is
missing is reported and is never a duplicate. A UUID the list holds more than
once is one photo shown twice, not a duplicate: the repeats are dropped from
the list and its files are kept.

Every photo kept gets its hash recorded, unless it already carries one (a
replaced photo carries the hash of the download it replaced, on purpose), so
these memories get the same protection as new imports.

Each memory's row is written and committed first, then the removed photos'
files are deleted. A run interrupted in between leaves files that no row lists
(nothing shows them, but they still count against the owner's storage), never
a listed photo without a file. Files in a selected memory's folder that its
row does not list are reported, never deleted.

The database is opened through the app's own connection, so run it with the
same environment as the server (``DATABASE_URL``). A dry run does not touch
the database or the files.

NOT SAFE WITH THE API RUNNING: stop it first. ``--apply`` refuses to run
without ``--api-stopped``.

DRY-RUN BY DEFAULT — prints what would change and changes nothing.

Usage:
    python scripts/dedupe_memory_photos.py --data-dir data
    python scripts/dedupe_memory_photos.py --data-dir data --project 42
    python scripts/dedupe_memory_photos.py --data-dir data --apply --api-stopped
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Allow running as a plain script: put the project root on sys.path so the
# `src` package imports (same convention as scripts/backfill_thumbnail_orientation.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlmodel import select  # noqa: E402

from api.photo_order import dump_state, load_state, remove  # noqa: E402
from models.db import get_session  # noqa: E402
from models.project_db import DBMemory, DBProject  # noqa: E402
from src.billing.usage import bytes_of, unlink_and_record  # noqa: E402
from src.utils.photo_paths import photo_file, photo_files, photo_folder  # noqa: E402
from src.utils.photo_privacy import remove_share_copy  # noqa: E402


@dataclass
class Report:
    memories_touched: int = 0
    photos_removed: int = 0
    bytes_freed: int = 0
    hashes_recorded: int = 0
    repeats_collapsed: int = 0
    missing: List[Tuple[int, str]] = field(default_factory=list)
    unlisted: List[Path] = field(default_factory=list)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan(folder: Path, photos: List[str], report: Report,
          memory_id: int) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``(kept uuid -> hash, dropped uuid -> kept uuid it duplicates)``.

    *photos* holds each UUID once: a UUID listed twice is the same photo, not
    a duplicate of itself, and must never land in the dropped set (U3R1-1).
    """
    kept: Dict[str, str] = {}
    first_of: Dict[str, str] = {}
    dropped: Dict[str, str] = {}
    for uuid in photos:
        full = photo_file(folder, uuid)
        if full is None or not full.is_file():
            report.missing.append((memory_id, uuid))
            continue
        digest = _sha256(full)
        if digest in first_of:
            dropped[uuid] = first_of[digest]
        else:
            first_of[digest] = uuid
            kept[uuid] = digest
    return kept, dropped


def _unlisted(folder: Path, photos: List[str]) -> List[Path]:
    """Files in *folder* that belong to no photo in *photos*.

    A photo's files (original, thumbnail, share copy and its temp files) all
    start with its UUID.
    """
    if not folder.is_dir():
        return []
    listed = set(photos)
    return sorted(p for p in folder.iterdir() if p.is_file() and p.name[:36] not in listed)


def _dedupe_memory(data_dir: Path, memory_id: int, apply: bool, report: Report) -> None:
    with get_session() as sess:
        mem = sess.get(DBMemory, memory_id)
        owner_id = sess.get(DBProject, mem.project_id).user_info_id
        folder = photo_folder(data_dir, owner_id, "memories", memory_id)
        photos = [p for p in json.loads(mem.photos_json or "[]") if p]
        # A UUID listed more than once is one photo shown twice: keep its
        # first place and drop the repeats from the list, never its files.
        unique = list(dict.fromkeys(photos))
        repeats = len(photos) - len(unique)
        kept, dropped = _plan(folder, unique, report, memory_id)

        state = load_state(mem.photo_order_json)
        new_photos = unique
        for uuid in dropped:
            new_photos, state = remove(new_photos, state, uuid)
        new_hashes = {u: h for u, h in kept.items() if u not in state["hashes"]}
        state = {**state, "hashes": {**state["hashes"], **new_hashes}}

        freed = bytes_of(*photo_files(folder, dropped))
        changed = bool(dropped or new_hashes or repeats)
        if changed:
            report.memories_touched += 1
            report.repeats_collapsed += repeats
            report.photos_removed += len(dropped)
            report.bytes_freed += freed
            report.hashes_recorded += len(new_hashes)
            verb, hverb = ("removed", "recorded") if apply else ("to remove", "to record")
            print(f"memory {memory_id} (project {mem.project_id}): {verb} {len(dropped)} "
                  f"duplicate(s), {freed} bytes; {len(new_hashes)} hash(es) {hverb}; "
                  f"{repeats} repeated entr{'y' if repeats == 1 else 'ies'} collapsed")
            for uuid, original in dropped.items():
                print(f"    {verb}: {uuid} (same as {original})")
        if apply and changed:
            # The row first: an interruption before the unlink below leaves
            # unlisted files (reported by the next run), never a listed photo
            # without its file (review R1-2).
            mem.photos_json = json.dumps(new_photos)
            mem.photo_order_json = dump_state(state)
            sess.add(mem)
            sess.commit()

    if apply and dropped:
        unlink_and_record(owner_id, photo_files(folder, dropped))
        for uuid in dropped:
            full = photo_file(folder, uuid)
            if full is not None:
                remove_share_copy(full)

    listed_now = new_photos if apply else photos
    report.unlisted.extend(_unlisted(folder, listed_now))


def dedupe(data_dir: Path, apply: bool, projects: Optional[List[int]] = None) -> Report:
    report = Report()
    with get_session() as sess:
        query = select(DBMemory.id).where(DBMemory.polarsteps_step_id.is_not(None))
        if projects:
            query = query.where(DBMemory.project_id.in_(sorted(set(projects))))
        memory_ids = list(sess.exec(query.order_by(DBMemory.project_id, DBMemory.id)).all())
    for memory_id in memory_ids:
        _dedupe_memory(data_dir, memory_id, apply, report)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="Path to the data/ dir holding user photo files")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument("--api-stopped", action="store_true",
                    help="Confirm the API is stopped; required with --apply")
    ap.add_argument("--project", action="append", type=int, default=[], metavar="ID",
                    help="Only memories of this project (repeatable; default: every project)")
    args = ap.parse_args(argv)

    if args.apply and not args.api_stopped:
        print("ERROR: --apply deletes photos and rewrites memories behind the API's "
              "back. Stop the API first, then re-run with --apply --api-stopped.",
              file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Polarsteps memory photo dedup [{mode}] — {data_dir} ===")
    report = dedupe(data_dir, args.apply, args.project)

    for memory_id, uuid in report.missing:
        print(f"memory {memory_id}: file missing for {uuid}, never counted as a duplicate")
    for path in report.unlisted:
        print(f"unlisted file (not deleted): {path}")
    verb = "removed" if args.apply else "to remove"
    print(f"memories touched {report.memories_touched} / photos {verb} {report.photos_removed} / "
          f"bytes freed {report.bytes_freed} / hashes recorded {report.hashes_recorded} / "
          f"missing files {len(report.missing)} / unlisted files {len(report.unlisted)} / "
          f"repeats collapsed {report.repeats_collapsed}")
    if not args.apply and report.memories_touched:
        print("Dry run: nothing written. Stop the API, then re-run with --apply --api-stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

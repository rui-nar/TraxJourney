#!/usr/bin/env python
"""Backfill correct photo order for memories scrambled by the issue #237 race.

Before that fix (see api/memories.py's ``_write_memory_photo`` and
api/photo_locks.py), Polarsteps import fired every photo of a step as a
concurrent background download with no ordering info sent to the server, so
``photos_json`` ended up in whatever order the downloads happened to
complete — not Polarsteps' original order. New imports are fixed; this
script repairs memories imported *before* the fix.

It can still recover the original order because ``_save_photo_files`` always
wrote the full-resolution file to disk byte-for-byte unmodified (only the
thumbnail is re-encoded) — so re-downloading a step's photos from Polarsteps
and content-hashing them against the locally stored full-res files recovers
which local UUID belongs at which position, even though the order they're
currently stored in is wrong.

For each project with Polarsteps-imported memories, the source trip is
resolved from ``projectsyncmeta.linked_ps_trip_id`` (set when the project is
linked for auto-sync), or from an explicit ``--project-trip
PROJECT_ID:TRIP_ID`` override for projects that were imported once and never
linked. The owner's Polarsteps session is the ``remember_token`` already
stored in ``polarstepstoken`` (see scripts/inspect_polarsteps_steps.py) — no
credentials need to be supplied.

A memory is left untouched (flagged for manual review, not guessed at) when
too many of its source downloads fail or too few of its local photos match
any source photo by content — that usually means the trip changed on
Polarsteps since import, not that this script's logic is wrong.

DRY-RUN BY DEFAULT — prints the plan and changes nothing. Pass --apply to write.
Always run against a copy first.

Usage:
    python scripts/reorder_polarsteps_memory_photos.py --db "traxjourney.db" --data-dir data
    python scripts/reorder_polarsteps_memory_photos.py --db copy.db --data-dir data --apply

    # a project that was imported once and never linked for auto-sync needs
    # an explicit trip id (repeatable, one per project):
    python scripts/reorder_polarsteps_memory_photos.py --db copy.db --data-dir data \\
        --project-trip 42:9876543210
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# Allow running as a plain script: put the project root on sys.path so the
# `src` package imports (same convention as scripts/dedupe_polarsteps_memories.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.api.polarsteps_client import PolarstepsClient, format_step  # noqa: E402

ClientFactory = Callable[[str], "object"]
Downloader = Callable[[str], bytes]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_photo_hashes(data_dir: Path, owner_id: int, memory_id: int, uuids: List[str]) -> Dict[str, str]:
    """uuid -> sha256 of its on-disk full-res file; skips any file that's missing."""
    base = data_dir / "users" / str(owner_id) / "memories" / str(memory_id)
    hashes: Dict[str, str] = {}
    for u in uuids:
        f = base / f"{u}.jpg"
        if f.exists():
            hashes[u] = _sha256(f.read_bytes())
    return hashes


def plan_memory_reorder(
    current_uuids: List[str],
    source_photos: List[dict],
    local_hashes: Dict[str, str],
    download: Downloader,
) -> Tuple[Optional[List[str]], str]:
    """Work out the corrected photo order for one memory.

    *source_photos* is ``format_step(raw_step)['photos']`` — Polarsteps'
    original order. *local_hashes* maps each currently-stored UUID to the
    sha256 of its on-disk full-res file.

    Returns ``(None, note)`` when there's nothing to change or the memory
    should be left for manual review, or ``(new_order, note)`` with the
    corrected UUID list, matched-by-content in source order followed by any
    local photo that didn't match anything (never dropped).
    """
    if not current_uuids:
        return None, "no photos stored, nothing to reorder"

    hash_to_uuid = {h: u for u, h in local_hashes.items()}
    matched: List[str] = []
    seen: set = set()
    failures = 0
    attempted = 0
    for photo in source_photos:
        url = photo.get("url")
        if not url:
            continue
        attempted += 1
        try:
            data = download(url)
        except Exception:
            failures += 1
            continue
        uuid = hash_to_uuid.get(_sha256(data))
        if uuid and uuid not in seen:
            matched.append(uuid)
            seen.add(uuid)

    if attempted and failures > attempted / 2:
        return None, f"flagged for manual review: {failures}/{attempted} source downloads failed"

    unmatched = [u for u in current_uuids if u not in seen]
    if matched and len(unmatched) > len(current_uuids) / 2:
        return None, (
            f"flagged for manual review: only {len(matched)}/{len(current_uuids)} "
            "local photos matched a source photo by content"
        )

    new_order = matched + unmatched
    if new_order == current_uuids:
        return None, "already in correct order"
    return new_order, f"{current_uuids} -> {new_order}"


def _parse_project_trip_overrides(pairs: List[str]) -> Dict[int, int]:
    overrides: Dict[int, int] = {}
    for pair in pairs:
        project_id_str, _, trip_id_str = pair.partition(":")
        overrides[int(project_id_str)] = int(trip_id_str)
    return overrides


def run(
    con: sqlite3.Connection,
    data_dir: Path,
    apply: bool,
    overrides: Dict[int, int],
    client_factory: Optional[ClientFactory] = None,
    download: Optional[Downloader] = None,
) -> int:
    """Return the number of memories whose photos_json was (or would be) corrected."""
    if client_factory is None:
        client_factory = PolarstepsClient  # resolved at call time so tests can monkeypatch it
    if download is None:
        import requests as _req
        download = lambda url: _req.get(url, timeout=30).content  # noqa: E731

    memories = con.execute(
        "SELECT id, project_id, photos_json, polarsteps_step_id FROM memory "
        "WHERE polarsteps_step_id IS NOT NULL ORDER BY project_id, id"
    ).fetchall()
    if not memories:
        print("No Polarsteps-imported memories found. Nothing to do.")
        return 0

    by_project: Dict[int, list] = {}
    for m in memories:
        by_project.setdefault(m["project_id"], []).append(m)

    changed = 0
    for project_id, project_memories in by_project.items():
        proj = con.execute(
            "SELECT user_info_id FROM project WHERE id=?", (project_id,)
        ).fetchone()
        if proj is None:
            print(f"• project {project_id}: not found, skipping {len(project_memories)} memory(ies)")
            continue
        owner_id = proj["user_info_id"]

        trip_id = overrides.get(project_id)
        if trip_id is None:
            meta = con.execute(
                "SELECT linked_ps_trip_id FROM projectsyncmeta WHERE project_id=?", (project_id,)
            ).fetchone()
            trip_id = meta["linked_ps_trip_id"] if meta else None
        if trip_id is None:
            print(
                f"• project {project_id}: no linked Polarsteps trip and no --project-trip override, "
                f"skipping {len(project_memories)} memory(ies)"
            )
            continue

        tok = con.execute(
            "SELECT remember_token FROM polarstepstoken WHERE user_info_id=?", (owner_id,)
        ).fetchone()
        if tok is None or not tok["remember_token"]:
            print(f"• project {project_id}: owner has no stored Polarsteps token, skipping")
            continue

        client = client_factory(tok["remember_token"])
        try:
            raw_steps = client.get_trip_steps(trip_id)
        except Exception as exc:
            print(f"• project {project_id}: failed to fetch trip {trip_id} steps ({exc}), skipping")
            continue
        steps_by_id = {s.get("id"): format_step(s) for s in raw_steps}

        print(f"\n• project {project_id} (trip {trip_id}, owner {owner_id}): "
              f"{len(project_memories)} Polarsteps memory(ies)")
        for m in project_memories:
            step = steps_by_id.get(m["polarsteps_step_id"])
            if step is None:
                print(f"    memory {m['id']}: step {m['polarsteps_step_id']} not found on Polarsteps, skipping")
                continue

            current_uuids = [u for u in json.loads(m["photos_json"] or "[]") if u]
            local_hashes = _local_photo_hashes(data_dir, owner_id, m["id"], current_uuids)
            new_order, note = plan_memory_reorder(current_uuids, step["photos"], local_hashes, download)

            if new_order is None:
                print(f"    memory {m['id']}: {note}")
                continue

            print(f"    memory {m['id']}: {note}")
            changed += 1
            if apply:
                con.execute(
                    "UPDATE memory SET photos_json=? WHERE id=?",
                    (json.dumps(new_order), m["id"]),
                )

    if apply:
        con.commit()
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="Path to the SQLite database file")
    ap.add_argument("--data-dir", required=True, help="Path to the data/ dir holding user photo files")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument(
        "--project-trip", action="append", default=[], metavar="PROJECT_ID:TRIP_ID",
        help="Explicit trip id for a project with no linked_ps_trip_id (repeatable)",
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 2

    overrides = _parse_project_trip_overrides(args.project_trip)

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Polarsteps memory photo-order backfill [{mode}] — {db_path} ===")
    changed = run(con, data_dir, args.apply, overrides)

    if args.apply:
        print(f"\nAPPLIED: corrected {changed} memory(ies).")
    else:
        print(f"\nDRY-RUN: would correct {changed} memory(ies). Re-run with --apply to write.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

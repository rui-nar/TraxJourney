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
credentials need to be supplied, but the server's CREDENTIALS_ENCRYPTION_KEY
must be set, since the stored token is encrypted.

Only memories imported before the fix are touched: ``--imported-before
YYYY-MM-DD`` (required) selects a memory when its oldest stored full-res
file was written before that date (UTC). ``memory`` has no creation
timestamp, and an import writes every photo file at import time, so the
oldest file's mtime is the import date; photos added by hand later are newer
and do not move it. A memory with no stored file is not selected (nothing to
reorder). ``--project ID`` (repeatable) narrows the run to those projects.

A memory is left untouched (flagged for manual review, not guessed at) when
any of its source downloads fails — a photo that could not be compared would
otherwise be moved to the end, scrambling a memory that may be correct — or
when too few of its local photos match any source photo by content, which
usually means the trip changed on Polarsteps since import, not that this
script's logic is wrong.

What it writes (issue #237's rank model, see api/photo_order.py): a dense
``photos_json`` (no ``null`` slots, which the reader below already skips) and
``photo_order_json`` reset to ``{"epoch": <unchanged>, "ranks": {}}`` — the
repaired list is the order now, and old ranks would place the next import
photo against the scrambled one. The epoch is kept so a download queued
before a re-import is still dropped. A DB not yet migrated to that column is
refused.

It may run with the API live: each memory is written and committed on its
own, and only if neither column changed since the run read it; a memory
edited meanwhile is skipped and reported, never overwritten.

DRY-RUN BY DEFAULT — prints the plan and changes nothing. Pass --apply to write.
Always take a DB copy first (docs/RELEASING.md, post-deploy owner actions).

Usage:
    python scripts/reorder_polarsteps_memory_photos.py --db "traxjourney.db" --data-dir data \\
        --imported-before 2026-08-27
    python scripts/reorder_polarsteps_memory_photos.py --db copy.db --data-dir data \\
        --imported-before 2026-08-27 --apply

    # a project that was imported once and never linked for auto-sync needs
    # an explicit trip id (repeatable, one per project); --project limits the
    # run to the given projects (repeatable):
    python scripts/reorder_polarsteps_memory_photos.py --db copy.db --data-dir data \\
        --imported-before 2026-08-27 --project 42 --project-trip 42:9876543210
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# Allow running as a plain script: put the project root on sys.path so the
# `src` package imports (same convention as scripts/dedupe_polarsteps_memories.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.api.polarsteps_client import PolarstepsClient, format_step  # noqa: E402
from src.auth.credentials_crypto import CredentialDecryptError, decrypt_credential  # noqa: E402
from src.utils.photo_paths import photo_file, photo_folder  # noqa: E402
from api.photo_order import dump_state, load_state  # noqa: E402

ClientFactory = Callable[[str], "object"]
Downloader = Callable[[str], bytes]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_photo_hashes(data_dir: Path, owner_id: int, memory_id: int, uuids: List[str]) -> Dict[str, str]:
    """uuid -> sha256 of its on-disk full-res file; skips any file that's missing."""
    base = photo_folder(data_dir, owner_id, "memories", memory_id)
    hashes: Dict[str, str] = {}
    for u in uuids:
        f = photo_file(base, u)
        if f is not None and f.exists():
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
    for photo in source_photos:
        url = photo.get("url")
        if not url:
            continue
        # Any failure flags the whole memory: the photo it would have matched
        # would land among the unmatched at the end, a partial reorder that
        # can scramble a memory already in the right order (U7R1-1).
        try:
            data = download(url)
        except Exception as exc:
            return None, f"flagged for manual review: source download failed ({type(exc).__name__}: {exc})"
        uuid = hash_to_uuid.get(_sha256(data))
        if uuid and uuid not in seen:
            matched.append(uuid)
            seen.add(uuid)

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


def _has_photo_order_column(con: sqlite3.Connection) -> bool:
    """True once migration 4b9d2e7a1c63 has added ``memory.photo_order_json``."""
    return any(r[1] == "photo_order_json" for r in con.execute("PRAGMA table_info(memory)"))


def _write_reorder(con: sqlite3.Connection, memory, new_order: List[str]) -> bool:
    """Store *new_order* with ranks reset and the epoch kept; commit at once.

    Compare-and-set against the values the plan was computed from, so a live
    API edit made since then is never overwritten. Committing per memory holds
    the write lock for one statement instead of the whole network-bound run.
    Returns False when the memory changed (or was deleted) and was left alone.
    """
    state = load_state(memory["photo_order_json"])
    cur = con.execute(
        "UPDATE memory SET photos_json=?, photo_order_json=? "
        "WHERE id=? AND photos_json IS ? AND photo_order_json IS ?",
        (
            json.dumps(new_order),
            dump_state({"epoch": state["epoch"], "ranks": {}}),
            memory["id"],
            memory["photos_json"],
            memory["photo_order_json"],
        ),
    )
    con.commit()
    return cur.rowcount == 1


def _parse_project_trip_overrides(pairs: List[str]) -> Dict[int, int]:
    overrides: Dict[int, int] = {}
    for pair in pairs:
        project_id_str, _, trip_id_str = pair.partition(":")
        overrides[int(project_id_str)] = int(trip_id_str)
    return overrides


def _parse_cutoff(value: str) -> float:
    """``YYYY-MM-DD`` -> POSIX timestamp of that day's 00:00 UTC."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")
    return day.replace(tzinfo=timezone.utc).timestamp()


def _oldest_original_mtime(data_dir: Path, owner_id: int, memory_id: int, uuids: List[str]) -> Optional[float]:
    """mtime of the memory's oldest stored full-res file, or None if none is on disk."""
    base = photo_folder(data_dir, owner_id, "memories", memory_id)
    mtimes = []
    for u in uuids:
        f = photo_file(base, u)
        if f is not None and f.exists():
            mtimes.append(f.stat().st_mtime)
    return min(mtimes) if mtimes else None


def _http_download(url: str) -> bytes:
    import requests as _req
    resp = _req.get(url, timeout=30)
    # An error page is a failed download, not a photo that matches nothing.
    resp.raise_for_status()
    return resp.content


def run(
    con: sqlite3.Connection,
    data_dir: Path,
    apply: bool,
    overrides: Dict[int, int],
    *,
    imported_before: float,
    projects: Optional[Iterable[int]] = None,
    client_factory: Optional[ClientFactory] = None,
    download: Optional[Downloader] = None,
) -> int:
    """Return the number of memories whose photos_json was (or would be) corrected.

    Only memories whose oldest stored original predates *imported_before* (a
    POSIX timestamp) are considered, and only in *projects* when given.
    """
    if client_factory is None:
        client_factory = PolarstepsClient  # resolved at call time so tests can monkeypatch it
    if download is None:
        download = _http_download

    sql = (
        "SELECT id, project_id, photos_json, photo_order_json, polarsteps_step_id FROM memory "
        "WHERE polarsteps_step_id IS NOT NULL"
    )
    params: List[int] = []
    if projects:
        project_ids = sorted(set(projects))
        sql += f" AND project_id IN ({','.join('?' * len(project_ids))})"
        params.extend(project_ids)
    memories = con.execute(sql + " ORDER BY project_id, id", params).fetchall()
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

        selected = []
        for m in project_memories:
            uuids = [u for u in json.loads(m["photos_json"] or "[]") if u]
            oldest = _oldest_original_mtime(data_dir, owner_id, m["id"], uuids)
            if oldest is not None and oldest < imported_before:
                selected.append(m)
        if len(selected) < len(project_memories):
            print(f"• project {project_id}: {len(project_memories) - len(selected)} memory(ies) "
                  "imported on or after --imported-before (or with no stored photo), not selected")
        if not selected:
            continue
        project_memories = selected

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

        try:
            remember_token = decrypt_credential(tok["remember_token"], "polarstepstoken.remember_token")
        except CredentialDecryptError as exc:
            print(f"• project {project_id}: owner's Polarsteps token unreadable ({exc}), skipping")
            continue
        client = client_factory(remember_token)
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
            if apply and not _write_reorder(con, m, new_order):
                print(f"    memory {m['id']}: changed or deleted during the run, left untouched")
                continue
            changed += 1

    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="Path to the SQLite database file")
    ap.add_argument("--data-dir", required=True, help="Path to the data/ dir holding user photo files")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument(
        "--imported-before", required=True, type=_parse_cutoff, metavar="YYYY-MM-DD",
        help="Only memories whose oldest stored photo file predates this day (00:00 UTC): "
             "the date the #239 fix (v0.48.0) reached the server",
    )
    ap.add_argument(
        "--project", action="append", type=int, default=[], metavar="ID",
        help="Only memories of this project (repeatable; default: every project)",
    )
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
    if not _has_photo_order_column(con):
        print(
            "ERROR: memory.photo_order_json is missing: this DB predates migration "
            "4b9d2e7a1c63. Start the new API image once (it runs `alembic upgrade head`) "
            "and run this script against the migrated DB.",
            file=sys.stderr,
        )
        con.close()
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Polarsteps memory photo-order backfill [{mode}] — {db_path} ===")
    changed = run(con, data_dir, args.apply, overrides,
                  imported_before=args.imported_before, projects=args.project)

    if args.apply:
        print(f"\nAPPLIED: corrected {changed} memory(ies).")
    else:
        print(f"\nDRY-RUN: would correct {changed} memory(ies). Re-run with --apply to write.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

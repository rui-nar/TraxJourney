#!/usr/bin/env python
"""Repair duplicate Polarsteps memories created by the old split-brain dedup.

Before the write path learned the name+date fallback, re-importing a trip created
a second memory for every step whose original copy predated the
``polarsteps_step_id`` column. This merges each such duplicate group back to a
single memory.

For every project, memories are grouped by the canonical ``step_key``
(normalized name + date) shared with the live import code. In a group of >1:

  * the **survivor** is the richest copy (most photos, then longest description);
  * the survivor's ``polarsteps_step_id`` is backfilled from a twin if it lacks
    one, so it dedups cleanly on future imports;
  * the other rows (and their project-item entries and, with --data-dir, their
    photo files) are deleted, then the project's item positions are compacted.

DRY-RUN BY DEFAULT — prints the plan and changes nothing. Pass --apply to write.
Always run against a copy first.

Usage:
    python scripts/dedupe_polarsteps_memories.py --db "traxjourney.db"
    python scripts/dedupe_polarsteps_memories.py --db copy.db --data-dir data --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Reuse the exact normalization the API uses, so the script and the live dedup
# never disagree about what counts as a duplicate.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.project.memory_match import step_key  # noqa: E402


def _nphotos(photos_json: str | None) -> int:
    try:
        return len(json.loads(photos_json or "[]"))
    except (ValueError, TypeError):
        return 0


def _richness(row: sqlite3.Row) -> tuple[int, int]:
    """Sort key for picking the survivor: (photo count, description length)."""
    return (_nphotos(row["photos_json"]), len(row["description"] or ""))


def _photo_files(data_dir: Path, user_id: int, memory_id: int, photos_json: str | None) -> list[Path]:
    base = data_dir / "users" / str(user_id) / "memories" / str(memory_id)
    out: list[Path] = []
    try:
        uuids = json.loads(photos_json or "[]")
    except (ValueError, TypeError):
        uuids = []
    for u in uuids:
        out.append(base / f"{u}.jpg")
        out.append(base / f"{u}_thumb.jpg")
    return out


def find_duplicate_groups(con: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    """All same-project memory groups sharing a step_key, size > 1."""
    rows = con.execute(
        "SELECT id, project_id, name, date, description, photos_json, "
        "polarsteps_step_id FROM memory ORDER BY id"
    ).fetchall()
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for r in rows:
        key = (r["project_id"], *step_key(r["name"], r["date"]))
        groups.setdefault(key, []).append(r)
    return [g for g in groups.values() if len(g) > 1]


def project_owner(con: sqlite3.Connection, project_id: int) -> int | None:
    row = con.execute(
        "SELECT user_info_id FROM project WHERE id=?", (project_id,)
    ).fetchone()
    return row["user_info_id"] if row else None


def compact_positions(con: sqlite3.Connection, project_id: int, apply: bool) -> None:
    items = con.execute(
        "SELECT id FROM projectitem WHERE project_id=? ORDER BY position, id",
        (project_id,),
    ).fetchall()
    for new_pos, item in enumerate(items):
        if apply:
            con.execute(
                "UPDATE projectitem SET position=? WHERE id=?", (new_pos, item["id"])
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="Path to the SQLite database file")
    ap.add_argument("--data-dir", help="Path to the data/ dir, to also delete orphaned photo files")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir) if args.data_dir else None

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    groups = find_duplicate_groups(con)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Polarsteps memory dedupe [{mode}] — {db_path} ===")
    if not groups:
        print("No duplicate groups found. Nothing to do.")
        return 0

    total_deleted = 0
    touched_projects: set[int] = set()

    for group in groups:
        survivor = max(group, key=_richness)
        losers = [r for r in group if r["id"] != survivor["id"]]
        # Ensure the survivor carries a step id (backfill from a twin if needed).
        step_id = survivor["polarsteps_step_id"]
        if step_id is None:
            twin = next((r for r in losers if r["polarsteps_step_id"] is not None), None)
            step_id = twin["polarsteps_step_id"] if twin else None

        pid = survivor["project_id"]
        touched_projects.add(pid)
        print(f"\n• project {pid}  key={step_key(survivor['name'], survivor['date'])}")
        print(f"    KEEP   id={survivor['id']} step_id={survivor['polarsteps_step_id']} "
              f"photos={_nphotos(survivor['photos_json'])} desc={len(survivor['description'] or '')}")

        # Delete losers FIRST, then backfill the survivor's step id. Doing it the
        # other way round would transiently put the same step id on two rows and
        # trip the partial unique index (uq_memory_project_polarsteps_step_id).
        for loser in losers:
            print(f"    DELETE id={loser['id']} step_id={loser['polarsteps_step_id']} "
                  f"photos={_nphotos(loser['photos_json'])} desc={len(loser['description'] or '')}")
            files = []
            if data_dir is not None:
                owner = project_owner(con, pid)
                if owner is not None:
                    files = [f for f in _photo_files(data_dir, owner, loser["id"], loser["photos_json"]) if f.exists()]
                    for f in files:
                        print(f"       rm {f}")
            if args.apply:
                con.execute("DELETE FROM projectitem WHERE item_type='memory' AND memory_id=?", (loser["id"],))
                con.execute("DELETE FROM memory WHERE id=?", (loser["id"],))
                for f in files:
                    f.unlink(missing_ok=True)
            total_deleted += 1

        if step_id != survivor["polarsteps_step_id"]:
            print(f"    backfill survivor step_id -> {step_id}")
            if args.apply:
                con.execute(
                    "UPDATE memory SET polarsteps_step_id=? WHERE id=?",
                    (step_id, survivor["id"]),
                )

    for pid in touched_projects:
        compact_positions(con, pid, args.apply)

    if args.apply:
        con.commit()
        print(f"\nAPPLIED: merged {len(groups)} group(s), deleted {total_deleted} memory(ies).")
    else:
        print(f"\nDRY-RUN: would merge {len(groups)} group(s), delete {total_deleted} memory(ies). "
              f"Re-run with --apply to write.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

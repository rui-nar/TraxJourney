#!/usr/bin/env python
"""List stored photo names the app would not have made.

Memory and journal photo lists, and people's avatars, name files the app
stores itself, always as ``str(uuid.uuid4())``. This reports every stored name
of any other form (null slots in a photo list are not reported). Such a name
names no file any more: every file operation refuses it (see
``src/utils/photo_paths.py``), so a hit shows as a missing photo and does no
harm on disk. It is listed for a look at where it came from.

READ-ONLY: prints a report and changes nothing.

Usage:
    python scripts/audit_photo_names.py --db "traxjourney.db"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlmodel import Session, create_engine, select  # noqa: E402

from models.project_db import DBJournalEntry, DBMemory, DBPerson  # noqa: E402
from src.utils.photo_paths import is_photo_name  # noqa: E402


def _bad_names(photos_json) -> List:
    try:
        photos = json.loads(photos_json or "[]")
    except (TypeError, ValueError):
        return [photos_json]
    if not isinstance(photos, list):
        return [photos]
    return [p for p in photos if p is not None and not is_photo_name(p)]


def find_bad_photo_names(sess: Session) -> List[Dict]:
    """Every stored photo or avatar name that is not an app-made photo name."""
    found: List[Dict] = []
    for kind, model in (("memory", DBMemory), ("journal", DBJournalEntry)):
        for row_id, project_id, photos_json in sess.exec(
            select(model.id, model.project_id, model.photos_json).order_by(model.id)
        ).all():
            for name in _bad_names(photos_json):
                found.append({"kind": kind, "id": row_id, "project_id": project_id,
                              "name": name})
    for row_id, project_id, avatar in sess.exec(
        select(DBPerson.id, DBPerson.project_id, DBPerson.avatar_photo)
        .where(DBPerson.avatar_photo.is_not(None)).order_by(DBPerson.id)
    ).all():
        if not is_photo_name(avatar):
            found.append({"kind": "person avatar", "id": row_id,
                          "project_id": project_id, "name": avatar})
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    args = parser.parse_args()
    engine = create_engine(f"sqlite:///{args.db}")
    with Session(engine) as sess:
        found = find_bad_photo_names(sess)
    for f in found:
        print(f"{f['kind']} {f['id']} (project {f['project_id']}): {f['name']!r}")
    print(f"{len(found)} stored name(s) found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

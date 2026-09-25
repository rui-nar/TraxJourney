#!/usr/bin/env python
"""List trips holding activities owned by an account outside the trip.

An activity row belongs to the account that created it, and a trip holds the
activities its owner and its companions added, each their own. This reports
every trip item whose activity is owned by an account that is neither the
trip's owner nor one of its current members.

A hit is not necessarily wrong: a companion who has since left the trip leaves
their activities behind, and those are reported too, since membership history
is not recorded. Each hit needs a look before anything is done about it.

READ-ONLY: prints a report and changes nothing.

Usage:
    python scripts/audit_activity_ownership.py --db "traxjourney.db"
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import and_, exists  # noqa: E402
from sqlmodel import Session, create_engine, select  # noqa: E402

from models.project_db import (  # noqa: E402
    DBActivity,
    DBProject,
    DBProjectItem,
    DBProjectMember,
)


def find_foreign_activity_refs(sess: Session) -> List[Dict]:
    """Every trip item whose activity's owner is neither the trip's owner nor
    a current member, ordered by trip then position."""
    is_member = exists().where(and_(
        DBProjectMember.project_id == DBProject.id,
        DBProjectMember.user_info_id == DBActivity.user_info_id,
    ))
    rows = sess.exec(
        select(DBProject.id, DBProject.user_info_id, DBProject.name,
               DBProjectItem.position, DBActivity.id, DBActivity.user_info_id)
        .join(DBProjectItem, DBProjectItem.project_id == DBProject.id)
        .join(DBActivity, DBActivity.id == DBProjectItem.activity_id)
        .where(DBProjectItem.item_type == "activity",
               DBActivity.user_info_id != DBProject.user_info_id,
               ~is_member)
        .order_by(DBProject.id, DBProjectItem.position)
    ).all()
    return [
        {"project_id": pid, "project_owner_id": owner, "project_name": name,
         "position": pos, "activity_id": aid, "activity_owner_id": act_owner}
        for pid, owner, name, pos, aid, act_owner in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    args = parser.parse_args()
    engine = create_engine(f"sqlite:///{args.db}")
    with Session(engine) as sess:
        refs = find_foreign_activity_refs(sess)
    for r in refs:
        print(f"project {r['project_id']} (owner {r['project_owner_id']}, "
              f"{r['project_name']!r}) position {r['position']}: activity "
              f"{r['activity_id']} owned by {r['activity_owner_id']}")
    print(f"{len(refs)} item(s) found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""List activity rows that break "one row per trip" (#592), and trip items
naming an activity that does not exist.

Every activity row is held by exactly one trip, and is owned by that trip's
owner (plan Decisions 1 and 4), so this reports:

* every row that two or more trips hold;
* every row whose owner is not the owner of the trip holding it, unless the
  row holds encrypted fields: an encrypted row keeps the account whose key
  encrypted it, since nobody else can read it (Decision 4's exception);
* every split link (``split_root_id``, ``split_parent_id``) from a row one
  trip holds to a row another trip holds;
* every trip item naming an activity that has no row, which would start
  showing whichever activity is later created under that id.

Rows that no trip holds are not reported: nothing reads them (review R1-9).

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
from sqlalchemy import and_, func  # noqa: E402
from sqlalchemy.orm import aliased  # noqa: E402
from sqlmodel import Session, create_engine, select  # noqa: E402

from models.project_db import DBActivity, DBProject, DBProjectItem  # noqa: E402
from src.auth.account_deletion import row_holds_envelope  # noqa: E402


def find_rows_held_by_several_trips(sess: Session) -> List[Dict]:
    """Every activity row that two or more trips hold, with those trips."""
    shared = (
        select(DBProjectItem.activity_id)
        .where(DBProjectItem.item_type == "activity",
               DBProjectItem.activity_id.is_not(None))
        .group_by(DBProjectItem.activity_id)
        .having(func.count(func.distinct(DBProjectItem.project_id)) > 1)
    )
    trips: Dict[int, List[int]] = {}
    for aid, pid in sess.exec(
        select(DBProjectItem.activity_id, DBProjectItem.project_id)
        .where(DBProjectItem.item_type == "activity",
               DBProjectItem.activity_id.in_(shared))
        .distinct()
        .order_by(DBProjectItem.activity_id, DBProjectItem.project_id)
    ).all():
        trips.setdefault(aid, []).append(pid)
    return [{"activity_id": aid, "project_ids": pids} for aid, pids in trips.items()]


def find_foreign_owned_rows(sess: Session) -> List[Dict]:
    """Every row a trip holds whose owner is not that trip's owner and which
    holds no encrypted field, ordered by trip then position."""
    rows = sess.exec(
        select(DBProject.id, DBProject.user_info_id, DBProject.name,
               DBProjectItem.position, DBActivity)
        .join(DBProjectItem, DBProjectItem.project_id == DBProject.id)
        .join(DBActivity, DBActivity.id == DBProjectItem.activity_id)
        .where(DBProjectItem.item_type == "activity",
               DBActivity.user_info_id != DBProject.user_info_id)
        .order_by(DBProject.id, DBProjectItem.position)
    ).all()
    return [
        {"project_id": pid, "project_owner_id": owner, "project_name": name,
         "position": pos, "activity_id": act.id, "activity_owner_id": act.user_info_id}
        for pid, owner, name, pos, act in rows
        if not row_holds_envelope(act)
    ]


def find_cross_trip_split_links(sess: Session) -> List[Dict]:
    """Every split link from a row one trip holds to a row another trip
    holds, ordered by trip then row. A link to a row no trip holds is not
    one: that row is not in any trip."""
    found: List[Dict] = []
    for column in ("split_root_id", "split_parent_id"):
        link = getattr(DBActivity, column)
        other = aliased(DBProjectItem)
        rows = sess.exec(
            select(DBProjectItem.project_id, DBActivity.id, link, other.project_id)
            .join(DBActivity, DBActivity.id == DBProjectItem.activity_id)
            .join(other, and_(other.activity_id == link,
                              other.item_type == "activity",
                              other.project_id != DBProjectItem.project_id))
            .where(DBProjectItem.item_type == "activity")
            .distinct()
        ).all()
        found += [
            {"project_id": pid, "activity_id": aid, "link": column,
             "linked_id": linked, "linked_project_id": linked_pid}
            for pid, aid, linked, linked_pid in rows
        ]
    found.sort(key=lambda r: (r["project_id"], r["activity_id"], r["link"],
                              r["linked_project_id"]))
    return found


def find_dangling_activity_refs(sess: Session) -> List[Dict]:
    """Every trip item naming an activity that has no row.

    Such an item shows nothing today, but would start showing whichever
    activity is later created under that id.
    """
    rows = sess.exec(
        select(DBProject.id, DBProject.user_info_id, DBProject.name,
               DBProjectItem.position, DBProjectItem.activity_id)
        .join(DBProjectItem, DBProjectItem.project_id == DBProject.id)
        .outerjoin(DBActivity, DBActivity.id == DBProjectItem.activity_id)
        .where(DBProjectItem.item_type == "activity",
               DBProjectItem.activity_id.is_not(None),
               DBActivity.id.is_(None))
        .order_by(DBProject.id, DBProjectItem.position)
    ).all()
    return [
        {"project_id": pid, "project_owner_id": owner, "project_name": name,
         "position": pos, "activity_id": aid}
        for pid, owner, name, pos, aid in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    args = parser.parse_args()
    engine = create_engine(f"sqlite:///{args.db}")
    with Session(engine) as sess:
        shared = find_rows_held_by_several_trips(sess)
        foreign = find_foreign_owned_rows(sess)
        links = find_cross_trip_split_links(sess)
        dangling = find_dangling_activity_refs(sess)
    for r in shared:
        print(f"activity {r['activity_id']} is held by trips {r['project_ids']}")
    print(f"{len(shared)} row(s) held by more than one trip")
    for r in foreign:
        print(f"project {r['project_id']} (owner {r['project_owner_id']}, "
              f"{r['project_name']!r}) position {r['position']}: activity "
              f"{r['activity_id']} owned by {r['activity_owner_id']}")
    print(f"{len(foreign)} plaintext row(s) not owned by their trip's owner")
    for r in links:
        print(f"project {r['project_id']}: activity {r['activity_id']} "
              f"{r['link']} {r['linked_id']}, held by project "
              f"{r['linked_project_id']}")
    print(f"{len(links)} split link(s) across trips")
    for r in dangling:
        print(f"project {r['project_id']} (owner {r['project_owner_id']}, "
              f"{r['project_name']!r}) position {r['position']}: activity "
              f"{r['activity_id']} has no row")
    print(f"{len(dangling)} item(s) naming an activity that does not exist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

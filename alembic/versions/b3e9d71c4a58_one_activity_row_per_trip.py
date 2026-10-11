"""one activity row per trip: split shared rows, strava_id / strava_user_id (#592)

An ``activity`` row used to be one global row whose primary key was the Strava
id, and trips only referenced it. Every write went to the shared row, so a
split, a reset, a rename or a Strava refresh in one trip reached every other
trip holding it (docs/TRIP_INDEPENDENT_ACTIVITIES_PLAN.md). From here on every
row is held by exactly one trip. This migration brings the stored data there,
in the order of the plan's Decision 10; each step relies on the ones before it.

0. Two nullable columns. ``strava_id`` (indexed, not unique) carries the
   Strava identity apart from the row id; ``strava_user_id`` is the account
   whose Strava connection the activity came from (Decision 5).
1. **Dangling items.** Activity items pointing at a missing row are deleted,
   counted per trip, so the copy step never meets an id with no row.
2. **Backfill.** ``strava_id`` = ``id`` and ``strava_user_id`` =
   ``user_info_id`` on positive Strava-sourced rows, before any copy is made
   or any owner changes, so copies inherit the true values. A negative row
   with a NULL source stays local.
3. **Find leaked splits.** A *head* is a row other rows name in
   ``split_root_id``; its family is the head plus those rows. A family held by
   more than one trip in total is *shared*: the trips holding its head and the
   trips holding at least one tail are recorded.
4. **Copy.** Of the trips referencing a row, the one with the lowest item id
   keeps it (a tie-break only) and every other trip gets a copy: every column
   copied, a fresh random id of the same sign (Decision 3), owned by that
   trip's owner unless the row holds an encrypted field (Decision 4), its
   prepared geometry copied. The trip's items are repointed to the copy.
5. **Remap.** Within each trip, split ids naming a row the trip got a copy of
   are pointed at the copy.
6. **Repair.** For each shared family and each trip holding its head: if some
   trip holds a tail and this one holds none, this trip never split it, and
   its head is restored from its first-edit snapshot as
   ``reset_activity_track`` restores it, without descendants; a head with an
   enveloped original or no snapshot is left and logged. If no trip holds a
   tail, every head is left and logged. Then, in every trip, a tail whose
   root or parent is not held by the same trip is re-rooted on its nearest
   ancestor the trip holds, or detached when there is none.
7. **Owner.** Every other row whose trip has a different owner is given to
   that owner, except rows holding an encrypted field, which are logged.
8. **Bump.** Every touched trip gets ``lock_version`` + 1 and its
   ``stats_json`` cleared; both stats readers recompute on NULL.

Rows no trip references are left alone.

It never holds the table in memory: shared rows and split heads are found by
grouped, keyset-paginated queries returning ids only, trips are walked in
keyset batches, and a full row is read one at a time. In memory: the copy map
and the shared families, ids only.

Re-runnable. The data steps all run in Alembic's one transaction. pysqlite
autocommits the ``ADD COLUMN`` before it, so an interrupted run can leave the
columns behind; they are added only when missing. Once a run has completed the
invariants hold, and a second run changes nothing.

Downgrade is not supported: a shared row cannot be re-formed safely from its
copies. Restore is from the backup taken before deploying.

Revision ID: b3e9d71c4a58
Revises: a8d3f5c2e917
Create Date: 2026-10-11

"""
import json
import logging
import secrets
from collections import defaultdict
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b3e9d71c4a58'
down_revision: Union[str, Sequence[str], None] = 'a8d3f5c2e917'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

#: Rows (or trips) per keyset batch, and the longest IN list.
_BATCH = 500

#: Below every activity id: local ids go down to -(2^53).
_KEY_FLOOR = -(2 ** 62)

#: As ``src/project/local_ids.py``: 53 bits keeps every id inside
#: JavaScript's safe integer range, and five draws exhaust only a broken table.
_ID_BITS = 53
_ALLOCATION_ATTEMPTS = 5

#: The activity columns the client encrypts (``_E2EE_ACTIVITY_COLUMNS`` in
#: ``src/project/repo_core.py``). A row holding an envelope in any of them
#: keeps its owner: only that owner's key reads it.
_ENCRYPTABLE = (
    "name", "summary_polyline", "start_latlng_json", "end_latlng_json",
    "elevation_profile_json", "elevation_profile_low_res_json",
    "original_polyline", "original_elevation_profile_json",
    "original_start_latlng_json", "original_end_latlng_json",
)

#: What ``reset_activity_track`` restores from the scalar snapshot
#: (``_SCALAR_SNAPSHOT`` in ``src/project/repo_activities.py``).
_SCALAR_SNAPSHOT = (
    "distance", "moving_time", "elapsed_time", "average_speed",
    "elev_high", "elev_low", "start_latlng_json", "end_latlng_json",
)


def _ids_param(name: str = "ids") -> sa.BindParameter:
    return sa.bindparam(name, expanding=True)


def _chunks(ids: list):
    for start in range(0, len(ids), _BATCH):
        yield ids[start:start + _BATCH]


def _add_columns() -> None:
    present = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("activity")}
    # Native ADD COLUMN — fast on SQLite, no table rewrite. No foreign key on
    # strava_user_id: adding one would rebuild the whole table, and SQLite does
    # not enforce it (models/db.py sets no PRAGMA foreign_keys).
    if "strava_id" not in present:
        op.add_column("activity", sa.Column("strava_id", sa.BigInteger(), nullable=True))
    if "strava_user_id" not in present:
        op.add_column("activity", sa.Column("strava_user_id", sa.Integer(), nullable=True))
    op.create_index("ix_activity_strava_id", "activity", ["strava_id"],
                    if_not_exists=True)


class _Run:
    """One pass of the data steps, and what it has done so far."""

    def __init__(self, bind) -> None:
        self.bind = bind
        #: Trips whose rows or items this run changed (step 8).
        self.touched: set = set()
        #: (trip, original row id) -> the copy that trip got (steps 4-6).
        self.copies: dict = {}
        #: Rows kept with their owner because they hold an envelope; logged once.
        self.kept_encrypted: set = set()
        self._owners: dict = {}
        self.restored = self.left = self.rerooted = self.detached = 0
        self.detached_with_held_root = 0

    def _scalars(self, sql: str, **params) -> list:
        stmt = sa.text(sql)
        if "ids" in params:
            stmt = stmt.bindparams(_ids_param())
        return list(self.bind.execute(stmt, params).scalars())

    def _rows(self, sql: str, **params) -> list:
        stmt = sa.text(sql)
        if "ids" in params:
            stmt = stmt.bindparams(_ids_param())
        return list(self.bind.execute(stmt, params))

    def _owner(self, trip: int):
        if trip not in self._owners:
            self._owners[trip] = self.bind.execute(
                sa.text("SELECT user_info_id FROM project WHERE id = :t"),
                {"t": trip}).scalar()
        return self._owners[trip]

    def _trips(self):
        """Every trip's (id, owner), in keyset batches."""
        last = _KEY_FLOOR
        while True:
            batch = self._rows(
                "SELECT id, user_info_id FROM project WHERE id > :last "
                "ORDER BY id LIMIT :n", last=last, n=_BATCH)
            if not batch:
                return
            last = batch[-1][0]
            for trip, owner in batch:
                self._owners[trip] = owner
                yield trip, owner

    def _held(self, trip: int) -> set:
        return set(self._scalars(
            "SELECT activity_id FROM projectitem WHERE project_id = :t "
            "AND item_type = 'activity' AND activity_id IS NOT NULL", t=trip))

    def _holding_trips(self, row_ids: list) -> dict:
        """row id -> the set of trips holding it, for *row_ids*."""
        out = defaultdict(set)
        for chunk in _chunks(row_ids):
            for row_id, trip in self._rows(
                    "SELECT activity_id, project_id FROM projectitem "
                    "WHERE item_type = 'activity' AND activity_id IN :ids "
                    "GROUP BY activity_id, project_id", ids=chunk):
                out[row_id].add(trip)
        return out

    # -- 1. dangling items --------------------------------------------------

    def delete_dangling_items(self) -> None:
        dangling = ("FROM projectitem WHERE item_type = 'activity' "
                    "AND activity_id IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM activity WHERE activity.id = projectitem.activity_id)")
        counts = self._rows(
            f"SELECT project_id, COUNT(*) {dangling} GROUP BY project_id ORDER BY project_id")
        if not counts:
            return
        self.bind.execute(sa.text(f"DELETE {dangling}"))
        for trip, count in counts:
            _log.info("one row per trip: trip %s: %d item(s) on a missing "
                      "activity deleted", trip, count)
            self.touched.add(trip)
        _log.info("one row per trip: %d dangling item(s) deleted in %d trip(s)",
                  sum(c for _, c in counts), len(counts))

    # -- 2. backfill ----------------------------------------------------------

    def backfill(self) -> None:
        self.bind.execute(sa.text(
            "UPDATE activity SET strava_id = id, strava_user_id = user_info_id "
            "WHERE strava_id IS NULL AND id > 0 "
            "AND (source IS NULL OR source IN ('', 'strava'))"))

    # -- 3. find leaked splits ----------------------------------------------

    def find_shared_families(self) -> dict:
        """head id -> (trips holding the head, trips holding a tail), for every
        family held by more than one trip in total."""
        families = {}
        last = _KEY_FLOOR
        while True:
            named = self._scalars(
                "SELECT DISTINCT split_root_id FROM activity "
                "WHERE split_root_id > :last ORDER BY split_root_id LIMIT :n",
                last=last, n=_BATCH)
            if not named:
                return families
            last = named[-1]
            heads = self._scalars("SELECT id FROM activity WHERE id IN :ids", ids=named)
            if not heads:
                continue
            tails = self._rows(
                "SELECT id, split_root_id FROM activity "
                "WHERE split_root_id IN :ids AND id != split_root_id", ids=heads)
            head_trips = self._holding_trips(heads)
            tail_trips_by_row = self._holding_trips([t for t, _ in tails])
            tail_trips = defaultdict(set)
            for tail, head in tails:
                tail_trips[head] |= tail_trips_by_row.get(tail, set())
            for head in heads:
                held_by = head_trips.get(head, set())
                tails_in = tail_trips.get(head, set())
                if len(held_by | tails_in) > 1:
                    families[head] = (held_by, tails_in)

    # -- 4. copy ----------------------------------------------------------------

    def copy_shared_rows(self) -> None:
        columns = ", ".join(
            f'"{c["name"]}"' for c in sa.inspect(self.bind).get_columns("activity")
            if c["name"] != "id")
        copy_sql = sa.text(
            f"INSERT INTO activity (id, {columns}) "
            f"SELECT :new, {columns} FROM activity WHERE id = :old")
        last = _KEY_FLOOR
        while True:
            shared = self._scalars(
                "SELECT activity_id FROM projectitem WHERE item_type = 'activity' "
                "AND activity_id > :last GROUP BY activity_id "
                "HAVING COUNT(DISTINCT project_id) > 1 "
                "ORDER BY activity_id LIMIT :n", last=last, n=_BATCH)
            if not shared:
                break
            last = shared[-1]
            trips_by_row = defaultdict(list)
            for row_id, trip in self._rows(
                    "SELECT activity_id, project_id FROM projectitem "
                    "WHERE item_type = 'activity' AND activity_id IN :ids "
                    "ORDER BY activity_id, id", ids=shared):
                if trip not in trips_by_row[row_id]:
                    trips_by_row[row_id].append(trip)
            for row_id in shared:
                # The trip with the lowest item id keeps the original.
                for trip in trips_by_row[row_id][1:]:
                    self._copy(copy_sql, row_id, trip)
        if self.copies:
            _log.info("one row per trip: %d cop(ies) made in %d trip(s)",
                      len(self.copies), len({t for t, _ in self.copies}))

    def _allocate(self, positive: bool) -> int:
        for _ in range(_ALLOCATION_ATTEMPTS):
            candidate = secrets.randbits(_ID_BITS)
            if candidate == 0:
                continue
            candidate = candidate if positive else -candidate
            if self.bind.execute(sa.text("SELECT 1 FROM activity WHERE id = :id"),
                                 {"id": candidate}).first() is None:
                return candidate
        raise RuntimeError(
            f"no free activity id after {_ALLOCATION_ATTEMPTS} attempts")

    def _copy(self, copy_sql, row_id: int, trip: int) -> None:
        new_id = self._allocate(positive=row_id > 0)
        self.bind.execute(copy_sql, {"new": new_id, "old": row_id})
        self.bind.execute(sa.text(
            "INSERT INTO activity_geo_prepared (activity_id, version, blob) "
            "SELECT :new, version, blob FROM activity_geo_prepared "
            "WHERE activity_id = :old"), {"new": new_id, "old": row_id})
        self.bind.execute(sa.text(
            "UPDATE projectitem SET activity_id = :new WHERE project_id = :t "
            "AND item_type = 'activity' AND activity_id = :old"),
            {"new": new_id, "old": row_id, "t": trip})
        self.copies[(trip, row_id)] = new_id
        self.touched.add(trip)
        self._give_to_owner(new_id, trip)

    # -- ownership (steps 4 and 7) ----------------------------------------------

    def _holds_envelope(self, row_id: int) -> bool:
        from src.utils.encryption_check import is_encrypted_envelope

        row = self.bind.execute(sa.text(
            f"SELECT {', '.join(_ENCRYPTABLE)} FROM activity WHERE id = :id"),
            {"id": row_id}).one()
        return any(is_encrypted_envelope(v) for v in row)

    def _give_to_owner(self, row_id: int, trip: int) -> None:
        owner = self._owner(trip)
        if owner is None:
            return
        current = self.bind.execute(
            sa.text("SELECT user_info_id FROM activity WHERE id = :id"),
            {"id": row_id}).scalar()
        if current == owner:
            return
        if self._holds_envelope(row_id):
            if row_id not in self.kept_encrypted:
                self.kept_encrypted.add(row_id)
                _log.info("one row per trip: activity %s in trip %s holds "
                          "encrypted fields and stays with its owner %s",
                          row_id, trip, current)
            return
        self.bind.execute(
            sa.text("UPDATE activity SET user_info_id = :o WHERE id = :id"),
            {"o": owner, "id": row_id})
        self.touched.add(trip)

    # -- 5. remap -------------------------------------------------------------

    def remap_families(self) -> None:
        by_trip = defaultdict(list)
        for (trip, old), new in self.copies.items():
            by_trip[trip].append((old, new))
        for trip in sorted(by_trip):
            for old, new in by_trip[trip]:
                for column in ("split_root_id", "split_parent_id"):
                    # Every referenced row is held by one trip now, so the
                    # trip's items name only its own rows.
                    self.bind.execute(sa.text(
                        f"UPDATE activity SET {column} = :new "
                        f"WHERE {column} = :old AND id IN ("
                        "  SELECT activity_id FROM projectitem"
                        "  WHERE project_id = :t AND item_type = 'activity')"),
                        {"new": new, "old": old, "t": trip})

    # -- 6. repair ------------------------------------------------------------

    def repair_heads(self, families: dict) -> None:
        for head in sorted(families):
            head_trips, tail_trips = families[head]
            for trip in sorted(head_trips):
                if trip in tail_trips:
                    continue    # the trip that split it keeps its cut head
                row_id = self.copies.get((trip, head), head)
                if not tail_trips:
                    self._leave(row_id, trip, "no trip holds a tail of it")
                    continue
                self._restore(row_id, trip)

    def _leave(self, row_id: int, trip: int, reason: str) -> None:
        self.left += 1
        _log.info("one row per trip: shared split head %s in trip %s left as "
                  "it is: %s", row_id, trip, reason)

    def _restore(self, row_id: int, trip: int) -> None:
        """Restore a leaked split head from its first-edit snapshot, as
        ``reset_activity_track`` does, without touching any other row."""
        from src.project.elevation_downsample import downsample_elevation
        from src.utils.encryption_check import is_encrypted_envelope

        snapshot = ", ".join(f"original_{name}" for name in _SCALAR_SNAPSHOT)
        row = self.bind.execute(sa.text(
            "SELECT is_edited, original_polyline, original_elevation_profile_json, "
            f"original_total_elevation_gain, total_elevation_gain, {snapshot} "
            "FROM activity WHERE id = :id"), {"id": row_id}).mappings().one()
        orig_poly = row["original_polyline"]
        orig_ep_json = row["original_elevation_profile_json"]
        if not row["is_edited"] or not orig_poly:
            return self._leave(row_id, trip, "no snapshot")
        if is_encrypted_envelope(orig_poly) or is_encrypted_envelope(orig_ep_json):
            return self._leave(row_id, trip, "its original is encrypted")
        if row["original_distance"] is None:
            # Migration c4e2a9f1b7d3 left the scalar snapshot empty only where
            # the original could not be read; the reset would fail on it too.
            return self._leave(row_id, trip, "no snapshot")

        profile = None
        low_res = None
        if orig_ep_json:
            try:
                ep = json.loads(orig_ep_json)
                profile = (ep.get("distances_km") or [], ep.get("elevations_m") or [])
                if profile[0] and profile[1]:
                    dd, ee = downsample_elevation(*profile)
                    low_res = json.dumps({"distances_km": dd, "elevations_m": ee})
            except (ValueError, TypeError, AttributeError):
                profile = low_res = None
        gain = row["original_total_elevation_gain"]
        if gain is None:
            gain = self._recomputed_gain(orig_poly, profile, row["total_elevation_gain"])

        assignments = ", ".join(f"{n} = original_{n}" for n in _SCALAR_SNAPSHOT)
        cleared = ", ".join(f"original_{n} = NULL" for n in _SCALAR_SNAPSHOT)
        self.bind.execute(sa.text(
            "UPDATE activity SET summary_polyline = original_polyline, "
            "elevation_profile_json = original_elevation_profile_json, "
            "elevation_profile_low_res_json = :low_res, "
            f"total_elevation_gain = :gain, {assignments}, "
            "name = CASE WHEN split_base_name IS NOT NULL AND split_base_name != '' "
            "THEN split_base_name ELSE name END, "
            "split_root_id = NULL, split_parent_id = NULL, split_base_name = NULL, "
            "is_edited = :false, original_polyline = NULL, "
            "original_elevation_profile_json = NULL, "
            f"original_total_elevation_gain = NULL, {cleared} "
            "WHERE id = :id"),
            {"low_res": low_res, "gain": gain, "false": False, "id": row_id})
        # Deleted, not copied: the next read rebuilds it from the restored track.
        self.bind.execute(sa.text(
            "DELETE FROM activity_geo_prepared WHERE activity_id = :id"),
            {"id": row_id})
        self.touched.add(trip)
        self.restored += 1
        shared_link = self.bind.execute(sa.text(
            "SELECT share_token IS NOT NULL OR share_token_no_memories IS NOT NULL "
            "FROM project WHERE id = :t"), {"t": trip}).scalar()
        _log.info("one row per trip: restored split head %s in trip %s, which "
                  "never split it%s", row_id, trip,
                  " (trip has a share link: its public tiles stay stale until "
                  "the trip next changes)" if shared_link else "")

    @staticmethod
    def _recomputed_gain(orig_poly, profile, current):
        """Gain from the original track, for a snapshot taken before
        ``original_total_elevation_gain`` existed — what the reset computes."""
        from src.models.track_edit import align_points, recompute_track_metrics

        try:
            points = align_points(orig_poly, profile)
        except (ValueError, TypeError, IndexError):
            return current
        if not points:
            return current
        return recompute_track_metrics(points).total_elevation_gain

    def repair_crossing_tails(self) -> None:
        for trip, _owner in self._trips():
            links = {
                row_id: (root, parent) for row_id, root, parent in self._rows(
                    "SELECT a.id, a.split_root_id, a.split_parent_id "
                    "FROM projectitem pi JOIN activity a ON a.id = pi.activity_id "
                    "WHERE pi.project_id = :t AND pi.item_type = 'activity' "
                    "AND (a.split_root_id IS NOT NULL OR a.split_parent_id IS NOT NULL)",
                    t=trip)}
            if not links:
                continue
            held = self._held(trip)

            def inside(link_id) -> bool:
                return link_id is None or link_id in held

            crossing = sorted(
                r for r, (root, parent) in links.items()
                if not (inside(root) and inside(parent)))
            if not crossing:
                continue
            resolved: dict = {}
            for row_id in crossing:
                root, parent = self._resolve(row_id, links, held, resolved)
                if root is None:
                    self.bind.execute(sa.text(
                        "UPDATE activity SET split_root_id = NULL, "
                        "split_parent_id = NULL, split_base_name = NULL "
                        "WHERE id = :id"), {"id": row_id})
                    self.detached += 1
                    own_root = links[row_id][0]
                    if own_root is not None and own_root in held:
                        # Guard (U1R1-1): no known path leaves a parent chain
                        # that dead-ends on a missing row; say so if one did.
                        self.detached_with_held_root += 1
                        _log.warning(
                            "one row per trip: split piece %s in trip %s "
                            "detached although the trip holds its root %s "
                            "(parent chain dead-ends)", row_id, trip, own_root)
                    else:
                        _log.info("one row per trip: split piece %s in trip %s "
                                  "detached: the trip holds none of its ancestors",
                                  row_id, trip)
                else:
                    self.bind.execute(sa.text(
                        "UPDATE activity SET split_root_id = :r, "
                        "split_parent_id = :p WHERE id = :id"),
                        {"r": root, "p": parent, "id": row_id})
                    self.rerooted += 1
                    _log.info("one row per trip: split piece %s in trip %s "
                              "re-rooted on %s (parent %s)",
                              row_id, trip, root, parent)
                self.touched.add(trip)

    def _resolve(self, row_id, links, held, resolved):
        """(root, parent) of *row_id* within its trip: the parent is its
        nearest ancestor the trip holds, the root that ancestor's own root, or
        the ancestor itself when it has none. (None, None) when the trip holds
        no ancestor."""
        if row_id in resolved:
            return resolved[row_id]
        resolved[row_id] = (None, None)     # guards a cyclic chain
        root, parent = links.get(row_id, (None, None))
        if (root is None or root in held) and (parent is None or parent in held):
            resolved[row_id] = (root, parent)
            return resolved[row_id]
        ancestor = self._nearest_held_ancestor(
            parent if parent is not None else root, links, held)
        if ancestor is not None:
            ancestor_root, _ = self._resolve(ancestor, links, held, resolved)
            resolved[row_id] = (ancestor_root if ancestor_root is not None
                                else ancestor, ancestor)
        return resolved[row_id]

    def _nearest_held_ancestor(self, start, links, held):
        seen = set()
        current = start
        while current is not None and current not in held and current not in seen:
            seen.add(current)
            if current in links:
                root, parent = links[current]
            else:
                found = self.bind.execute(sa.text(
                    "SELECT split_root_id, split_parent_id FROM activity "
                    "WHERE id = :id"), {"id": current}).first()
                if found is None:
                    return None
                root, parent = found
            nxt = parent if parent is not None else root
            current = nxt if nxt != current else None
        return current if current in held else None

    # -- 7. owner -------------------------------------------------------------

    def give_rows_to_trip_owners(self) -> None:
        for trip, owner in self._trips():
            for row_id in self._scalars(
                    "SELECT DISTINCT a.id FROM projectitem pi "
                    "JOIN activity a ON a.id = pi.activity_id "
                    "WHERE pi.project_id = :t AND pi.item_type = 'activity' "
                    "AND a.user_info_id != :o ORDER BY a.id", t=trip, o=owner):
                self._give_to_owner(row_id, trip)

    # -- 8. bump --------------------------------------------------------------

    def bump_touched(self) -> None:
        for chunk in _chunks(sorted(self.touched)):
            self.bind.execute(sa.text(
                "UPDATE project SET lock_version = lock_version + 1, "
                "stats_json = NULL WHERE id IN :ids").bindparams(_ids_param()),
                {"ids": chunk})
        _log.info(
            "one row per trip: %d trip(s) touched; %d head(s) restored, %d "
            "shared head(s) left as they are, %d piece(s) re-rooted, %d "
            "detached (detached_with_held_root=%d), %d encrypted row(s) kept "
            "with their owner",
            len(self.touched), self.restored, self.left, self.rerooted,
            self.detached, self.detached_with_held_root, len(self.kept_encrypted))


def upgrade() -> None:
    _add_columns()
    run = _Run(op.get_bind())
    run.delete_dangling_items()                  # 1
    run.backfill()                               # 2
    families = run.find_shared_families()        # 3
    run.copy_shared_rows()                       # 4
    run.remap_families()                         # 5
    run.repair_heads(families)                   # 6
    run.repair_crossing_tails()                  # 6, tails
    run.give_rows_to_trip_owners()               # 7
    run.bump_touched()                           # 8


def downgrade() -> None:
    raise RuntimeError(
        "Migration b3e9d71c4a58 (one activity row per trip) cannot be "
        "downgraded: an activity shared between trips cannot be re-formed "
        "safely from its per-trip copies. Restore the database backup taken "
        "before the upgrade instead.")

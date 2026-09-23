"""Activity persistence, enrichment writes, and geometry editing (issue #31).

Part of the ``ProjectRepo`` mixin split — see ``src/project/project_repo.py``
for the composed class and module docstring.
"""
from __future__ import annotations

import json
from typing import List, Optional

from sqlalchemy import delete
from sqlmodel import Session, select

from models.db import get_session
from models.project_db import DBActivity, DBActivityGeoPrepared, DBProjectItem
from src.models.activity import Activity
from src.models.prepared_geo import prepare_polyline
from src.models.simplify import PREPARED_GEO_VERSION
from src.project.local_ids import allocate_local_activity_id
from src.project.elevation_downsample import downsample_elevation
from src.project.repo_core import bump_lock_version, check_and_bump_lock_version
from src.utils.encryption_check import is_encrypted_envelope


def _low_res_ep_json(ep_json: Optional[str]) -> Optional[str]:
    """Downsample a full ``elevation_profile_json`` blob to the low-res form
    stored in ``elevation_profile_low_res_json`` (~300 pts). Returns None when
    the input is missing or unparseable."""
    if not ep_json:
        return None
    try:
        ep = json.loads(ep_json)
        d = ep.get("distances_km") or []
        e = ep.get("elevations_m") or []
        if not d or not e:
            return None
        dd, ee = downsample_elevation(d, e)
        return json.dumps({"distances_km": dd, "elevations_m": ee})
    except Exception:
        return None


def store_prepared_geometry(sess: Session, row: DBActivity) -> None:
    """Keep ``activity_geo_prepared`` in step with ``row.summary_polyline``.

    Called by every path that writes the polyline — the same way
    ``elevation_profile_low_res_json`` is re-derived beside every write of the
    full profile — so the simplified geo endpoints can serve a trip without
    decoding and simplifying it on the request path (issue #369). Computing
    here costs ~25 ms of local CPU per activity, once per polyline write.

    A polyline the server cannot prepare — absent, a client-side E2EE envelope,
    or one that does not even decode — leaves NO row, and removes any it
    finds: the reader treats a missing row as "prepare on demand", and a row
    left behind by a previous plaintext value would otherwise be served for a
    track that has since been encrypted or removed. No commit: the caller's
    transaction covers the polyline and this together.
    """
    try:
        blob = prepare_polyline(row.summary_polyline)
    except Exception:  # noqa: BLE001 — a polyline that does not decode is the row's problem, not this write's
        blob = None
    if blob is None:
        sess.execute(delete(DBActivityGeoPrepared).where(
            DBActivityGeoPrepared.activity_id == row.id))
        return
    sess.merge(DBActivityGeoPrepared(
        activity_id=row.id, version=PREPARED_GEO_VERSION, blob=blob))


def _parse_ep(ep_json: Optional[str]):
    """Parse a stored elevation_profile JSON blob to ``(distances_km, elevations_m)``.

    Returns None when the blob is missing or unparseable.
    """
    if not ep_json:
        return None
    try:
        ep = json.loads(ep_json)
        return (ep.get("distances_km") or [], ep.get("elevations_m") or [])
    except Exception:
        return None


def _apportion_gain(stored: Optional[float], before: float, after: float) -> float:
    """Scale a stored elevation gain to the share *after* accounts for.

    ``before`` and ``after`` are OUR measure of the same two geometries, so the
    estimator's own bias divides out and only the proportion survives. Falls
    back to the freshly measured figure when there is nothing to scale — no
    stored value, or a ``before`` of zero, which is what a flat activity or an
    unreadable (client-encrypted) profile gives.
    """
    if stored is None or before <= 0:
        return after
    return stored * (after / before)


class ActivityMixin:
    """Activity CRUD, enrichment writes, and track-geometry editing."""

    # ------------------------------------------------------------------
    # Activity enrichment (background task writes)
    # ------------------------------------------------------------------

    def update_activity_enrichment(
        self,
        sess: Session,
        activity_id: int,
        summary_polyline: Optional[str],
        elevation_profile_json: Optional[str],
    ) -> None:
        """Update only the enrichment columns of an activity row.

        Skips a field whose EXISTING value is already a client-side E2EE
        ciphertext envelope (issue #29) — once a field is encrypted, a
        background Strava enrichment pass (which only ever has fresh plaintext
        streams) must not silently overwrite it back to plaintext.
        """
        row = sess.get(DBActivity, activity_id)
        if row is None:
            return
        if summary_polyline is not None and not is_encrypted_envelope(row.summary_polyline):
            row.summary_polyline = summary_polyline
            store_prepared_geometry(sess, row)
        if elevation_profile_json is not None and not is_encrypted_envelope(row.elevation_profile_json):
            row.elevation_profile_json = elevation_profile_json
            row.elevation_profile_low_res_json = _low_res_ep_json(elevation_profile_json)
        sess.commit()

    # ------------------------------------------------------------------
    # Geometry editing (issue #31)
    # ------------------------------------------------------------------

    def activity_is_edited(self, activity_id: int) -> bool:
        """Return True if the activity has a locally edited track.

        Opens its own session so background enrichment tasks (which hold no
        session) can cheaply check before overwriting geometry.
        """
        with get_session() as sess:
            row = sess.get(DBActivity, activity_id)
            return bool(row is not None and row.is_edited)

    @staticmethod
    def _write_track_geometry(
        row: DBActivity, points: "list", *, sess: Session | None = None,
    ) -> None:
        """Re-derive geometry + scalar metrics from *points* onto *row* (no commit).

        Snapshots the pre-edit polyline/elevation into the original_* columns on
        the FIRST edit only, so a later "Reset to Strava" can restore them.

        Elevation gain is SCALED, not recomputed (issue #386). A synced activity
        arrives with Strava's own figure, measured from data we never see and
        corrected in ways we cannot reproduce; recomputing from the stored
        profile threw that away and replaced it with ours, so trimming fifty
        metres off a ride could move its climb by hundreds. The edited piece now
        keeps the share of the original figure that its own geometry accounts
        for — the same treatment moving/elapsed time already gets just below,
        and for the same reason.

        The share is measured with our own estimator on both sides, so its
        absolute bias cancels: only the RATIO between the two geometries is
        used. That also makes a split's pieces sum to their parent, since each
        piece is apportioned against the same pre-split denominator.
        """
        from src.models.track_edit import (
            align_points,
            points_to_elevation_profile,
            points_to_polyline,
            recompute_track_metrics,
        )

        if not row.is_edited:
            row.original_polyline = row.summary_polyline
            row.original_elevation_profile_json = row.elevation_profile_json
            row.original_total_elevation_gain = row.total_elevation_gain

        # Apportion times against the CURRENT geometry's haversine length (not
        # the stored scalar distance, which Strava derives differently). This
        # keeps edit→reset time recovery exact, since reset re-apportions with
        # the same geometry-derived basis.
        prev_metrics = recompute_track_metrics(align_points(
            row.summary_polyline,
            _parse_ep(row.elevation_profile_json),
        ))

        metrics = recompute_track_metrics(
            points,
            original_distance_m=prev_metrics.distance,
            original_moving_time=row.moving_time or 0,
            original_elapsed_time=row.elapsed_time or 0,
        )

        row.summary_polyline = points_to_polyline(points)
        if sess is not None:
            # Optional so this stays callable without a database: it is mostly
            # metric arithmetic, and the gain-apportioning tests exercise that
            # on a bare row. Every production path passes a session — the three
            # callers below, and the "each writer leaves a prepared row" tests
            # in test_prepared_geo.py are what hold them to it.
            store_prepared_geometry(sess, row)
        ep = points_to_elevation_profile(points)
        ep_json = (
            json.dumps({"distances_km": ep[0], "elevations_m": ep[1]}) if ep else None
        )
        row.elevation_profile_json = ep_json
        row.elevation_profile_low_res_json = _low_res_ep_json(ep_json)

        row.distance = metrics.distance
        row.total_elevation_gain = _apportion_gain(
            row.total_elevation_gain,
            prev_metrics.total_elevation_gain,
            metrics.total_elevation_gain,
        )
        row.elev_high = metrics.elev_high
        row.elev_low = metrics.elev_low
        row.start_latlng_json = json.dumps(metrics.start_latlng) if metrics.start_latlng else None
        row.end_latlng_json = json.dumps(metrics.end_latlng) if metrics.end_latlng else None
        row.average_speed = metrics.average_speed
        row.moving_time = metrics.moving_time
        row.elapsed_time = metrics.elapsed_time
        row.is_edited = True

    def edit_activity_track(
        self, sess: Session, project_id: int, activity_id: int, points: "list",
        *, expected_version: Optional[int] = None,
    ) -> bool:
        """Apply an edited point list to an activity, recomputing all metrics.

        Snapshots the original geometry on the first edit and sets is_edited.
        Returns False if the activity row does not exist.

        Advances the project's lock_version (issue #173) so a native client's
        on-disk cache — which only ever checks that counter — notices the
        edit. When *expected_version* is given, this is instead an atomic
        compare-and-swap (like ``save_project(check_version=True)``): it
        raises ``StaleWriteError`` if the project changed since the caller
        last loaded it, so two edits racing on the same activity don't
        silently clobber each other.
        """
        row = sess.get(DBActivity, activity_id)
        if row is None:
            return False
        if expected_version is not None:
            check_and_bump_lock_version(sess, project_id, expected_version)
        else:
            bump_lock_version(sess, project_id)
        self._write_track_geometry(row, points, sess=sess)
        sess.commit()
        return True

    @staticmethod
    def _split_descendants(sess: Session, activity_id: int) -> List[DBActivity]:
        """Every LOCAL piece transitively cut out of *activity_id*.

        Walks ``split_parent_id`` breadth-first: a piece records which piece it
        was cut directly out of, so this is exactly the set that resetting
        *activity_id* would grow back over. ``split_root_id`` cannot answer this
        — it points every piece of a family at the family's FIRST piece however
        deep the chain, which says nothing about who cut whom (issue #143).

        The ``id < 0`` filter is a guard, not a subtlety: only split tails have a
        parent and they are always local, but a Strava row is shared across every
        project that references it and must never be row-deleted here. ``seen``
        likewise guards a cycle that split_activity cannot create (a new tail's
        id is always below every existing id, so parents are strictly greater).
        """
        found: List[DBActivity] = []
        seen = {activity_id}
        frontier = [activity_id]
        while frontier:
            children = sess.exec(
                select(DBActivity).where(
                    DBActivity.split_parent_id.in_(frontier), DBActivity.id < 0
                )
            ).all()
            children = [c for c in children if c.id not in seen]
            if not children:
                break
            found.extend(children)
            seen.update(c.id for c in children)
            frontier = [c.id for c in children]
        return found

    def _remove_split_descendants(
        self, sess: Session, project_id: int, row: DBActivity
    ) -> int:
        """Delete the pieces cut out of *row* and unlink their timeline items.

        Returns how many were removed. On a family root this removes the whole
        family, i.e. undoes the split outright (issue #141); on a piece that was
        itself split again it removes just that piece's own children (#143).
        """
        pieces = self._split_descendants(sess, row.id)
        if not pieces:
            return 0

        sess.execute(
            delete(DBProjectItem).where(
                DBProjectItem.project_id == project_id,
                DBProjectItem.item_type == "activity",
                DBProjectItem.activity_id.in_([p.id for p in pieces]),
            )
        )
        sess.execute(
            delete(DBActivityGeoPrepared).where(
                DBActivityGeoPrepared.activity_id.in_([p.id for p in pieces]))
        )
        for piece in pieces:
            sess.delete(piece)

        # Keep item positions contiguous, then let _renumber_split_family drop the
        # now-meaningless "(i/N)" suffix — restoring the plain pre-split name when
        # the family is down to one piece, renumbering the survivors otherwise.
        remaining = sess.exec(
            select(DBProjectItem)
            .where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.position)
        ).all()
        for pos, it in enumerate(remaining):
            it.position = pos
            sess.add(it)
        self._renumber_split_family(
            sess, row.split_root_id if row.split_root_id is not None else row.id)
        return len(pieces)

    def reset_activity_track(
        self, sess: Session, project_id: int, activity_id: int
    ) -> bool:
        """Restore an edited activity's geometry from its original snapshot.

        Recomputes scalar metrics from the restored geometry and clears
        is_edited + the snapshot columns.  Returns False if the row does not
        exist or was never edited (nothing to reset).

        The snapshot is whatever geometry the row held before its last edit, so
        on anything that has been split it spans the pieces cut out of it too.
        Restoring it verbatim would leave those pieces sitting on top of the
        restored track, duplicating it. They are therefore deleted as part of the
        reset, their timeline items unlinked and the family renamed — on a root
        that undoes the split outright (issue #141), on a piece that was itself
        split again it removes just that piece's own children (issue #143).

        That destroys any edits made to those pieces, so the editor confirms
        first; see _splitPiecesRemovedByReset in activity_editor_page.dart.

        A piece with no children of its own cascades nothing: resetting it undoes
        just its own post-split edits (issue #131).
        """
        row = sess.get(DBActivity, activity_id)
        if row is None or not row.is_edited:
            return False

        # Advance the project's lock_version (issue #173) so a native client's
        # on-disk cache — which only ever checks that counter — notices the
        # restored geometry.
        bump_lock_version(sess, project_id)

        self._remove_split_descendants(sess, project_id, row)

        from src.models.track_edit import align_points, recompute_track_metrics

        orig_poly = row.original_polyline
        orig_ep_json = row.original_elevation_profile_json
        orig_ep = None
        if orig_ep_json:
            ep = json.loads(orig_ep_json)
            orig_ep = (ep.get("distances_km") or [], ep.get("elevations_m") or [])
        points = align_points(orig_poly, orig_ep)

        row.summary_polyline = orig_poly
        store_prepared_geometry(sess, row)
        row.elevation_profile_json = orig_ep_json
        row.elevation_profile_low_res_json = _low_res_ep_json(orig_ep_json)

        if points:
            # Geometry-derived metrics (distance, elevation, latlng) restore
            # exactly from the snapshot. Scalar times were apportioned DOWN to
            # the retained-distance fraction on edit; scale them back UP by the
            # inverse ratio (restored ÷ edited distance) so a trim→reset round
            # trip recovers the original times too.
            edited_distance = row.distance or 0.0
            metrics = recompute_track_metrics(points)
            row.distance = metrics.distance
            # The snapshot is the figure the activity had before anything was
            # edited — Strava's own, where Strava supplied one. Restoring it is
            # the whole point of keeping it; recomputing here would hand back a
            # different number than the one the reset is undoing to.
            row.total_elevation_gain = (
                row.original_total_elevation_gain
                if row.original_total_elevation_gain is not None
                else metrics.total_elevation_gain
            )
            row.elev_high = metrics.elev_high
            row.elev_low = metrics.elev_low
            row.start_latlng_json = json.dumps(metrics.start_latlng) if metrics.start_latlng else None
            row.end_latlng_json = json.dumps(metrics.end_latlng) if metrics.end_latlng else None
            if edited_distance > 0 and metrics.distance > 0:
                ratio = metrics.distance / edited_distance
                row.moving_time = int(round((row.moving_time or 0) * ratio))
                row.elapsed_time = int(round((row.elapsed_time or 0) * ratio))
                row.average_speed = (
                    metrics.distance / row.moving_time if row.moving_time > 0 else 0.0
                )

        row.is_edited = False
        row.original_polyline = None
        row.original_elevation_profile_json = None
        row.original_total_elevation_gain = None
        sess.commit()
        return True

    @staticmethod
    def _renumber_split_family(sess: Session, root_id: int) -> None:
        """Rename every surviving member of a split family as ``"<base> (i/N)"``,
        1-indexed in chronological (start_date) order. Called after a split
        creates a new piece and after a local tail is deleted, so the numbering
        always reflects the CURRENT family size instead of accumulating suffixes
        (issue #45 follow-up — this replaced the old ``"<name> (2)"`` scheme,
        which produced "<name> (2) (2)" when a piece was split again).

        No-op if the root's name is an E2EE ciphertext envelope (issue #29) — a
        base name can't be meaningfully derived from ciphertext, so this leaves
        whatever name each piece already has untouched. Also no-op if the
        family has been reduced to (or never grew past) a single surviving
        piece, other than restoring its plain base name if one was recorded.
        """
        root = sess.get(DBActivity, root_id)
        if root is None or is_encrypted_envelope(root.name):
            return

        members = sess.exec(
            select(DBActivity).where(
                (DBActivity.id == root_id) | (DBActivity.split_root_id == root_id)
            )
        ).all()
        if len(members) < 2:
            if members and root.split_base_name:
                members[0].name = root.split_base_name
            return

        base_name = root.split_base_name or root.name
        if root.split_base_name is None:
            root.split_base_name = base_name

        members.sort(key=lambda a: a.start_date or "")
        n = len(members)
        for i, act in enumerate(members, start=1):
            act.name = f"{base_name} ({i}/{n})"

    def split_activity(
        self,
        sess: Session,
        user_info_id: int,
        project_id: int,
        activity_id: int,
        split_index: int,
        drop_boundary: bool = False,
        points: Optional[list] = None,
        expected_version: Optional[int] = None,
    ) -> Optional[int]:
        """Split an activity into a head (keeps id) and a local tail (negative id).

        *points* is the caller's current track. The editor holds its edits locally
        until Save, so a split driven from the stored geometry silently discarded
        them AND applied *split_index* to a different point list than the one the
        user was looking at (issue #127); passing the edited points makes the cut
        compound with them in this one commit. When None the stored geometry is
        used, which is what a split with no pending edits sends.

        The boundary point at *split_index* is shared by default: the head keeps
        ``points[:split_index+1]`` and the tail gets ``points[split_index:]`` so
        the two pieces stay contiguous. When *drop_boundary* is True the tail
        instead starts at ``split_index + 1``, excluding the boundary point —
        used when a transportation segment will bridge the gap left at the cut
        (issue #104), so the tail's track doesn't also sit on the departure point.
        The tail is a new LOCAL activity (``manual=True``, synthetic negative id)
        inserted into the project items directly after the head. Both pieces are
        marked is_edited with their own geometry snapshot, and every surviving
        member of the split family (head, tail, and any earlier siblings) is
        renamed ``"<base name> (i/N)"`` in chronological order — see
        ``_renumber_split_family``.

        Returns the new tail activity id, or None if the activity is missing.
        Raises ValueError if *split_index* does not yield two non-trivial pieces.

        Advances the project's lock_version (issue #173) so a native client's
        on-disk cache — which only ever checks that counter — notices the new
        tail. When *expected_version* is given, this is instead an atomic
        compare-and-swap (like ``save_project(check_version=True)``): it
        raises ``StaleWriteError`` if the project changed since the caller
        last loaded it, so two splits racing on the same activity don't
        silently clobber each other.
        """
        from src.models.track_edit import align_points

        head = sess.get(DBActivity, activity_id)
        if head is None:
            return None

        if points is None:
            points = align_points(
                head.summary_polyline, _parse_ep(head.elevation_profile_json))
        # Need at least 2 points on each side of the boundary (3 on the tail side
        # when dropping the boundary, so 2 remain once it's excluded).
        min_tail_start = split_index + 1 if drop_boundary else split_index
        if split_index < 1 or len(points) - min_tail_start < 2:
            raise ValueError(
                f"split_index {split_index} out of range for a {len(points)}-point track")

        if expected_version is not None:
            check_and_bump_lock_version(sess, project_id, expected_version)
        else:
            bump_lock_version(sess, project_id)

        head_points = points[: split_index + 1]
        tail_points = points[min_tail_start:]

        # A tail is an activity the app creates, so it takes a local (negative)
        # id from the shared allocator — the same one GPX import uses, rather
        # than the decrementing scheme this used to have. That scheme handed out
        # -1, -2, -3 in every deployment, which collides the moment a .traxj
        # exported from one is imported into another. The allocator also keeps
        # the property that mattered here: it checks the activity TABLE, not
        # this project's timeline, because a tail whose item was removed leaves
        # the row behind and reusing its id would collide on INSERT.
        tail_id = allocate_local_activity_id(sess)

        # Resolve the family root: the very first piece ever split, regardless
        # of how deep this split is in the chain (splitting an already-split
        # tail still points its own new tail at the SAME root as its parent).
        root_id = head.split_root_id if head.split_root_id is not None else head.id

        # Create the tail row copying the head's metadata. name is a
        # placeholder — _renumber_split_family overwrites it below (kept here
        # only as the fallback when that's skipped, e.g. an E2EE-encrypted name).
        tail = DBActivity(
            id=tail_id,
            user_info_id=user_info_id,
            name=f"{head.name} (2)",
            split_root_id=root_id,
            # ...and the piece it was cut directly out of, which is the head
            # whatever its depth. Unlike split_root_id this is the IMMEDIATE
            # parent, so reset can find a piece's own children (issue #143).
            split_parent_id=head.id,
            type=head.type,
            moving_time=head.moving_time,
            elapsed_time=head.elapsed_time,
            start_date=head.start_date,
            start_date_local=head.start_date_local,
            timezone=head.timezone,
            trainer=head.trainer,
            commute=head.commute,
            manual=True,
            private=head.private,
            gear_id=head.gear_id,
        )
        # Seed the tail with the FULL pre-split geometry + the original scalar
        # times so _write_track_geometry apportions the tail's time to its own
        # retained fraction (tail_length / full_length), mirroring the head.
        # This stays the STORED geometry even when the caller supplied edited
        # points (issue #127): the head's own apportioning below also measures
        # against the stored track, so both pieces share one denominator and
        # their times sum to the edited track's share of the original. Seeding
        # from the edited points instead would give the tail a smaller
        # denominator than the head and inflate it.
        tail.summary_polyline = head.summary_polyline
        tail.elevation_profile_json = head.elevation_profile_json
        # The pre-split gain rides along for the same reason the times do: each
        # piece then keeps the share of it its own geometry accounts for, and
        # the two sum to the track they came out of instead of each being
        # measured from scratch. Captured before the head is written, since that
        # write replaces the head's figure with its own share.
        tail.total_elevation_gain = head.total_elevation_gain
        sess.add(tail)

        # Write head then tail geometry (each snapshots its own original + recomputes).
        self._write_track_geometry(head, head_points, sess=sess)
        # The tail begins at the split boundary — i.e. where the head ends. Tracks
        # carry no per-point timestamps, so derive the boundary time as the head's
        # start plus its (now apportioned) elapsed duration. Without this the tail
        # inherits the head's start_date and sorts out of order (often *before* the
        # head, since its negative id wins date ties). start_date columns are stored
        # as ISO-8601 strings, so parse → shift → re-serialise in the same format.
        from datetime import datetime, timedelta

        def _shift(iso: str) -> str:
            if not iso:
                return iso
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return (dt + timedelta(seconds=head.elapsed_time or 0)) \
                .isoformat().replace("+00:00", "Z")

        tail.start_date = _shift(head.start_date)
        tail.start_date_local = _shift(head.start_date_local)
        self._write_track_geometry(tail, tail_points, sess=sess)
        # Re-point the tail's snapshot at its OWN geometry. The seeding above is
        # a time-apportioning device, but _write_track_geometry snapshots whatever
        # sat on the row before the write — for a fresh tail that's the head's
        # full pre-split track, so "Reset" expanded the tail back into a duplicate
        # of the whole track it was cut out of (issue #131). A tail has no upstream
        # original; the only thing reset can meaningfully undo is edits made to the
        # tail AFTER the split, so snapshot the post-split geometry instead. This
        # keeps the seeding trick and decouples it from the snapshot.
        tail.original_polyline = tail.summary_polyline
        tail.original_elevation_profile_json = tail.elevation_profile_json
        tail.original_total_elevation_gain = tail.total_elevation_gain

        # Insert the tail item directly after the head item, renumbering positions.
        item_rows = sess.exec(
            select(DBProjectItem)
            .where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.position)
        ).all()
        new_order: List[DBProjectItem] = []
        for it in item_rows:
            new_order.append(it)
            if it.item_type == "activity" and it.activity_id == activity_id:
                new_order.append(DBProjectItem(
                    project_id=project_id, position=0,
                    item_type="activity", activity_id=tail_id,
                ))
        for pos, it in enumerate(new_order):
            it.position = pos
            sess.add(it)

        self._renumber_split_family(sess, root_id)
        sess.commit()
        return tail_id

    def delete_local_activity(
        self, sess: Session, project_id: int, activity_id: int
    ) -> bool:
        """Delete a LOCAL (negative-id) activity row and unlink it from items.

        Only local activities (split tails, id < 0) may be deleted — Strava
        activities are shared across projects and must never be row-deleted here.
        Also renumbers any surviving split-family siblings (see
        ``_renumber_split_family``) so "(i/N)" reflects the new, smaller N.
        Returns False if the id is not local, the row is absent, or the row
        belongs to a DIFFERENT project than *project_id*.

        That last check is issue #405. The caller's permission check proves you
        may edit *project_id*, not that the activity is one of its own, so
        without it ``DELETE /api/projects/<trip B>/activities/<trip A's id>``
        deleted trip A's activity, bumped trip B's lock_version, and left trip A
        holding an item pointing at a row that no longer existed.

        **Orphans are deliberately allowed through.** The test is "no OTHER
        project references this row", not "this project references it", and the
        difference is not a shortcut — inverting it is a second bug:

        * The timeline path *depends* on it. ``api/project_items.py`` removes
          and commits the item FIRST and calls this second, so by the time we
          are reached the row is referenced by nobody. A "must be referenced by
          this project" test would refuse it, re-orphaning every deleted split
          tail and bringing back the id-reuse UNIQUE violation that PR #44 and
          migration ``d5b1c0a2e3f4`` exist to clear up.
        * Nothing is at risk. An unreferenced local row sits in no one's
          timeline; ``d5b1c0a2e3f4`` deletes such rows wholesale, so letting a
          caller delete one grants no reach the pruning migration does not
          already take. Ids are 53-bit random (``local_ids``), so they cannot be
          walked either.

        A row referenced by this project *and* another is refused rather than
        unlinked: local rows are single-owner by construction, so that state is
        corruption, and destroying the other project's activity is the worse
        answer to it.

        Advances the project's lock_version (issue #173) so a native client's
        on-disk cache — which only ever checks that counter — notices the
        deletion.
        """
        if activity_id >= 0:
            return False
        row = sess.get(DBActivity, activity_id)
        if row is None:
            return False
        foreign_ref = sess.exec(
            select(DBProjectItem).where(
                DBProjectItem.item_type == "activity",
                DBProjectItem.activity_id == activity_id,
                DBProjectItem.project_id != project_id,
            )
        ).first()
        if foreign_ref is not None:
            return False
        bump_lock_version(sess, project_id)
        root_id = row.split_root_id
        # Adopt this row's children onto its own parent before it goes, so the
        # split chain stays connected. Left dangling they would be unreachable
        # from any ancestor, and a later reset would restore a track right over
        # the top of them without knowing they were there (issue #143).
        for child in sess.exec(
            select(DBActivity).where(DBActivity.split_parent_id == activity_id)
        ).all():
            child.split_parent_id = row.split_parent_id
            sess.add(child)
        sess.execute(
            delete(DBProjectItem).where(
                DBProjectItem.project_id == project_id,
                DBProjectItem.item_type == "activity",
                DBProjectItem.activity_id == activity_id,
            )
        )
        # SQLite is not enforcing the foreign key (models/db.py sets no
        # PRAGMA foreign_keys), so the prepared-geometry row goes explicitly.
        sess.execute(delete(DBActivityGeoPrepared).where(
            DBActivityGeoPrepared.activity_id == activity_id))
        sess.delete(row)
        # Renumber remaining item positions to stay contiguous.
        remaining = sess.exec(
            select(DBProjectItem)
            .where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.position)
        ).all()
        for pos, it in enumerate(remaining):
            it.position = pos
            sess.add(it)
        if root_id is not None:
            self._renumber_split_family(sess, root_id)
        sess.commit()
        return True

    def force_update_activity(
        self, sess: Session, user_info_id: int, act: Activity,
        project_id: Optional[int] = None,
    ) -> None:
        """Overwrite ALL columns of an existing activity row (used for re-fetch).

        If no row exists, inserts a new one.  Unlike ``_upsert_activity``,
        this always overwrites enrichment columns (polyline, elevation) —
        EXCEPT any of the six E2EE-in-scope fields (issue #29) whose EXISTING
        value is already an encrypted envelope: a "force refresh from Strava"
        still refreshes plaintext stats (distance, counts, ...) normally, but
        must not clobber ciphertext back to plaintext geometry/name.

        Advances *project_id*'s lock_version (issue #173) so a native client's
        on-disk cache — which only ever checks that counter — notices the
        re-fetched polyline/elevation. Optional (defaults to no bump) because
        an activity row is shared across every project that references it, so
        only a caller that knows which project's cache this refresh should
        invalidate can supply it.
        """
        if act.id is None:
            return

        def _iso(dt) -> str:
            if dt is None:
                return ""
            return dt.isoformat().replace("+00:00", "Z")

        ep_json: Optional[str] = None
        if act.elevation_profile:
            ep_json = json.dumps({
                "distances_km": act.elevation_profile[0],
                "elevations_m": act.elevation_profile[1],
            })

        existing = sess.get(DBActivity, act.id)
        if existing is None:
            self._upsert_activity(sess, user_info_id, act)
            return

        if project_id is not None:
            bump_lock_version(sess, project_id)

        existing.user_info_id = user_info_id
        if not is_encrypted_envelope(existing.name):
            existing.name = act.name
        existing.type = act.type
        existing.distance = act.distance
        existing.moving_time = act.moving_time
        existing.elapsed_time = act.elapsed_time
        existing.total_elevation_gain = act.total_elevation_gain
        existing.start_date = _iso(act.start_date)
        existing.start_date_local = _iso(act.start_date_local)
        existing.timezone = act.timezone
        existing.achievement_count = act.achievement_count
        existing.kudos_count = act.kudos_count
        existing.comment_count = act.comment_count
        existing.athlete_count = act.athlete_count
        existing.photo_count = act.photo_count
        existing.pr_count = act.pr_count
        existing.total_photo_count = act.total_photo_count
        existing.trainer = act.trainer
        existing.commute = act.commute
        existing.manual = act.manual
        existing.private = act.private
        existing.flagged = act.flagged
        existing.has_heartrate = act.has_heartrate
        existing.has_kudoed = act.has_kudoed
        existing.heartrate_opt_out = act.heartrate_opt_out
        existing.display_hide_heartrate_option = act.display_hide_heartrate_option
        existing.average_speed = act.average_speed
        existing.max_speed = act.max_speed
        existing.gear_id = act.gear_id
        existing.average_heartrate = act.average_heartrate
        existing.max_heartrate = act.max_heartrate
        existing.elev_high = act.elev_high
        existing.elev_low = act.elev_low
        if not is_encrypted_envelope(existing.start_latlng_json):
            existing.start_latlng_json = json.dumps(act.start_latlng) if act.start_latlng else None
        if not is_encrypted_envelope(existing.end_latlng_json):
            existing.end_latlng_json = json.dumps(act.end_latlng) if act.end_latlng else None
        if not is_encrypted_envelope(existing.summary_polyline):
            existing.summary_polyline = act.summary_polyline
            store_prepared_geometry(sess, existing)
        if not is_encrypted_envelope(existing.elevation_profile_json):
            existing.elevation_profile_json = ep_json
            existing.elevation_profile_low_res_json = _low_res_ep_json(ep_json)
        sess.commit()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _upsert_activity(
        self, sess: Session, user_info_id: int, act: Activity
    ) -> None:
        """Insert the activity row if it doesn't exist; skip if it does.

        Enriched data (summary_polyline, elevation_profile) is only written
        for new rows — existing rows may already have richer data from a
        previous enrichment pass.
        """
        if act.id is None:
            return
        existing = sess.get(DBActivity, act.id)
        if existing is not None:
            # Update mutable user-visible fields always — except name, once it's
            # already a client-side E2EE ciphertext envelope (issue #29): a
            # plain re-sync must not silently overwrite it back to plaintext.
            if not is_encrypted_envelope(existing.name):
                existing.name = act.name
            existing.kudos_count = act.kudos_count
            existing.achievement_count = act.achievement_count
            # If the stored polyline is null but the incoming one is not, take
            # the incoming value — the user may have fixed their Strava map
            # privacy and re-synced, or the first sync happened to return null.
            # Already safe against clobbering ciphertext without an explicit
            # guard: an encrypted summary_polyline is never None (it's a
            # non-empty "v1.…" envelope), so this branch can't fire once the
            # field is encrypted.
            if existing.summary_polyline is None and act.summary_polyline:
                existing.summary_polyline = act.summary_polyline
                store_prepared_geometry(sess, existing)
            return

        def _iso(dt) -> str:
            if dt is None:
                return ""
            return dt.isoformat().replace("+00:00", "Z")

        _ep_json = (
            json.dumps({
                "distances_km": act.elevation_profile[0],
                "elevations_m": act.elevation_profile[1],
            })
            if act.elevation_profile else None
        )

        row = DBActivity(
            id=act.id,
            user_info_id=user_info_id,
            name=act.name,
            type=act.type,
            distance=act.distance,
            moving_time=act.moving_time,
            elapsed_time=act.elapsed_time,
            total_elevation_gain=act.total_elevation_gain,
            start_date=_iso(act.start_date),
            start_date_local=_iso(act.start_date_local),
            timezone=act.timezone,
            achievement_count=act.achievement_count,
            kudos_count=act.kudos_count,
            comment_count=act.comment_count,
            athlete_count=act.athlete_count,
            photo_count=act.photo_count,
            pr_count=act.pr_count,
            total_photo_count=act.total_photo_count,
            trainer=act.trainer,
            commute=act.commute,
            manual=act.manual,
            private=act.private,
            flagged=act.flagged,
            has_heartrate=act.has_heartrate,
            has_kudoed=act.has_kudoed,
            heartrate_opt_out=act.heartrate_opt_out,
            display_hide_heartrate_option=act.display_hide_heartrate_option,
            average_speed=act.average_speed,
            max_speed=act.max_speed,
            gear_id=act.gear_id,
            average_heartrate=act.average_heartrate,
            max_heartrate=act.max_heartrate,
            elev_high=act.elev_high,
            elev_low=act.elev_low,
            start_latlng_json=json.dumps(act.start_latlng) if act.start_latlng else None,
            end_latlng_json=json.dumps(act.end_latlng) if act.end_latlng else None,
            summary_polyline=act.summary_polyline,
            elevation_profile_json=_ep_json,
            elevation_profile_low_res_json=_low_res_ep_json(_ep_json),
            source=act.source,
            source_id=act.source_id,
        )
        sess.add(row)
        store_prepared_geometry(sess, row)

    @staticmethod
    def _row_to_activity(row: DBActivity, include_heavy: bool = True,
                         include_elevation: bool = True) -> Activity:
        """Reconstruct the domain Activity dataclass from a DB row.

        include_heavy=False skips accessing deferred columns summary_polyline and
        elevation_profile_json (both returned as None).  The caller must have queried
        with those columns deferred so that no lazy-load round-trip is triggered.

        include_elevation=False skips only elevation_profile_json (summary_polyline
        is still read); the caller must have deferred that column.
        """
        from datetime import datetime

        def _dt(s: str) -> datetime:
            if not s:
                return datetime.now()
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        # start_latlng_json / end_latlng_json / elevation_profile*_json may hold a
        # client-side E2EE ciphertext envelope (issue #29) instead of parseable
        # JSON once the user has encryption enabled. json.loads() on that string
        # would raise — every project load goes through here, not just geo
        # endpoints — so treat an encrypted field as absent for the parsed form
        # (every consumer of start_latlng/end_latlng/elevation_profile already
        # has a "no geometry" fallback) and surface the raw ciphertext via the
        # *_enc fields instead, so the client can decrypt + reconstruct it.
        start_latlng = None
        start_latlng_enc = None
        if row.start_latlng_json:
            if is_encrypted_envelope(row.start_latlng_json):
                start_latlng_enc = row.start_latlng_json
            else:
                start_latlng = json.loads(row.start_latlng_json)

        end_latlng = None
        end_latlng_enc = None
        if row.end_latlng_json:
            if is_encrypted_envelope(row.end_latlng_json):
                end_latlng_enc = row.end_latlng_json
            else:
                end_latlng = json.loads(row.end_latlng_json)

        elevation_profile = None
        elevation_profile_enc = None
        if include_heavy and include_elevation and row.elevation_profile_json:
            if is_encrypted_envelope(row.elevation_profile_json):
                elevation_profile_enc = row.elevation_profile_json
            else:
                ep = json.loads(row.elevation_profile_json)
                elevation_profile = (ep["distances_km"], ep["elevations_m"])

        elevation_profile_low_res = None
        elevation_profile_low_res_enc = None
        if row.elevation_profile_low_res_json:
            if is_encrypted_envelope(row.elevation_profile_low_res_json):
                elevation_profile_low_res_enc = row.elevation_profile_low_res_json
            else:
                lr = json.loads(row.elevation_profile_low_res_json)
                elevation_profile_low_res = (lr["distances_km"], lr["elevations_m"])

        return Activity(
            id=row.id,
            name=row.name,
            type=row.type,
            distance=row.distance,
            moving_time=row.moving_time,
            elapsed_time=row.elapsed_time,
            total_elevation_gain=row.total_elevation_gain,
            start_date=_dt(row.start_date),
            start_date_local=_dt(row.start_date_local),
            timezone=row.timezone,
            achievement_count=row.achievement_count,
            kudos_count=row.kudos_count,
            comment_count=row.comment_count,
            athlete_count=row.athlete_count,
            photo_count=row.photo_count,
            trainer=row.trainer,
            commute=row.commute,
            manual=row.manual,
            private=row.private,
            flagged=row.flagged,
            average_speed=row.average_speed,
            max_speed=row.max_speed,
            has_heartrate=row.has_heartrate,
            pr_count=row.pr_count,
            total_photo_count=row.total_photo_count,
            has_kudoed=row.has_kudoed,
            gear_id=row.gear_id,
            average_heartrate=row.average_heartrate,
            max_heartrate=row.max_heartrate,
            heartrate_opt_out=row.heartrate_opt_out,
            display_hide_heartrate_option=row.display_hide_heartrate_option,
            elev_high=row.elev_high,
            elev_low=row.elev_low,
            start_latlng=start_latlng,
            end_latlng=end_latlng,
            summary_polyline=row.summary_polyline if include_heavy else None,
            elevation_profile=elevation_profile,
            # Always loaded — lightweight, never deferred — so meta/low-res
            # responses can render the chart before the full profile arrives.
            elevation_profile_low_res=elevation_profile_low_res,
            is_edited=bool(getattr(row, "is_edited", False)),
            split_parent_id=getattr(row, "split_parent_id", None),
            refresh_status=getattr(row, "refresh_status", None),
            refresh_started_at=getattr(row, "refresh_started_at", None),
            refresh_error=getattr(row, "refresh_error", None),
            source=getattr(row, "source", None),
            source_id=getattr(row, "source_id", None),
            start_latlng_enc=start_latlng_enc,
            end_latlng_enc=end_latlng_enc,
            # Prefer the full profile's ciphertext; fall back to the low-res
            # ciphertext when the full profile is deferred/unavailable — mirrors
            # ProjectIO.to_dict()'s _ep_pairs() fallback for the plaintext case.
            elevation_profile_enc=elevation_profile_enc or elevation_profile_low_res_enc,
        )

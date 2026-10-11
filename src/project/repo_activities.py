"""Activity persistence, enrichment writes, and geometry editing (issue #31).

Part of the ``ProjectRepo`` mixin split — see ``src/project/project_repo.py``
for the composed class and module docstring.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy import delete
from sqlmodel import Session, select

from models.db import get_session
from models.project_db import DBActivity, DBActivityGeoPrepared, DBProject, DBProjectItem, DBProjectMember
from src.models.activity import Activity, is_activity_id
from src.models.prepared_geo import prepare_polyline
from src.models.project import Project, strava_identity
from src.models.simplify import PREPARED_GEO_VERSION
from src.project.local_ids import allocate_activity_id
from src.project.elevation_downsample import downsample_elevation
from src.project.repo_core import bump_lock_version, check_and_bump_lock_version
from src.utils.encryption_check import is_encrypted_envelope
from src.utils.logging import get_logger

_log = get_logger(__name__)

# Bound on the ids one IN (...) carries, well under SQLite's variable limit.
_ID_CHUNK = 500


def _chunks(ids: List[int]):
    """*ids* in slices of at most ``_ID_CHUNK``."""
    for i in range(0, len(ids), _ID_CHUNK):
        yield ids[i:i + _ID_CHUNK]


def trip_rows(project_id: int):
    """SQL: the ids of the activity rows trip *project_id* holds — those its
    activity items name. A row belongs to exactly one trip (#592), so this is
    the scope of every write the trip makes to its activities."""
    return select(DBProjectItem.activity_id).where(
        DBProjectItem.project_id == project_id,
        DBProjectItem.item_type == "activity",
        DBProjectItem.activity_id.is_not(None),
    )


def other_trips_rows(project_id: int):
    """SQL: the ids of the activity rows any trip but *project_id* holds —
    rows a write made from that trip must never touch (review R1-2)."""
    return select(DBProjectItem.activity_id).where(
        DBProjectItem.project_id != project_id,
        DBProjectItem.activity_id.is_not(None),
    )


def _carries_envelope(act: Activity) -> bool:
    """Whether *act* holds a client-side E2EE envelope in any field."""
    return bool(
        act.start_latlng_enc or act.end_latlng_enc or act.elevation_profile_enc
        or is_encrypted_envelope(act.name)
        or is_encrypted_envelope(act.summary_polyline))


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


def _ep_tuple(ep_json: str):
    """A stored elevation_profile JSON blob as the model's ``(distances_km,
    elevations_m)`` pair."""
    ep = json.loads(ep_json)
    return (ep["distances_km"], ep["elevations_m"])


def _parsed_or_envelope(text: Optional[str], parse):
    """``(parsed, None)`` for a plaintext JSON column, ``(None, envelope)`` for
    one holding a client-side E2EE envelope, ``(None, None)`` when empty."""
    if not text:
        return None, None
    if is_encrypted_envelope(text):
        return None, text
    return parse(text), None


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


class EncryptedEditOnDevice(Exception):
    """The stored polyline or profile is a client-side E2EE envelope.

    The plaintext track routes take decrypted points and would store their
    result in plaintext, so they refuse such a row; the device edits it
    instead. Raised before anything is decoded or written.
    """


class NothingToRestore(Exception):
    """An edited row whose ``original_polyline`` is null has no geometry to
    reset to: the shipped encryption migration nulled the snapshots, and
    copying the nulls back would wipe the track."""


class NotEncrypted(Exception):
    """The stored polyline is not an E2EE envelope, so the encrypted track
    routes, which store only device-computed ciphertext, refuse the row: its
    track is edited on the server. Raised before anything is written."""


#: The scalars a first edit snapshots beside the geometry (E2EE remnants
#: decision 7), each into ``original_<name>``: reset restores them exactly,
#: which it must do without reading the geometry once that is ciphertext.
_SCALAR_SNAPSHOT = (
    "distance", "moving_time", "elapsed_time", "average_speed",
    "elev_high", "elev_low", "start_latlng_json", "end_latlng_json",
)

#: What an encrypted edit writes, from the device: the geometry envelopes and
#: the scalars computed from it. ``elevation_profile_json`` also goes to the
#: low-res column, which cannot be derived from ciphertext.
ENCRYPTED_PIECE_FIELDS = (
    "summary_polyline", "elevation_profile_json", "start_latlng_json",
    "end_latlng_json", "distance", "moving_time", "elapsed_time",
    "average_speed", "total_elevation_gain", "elev_high", "elev_low",
)


def _snapshot_current(row: DBActivity) -> None:
    """Make the row's current track and figures its edit snapshot."""
    row.original_polyline = row.summary_polyline
    row.original_elevation_profile_json = row.elevation_profile_json
    row.original_total_elevation_gain = row.total_elevation_gain
    for name in _SCALAR_SNAPSHOT:
        setattr(row, f"original_{name}", getattr(row, name))


def _snapshot_first_edit(row: DBActivity) -> None:
    """Snapshot the pre-edit track and figures on the FIRST edit only, so a
    later "Reset to Strava" can restore them."""
    if not row.is_edited:
        _snapshot_current(row)


def _apply_encrypted_piece(sess: Session, row: DBActivity, piece: Dict) -> None:
    """Store a device-computed track on *row*, verbatim (no commit)."""
    for name in ENCRYPTED_PIECE_FIELDS:
        setattr(row, name, piece[name])
    row.elevation_profile_low_res_json = piece["elevation_profile_json"]
    # Removes any prepared row: ciphertext cannot be prepared.
    store_prepared_geometry(sess, row)
    row.is_edited = True


def _refuse_plaintext_geometry(row: DBActivity) -> None:
    if not is_encrypted_envelope(row.summary_polyline):
        raise NotEncrypted(row.id)


def _recheck_encrypted_geometry(sess: Session, row: DBActivity) -> None:
    """The mirror of ``_recheck_plaintext_geometry`` for the encrypted
    routes: a row decrypted since the first check is refused, the bump
    rolled back."""
    sess.refresh(row, ["summary_polyline"])
    try:
        _refuse_plaintext_geometry(row)
    except NotEncrypted:
        sess.rollback()
        raise


def _refuse_encrypted_geometry(row: DBActivity) -> None:
    if (is_encrypted_envelope(row.summary_polyline)
            or is_encrypted_envelope(row.elevation_profile_json)):
        raise EncryptedEditOnDevice(row.id)


def _recheck_plaintext_geometry(sess: Session, row: DBActivity) -> None:
    """Re-read the geometry once the lock-version bump holds the write lock.

    The first check ran on a read taken before this transaction began, so the
    catch-up may have encrypted the row since. Writing the plaintext result
    over that would leave an encrypted name beside a plaintext track. Rolls
    the bump back when it refuses.
    """
    sess.refresh(row, ["summary_polyline", "elevation_profile_json"])
    try:
        _refuse_encrypted_geometry(row)
    except EncryptedEditOnDevice:
        sess.rollback()
        raise


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
        *,
        owner_id: int,
        project_id: Optional[int] = None,
    ) -> None:
        """Update only the enrichment columns of the activity row *activity_id*.

        Writes that one row (#592, plan Decision 6): another trip's copy of
        the same Strava activity keeps what it has. ``owner_id`` is the
        account whose Strava streams these are; a row whose Strava data came
        through any other account (``strava_user_id``) is left alone. With
        *project_id*, so is a row that trip does not hold.

        Skips a field whose EXISTING value is already a client-side E2EE
        ciphertext envelope (issue #29) — once a field is encrypted, a
        background Strava enrichment pass (which only ever has fresh plaintext
        streams) must not silently overwrite it back to plaintext.
        """
        row = sess.get(DBActivity, activity_id)
        if row is None or row.strava_user_id != owner_id:
            return
        if project_id is not None and not self.trip_holds_activity(
                sess, project_id, activity_id):
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

        Snapshots the pre-edit polyline/elevation and scalars into the
        original_* columns on the FIRST edit only, so a later "Reset to Strava"
        can restore them.

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

        _snapshot_first_edit(row)

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

        Raises ``EncryptedEditOnDevice``, writing nothing, when the stored
        polyline or profile is an envelope.
        """
        row = sess.get(DBActivity, activity_id)
        if row is None:
            return False
        _refuse_encrypted_geometry(row)
        if expected_version is not None:
            check_and_bump_lock_version(sess, project_id, expected_version)
        else:
            bump_lock_version(sess, project_id)
        _recheck_plaintext_geometry(sess, row)
        self._write_track_geometry(row, points, sess=sess)
        sess.commit()
        return True

    def edit_activity_track_encrypted(
        self, sess: Session, project_id: int, activity_id: int, piece: Dict,
        *, expected_version: int,
    ) -> bool:
        """Store a track the device edited and encrypted (E2EE remnants
        decision 7): *piece* holds ``ENCRYPTED_PIECE_FIELDS``, already checked.

        The ``edit_activity_track`` of an encrypted row: the same snapshot on
        the first edit, the same is_edited mark and the same compare-and-swap
        on the project's lock_version (required here), with the geometry and
        figures taken as sent instead of computed. Returns False if the row
        does not exist; raises ``NotEncrypted``, writing nothing, when its
        stored polyline is not an envelope.
        """
        row = sess.get(DBActivity, activity_id)
        if row is None:
            return False
        _refuse_plaintext_geometry(row)
        check_and_bump_lock_version(sess, project_id, expected_version)
        _recheck_encrypted_geometry(sess, row)
        _snapshot_first_edit(row)
        _apply_encrypted_piece(sess, row, piece)
        sess.commit()
        return True

    @staticmethod
    def _split_descendants(
        sess: Session, project_id: int, activity_id: int
    ) -> List[DBActivity]:
        """Every piece transitively cut out of *activity_id* that no other trip
        than *project_id* holds: the trip's own pieces, and pieces no trip
        holds any more (an item unlinked while its row stayed).

        Walks ``split_parent_id`` breadth-first: a piece records which piece it
        was cut directly out of, so this is exactly the set that resetting
        *activity_id* would grow back over. ``split_root_id`` cannot answer this
        — it points every piece of a family at the family's FIRST piece however
        deep the chain, which says nothing about who cut whom (issue #143).

        A piece another trip holds is never walked (#592, review R1-2). The
        migration leaves no split link crossing trips; this is the guard behind
        that invariant, so a reset can never delete another trip's row. An
        unreferenced piece is walked, so a reset deletes it rather than leave
        it naming the head it was cut out of (U3R1-1). ``seen`` likewise guards
        a cycle that split_activity cannot create.
        """
        found: List[DBActivity] = []
        seen = {activity_id}
        frontier = [activity_id]
        while frontier:
            children = sess.exec(
                select(DBActivity).where(
                    DBActivity.split_parent_id.in_(frontier),
                    DBActivity.id.not_in(other_trips_rows(project_id)),
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
        pieces = self._split_descendants(sess, project_id, row.id)
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
            sess, project_id,
            row.split_root_id if row.split_root_id is not None else row.id)
        return len(pieces)

    def reset_activity_track(
        self, sess: Session, project_id: int, activity_id: int,
        *, expected_version: Optional[int] = None,
    ) -> bool:
        """Restore an edited activity's geometry from its original snapshot.

        Restores the scalar metrics with it and clears is_edited + the
        snapshot columns.  Returns False if the row does not exist or was never
        edited (nothing to reset).

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

        The geometry comes back verbatim. The figures come back exactly from
        the scalar snapshot when the row has one (``original_distance`` set:
        every edit since E2EE remnants U12, and the backfill); a row edited
        before that recomputes them from the restored track, as reset always
        did. An enveloped original can only be restored from the snapshot,
        since the server cannot measure ciphertext.

        Raises ``NothingToRestore``, writing nothing, for an edited row whose
        ``original_polyline`` is null, or whose originals are envelopes with no
        scalar snapshot. With *expected_version* the reset is a
        compare-and-swap on the project's lock_version, as in
        ``edit_activity_track``.
        """
        row = sess.get(DBActivity, activity_id)
        if row is None or not row.is_edited:
            return False
        if not row.original_polyline:
            raise NothingToRestore(activity_id)
        has_snapshot = row.original_distance is not None
        enveloped = (is_encrypted_envelope(row.original_polyline)
                     or is_encrypted_envelope(row.original_elevation_profile_json))
        if enveloped and not has_snapshot:
            # The server cannot measure ciphertext, and decoding it below would
            # raise or restore garbage.
            _log.error("reset refused: activity %s has an enveloped original "
                       "and no scalar snapshot", activity_id)
            raise NothingToRestore(activity_id)

        # Advance the project's lock_version (issue #173) so a native client's
        # on-disk cache — which only ever checks that counter — notices the
        # restored geometry.
        if expected_version is not None:
            check_and_bump_lock_version(sess, project_id, expected_version)
        else:
            bump_lock_version(sess, project_id)

        self._remove_split_descendants(sess, project_id, row)

        orig_ep_json = row.original_elevation_profile_json
        row.summary_polyline = row.original_polyline
        store_prepared_geometry(sess, row)
        row.elevation_profile_json = orig_ep_json
        # The low-res copy of an enveloped profile is the same envelope, as the
        # client's encryption writes it: it cannot be derived from ciphertext.
        row.elevation_profile_low_res_json = (
            orig_ep_json if is_encrypted_envelope(orig_ep_json)
            else _low_res_ep_json(orig_ep_json))

        from src.models.track_edit import recompute_track_metrics

        if has_snapshot:
            for name in _SCALAR_SNAPSHOT:
                setattr(row, name, getattr(row, f"original_{name}"))
            if row.original_total_elevation_gain is not None:
                row.total_elevation_gain = row.original_total_elevation_gain
            elif not enveloped:
                points = self._original_points(row)
                if points:
                    row.total_elevation_gain = recompute_track_metrics(points).total_elevation_gain
        else:
            points = self._original_points(row)
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
        for name in _SCALAR_SNAPSHOT:
            setattr(row, f"original_{name}", None)
        sess.commit()
        return True

    @staticmethod
    def _original_points(row: DBActivity) -> list:
        """The points of a row's plaintext original track."""
        from src.models.track_edit import align_points

        orig_ep = None
        if row.original_elevation_profile_json:
            ep = json.loads(row.original_elevation_profile_json)
            orig_ep = (ep.get("distances_km") or [], ep.get("elevations_m") or [])
        return align_points(row.original_polyline, orig_ep)

    @staticmethod
    def _renumber_split_family(sess: Session, project_id: int, root_id: int) -> None:
        """Rename every member of a split family that trip *project_id* holds
        as ``"<base> (i/N)"``, 1-indexed in chronological (start_date) order.
        Called after a split creates a new piece and after a local tail is
        deleted, so the numbering always reflects the CURRENT family size
        instead of accumulating suffixes (issue #45 follow-up — this replaced
        the old ``"<name> (2)"`` scheme, which produced "<name> (2) (2)" when a
        piece was split again).

        Only the trip's own rows are counted and renamed (#592, review R1-2):
        no-op when another trip holds the root, which the migration leaves no
        family in. A root no trip holds (its item removed while its pieces
        stayed) still gives the base name.

        No-op if the root's name is an E2EE ciphertext envelope (issue #29) — a
        base name can't be meaningfully derived from ciphertext, so this leaves
        whatever name each piece already has untouched. Also no-op if the
        family has been reduced to (or never grew past) a single surviving
        piece, other than restoring its plain base name if one was recorded.
        """
        root = sess.get(DBActivity, root_id)
        if root is None or is_encrypted_envelope(root.name):
            return
        if sess.exec(select(DBProjectItem.id).where(
                DBProjectItem.item_type == "activity",
                DBProjectItem.activity_id == root_id,
                DBProjectItem.project_id != project_id)).first() is not None:
            return

        members = sess.exec(
            select(DBActivity).where(
                (DBActivity.id == root_id) | (DBActivity.split_root_id == root_id),
                DBActivity.id.in_(trip_rows(project_id)),
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

        Raises ``EncryptedEditOnDevice``, writing nothing, when the head's
        stored polyline or profile is an envelope.
        """
        from src.models.track_edit import align_points

        head = sess.get(DBActivity, activity_id)
        if head is None:
            return None
        _refuse_encrypted_geometry(head)

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
        _recheck_plaintext_geometry(sess, head)

        head_points = points[: split_index + 1]
        tail_points = points[min_tail_start:]

        tail = self._new_split_tail(sess, head)
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

        # Write head then tail geometry (each snapshots its own original + recomputes).
        self._write_track_geometry(head, head_points, sess=sess)
        self._start_tail_after_head(tail, head)
        self._write_track_geometry(tail, tail_points, sess=sess)
        # Re-point the tail's snapshot at its OWN geometry. The seeding above is
        # a time-apportioning device, but _write_track_geometry snapshots whatever
        # sat on the row before the write — for a fresh tail that's the head's
        # full pre-split track, so "Reset" expanded the tail back into a duplicate
        # of the whole track it was cut out of (issue #131). A tail has no upstream
        # original; the only thing reset can meaningfully undo is edits made to the
        # tail AFTER the split, so snapshot the post-split geometry instead. This
        # keeps the seeding trick and decouples it from the snapshot.
        _snapshot_current(tail)

        self._insert_split_tail(sess, project_id, head.id, tail.id)
        self._renumber_split_family(sess, project_id, tail.split_root_id)
        sess.commit()
        return tail.id

    def split_activity_encrypted(
        self, sess: Session, project_id: int, activity_id: int,
        head_piece: Dict, tail_piece: Dict, tail_name: str,
        *, expected_version: int,
    ) -> Optional[int]:
        """Split an encrypted activity the device has already cut (E2EE
        remnants decision 7). *head_piece* and *tail_piece* hold
        ``ENCRYPTED_PIECE_FIELDS``, *tail_name* the tail's encrypted name, all
        already checked.

        Keeps ``split_activity``'s bookkeeping — a local tail id, the family
        links, the head's snapshot on its first edit, the tail item right after
        the head, the tail starting where the head ends, the tail's snapshot
        being its own values, the family renamed (which an enveloped root
        skips) — and takes each piece's geometry and figures as sent. The
        compare-and-swap on the project's lock_version is required here.

        Returns the new tail id, or None if the activity is missing. Raises
        ``NotEncrypted``, writing nothing, when the head's stored polyline is
        not an envelope.
        """
        head = sess.get(DBActivity, activity_id)
        if head is None:
            return None
        _refuse_plaintext_geometry(head)
        check_and_bump_lock_version(sess, project_id, expected_version)
        _recheck_encrypted_geometry(sess, head)

        tail = self._new_split_tail(sess, head)
        tail.name = tail_name
        _snapshot_first_edit(head)
        _apply_encrypted_piece(sess, head, head_piece)
        self._start_tail_after_head(tail, head)
        _apply_encrypted_piece(sess, tail, tail_piece)
        _snapshot_current(tail)

        self._insert_split_tail(sess, project_id, head.id, tail.id)
        self._renumber_split_family(sess, project_id, tail.split_root_id)
        sess.commit()
        return tail.id

    @staticmethod
    def _new_split_tail(sess: Session, head: DBActivity) -> DBActivity:
        """A new tail row cut out of *head*, added to *sess*, with no geometry.

        Shared by the plaintext and the encrypted split.
        """
        # A tail is an activity the app creates, so it takes a local (negative)
        # id from the shared allocator — the same one GPX import uses, rather
        # than the decrementing scheme this used to have. That scheme handed out
        # -1, -2, -3 in every deployment, which collides the moment a .traxj
        # exported from one is imported into another. The allocator also keeps
        # the property that mattered here: it checks the activity TABLE, not
        # this project's timeline, because a tail whose item was removed leaves
        # the row behind and reusing its id would collide on INSERT. A tail has
        # no strava_id, whatever its head came from: there is no Strava
        # activity to refresh it from.
        tail_id = allocate_activity_id(sess, strava=False)

        # Resolve the family root: the very first piece ever split, regardless
        # of how deep this split is in the chain (splitting an already-split
        # tail still points its own new tail at the SAME root as its parent).
        root_id = head.split_root_id if head.split_root_id is not None else head.id

        # Create the tail row copying the head's metadata. name is a
        # placeholder — _renumber_split_family overwrites it below (kept here
        # only as the fallback when that's skipped, e.g. an E2EE-encrypted name).
        tail = DBActivity(
            id=tail_id,
            # Cut out of the head, so owned by whoever owns the head: the
            # trip's owner, or the account whose key encrypted it (plan
            # Decision 4).
            user_info_id=head.user_info_id,
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
            # Measured by the app exactly as its head is: a tail of a GPX
            # upload is one too (issue #462), not a Strava activity.
            source=head.source,
        )
        sess.add(tail)
        return tail

    @staticmethod
    def _start_tail_after_head(tail: DBActivity, head: DBActivity) -> None:
        """Start *tail* where the already written *head* ends."""
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

    @staticmethod
    def _insert_split_tail(
        sess: Session, project_id: int, head_id: int, tail_id: int
    ) -> None:
        """Insert the tail item directly after the head item, renumbering positions."""
        item_rows = sess.exec(
            select(DBProjectItem)
            .where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.position)
        ).all()
        new_order: List[DBProjectItem] = []
        for it in item_rows:
            new_order.append(it)
            if it.item_type == "activity" and it.activity_id == head_id:
                new_order.append(DBProjectItem(
                    project_id=project_id, position=0,
                    item_type="activity", activity_id=tail_id,
                ))
        for pos, it in enumerate(new_order):
            it.position = pos
            sess.add(it)

    def delete_local_activity(
        self, sess: Session, project_id: int, activity_id: int
    ) -> bool:
        """Delete a LOCAL activity row and unlink it from items.

        Local means the app made it — a split piece or a GPX import — so it has
        no ``strava_id``; a Strava activity leaves a trip through its timeline
        item instead. Also renumbers any surviving split-family siblings (see
        ``_renumber_split_family``) so "(i/N)" reflects the new, smaller N.
        Returns False if the row is not local, is absent, or belongs to a
        DIFFERENT project than *project_id*.

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

        A row another trip holds is that trip's (#592: one row per trip), so it
        is refused whether or not this project holds it too.

        Advances the project's lock_version (issue #173) so a native client's
        on-disk cache — which only ever checks that counter — notices the
        deletion.
        """
        row = sess.get(DBActivity, activity_id)
        if row is None or row.strava_id is not None:
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
        # the top of them without knowing they were there (issue #143). A piece
        # another trip holds is that trip's, and is left alone (review R1-2).
        for child in sess.exec(
            select(DBActivity).where(
                DBActivity.split_parent_id == activity_id,
                DBActivity.id.not_in(other_trips_rows(project_id)),
            )
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
            self._renumber_split_family(sess, project_id, root_id)
        sess.commit()
        return True

    def delete_unreferenced_activities(
        self, sess: Session, ids: Iterable[int]
    ) -> Set[int]:
        """Delete the rows among *ids* that no trip item references (#592,
        plan Decision 9), with their prepared geometry. No commit.

        A row belongs to the one trip holding it, so once that trip lets go of
        it — its item removed, or the trip deleted — nothing else reaches it.
        Whatever the row came from: Strava, a GPX file or a split.

        Split links are kept whole. A family root that a surviving piece still
        names stays, and goes with the last piece naming it: the ancestors of
        the rows given are considered with them, so a head no item references
        goes when its last piece does (U3R1-1). A surviving piece
        cut out of a deleted one is adopted by its nearest surviving ancestor,
        as ``delete_local_activity`` does. The survivors of a family that lost
        pieces are renumbered in the trip holding them, its ``lock_version``
        advanced first (issue #173: names are part of what a native client
        caches under that counter).

        Returns the ids of those renumbered trips.
        """
        given = {i for i in ids if is_activity_id(i)}
        # The pieces' ancestors are considered too (U3R1-1): a head whose item
        # was removed while its pieces kept it stays only as long as they do.
        frontier = set(given)
        while frontier:
            ancestors: Set[int] = set()
            for chunk in _chunks(sorted(frontier)):
                for root, parent in sess.exec(select(
                        DBActivity.split_root_id, DBActivity.split_parent_id,
                ).where(DBActivity.id.in_(chunk))).all():
                    ancestors.update(a for a in (root, parent) if a is not None)
            frontier = ancestors - given
            given |= frontier
        given = sorted(given)
        referenced: Set[int] = set()
        for chunk in _chunks(given):
            referenced.update(sess.exec(select(DBProjectItem.activity_id).where(
                DBProjectItem.activity_id.in_(chunk))).all())
        doomed = set(given) - referenced
        while doomed:
            namers: Dict[int, List[int]] = {}
            for chunk in _chunks(sorted(doomed)):
                for aid, root in sess.exec(select(
                        DBActivity.id, DBActivity.split_root_id,
                ).where(DBActivity.split_root_id.in_(chunk))).all():
                    if aid not in doomed:
                        namers.setdefault(root, []).append(aid)
            if not namers:
                break
            for root, pieces in sorted(namers.items()):
                if not sess.exec(select(DBProjectItem.id).where(
                        DBProjectItem.activity_id.in_(pieces))).first():
                    # A split committed while its trip was being deleted
                    # (review R3-3): kept, and said so.
                    _log.warning("activity %s kept only by split tails no trip holds: %s",
                                 root, sorted(pieces))
            doomed -= set(namers)
        if not doomed:
            return set()

        parent_of: Dict[int, Optional[int]] = {}
        roots: Set[int] = set()
        for chunk in _chunks(sorted(doomed)):
            for aid, root, parent in sess.exec(select(
                    DBActivity.id, DBActivity.split_root_id, DBActivity.split_parent_id,
            ).where(DBActivity.id.in_(chunk))).all():
                parent_of[aid] = parent
                if root is not None:
                    roots.add(root)
        for chunk in _chunks(sorted(parent_of)):
            for child in sess.exec(select(DBActivity).where(
                    DBActivity.split_parent_id.in_(chunk))).all():
                if child.id in parent_of:
                    continue
                parent = child.split_parent_id
                while parent in parent_of:
                    parent = parent_of[parent]
                child.split_parent_id = parent
                sess.add(child)
        self._delete_activity_rows(sess, sorted(parent_of))

        renamed: Set[int] = set()
        for root_id in sorted(roots - doomed):
            family = select(DBActivity.id).where(
                (DBActivity.id == root_id) | (DBActivity.split_root_id == root_id))
            for project_id in sorted(set(sess.exec(select(DBProjectItem.project_id).where(
                    DBProjectItem.activity_id.in_(family))).all())):
                if project_id not in renamed:
                    bump_lock_version(sess, project_id)
                    renamed.add(project_id)
                self._renumber_split_family(sess, project_id, root_id)
        return renamed

    def delete_unreferenced_strava_activities(
        self,
        sess: Session,
        user_info_id: int,
        ids: Optional[Iterable[int]] = None,
        tail_ids: Optional[Iterable[int]] = None,
    ) -> Set[int]:
        """Adapter for the callers in ``api/`` until #592's U5 and U6 call
        ``delete_unreferenced_activities`` themselves: deletes the rows among
        *ids* and *tail_ids* that no trip references, and commits.

        *user_info_id* is no longer read: a row is its trip's, whoever owns
        it. Without *ids* (a Strava disconnect) nothing is deleted: an
        activity stays in its trip when its importer disconnects (plan
        Decision 9).
        """
        if ids is None:
            return set()
        renamed = self.delete_unreferenced_activities(
            sess, list(ids) + list(tail_ids or ()))
        sess.commit()
        return renamed

    @staticmethod
    def _delete_activity_rows(sess: Session, ids: List[int]) -> None:
        """Delete activity rows and their prepared geometry, in bounded batches.

        SQLite is not enforcing the foreign key (models/db.py sets no PRAGMA
        foreign_keys), so the prepared-geometry rows go explicitly.
        """
        for i in range(0, len(ids), _ID_CHUNK):
            chunk = ids[i:i + _ID_CHUNK]
            sess.execute(delete(DBActivityGeoPrepared).where(
                DBActivityGeoPrepared.activity_id.in_(chunk)))
            sess.execute(delete(DBActivity).where(DBActivity.id.in_(chunk)))

    def force_update_activity(
        self, sess: Session, user_info_id: int, act: Activity,
        project_id: Optional[int] = None,
        *,
        row_id: Optional[int] = None,
    ) -> None:
        """Overwrite ALL data columns of one activity row from *act*, a
        re-fetch from Strava.

        Writes the row *row_id* (default ``act.id``) and no other (#592, plan
        Decision 6): another trip's copy of the same Strava activity keeps
        what it has until that trip refreshes it. Unlike a save, this always
        overwrites enrichment columns (polyline, elevation) — EXCEPT any of
        the six E2EE-in-scope fields (issue #29) whose EXISTING value is
        already an encrypted envelope: a "force refresh from Strava" still
        refreshes plaintext stats (distance, counts, ...) normally, but must
        not clobber ciphertext back to plaintext geometry/name.

        Writes nothing when the row is gone (the activity left its trip while
        Strava was answering: a row created now would belong to no trip),
        when its Strava data came through another account than
        *user_info_id* (``strava_user_id``, plan Decision 5), or when
        *project_id* is given and does not hold it.

        Advances *project_id*'s lock_version (issue #173) so a native client's
        on-disk cache — which only ever checks that counter — notices the
        re-fetched polyline/elevation.
        """
        target = row_id if row_id is not None else act.id
        if target is None:
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

        existing = sess.get(DBActivity, target)
        if existing is None:
            # The background re-fetch outlived the row (issue #509, review
            # guard U6-R1-1): nothing to write it to.
            _log.warning(
                "refresh dropped for activity=%s user=%s: row deleted while the "
                "re-fetch was in flight", target, user_info_id)
            return
        # The two exits below leave the row as it was, and the refresh job
        # still reports it resolved: said in the log at least (review guard
        # U4R1-2).
        if existing.strava_user_id != user_info_id:
            _log.warning(
                "refresh dropped for activity=%s user=%s: strava_user_id no longer "
                "matches (now %s)", target, user_info_id, existing.strava_user_id)
            return
        if project_id is not None and not self.trip_holds_activity(
                sess, project_id, target):
            _log.warning(
                "refresh dropped for activity=%s user=%s: trip %s no longer holds "
                "the row", target, user_info_id, project_id)
            return

        if project_id is not None:
            bump_lock_version(sess, project_id)

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
        existing.has_kudoed = act.has_kudoed
        existing.average_speed = act.average_speed
        existing.max_speed = act.max_speed
        existing.gear_id = act.gear_id
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

    def activity_rewritable_by_trip(
        self, sess: Session, project_id: int, activity_id: int
    ) -> bool:
        """Whether a trip may rewrite or delete this activity's row.

        An activity row is shared by every trip that holds it, so changing it
        from one trip changes it in all of them. A trip may do that only to an
        activity of its owner or of a current member: someone who is part of
        the trip now. True for a row that does not exist: there is nothing to
        protect, and the caller answers for the missing row itself.
        """
        act = sess.get(DBActivity, activity_id)
        if act is None:
            return True
        project = sess.get(DBProject, project_id)
        if project is not None and act.user_info_id == project.user_info_id:
            return True
        return sess.exec(select(DBProjectMember.id).where(
            DBProjectMember.project_id == project_id,
            DBProjectMember.user_info_id == act.user_info_id,
        )).first() is not None

    def activity_e2ee_writable_by(
        self, sess: Session, activity_id: int, user_info_id: int
    ) -> bool:
        """Whether *user_info_id* may write this activity's E2EE fields: only
        the row's owner may. That is the owner of the trip holding it, or for
        a row already encrypted under someone else's key, that someone (plan
        Decision 4), since no one else could read what they wrote. False for a
        row that does not exist.
        """
        owner = sess.exec(
            select(DBActivity.user_info_id).where(DBActivity.id == activity_id)
        ).first()
        return owner is not None and owner == user_info_id

    def activity_shared_with_others(
        self, sess: Session, activity_id: int, user_info_id: int
    ) -> bool:
        """Always False: a row belongs to one trip (#592), so no trip of
        anyone else holds it. Kept for ``api/activities.py``'s
        ``_refuse_shared`` until #592's U4 removes it."""
        return False

    def activity_owners(self, sess: Session, activity_ids) -> Dict[int, int]:
        """Owner account of each of *activity_ids* that has a row.

        Only plain integers are looked up: anything else names no row here,
        though SQLite would match "9001" to 9001 (see ``is_activity_id``).
        Kept for the ``.traxj`` import (``repo_transfer``) until #592's U6
        gives every file activity a fresh row.
        """
        ids = {aid for aid in activity_ids if is_activity_id(aid)}
        if not ids:
            return {}
        return dict(sess.exec(
            select(DBActivity.id, DBActivity.user_info_id).where(DBActivity.id.in_(ids))
        ).all())

    def own_activities_only(
        self, sess: Session, user_info_id: int, activities: List[Activity]
    ) -> List[Activity]:
        """*activities* less those whose id is not a plain integer.

        An activity no longer has to be the caller's to be added: whatever row
        another account has for it, the trip gets a row of its own (#592).
        Kept for ``api/activities.py`` and ``api/strava.py`` until #592's U4
        and U5 drop it.
        """
        return [a for a in activities if a.id is None or is_activity_id(a.id)]

    def trip_holds_activity(self, sess: Session, project_id: int, activity_id: int) -> bool:
        """Whether trip *project_id* holds the activity row *activity_id*."""
        return sess.exec(trip_rows(project_id).where(
            DBProjectItem.activity_id == activity_id)).first() is not None

    def trip_activity_ids_by_strava_id(
        self, sess: Session, project_id: int
    ) -> Dict[int, int]:
        """The rows trip *project_id* holds for Strava activities, by
        ``strava_id``: how an activity posted or synced from Strava finds the
        trip's own row (#592, plan Decision 7)."""
        return dict(sess.exec(
            select(DBActivity.strava_id, DBActivity.id).where(
                DBActivity.id.in_(trip_rows(project_id)),
                DBActivity.strava_id.is_not(None),
            )
        ).all())

    def _save_trip_activities(
        self, sess: Session, project_id: int, trip_owner_id: int, adder_id: int,
        project: Project,
    ) -> None:
        """Write the activities of *project*, being saved as trip
        *project_id*, as rows of that trip alone (#592, plan Decisions 1 and
        7). No commit.

        * A row the trip already holds is left as it is. A save carries back
          what was loaded, so there is nothing to write, and a re-add of an
          activity the trip holds changes nothing: a split's "(i/N)" name
          survives a sync.
        * An activity an item names that the trip does not hold gets a new row
          — even when another trip, or another account, has a row for the
          same Strava activity. Unless the trip already holds a row with its
          Strava id: then that row stands for it, and the item a re-add made
          is dropped. A new Strava row takes a positive id, a local one a
          negative id (plan Decision 3); a local activity keeps the id its
          caller already allocated while no row has it.
        * An item naming any other row is left out, so the trip never comes to
          reference another trip's row, nor an id with nothing to create it.
        * An activity named by a held row's id but carrying another Strava id
          than that row's is not that row: a Strava id is never compared with
          a row id. Its items cannot be told from the row's, so it is left out
          (``Project.add_activities`` gives an added one an id of its own).

        A new row belongs to the trip's owner, unless it carries an envelope:
        then to *adder_id*, the only account that can read it (plan Decision
        4). A new Strava row records *adder_id* as ``strava_user_id``, the
        account whose token refreshes it (plan Decision 5).

        *project*'s items and activities are rewritten in place to name the
        rows; the Activity objects the caller passed in are not changed, so a
        retried save starts again from the same input.
        """
        held = set(sess.exec(trip_rows(project_id)).all())
        wanted = {
            it.activity_id for it in project.items
            if it.item_type == "activity" and is_activity_id(it.activity_id)
            and it.activity_id not in held
        }
        by_strava = self.trip_activity_ids_by_strava_id(sess, project_id) if held else {}
        strava_of_row = {row_id: sid for sid, row_id in by_strava.items()}

        row_of: Dict[int, int] = {}
        readded: Set[int] = set()
        activities: List[Activity] = []
        for act in project.activities:
            if not is_activity_id(act.id):
                continue
            if act.id in held:
                held_sid = strava_of_row.get(act.id)
                sid = strava_identity(act)
                if held_sid is not None and sid is not None and sid != held_sid:
                    _log.warning("project id=%s: left out an activity naming row %s, "
                                 "which holds another Strava activity", project_id, act.id)
                    continue
                activities.append(act)
                continue
            if act.id not in wanted or act.id in row_of:
                continue
            sid = strava_identity(act)
            if sid is not None and sid in by_strava:
                row_of[act.id] = by_strava[sid]
                readded.add(act.id)
                continue
            if sid is None and act.id < 0 and sess.get(DBActivity, act.id) is None:
                row_id = act.id
            else:
                row_id = allocate_activity_id(sess, strava=sid is not None)
            row = self._new_activity_row(
                act, row_id, adder_id if _carries_envelope(act) else trip_owner_id)
            row.strava_id = sid
            row.strava_user_id = adder_id if sid is not None else None
            sess.add(row)
            store_prepared_geometry(sess, row)
            row_of[act.id] = row_id
            if sid is not None:
                by_strava[sid] = row_id
            activities.append(act if row_id == act.id else dataclasses.replace(act, id=row_id))

        present = {
            it.activity_id for it in project.items
            if it.item_type == "activity" and is_activity_id(it.activity_id)
            and it.activity_id in held
        }
        items = []
        dropped = 0
        for it in project.items:
            aid = it.activity_id
            if it.item_type != "activity" or aid is None:
                items.append(it)
                continue
            if is_activity_id(aid) and aid in held:
                items.append(it)
                continue
            row_id = row_of.get(aid) if is_activity_id(aid) else None
            if row_id is None:
                dropped += 1
                continue
            if aid in readded and row_id in present:
                continue
            present.add(row_id)
            items.append(it if row_id == aid else dataclasses.replace(it, activity_id=row_id))
        if dropped:
            _log.warning("project id=%s: left out %d activity items naming rows the "
                         "trip does not hold", project_id, dropped)
        project.items = items
        project.activities = activities
        project.rebuild_map()

    def _upsert_activity(
        self, sess: Session, user_info_id: int, act: Activity
    ) -> None:
        """Insert the activity row under its own id if it doesn't exist; skip
        if it does.

        Kept for the ``.traxj`` import (``repo_transfer``), which remaps ids
        another account holds before calling this, until #592's U6 gives every
        file activity a fresh row. Every other writer goes through
        ``_save_trip_activities``.

        Enriched data (summary_polyline, elevation_profile) is only written
        for new rows — existing rows may already have richer data from a
        previous enrichment pass. A row owned by another account is never
        written.
        """
        if act.id is None:
            return
        existing = sess.get(DBActivity, act.id)
        if existing is not None and existing.user_info_id != user_info_id:
            return
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

        row = self._new_activity_row(act, act.id, user_info_id)
        row.strava_id = strava_identity(act)
        row.strava_user_id = user_info_id if row.strava_id is not None else None
        sess.add(row)
        store_prepared_geometry(sess, row)

    @staticmethod
    def _new_activity_row(act: Activity, row_id: int, owner_id: int) -> DBActivity:
        """A new row *row_id*, owned by *owner_id*, holding *act*'s content."""
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
        # An activity the client encrypted (issue #29) carries its endpoints and
        # elevation as ciphertext envelopes in the *_enc fields, which only an
        # import supplies (issue #466): stored as the client's encryption
        # migration stores them, the low-res copy holding the same envelope as
        # the full profile (encryption_migration.dart). A readable value wins.
        _low_res_json = _low_res_ep_json(_ep_json)
        if _ep_json is None and act.elevation_profile_enc:
            _ep_json = _low_res_json = act.elevation_profile_enc

        return DBActivity(
            id=row_id,
            user_info_id=owner_id,
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
            has_kudoed=act.has_kudoed,
            average_speed=act.average_speed,
            max_speed=act.max_speed,
            gear_id=act.gear_id,
            elev_high=act.elev_high,
            elev_low=act.elev_low,
            start_latlng_json=(json.dumps(act.start_latlng) if act.start_latlng
                               else act.start_latlng_enc or None),
            end_latlng_json=(json.dumps(act.end_latlng) if act.end_latlng
                             else act.end_latlng_enc or None),
            summary_polyline=act.summary_polyline,
            elevation_profile_json=_ep_json,
            elevation_profile_low_res_json=_low_res_json,
            source=act.source,
            source_id=act.source_id,
        )

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

        act = Activity(
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
            pr_count=row.pr_count,
            total_photo_count=row.total_photo_count,
            has_kudoed=row.has_kudoed,
            gear_id=row.gear_id,
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
            # Never deferred, so the light path reads it with no extra query.
            has_gain_snapshot=getattr(row, "original_total_elevation_gain", None) is not None,
            strava_id=getattr(row, "strava_id", None),
            split_root_id=getattr(row, "split_root_id", None),
            split_base_name=getattr(row, "split_base_name", None),
        )
        if include_heavy:
            # The edit snapshot, which only a .traxj export carries (#592, plan
            # Decision 8) and an export loads heavy: the light path skips
            # parsing it. Each JSON column splits into its parsed field or its
            # envelope, as the live columns do above.
            act.original_polyline = getattr(row, "original_polyline", None)
            if include_elevation:
                act.original_elevation_profile, act.original_elevation_profile_enc = (
                    _parsed_or_envelope(
                        getattr(row, "original_elevation_profile_json", None), _ep_tuple))
            act.original_start_latlng, act.original_start_latlng_enc = (
                _parsed_or_envelope(getattr(row, "original_start_latlng_json", None), json.loads))
            act.original_end_latlng, act.original_end_latlng_enc = (
                _parsed_or_envelope(getattr(row, "original_end_latlng_json", None), json.loads))
            act.original_total_elevation_gain = getattr(row, "original_total_elevation_gain", None)
            for name in _SCALAR_SNAPSHOT:
                if not name.endswith("_json"):
                    setattr(act, f"original_{name}", getattr(row, f"original_{name}", None))
        return act

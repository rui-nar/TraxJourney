"""remove what encrypted accounts still kept in plaintext; scalar edit snapshots

Part of docs/E2EE_REMNANTS_PLAN.md (unit U4). Four changes:

* ``stravacache``: rows of users with ``encryption_enabled`` are deleted. The
  blob is Strava's raw activity list — names and tracks in plaintext — and
  such an account's list is now kept in the API process only
  (``api/strava.py``, decision 8). The cache is disposable: the next request
  refetches it.
* ``activity.split_base_name`` is nulled where ``name`` is a client-side E2EE
  envelope (decision 10). The base name was captured in plaintext at the
  first split and never encrypted with the name; renumbering already ignores
  it for an enveloped root, so nothing reads it. Checked in Python with the
  app's envelope test, as b7f1a3c9d204 does, after a ``LIKE 'v1.%'``
  prefilter.
* ``project.low_res_geo_json`` is dropped (decision 9). Every save wrote a
  straight-line GeoJSON of the trip into it, start and end points included,
  and nothing has read it since the low-res endpoint started computing its
  response live. Batch mode, as a442d0e1f2b3 drops columns.
* Eight nullable ``activity.original_*`` columns snapshot the scalars an
  activity had before its first edit (decision 7), so a reset can restore
  them without recomputing from the original track — which it cannot do
  once that track is an envelope. They are backfilled for edited rows whose
  original polyline is plaintext, with exactly the values
  ``reset_activity_track`` would restore today: metrics recomputed from the
  original geometry, times scaled by the restored ÷ edited distance ratio.
  Rows with a NULL or enveloped original stay NULL — there is nothing the
  server could compute them from. Each row is read on its own: this runs at
  API startup and a profile can be megabytes.

No lock_version bump: neither ``split_base_name``, ``low_res_geo_json`` nor
the snapshot columns are in any payload a client caches.

Idempotent on the data side. Downgrade re-adds ``low_res_geo_json`` empty
(the next save of each trip used to fill it; no reader needs it) and drops
the eight columns; deleted cache rows and base names are not restored.

Revision ID: c4e2a9f1b7d3
Revises: 87200bcb9342
Create Date: 2026-10-04

"""
import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c4e2a9f1b7d3'
down_revision: Union[str, Sequence[str], None] = '87200bcb9342'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

_SNAPSHOT_COLUMNS = (
    ('original_distance', sa.Float()),
    ('original_moving_time', sa.Integer()),
    ('original_elapsed_time', sa.Integer()),
    ('original_average_speed', sa.Float()),
    ('original_elev_high', sa.Float()),
    ('original_elev_low', sa.Float()),
    ('original_start_latlng_json', sa.String()),
    ('original_end_latlng_json', sa.String()),
)


def upgrade() -> None:
    _delete_encrypted_users_strava_cache()
    _null_enveloped_split_base_names()
    with op.batch_alter_table('project') as batch_op:
        batch_op.drop_column('low_res_geo_json')
    for name, type_ in _SNAPSHOT_COLUMNS:
        op.add_column('activity', sa.Column(name, type_, nullable=True))
    _backfill_scalar_snapshots()


def _delete_encrypted_users_strava_cache() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM stravacache WHERE user_info_id IN "
        "(SELECT id FROM userinfo WHERE encryption_enabled IS TRUE)"
    ))


def _null_enveloped_split_base_names() -> None:
    from src.utils.encryption_check import is_encrypted_envelope

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, name FROM activity "
        "WHERE split_base_name IS NOT NULL AND name LIKE 'v1.%'"
    )).fetchall()
    for row_id, name in rows:
        if is_encrypted_envelope(name):
            bind.execute(
                sa.text("UPDATE activity SET split_base_name = NULL WHERE id = :id"),
                {"id": row_id},
            )


def _backfill_scalar_snapshots() -> None:
    """Snapshot what ``reset_activity_track`` would restore for every edited
    row whose original geometry is plaintext.

    Mirrors ``src/project/repo_activities.py`` ``reset_activity_track`` line
    for line, and imports the same app functions rather than reimplementing
    them, so the snapshot cannot disagree with the reset it replaces. Where
    the reset would leave a scalar as it is (no usable original points, or a
    zero distance on either side), the snapshot is that current value too.
    """
    from src.models.track_edit import align_points, recompute_track_metrics
    from src.utils.encryption_check import is_encrypted_envelope

    bind = op.get_bind()
    ids = [r[0] for r in bind.execute(sa.text(
        "SELECT id FROM activity WHERE is_edited IS TRUE "
        "AND original_polyline IS NOT NULL ORDER BY id"
    ))]
    filled = encrypted = unusable = 0
    # One row at a time: this runs at startup and a profile can be megabytes.
    for row_id in ids:
        row = bind.execute(sa.text(
            "SELECT distance, moving_time, elapsed_time, average_speed, "
            "elev_high, elev_low, start_latlng_json, end_latlng_json, "
            "original_polyline, original_elevation_profile_json "
            "FROM activity WHERE id = :id"
        ), {"id": row_id}).mappings().one()
        orig_poly = row["original_polyline"]
        orig_ep_json = row["original_elevation_profile_json"]
        if is_encrypted_envelope(orig_poly) or is_encrypted_envelope(orig_ep_json):
            encrypted += 1
            continue
        try:
            snapshot = _what_reset_restores(
                row, orig_poly, orig_ep_json, align_points, recompute_track_metrics)
        except (ValueError, TypeError, AttributeError, IndexError):
            # A snapshot the reset itself would fail on: nothing to compute.
            unusable += 1
            continue
        bind.execute(sa.text(
            "UPDATE activity SET original_distance = :distance, "
            "original_moving_time = :moving_time, "
            "original_elapsed_time = :elapsed_time, "
            "original_average_speed = :average_speed, "
            "original_elev_high = :elev_high, original_elev_low = :elev_low, "
            "original_start_latlng_json = :start_latlng_json, "
            "original_end_latlng_json = :end_latlng_json "
            "WHERE id = :id"
        ), dict(snapshot, id=row_id))
        filled += 1
        del row, orig_poly, orig_ep_json
    _log.info(
        "scalar edit snapshots: %d backfilled, %d left empty (encrypted "
        "originals), %d left empty (unreadable originals)",
        filled, encrypted, unusable,
    )


def _what_reset_restores(row, orig_poly, orig_ep_json,
                         align_points, recompute_track_metrics) -> dict:
    out = {
        "distance": row["distance"],
        "moving_time": row["moving_time"],
        "elapsed_time": row["elapsed_time"],
        "average_speed": row["average_speed"],
        "elev_high": row["elev_high"],
        "elev_low": row["elev_low"],
        "start_latlng_json": row["start_latlng_json"],
        "end_latlng_json": row["end_latlng_json"],
    }
    orig_ep = None
    if orig_ep_json:
        ep = json.loads(orig_ep_json)
        orig_ep = (ep.get("distances_km") or [], ep.get("elevations_m") or [])
    points = align_points(orig_poly, orig_ep)
    if not points:
        return out
    edited_distance = row["distance"] or 0.0
    metrics = recompute_track_metrics(points)
    out["distance"] = metrics.distance
    out["elev_high"] = metrics.elev_high
    out["elev_low"] = metrics.elev_low
    out["start_latlng_json"] = (
        json.dumps(metrics.start_latlng) if metrics.start_latlng else None)
    out["end_latlng_json"] = (
        json.dumps(metrics.end_latlng) if metrics.end_latlng else None)
    if edited_distance > 0 and metrics.distance > 0:
        ratio = metrics.distance / edited_distance
        out["moving_time"] = int(round((row["moving_time"] or 0) * ratio))
        out["elapsed_time"] = int(round((row["elapsed_time"] or 0) * ratio))
        out["average_speed"] = (
            metrics.distance / out["moving_time"] if out["moving_time"] > 0 else 0.0)
    return out


def downgrade() -> None:
    with op.batch_alter_table('activity') as batch_op:
        for name, _ in reversed(_SNAPSHOT_COLUMNS):
            batch_op.drop_column(name)
    op.add_column('project', sa.Column('low_res_geo_json', sa.String(), nullable=True))

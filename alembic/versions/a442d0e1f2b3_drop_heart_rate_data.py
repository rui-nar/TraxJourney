"""drop heart-rate data (issue #442)

Strava imports copied each activity's average_heartrate / max_heartrate and
Strava's has_heartrate / heartrate_opt_out / display_hide_heartrate_option
flags into ``activity``, and the per-user ``stravacache`` row kept the raw
Strava list whole, heart rate included. No feature ever read any of it, and
heart rate is health data under GDPR — a special category we have no basis to
hold. The model no longer has the fields, so this migration removes what was
already stored:

* ``stravacache.activities_json``: every key containing "heartrate" is removed
  from each cached activity, the rest of the blob is kept as is. A row whose
  blob does not parse is deleted instead — nothing can be scrubbed from it,
  and the cache is disposable (TTL-bound, refetched on demand). The user ids
  are selected first and each blob is read and rewritten on its own, as
  6c1f0e9a2b47 does: this runs at API startup, and a blob is a user's whole
  Strava history.
* ``project``: every trip holding an activity with a heart-rate value has its
  lock_version advanced, in SQL, before the columns go. A native client
  caches a trip's detail JSON until that number moves (issue #173), so this
  is what makes it refetch a copy that still carries the values.
* ``activity``: the five columns are dropped. Batch mode, because SQLite
  rebuilds the table for that; it is what every earlier column drop on this
  table already uses (see e7f8a9b0c1d2, a3f7c1e9b204).

Downgrade re-adds the columns empty (NULL / false) — the values are gone by
design. The booleans get a server default of false, which the original DDL
did not have, so they can be added to a populated table.

Revision ID: a442d0e1f2b3
Revises: b8c4e2f19a37
Create Date: 2026-09-23

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a442d0e1f2b3'
down_revision: Union[str, Sequence[str], None] = 'b8c4e2f19a37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _scrub_strava_cache()
    _tell_the_trips()
    with op.batch_alter_table('activity') as batch_op:
        batch_op.drop_column('has_heartrate')
        batch_op.drop_column('heartrate_opt_out')
        batch_op.drop_column('display_hide_heartrate_option')
        batch_op.drop_column('average_heartrate')
        batch_op.drop_column('max_heartrate')


def _scrub_strava_cache() -> None:
    bind = op.get_bind()
    user_ids = [r[0] for r in bind.execute(sa.text(
        "SELECT user_info_id FROM stravacache ORDER BY user_info_id"))]
    # One row at a time: this runs at startup, and a blob is a whole history.
    for user_info_id in user_ids:
        blob = bind.execute(
            sa.text("SELECT activities_json FROM stravacache WHERE user_info_id = :id"),
            {"id": user_info_id},
        ).scalar()
        try:
            activities = json.loads(blob)
            cleaned = [
                {k: v for k, v in a.items() if "heartrate" not in k}
                for a in activities
            ]
        except (TypeError, ValueError, AttributeError):
            bind.execute(
                sa.text("DELETE FROM stravacache WHERE user_info_id = :id"),
                {"id": user_info_id},
            )
            continue
        bind.execute(
            sa.text(
                "UPDATE stravacache SET activities_json = :v "
                "WHERE user_info_id = :id"
            ),
            {"v": json.dumps(cleaned), "id": user_info_id},
        )
        del blob, activities, cleaned


def _tell_the_trips() -> None:
    """Advance the lock_version of every trip holding an activity with a
    heart-rate value, so a client's on-device copy of it is refetched (issue
    #173) — the same bump 6c1f0e9a2b47 makes, done in SQL."""
    op.get_bind().execute(sa.text(
        "UPDATE project SET lock_version = lock_version + 1 "
        "WHERE id IN (SELECT DISTINCT project_id FROM projectitem "
        "WHERE activity_id IN (SELECT id FROM activity "
        "WHERE average_heartrate IS NOT NULL OR max_heartrate IS NOT NULL "
        "OR has_heartrate IS TRUE OR heartrate_opt_out IS TRUE "
        "OR display_hide_heartrate_option IS TRUE))"
    ))


def downgrade() -> None:
    op.add_column('activity', sa.Column(
        'max_heartrate', sa.Integer(), nullable=True))
    op.add_column('activity', sa.Column(
        'average_heartrate', sa.Float(), nullable=True))
    op.add_column('activity', sa.Column(
        'display_hide_heartrate_option', sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.add_column('activity', sa.Column(
        'heartrate_opt_out', sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.add_column('activity', sa.Column(
        'has_heartrate', sa.Boolean(), nullable=False,
        server_default=sa.false()))

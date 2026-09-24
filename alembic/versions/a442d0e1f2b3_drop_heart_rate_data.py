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
  and the cache is disposable (TTL-bound, refetched on demand).
* ``activity``: the five columns are dropped. Batch mode, because SQLite
  rebuilds the table for that; it is what every earlier column drop on this
  table already uses (see e7f8a9b0c1d2, a3f7c1e9b204).

Downgrade re-adds the columns empty (NULL / false) — the values are gone by
design. The booleans get a server default of false, which the original DDL
did not have, so they can be added to a populated table.

Revision ID: a442d0e1f2b3
Revises: 04a606ace483
Create Date: 2026-09-23

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a442d0e1f2b3'
down_revision: Union[str, Sequence[str], None] = '04a606ace483'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _scrub_strava_cache()
    with op.batch_alter_table('activity') as batch_op:
        batch_op.drop_column('has_heartrate')
        batch_op.drop_column('heartrate_opt_out')
        batch_op.drop_column('display_hide_heartrate_option')
        batch_op.drop_column('average_heartrate')
        batch_op.drop_column('max_heartrate')


def _scrub_strava_cache() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT user_info_id, activities_json FROM stravacache"
    )).fetchall()
    for user_info_id, blob in rows:
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

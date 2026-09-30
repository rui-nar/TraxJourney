"""add kind to video job (trip video preview, issue #519)

A short free preview is a ``videojob`` row of ``kind = 'preview'`` (see
docs/VIDEO_PREVIEW_PLAN.md D2): it reuses the job lifecycle, paths and sweeps
of a full render. Previews never count toward the monthly video quota, which
now filters ``kind = 'video'``; they count only toward the hourly preview rate
limit. Every row that exists before this migration is a full video, so the
column is added with a server default of ``'video'`` and existing rows are
backfilled to it.

Revision ID: c519a0b1d2e3
Revises: a442d0e1f2b3
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c519a0b1d2e3'
down_revision: Union[str, Sequence[str], None] = 'a442d0e1f2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'videojob',
        sa.Column('kind', sa.String(), nullable=False,
                  server_default=sa.text("'video'")),
    )  # the server default is the backfill: existing rows read 'video'


def downgrade() -> None:
    """Downgrade schema."""
    # Batch mode: SQLite rebuilds the table to drop a column.
    with op.batch_alter_table('videojob') as batch_op:
        batch_op.drop_column('kind')

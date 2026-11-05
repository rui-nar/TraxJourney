"""add the original's size to photo upgrade album photos (#33)

``photoupgradeasset`` gains ``original_width`` and ``original_height``: the
album photo's dimensions from Immich's EXIF, as stored (not turned by its
orientation), or NULL when Immich gave none. ``width``/``height`` keep the
fingerprinted thumbnail's upright size.

Nothing existing is migrated. The downgrade drops both columns.

Revision ID: b9d3e6f1a274
Revises: f4a8c2d61e37
Create Date: 2026-11-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9d3e6f1a274'
down_revision: Union[str, Sequence[str], None] = 'f4a8c2d61e37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('photoupgradeasset', sa.Column('original_width', sa.Integer(), nullable=True))
    op.add_column('photoupgradeasset', sa.Column('original_height', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('photoupgradeasset', schema=None) as batch_op:
        batch_op.drop_column('original_height')
        batch_op.drop_column('original_width')

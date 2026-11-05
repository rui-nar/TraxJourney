"""carry rejected pairs on photo upgrade scans (#33, U9-R1-1)

``photoupgradescan`` gains ``rejected_pairs_json``: every (photo key, asset
id) pair the person rejected on the trip so far, as a JSON list. A new scan
copies the union of its predecessors' pairs before it deletes them, so a
rejection outlives the scan it was made in. Existing scans get ``[]``.

The downgrade drops the column.

Revision ID: c5a1e8d27f40
Revises: b9d3e6f1a274
Create Date: 2026-11-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5a1e8d27f40'
down_revision: Union[str, Sequence[str], None] = 'b9d3e6f1a274'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('photoupgradescan', sa.Column(
        'rejected_pairs_json', sa.String(), nullable=False, server_default='[]'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('photoupgradescan', schema=None) as batch_op:
        batch_op.drop_column('rejected_pairs_json')

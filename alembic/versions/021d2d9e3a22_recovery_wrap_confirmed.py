"""mark whether a recovery key was confirmed as saved (Decision 16, #418)

A recovery key is shown once at setup. If the session ends while the enable
request is in flight, or the user leaves the setup screen, the server has the
wrap but the user never saw or saved the key. ``recovery_wrap.confirmed``
records whether the user confirmed saving it; an unconfirmed ``recovery_key``
wrap can be replaced after the next sign-in, a confirmed one cannot.

Existing rows become confirmed through the server default, so no existing user
is asked to replace a key.

Revision ID: 021d2d9e3a22
Revises: 87200bcb9342
Create Date: 2026-10-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '021d2d9e3a22'
down_revision: Union[str, Sequence[str], None] = '87200bcb9342'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('recovery_wrap', sa.Column(
        'confirmed', sa.Boolean(), nullable=False,
        server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table('recovery_wrap') as batch_op:
        batch_op.drop_column('confirmed')

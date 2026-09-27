"""refund ledger: when the cancel was attempted; a row version (issue #441)

Review round 5 of #441 (docs/reviews/feat-441-refund-withdrawal-vat.md):
* ``cancel_attempted_at`` (R5-1) — the ledger row is now created, with the
  frozen request time, *before* the cancellation, and a retry after a failed
  cancel cancels again, possibly days later. This records when the
  cancellation was last attempted: the 10-minute late-landing bound runs from
  there, not from the request.
* ``version`` (R5-5) — bumped by every write, so the admin settle fences on
  "nothing changed since I read it". The claim token alone is "" both before
  and after a claim.

Existing rows: ``cancel_attempted_at`` = ``requested_at`` (their cancellation
followed the request), ``version`` = 0.

Revision ID: c69914246e8e
Revises: ca17b22c22d5
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c69914246e8e'
down_revision: Union[str, Sequence[str], None] = 'ca17b22c22d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscription_refund') as batch_op:
        batch_op.add_column(sa.Column('cancel_attempted_at', sa.Float(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('version', sa.Integer(),
                                      nullable=False, server_default='0'))
    op.execute("UPDATE subscription_refund SET cancel_attempted_at = requested_at")


def downgrade() -> None:
    with op.batch_alter_table('subscription_refund') as batch_op:
        batch_op.drop_column('version')
        batch_op.drop_column('cancel_attempted_at')

"""subscription_refund.failed_refund_id: a failure already recorded (issue #441)

Review finding R6-1 of #441 (docs/reviews/feat-441-refund-withdrawal-vat.md):
Stripe reports one failed refund in several events (``refund.failed``,
``refund.updated``, ``charge.refund.updated``). The first records it as owed
and clears ``refund_id``; the id is kept here, so the later ones are answered
"already recorded" instead of logging a refund that is not ours — which told
the owner to refund it by hand a second time.

Existing rows: empty. A failure recorded before this migration has lost its
refund id, so a later event for it still logs the no-match ERROR.

Revision ID: 26b1b2cd05bf
Revises: c69914246e8e
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = '26b1b2cd05bf'
down_revision: Union[str, Sequence[str], None] = 'c69914246e8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscription_refund') as batch_op:
        batch_op.add_column(sa.Column('failed_refund_id',
                                      sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default=''))
        batch_op.create_index(batch_op.f('ix_subscription_refund_failed_refund_id'),
                              ['failed_refund_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('subscription_refund') as batch_op:
        batch_op.drop_index(batch_op.f('ix_subscription_refund_failed_refund_id'))
        batch_op.drop_column('failed_refund_id')

"""refund ledger: remember the refund a credit note made (issue #441)

A refund can fail after it was created — Stripe then sends ``refund.failed``
(and ``refund.updated`` / ``charge.refund.updated``). ``refund_id`` matches
such an event to the ledger row, which then records the refund as owed
instead of done.

Existing rows keep "": their credit notes were made before this was stored,
and a late failure of one is found in the Stripe dashboard.

Revision ID: ca17b22c22d5
Revises: ed0f801e164c
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = 'ca17b22c22d5'
down_revision: Union[str, Sequence[str], None] = 'ed0f801e164c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscription_refund') as batch_op:
        batch_op.add_column(sa.Column('refund_id', sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default=''))
        batch_op.create_index(batch_op.f('ix_subscription_refund_refund_id'),
                              ['refund_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('subscription_refund') as batch_op:
        batch_op.drop_index(batch_op.f('ix_subscription_refund_refund_id'))
        batch_op.drop_column('refund_id')

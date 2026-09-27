"""refund ledger; drop withdrawal_requested_at (issue #441, review round 2)

``subscription_refund`` holds one row per subscription whose unused period is
being, or has been, refunded. It is claimed under the account's lock before
Stripe is asked, which is what stops a withdrawal and a deletion (or two taps)
from both refunding. A row whose refund Stripe refused for good records the
refund as owed, and survives the account's deletion: there is deliberately no
foreign key to ``userinfo``.

``subscription.withdrawal_requested_at`` goes: it was written before the
cancellation, so a cancellation that then failed left the window open for the
life of the subscription. A request now counts as made in time only once its
cancellation has landed — which is when its ledger row is created.

Revision ID: e3bb990551f8
Revises: 3828d92db32c
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = 'e3bb990551f8'
down_revision: Union[str, Sequence[str], None] = '3828d92db32c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    text = sqlmodel.sql.sqltypes.AutoString()
    op.create_table(
        'subscription_refund',
        sa.Column('subscription_id', text, nullable=False),
        sa.Column('customer_id', text, nullable=False),
        sa.Column('state', text, nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('refunded', sa.Integer(), nullable=False),
        sa.Column('currency', text, nullable=False),
        sa.Column('invoice_id', text, nullable=False),
        sa.Column('credit_note_id', text, nullable=False),
        sa.Column('reason', text, nullable=False),
        sa.Column('requested_at', sa.Float(), nullable=False),
        sa.Column('lease_until', sa.Float(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('subscription_id'),
    )
    op.create_index(op.f('ix_subscription_refund_customer_id'),
                    'subscription_refund', ['customer_id'], unique=False)
    with op.batch_alter_table('subscription') as batch_op:
        batch_op.drop_column('withdrawal_requested_at')


def downgrade() -> None:
    with op.batch_alter_table('subscription') as batch_op:
        batch_op.add_column(sa.Column(
            'withdrawal_requested_at', sa.Float(), nullable=False, server_default='0'))
    op.drop_index(op.f('ix_subscription_refund_customer_id'),
                  table_name='subscription_refund')
    op.drop_table('subscription_refund')

"""withdrawal window per contract; record withdrawal requests (issue #441)

Owner decision of 2026-09-26: each new subscription started after the previous
one ended opens its own 14-day window. Renewals and plan changes do not. So:

* ``initial_paid_at`` (the first purchase ever) becomes ``contract_started_at``
  (the start of the current contract), with ``contract_subscription_id`` naming
  the subscription it belongs to.
* ``withdrawal_requested_at`` records when a withdrawal was first asked for, so
  a withdrawal asked for inside the window can be completed after it.
* ``withdrawn_subscription_id`` ties a completed withdrawal to its subscription.

Backfill, replacing the one in 2d5c660f9f6e:
* a row whose subscription is still in force — ``active``, ``trialing``,
  ``past_due``, ``unpaid`` or ``paused`` — is that subscription's contract,
  started before tracking (1.0): its window is closed, and its renewals cannot
  open one;
* every other row — no subscription, or one that ended, or one that was never
  paid (``incomplete``, ``incomplete_expired``) — has no contract (0), so the
  next subscription it starts opens a window as it should.

Revision ID: 3828d92db32c
Revises: 2d5c660f9f6e
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = '3828d92db32c'
down_revision: Union[str, Sequence[str], None] = '2d5c660f9f6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Statuses of a subscription that is still in force: it may renew or bill.
IN_FORCE = ('active', 'trialing', 'past_due', 'unpaid', 'paused')
STARTED_BEFORE_TRACKING = 1.0


def upgrade() -> None:
    with op.batch_alter_table('subscription') as batch_op:
        batch_op.alter_column('initial_paid_at', new_column_name='contract_started_at',
                              existing_type=sa.Float(), existing_nullable=False,
                              existing_server_default='0')
        batch_op.add_column(sa.Column(
            'contract_subscription_id', sqlmodel.sql.sqltypes.AutoString(),
            nullable=False, server_default=''))
        batch_op.add_column(sa.Column(
            'withdrawal_requested_at', sa.Float(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column(
            'withdrawn_subscription_id', sqlmodel.sql.sqltypes.AutoString(),
            nullable=False, server_default=''))

    in_force = sa.text(
        "UPDATE subscription SET contract_started_at = :before, "
        "contract_subscription_id = provider_subscription_id "
        "WHERE COALESCE(provider_subscription_id, '') != '' "
        "AND status IN :statuses"
    ).bindparams(sa.bindparam('statuses', expanding=True))
    op.execute(in_force.bindparams(before=STARTED_BEFORE_TRACKING,
                                   statuses=list(IN_FORCE)))
    no_contract = sa.text(
        "UPDATE subscription SET contract_started_at = 0, "
        "contract_subscription_id = '' "
        "WHERE COALESCE(provider_subscription_id, '') = '' "
        "OR status NOT IN :statuses"
    ).bindparams(sa.bindparam('statuses', expanding=True))
    op.execute(no_contract.bindparams(statuses=list(IN_FORCE)))


def downgrade() -> None:
    with op.batch_alter_table('subscription') as batch_op:
        batch_op.drop_column('withdrawn_subscription_id')
        batch_op.drop_column('withdrawal_requested_at')
        batch_op.drop_column('contract_subscription_id')
        batch_op.alter_column('contract_started_at', new_column_name='initial_paid_at',
                              existing_type=sa.Float(), existing_nullable=False,
                              existing_server_default='0')
    # Back to "first purchase ever": any account that ever named a
    # subscription has had it, so its window is closed (2d5c660f9f6e's rule).
    op.execute(
        "UPDATE subscription SET initial_paid_at = 1.0 "
        "WHERE COALESCE(provider_subscription_id, '') != '' AND initial_paid_at = 0"
    )

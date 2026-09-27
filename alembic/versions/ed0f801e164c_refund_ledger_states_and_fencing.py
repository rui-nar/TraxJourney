"""refund ledger: owed is final, settled tombstones, fencing (issue #441)

Review round 3 of #441:
* ``failed_permanent`` becomes ``owed``, final for the app; the owner's
  settlement is ``settled``, a tombstone rather than a delete, so a settled
  subscription can never be refunded again.
* The split of the amount is frozen (``to_refund`` to the card, ``owed`` by
  hand), so a replay sends identical parameters.
* ``claim_token`` fences writes to the holder of the current claim.
* ``contract_started_at`` lets a refund of an earlier contract be completed.
* ``subscription.contract_checked_subscription_id``: each subscription is
  judged for a contract once, on its first paid event. Subscriptions in force
  have had theirs, so it is backfilled with the tracked one for those.
* Pending rows get the contract start of the subscription they refund.
* Downgrade: ``owed`` goes back to ``failed_permanent``, and ``settled`` to
  ``done`` — the state the older code never sends to Stripe again.
* ``subscription.withdrawn_at`` / ``withdrawn_subscription_id`` go: nothing
  read them once the ledger drove the withdrawal state.

Revision ID: ed0f801e164c
Revises: e3bb990551f8
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = 'ed0f801e164c'
down_revision: Union[str, Sequence[str], None] = 'e3bb990551f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    text = sqlmodel.sql.sqltypes.AutoString()
    with op.batch_alter_table('subscription_refund') as batch_op:
        batch_op.add_column(sa.Column('contract_started_at', sa.Float(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('to_refund', sa.Integer(),
                                      nullable=False, server_default='-1'))
        batch_op.add_column(sa.Column('owed', sa.Integer(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('claim_token', text,
                                      nullable=False, server_default=''))
        batch_op.add_column(sa.Column('settled_at', sa.Float(),
                                      nullable=False, server_default='0'))
    op.execute(
        "UPDATE subscription_refund SET state = 'owed', "
        "owed = CASE WHEN amount > refunded THEN amount - refunded ELSE 0 END "
        "WHERE state = 'failed_permanent'"
    )
    op.execute("UPDATE subscription_refund SET to_refund = refunded "
               "WHERE state IN ('done', 'owed')")
    # A pending refund to complete needs the start of the contract it belongs
    # to: the tracked contract's, when it is that subscription.
    op.execute(
        "UPDATE subscription_refund SET contract_started_at = ("
        " SELECT s.contract_started_at FROM subscription s"
        " WHERE s.contract_subscription_id = subscription_refund.subscription_id)"
        " WHERE state = 'pending' AND EXISTS ("
        " SELECT 1 FROM subscription s"
        " WHERE s.contract_subscription_id = subscription_refund.subscription_id)"
    )

    with op.batch_alter_table('subscription') as batch_op:
        batch_op.add_column(sa.Column('contract_checked_subscription_id', text,
                                      nullable=False, server_default=''))
        batch_op.drop_column('withdrawn_subscription_id')
        batch_op.drop_column('withdrawn_at')
    # Judged already: only a subscription in force when this ships has had
    # its first paid event. One still incomplete has not, and must be judged
    # when it is paid.
    op.execute(
        sa.text("UPDATE subscription SET contract_checked_subscription_id = "
                "provider_subscription_id WHERE status IN :statuses")
        .bindparams(sa.bindparam('statuses', expanding=True))
        .bindparams(statuses=['active', 'trialing', 'past_due', 'unpaid', 'paused'])
    )


def downgrade() -> None:
    text = sqlmodel.sql.sqltypes.AutoString()
    with op.batch_alter_table('subscription') as batch_op:
        batch_op.add_column(sa.Column('withdrawn_at', sa.Float(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('withdrawn_subscription_id', text,
                                      nullable=False, server_default=''))
        batch_op.drop_column('contract_checked_subscription_id')
    # The older code retries failed_permanent and treats done as final, so a
    # settled refund — the owner paid it by hand — goes back as done, never
    # as something that code would send to Stripe again.
    op.execute("UPDATE subscription_refund SET state = 'failed_permanent' "
               "WHERE state = 'owed'")
    op.execute("UPDATE subscription_refund SET state = 'done' "
               "WHERE state = 'settled'")
    with op.batch_alter_table('subscription_refund') as batch_op:
        batch_op.drop_column('settled_at')
        batch_op.drop_column('claim_token')
        batch_op.drop_column('owed')
        batch_op.drop_column('to_refund')
        batch_op.drop_column('contract_started_at')

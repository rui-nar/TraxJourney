"""add withdrawal fields to subscription (issue #441)

The 14-day withdrawal window runs from the account's first paid subscription,
so that instant has to be stored; renewals and plan changes must not move it.
Alongside it: proof of the consent collected at checkout (when, and which
wording), and when the user last withdrew.

Backfill: the purchase date of an account that already reached Stripe is not
known here, and cannot be fetched offline. Every such row (one that names a
Stripe subscription) gets ``initial_paid_at = 1.0`` — a purchase at the start of
the epoch — so its window is closed rather than reopened by the next webhook.
Rows that never named a subscription keep 0, and their first purchase opens the
window as it should.

Revision ID: 2d5c660f9f6e
Revises: 6abe17b5d61f
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = '2d5c660f9f6e'
down_revision: Union[str, Sequence[str], None] = '6abe17b5d61f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: What the backfill writes for a purchase whose date is unknown. Any value
#: more than 14 days in the past closes the window; this one is recognisable.
PURCHASED_BEFORE_TRACKING = 1.0


def upgrade() -> None:
    op.add_column(
        'subscription',
        sa.Column('initial_paid_at', sa.Float(), nullable=False,
                  server_default='0'),
    )
    op.add_column(
        'subscription',
        sa.Column('terms_accepted_at', sa.Float(), nullable=False,
                  server_default='0'),
    )
    op.add_column(
        'subscription',
        sa.Column('terms_version', sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False, server_default=''),
    )
    op.add_column(
        'subscription',
        sa.Column('withdrawn_at', sa.Float(), nullable=False,
                  server_default='0'),
    )
    op.execute(
        sa.text(
            "UPDATE subscription SET initial_paid_at = :before "
            "WHERE COALESCE(provider_subscription_id, '') != ''"
        ).bindparams(before=PURCHASED_BEFORE_TRACKING)
    )


def downgrade() -> None:
    op.drop_column('subscription', 'withdrawn_at')
    op.drop_column('subscription', 'terms_version')
    op.drop_column('subscription', 'terms_accepted_at')
    op.drop_column('subscription', 'initial_paid_at')

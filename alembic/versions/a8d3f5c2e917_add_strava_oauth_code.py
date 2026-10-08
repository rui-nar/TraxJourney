"""add strava_oauth_code: each Strava code bound to the state it arrived with

The OAuth callback records, for every code it relays, the ``jti`` of the
state Strava returned it with; ``POST /api/strava/complete`` exchanges a code
only together with that state (docs/STRAVA_CONNECT_BINDING_PLAN.md D9). Rows
hold the code's sha256 only and live as long as their state (10 minutes).
Nothing existing is migrated.

Revision ID: a8d3f5c2e917
Revises: c4e2a9f1b7d3
Create Date: 2026-10-08 00:00:00.000000

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = 'a8d3f5c2e917'
down_revision: Union[str, Sequence[str], None] = 'c4e2a9f1b7d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    text = sqlmodel.sql.sqltypes.AutoString()
    op.create_table(
        'strava_oauth_code',
        sa.Column('code_hash', text, nullable=False),
        sa.Column('state_jti', text, nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('code_hash'),
    )


def downgrade() -> None:
    op.drop_table('strava_oauth_code')

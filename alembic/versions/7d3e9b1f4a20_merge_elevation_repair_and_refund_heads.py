"""Merge the two heads left by #462 and #441/#429.

#462's repair migration (6c1f0e9a2b47) and #429's userinfo migration
(6abe17b5d61f, followed by #441's refund-ledger chain up to 26b1b2cd05bf) were
each written on top of 5e2b7c1d9a40 in separate branches, so main had two
heads and `alembic upgrade head`, which the API runs at startup, refused to
run. The two lines touch unrelated tables (activity elevations; userinfo and
the refund ledger), so their order does not matter: this revision only joins
them, and a database at any point on either line upgrades cleanly.

Revision ID: 7d3e9b1f4a20
Revises: 26b1b2cd05bf, 6c1f0e9a2b47
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union


revision: str = '7d3e9b1f4a20'
down_revision: Union[str, Sequence[str], None] = ('26b1b2cd05bf', '6c1f0e9a2b47')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

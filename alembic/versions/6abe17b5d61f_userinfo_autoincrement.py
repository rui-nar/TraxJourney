"""make userinfo ids AUTOINCREMENT, so they are never reused (issue #429)

Without AUTOINCREMENT, SQLite gives a new row ``max(id) + 1`` — so deleting
the newest account hands its id to the next one registered. That id outlives
the row: Stripe carries it in subscription metadata, and a late event for the
deleted account would then name a stranger.

SQLite cannot add AUTOINCREMENT to an existing table, so the table is rebuilt
(batch mode: copy, drop, rename). Rows and indexes are kept, and copying the
rows sets ``sqlite_sequence`` to the current max id, so the next account gets
a fresh one. An id deleted *before* this migration that was above the current
max can still be handed out once; nothing records it any more.

Postgres (and any other backend) already never reuses sequence values, so
this is SQLite-only.

Revision ID: 6abe17b5d61f
Revises: 04a606ace483
Create Date: 2026-09-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = '6abe17b5d61f'
down_revision: Union[str, Sequence[str], None] = '04a606ace483'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rebuild(autoincrement: bool) -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    with op.batch_alter_table(
        "userinfo",
        recreate="always",
        table_kwargs={"sqlite_autoincrement": autoincrement},
    ):
        pass


def upgrade() -> None:
    _rebuild(True)


def downgrade() -> None:
    _rebuild(False)

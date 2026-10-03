"""delete Immich connections and jobs of accounts already deleted

Account deletion used to leave four kinds of rows behind: the Immich
connection (server URL and API key) and the poster, video and route jobs the
account had started. SQLite runs without foreign keys, so nothing removed
them. Deletion now does; this one-off data migration removes the rows earlier
deletions left: every row of those four tables whose ``user_info_id`` names no
``userinfo`` row.

Their files are already gone: they lived under ``data/users/{id}/``, which
deletion removed at the time. A job another user started on a deleted
account's trip names that other user, so it is not touched. Idempotent: a
no-op once clean.

Revision ID: b7e4d2c9a1f0
Revises: c519a0b1d2e3
Create Date: 2026-10-03

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7e4d2c9a1f0'
down_revision: Union[str, Sequence[str], None] = 'c519a0b1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("immichtoken", "posterjob", "videojob", "routejob")


def upgrade() -> None:
    """Delete the rows whose account no longer exists."""
    for table in _TABLES:
        op.execute(
            f"""
            DELETE FROM {table}
            WHERE NOT EXISTS (
                SELECT 1 FROM userinfo WHERE userinfo.id = {table}.user_info_id
            )
            """
        )


def downgrade() -> None:
    """No-op: the deleted rows belonged to accounts that no longer exist, and
    restoring them would bring back a deleted account's API key."""

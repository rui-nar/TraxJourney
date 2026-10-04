"""show "Traveller", not a sign-in address, as stored comment/like authors (issue #507)

``memory_comment.commenter_name`` and ``memory_like.liker_name`` snapshot the
author's display_name when the comment or like is posted, and that snapshot is
what share pages and the memory modal show. Accounts auto-created at sign-in
before #507 copied the username — the email since #110 — into display_name,
so rows written before #507's write-time fix show another user's address.

This rewrites those snapshots to "Traveller" under the rule new rows are
written with (``api/members.py`` ``public_name_given_username``): a name that
is blank, or that equals the author's ``userinfo.email`` or the linked
``localuser.username`` (case-insensitive, after trimming). A real name stays
the name the author had at the time, as the privacy policy describes. A row
whose author no longer exists has no address to compare against, so only a
blank name is rewritten there.

Done in SQL — this runs at API startup. On SQLite the comparison uses a
function registered on the connection that folds exactly as the rule does
(``str.strip().casefold()``); elsewhere ``LOWER(TRIM())``, which agrees for
ASCII and spaces.

No lock_version bump: comment and like names are not in any payload a client
caches against it. The project payload carries only like/comment counts, and
the names are fetched live from the comments/likes endpoints each time a
memory is opened, with no cache headers.

Idempotent. Downgrade is a no-op: the original values are personal data (the
addresses) and are not kept anywhere to restore.

Revision ID: 87200bcb9342
Revises: e3a91c5d7f20
Create Date: 2026-10-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '87200bcb9342'
down_revision: Union[str, Sequence[str], None] = 'e3a91c5d7f20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FALLBACK = "Traveller"


def _fold_sql(bind) -> str:
    """SQL template folding ``{}`` the way the rule compares names."""
    if bind.dialect.name == "sqlite":
        bind.connection.driver_connection.create_function(
            "tj_fold_name", 1, lambda s: (s or "").strip().casefold(),
            deterministic=True)
        return "tj_fold_name({})"
    return "LOWER(TRIM({}))"


def upgrade() -> None:
    bind = op.get_bind()
    fold = _fold_sql(bind)
    for table, column in (("memory_comment", "commenter_name"),
                          ("memory_like", "liker_name")):
        name = fold.format(f"{table}.{column}")
        bind.execute(sa.text(
            f"UPDATE {table} SET {column} = :fallback "
            f"WHERE {column} != :fallback AND ({name} = '' "
            f"OR EXISTS (SELECT 1 FROM userinfo u WHERE u.id = {table}.user_info_id "
            f"AND ({fold.format('u.email')} = {name} "
            f"OR EXISTS (SELECT 1 FROM localuser l WHERE l.id = u.local_auth_id "
            f"AND {fold.format('l.username')} = {name}))))"
        ), {"fallback": _FALLBACK})


def downgrade() -> None:
    """No-op: the replaced values were sign-in addresses — personal data this
    migration exists to stop showing, and not kept anywhere to restore."""

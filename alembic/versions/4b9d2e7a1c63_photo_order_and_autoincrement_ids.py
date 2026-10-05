"""photo order state; dense photos_json; memory/journal ids never reused (#237, #511)

Three steps, on ``memory`` and ``journalentry`` alike
(docs/PHOTOS_ORDER_ORIENTATION_PLAN.md, D3 and D4c):

1. A nullable ``photo_order_json`` column holds the placement state
   ``{"epoch": int, "ranks": {uuid: int}}`` (see ``api/photo_order.py``).
   ``NULL`` reads as ``{"epoch": 0, "ranks": {}}``, so no backfill.
2. ``photos_json`` becomes dense: the positional writer used to leave
   ``null`` placeholders, which every reader already skips. Rows holding a
   falsy entry are rewritten without it, order kept. A value that is not a
   JSON list is left as it is — this migration only removes placeholders.
3. Both tables become AUTOINCREMENT. Without it SQLite gives a new row
   ``max(id) + 1``, so deleting the newest memory handed its id to the next
   one created, and a photo write still in flight for the deleted one landed
   in the stranger. SQLite cannot add AUTOINCREMENT to an existing table, so
   the table is rebuilt (batch mode: copy, drop, rename), exactly as
   ``6abe17b5d61f`` did for ``userinfo`` (#429). Rows and ids are kept, and
   copying the rows sets ``sqlite_sequence`` to the current max id. An id
   deleted *before* this migration that was above the current max can still
   be handed out once; nothing records it any more. Postgres never reuses
   sequence values, so the rebuild is SQLite-only.

The partial unique indexes (``uq_memory_project_polarsteps_step_id``,
``uq_journalentry_project_client_token``) are dropped before each rebuild and
created again after it, so their ``WHERE`` clause never depends on how the
batch copy reflects them.

Downgrade drops the column and rebuilds both tables without AUTOINCREMENT.
The ``null`` compaction is not reversed: a ``null`` carried no data.

Revision ID: 4b9d2e7a1c63
Revises: 87200bcb9342
Create Date: 2026-10-04

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '4b9d2e7a1c63'
down_revision: Union[str, Sequence[str], None] = '87200bcb9342'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# table -> (partial unique index name, its columns, the column it is partial on)
_PARTIAL_UNIQUE = {
    "memory": (
        "uq_memory_project_polarsteps_step_id",
        ["project_id", "polarsteps_step_id"],
        "polarsteps_step_id",
    ),
    "journalentry": (
        "uq_journalentry_project_client_token",
        ["project_id", "client_token"],
        "client_token",
    ),
}


def _alter(table: str, autoincrement: bool, add: bool) -> None:
    """Add (``add``) or drop ``photo_order_json``; on SQLite, rebuild the
    table in the same copy with the given AUTOINCREMENT setting."""
    sqlite = op.get_bind().dialect.name == "sqlite"
    index, columns, partial_on = _PARTIAL_UNIQUE[table]
    if sqlite:
        op.drop_index(index, table_name=table)
    with op.batch_alter_table(
        table,
        recreate="always" if sqlite else "auto",
        table_kwargs={"sqlite_autoincrement": autoincrement},
    ) as batch:
        if add:
            batch.add_column(sa.Column("photo_order_json", sa.String(), nullable=True))
        else:
            batch.drop_column("photo_order_json")
    if sqlite:
        where = sa.text(f"{partial_on} IS NOT NULL")
        op.create_index(index, table, columns, unique=True, sqlite_where=where)


def _compact_photos(table: str) -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"SELECT id, photos_json FROM {table}")).fetchall()
    for row_id, raw in rows:
        try:
            photos = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            continue
        if not isinstance(photos, list) or all(photos):
            continue
        conn.execute(
            sa.text(f"UPDATE {table} SET photos_json = :p WHERE id = :id"),
            {"p": json.dumps([p for p in photos if p]), "id": row_id},
        )


def upgrade() -> None:
    for table in ("memory", "journalentry"):
        _alter(table, autoincrement=True, add=True)
        _compact_photos(table)


def downgrade() -> None:
    for table in ("memory", "journalentry"):
        _alter(table, autoincrement=False, add=False)

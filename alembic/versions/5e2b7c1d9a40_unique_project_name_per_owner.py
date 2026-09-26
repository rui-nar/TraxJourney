"""unique project name per owner (issue #452)

Trips are addressed by (owner, name), yet the name was only ever checked to be
free before an insert, so two racing requests could leave one owner with two
trips of the same name: one of them unreachable by name. The import's "Keep
both" choice picks a free name for a copy and has to rely on the database to
make a racing loser fail rather than shadow the winner.

Before adding the index, any existing duplicates are renamed: per owner and
name, the oldest trip (lowest id) keeps the name and each later one takes the
first free "<name> (n)", n >= 2. Nothing else refers to a trip by name in the
database, so a rename is only a rename. The downgrade drops the index and
leaves the new names alone.

Revision ID: 5e2b7c1d9a40
Revises: 04a606ace483
Create Date: 2026-09-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '5e2b7c1d9a40'
down_revision: Union[str, Sequence[str], None] = '04a606ace483'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = 'uq_project_user_name'


def _rename_duplicates(conn) -> None:
    rows = conn.execute(sa.text(
        "SELECT id, user_info_id, name FROM project ORDER BY user_info_id, id"
    )).fetchall()
    taken: dict[int, set[str]] = {}
    for _pid, uid, name in rows:
        taken.setdefault(uid, set()).add(name)
    seen: dict[int, set[str]] = {}
    for pid, uid, name in rows:
        mine = seen.setdefault(uid, set())
        if name not in mine:
            mine.add(name)
            continue
        n = 2
        while f"{name} ({n})" in taken[uid]:
            n += 1
        new_name = f"{name} ({n})"
        taken[uid].add(new_name)
        mine.add(new_name)
        conn.execute(
            sa.text("UPDATE project SET name = :name WHERE id = :id"),
            {"name": new_name, "id": pid},
        )


def upgrade() -> None:
    _rename_duplicates(op.get_bind())
    op.create_index(_INDEX, 'project', ['user_info_id', 'name'], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name='project')

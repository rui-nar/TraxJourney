"""add Sign in with Apple: userinfo.apple_sub and the appletoken table (#580)

- ``userinfo.apple_sub``: the Apple ``sub`` an account signs in with, indexed
  because every Apple sign-in looks it up. Existing rows get ``""``.
- ``appletoken``: one row per Apple account, holding the refresh token
  (encrypted at rest by the model's ``EncryptedString``) and the
  ``client_id`` it was issued to, which account deletion needs to revoke it.

Nothing existing is migrated. The downgrade drops both; on SQLite the column
is dropped in place, so ``userinfo`` keeps its AUTOINCREMENT (6abe17b5d61f).

Revision ID: e5c3a7b91d24
Revises: b3e9d71c4a58
Create Date: 2026-10-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


revision: str = 'e5c3a7b91d24'
down_revision: Union[str, Sequence[str], None] = 'b3e9d71c4a58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    text = sqlmodel.sql.sqltypes.AutoString()
    op.add_column('userinfo', sa.Column(
        'apple_sub', text, nullable=False, server_default=''))
    op.create_index(op.f('ix_userinfo_apple_sub'), 'userinfo', ['apple_sub'], unique=False)

    op.create_table(
        'appletoken',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_info_id', sa.Integer(), nullable=False),
        sa.Column('refresh_token', text, nullable=False),
        sa.Column('client_id', text, nullable=False),
        sa.ForeignKeyConstraint(['user_info_id'], ['userinfo.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_appletoken_user_info_id'), 'appletoken', ['user_info_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_appletoken_user_info_id'), table_name='appletoken')
    op.drop_table('appletoken')

    op.drop_index(op.f('ix_userinfo_apple_sub'), table_name='userinfo')
    op.drop_column('userinfo', 'apple_sub')

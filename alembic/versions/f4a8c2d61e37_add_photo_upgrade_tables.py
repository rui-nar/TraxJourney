"""add photo upgrade tables: scans, album photos and suggestions (#33)

- ``photoupgradescan``: one row per scan of an Immich album against a trip,
  per person. A partial unique index allows one active scan (``pending`` or
  ``scanning``) per (trip, person) beside any number of finished ones.
- ``photoupgradeasset``: the scan's album photos with their fingerprints.
- ``photoupgradesuggestion``: one row per trip photo, with its position in
  its memory or journal entry.

Nothing existing is migrated. The downgrade drops the three tables.

Revision ID: f4a8c2d61e37
Revises: e5c3a7b91d24
Create Date: 2026-10-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a8c2d61e37'
down_revision: Union[str, Sequence[str], None] = 'e5c3a7b91d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ACTIVE_INDEX = 'uq_photoupgradescan_active'
_ACTIVE_WHERE = "status IN ('pending', 'scanning')"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'photoupgradescan',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('user_info_id', sa.Integer(), nullable=False),
        sa.Column('album_id', sa.String(), nullable=False),
        sa.Column('album_name', sa.String(), nullable=True),
        sa.Column('include_memories', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('include_journal', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('status', sa.String(), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column('stage', sa.String(), nullable=True),
        sa.Column('progress_done', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('progress_total', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('heartbeat_at', sa.Float(), nullable=True),
        sa.Column('apply_status', sa.String(), nullable=False,
                  server_default=sa.text("'idle'")),
        sa.Column('apply_stopped_reason', sa.String(), nullable=True),
        sa.Column('apply_heartbeat_at', sa.Float(), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('completed_at', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.ForeignKeyConstraint(['user_info_id'], ['userinfo.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_photoupgradescan_project_id', 'photoupgradescan',
                    ['project_id'])
    op.create_index('ix_photoupgradescan_user_info_id', 'photoupgradescan',
                    ['user_info_id'])
    op.create_index('ix_photoupgradescan_project_user_created', 'photoupgradescan',
                    ['project_id', 'user_info_id', 'created_at'])
    op.create_index(
        _ACTIVE_INDEX,
        'photoupgradescan',
        ['project_id', 'user_info_id'],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_WHERE),
        postgresql_where=sa.text(_ACTIVE_WHERE),
    )

    op.create_table(
        'photoupgradeasset',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scan_id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.String(), nullable=False),
        sa.Column('local_date', sa.String(), nullable=False),
        sa.Column('lat', sa.Float(), nullable=True),
        sa.Column('lon', sa.Float(), nullable=True),
        sa.Column('mime', sa.String(), nullable=True),
        sa.Column('file_bytes', sa.Integer(), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('ahash', sa.String(), nullable=True),
        sa.Column('dhash', sa.String(), nullable=True),
        sa.Column('fingerprinted', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('fetch_failed', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.ForeignKeyConstraint(['scan_id'], ['photoupgradescan.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_photoupgradeasset_scan_id', 'photoupgradeasset',
                    ['scan_id'])
    op.create_index('ix_photoupgradeasset_scan_local_date', 'photoupgradeasset',
                    ['scan_id', 'local_date'])
    op.create_index('ix_photoupgradeasset_scan_fingerprinted', 'photoupgradeasset',
                    ['scan_id', 'fingerprinted'])

    op.create_table(
        'photoupgradesuggestion',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scan_id', sa.Integer(), nullable=False),
        sa.Column('artifact_kind', sa.String(), nullable=False),
        sa.Column('artifact_id', sa.Integer(), nullable=False),
        sa.Column('photo_uuid', sa.String(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('date', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False,
                  server_default=sa.text("'proposed'")),
        sa.Column('confidence', sa.String(), nullable=True),
        sa.Column('selected', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('asset_id', sa.String(), nullable=True),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('alternatives_json', sa.String(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column('asset_bytes', sa.Integer(), nullable=True),
        sa.Column('asset_width', sa.Integer(), nullable=True),
        sa.Column('asset_height', sa.Integer(), nullable=True),
        sa.Column('asset_mime', sa.String(), nullable=True),
        sa.Column('current_bytes', sa.Integer(), nullable=True),
        sa.Column('current_width', sa.Integer(), nullable=True),
        sa.Column('current_height', sa.Integer(), nullable=True),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('applied_uuid', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['scan_id'], ['photoupgradescan.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_photoupgradesuggestion_scan_id', 'photoupgradesuggestion',
                    ['scan_id'])
    op.create_index('ix_photoupgradesuggestion_scan_state', 'photoupgradesuggestion',
                    ['scan_id', 'state'])
    op.create_index('ix_photoupgradesuggestion_scan_order', 'photoupgradesuggestion',
                    ['scan_id', 'date', 'artifact_kind', 'artifact_id', 'position'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_photoupgradesuggestion_scan_order',
                  table_name='photoupgradesuggestion')
    op.drop_index('ix_photoupgradesuggestion_scan_state',
                  table_name='photoupgradesuggestion')
    op.drop_index('ix_photoupgradesuggestion_scan_id',
                  table_name='photoupgradesuggestion')
    op.drop_table('photoupgradesuggestion')
    op.drop_index('ix_photoupgradeasset_scan_fingerprinted',
                  table_name='photoupgradeasset')
    op.drop_index('ix_photoupgradeasset_scan_local_date',
                  table_name='photoupgradeasset')
    op.drop_index('ix_photoupgradeasset_scan_id', table_name='photoupgradeasset')
    op.drop_table('photoupgradeasset')
    op.drop_index(_ACTIVE_INDEX, table_name='photoupgradescan')
    op.drop_index('ix_photoupgradescan_project_user_created',
                  table_name='photoupgradescan')
    op.drop_index('ix_photoupgradescan_user_info_id', table_name='photoupgradescan')
    op.drop_index('ix_photoupgradescan_project_id', table_name='photoupgradescan')
    op.drop_table('photoupgradescan')

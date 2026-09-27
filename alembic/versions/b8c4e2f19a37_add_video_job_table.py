"""add video job table (trip video export)

Adds the `videojob` table backing async server-side trip-video renders: one row
per render request, tracking status/progress and, once done, the MP4's path,
size, download token and retention deadline. The rows double as the monthly
video quota (count per requester per UTC month, failed jobs excluded), which is
what the (user_info_id, created_at) index serves.

Revision ID: b8c4e2f19a37
Revises: 7d3e9b1f4a20
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8c4e2f19a37'
down_revision: Union[str, Sequence[str], None] = '7d3e9b1f4a20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'videojob',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('user_info_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column('stage', sa.String(), nullable=True),
        sa.Column('progress', sa.Float(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('request_json', sa.String(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column('created_at', sa.Float(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('started_at', sa.Float(), nullable=True),
        sa.Column('completed_at', sa.Float(), nullable=True),
        sa.Column('result_path', sa.String(), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('download_token', sa.String(), nullable=True),
        sa.Column('expires_at', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.ForeignKeyConstraint(['user_info_id'], ['userinfo.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_videojob_project_id', 'videojob', ['project_id'])
    op.create_index('ix_videojob_download_token', 'videojob', ['download_token'])
    op.create_index('ix_videojob_user_created', 'videojob',
                    ['user_info_id', 'created_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_videojob_user_created', table_name='videojob')
    op.drop_index('ix_videojob_download_token', table_name='videojob')
    op.drop_index('ix_videojob_project_id', table_name='videojob')
    op.drop_table('videojob')

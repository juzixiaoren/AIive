"""audit: memory revision fields

Revision ID: 12579c3101b3
Revises: 553f2bf14edf
Create Date: 2026-07-08 17:24:33.198301

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '12579c3101b3'
down_revision: Union[str, Sequence[str], None] = '553f2bf14edf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    前置迁移 553f2bf14edf 已新增 memory_key/supersedes/superseded_by 及其索引，
    本迁移仅将旧列 revision 重命名为 revision_num（先删旧列、再建新列），
    不重复新增前置迁移已存在的列。
    """
    op.drop_column('memory_records', 'revision')
    op.add_column(
        'memory_records',
        sa.Column('revision_num', sa.Integer(), nullable=False, server_default='1'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('memory_records', 'revision_num')
    op.add_column(
        'memory_records',
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
    )

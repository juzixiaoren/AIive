"""Phase 4 补充：threads.last_activity_at 列。

Idle Scanner（Phase 3）与维护 Idle 触发（Phase 4）依赖 `threads.last_activity_at`
判定空闲线程；此前该列缺失，导致 idle 分支查询在 SQL 编译期失败（被异常吞掉），
idle 触发形同虚设。本次补齐该列，使 idle 判定真正可用。

Revision ID: p4b2c3d4e5f6
Revises: p4a0b1c2d3e4f
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "p4b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "p4a0b1c2d3e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    try:
        insp = sa.inspect(conn)
        cols = [c["name"] for c in insp.get_columns(table)]
        return column in cols
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "threads", "last_activity_at"):
        op.add_column(
            "threads",
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "threads", "last_activity_at"):
        op.drop_column("threads", "last_activity_at")

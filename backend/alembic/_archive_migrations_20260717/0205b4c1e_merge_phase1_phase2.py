"""Merge Phase 1 (d5e6f7a8b9c0) and Phase 2 (0205b4c1d) — single linear head.

Revision ID: 0205b4c1e
Revises: d5e6f7a8b9c0, 0205b4c1d
Create Date: 2026-07-15 14:40:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0205b4c1e"
down_revision: Union[str, tuple[str, ...]] = ("d5e6f7a8b9c0", "0205b4c1d")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

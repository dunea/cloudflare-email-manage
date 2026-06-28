"""add cf account capability report

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-28 21:00:00+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 CF 账号保存最近一次 Token 权限预检报告。"""
    with op.batch_alter_table("cf_account", schema=None) as batch_op:
        batch_op.add_column(sa.Column("capability_report_json", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("capability_checked_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    """移除 CF 账号 Token 权限预检报告。"""
    with op.batch_alter_table("cf_account", schema=None) as batch_op:
        batch_op.drop_column("capability_checked_at")
        batch_op.drop_column("capability_report_json")

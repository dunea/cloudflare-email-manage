"""add api_key scopes

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-28 18:00:00+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 API Key 增加权限范围，既有 key 默认保持程序化收发能力。"""
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scopes",
                sa.Text(),
                nullable=False,
                server_default="send,read_inbound",
            )
        )
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.alter_column("scopes", server_default=None)


def downgrade() -> None:
    """移除 API Key 权限范围。"""
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.drop_column("scopes")

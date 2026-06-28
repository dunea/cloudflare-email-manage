"""add domain inbound_routing_enabled column

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-29 02:00:00+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为域名新增收件路由启用状态，并保留旧部署/邮箱地址域名。"""
    with op.batch_alter_table("domain", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "inbound_routing_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.execute(
        """
        UPDATE domain
        SET inbound_routing_enabled = 1
        WHERE webhook_secret IS NOT NULL
           OR id IN (
               SELECT DISTINCT domain_id
               FROM email_address
               WHERE is_deleted = 0
           )
        """
    )


def downgrade() -> None:
    """移除域名收件路由启用状态。"""
    with op.batch_alter_table("domain", schema=None) as batch_op:
        batch_op.drop_column("inbound_routing_enabled")

"""add domain webhook_secret column

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-26 10:00:00+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 domain 表新增 webhook_secret 列，用于每域名独立的 Webhook 签名密钥。"""
    op.add_column(
        'domain',
        sa.Column('webhook_secret', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('domain', 'webhook_secret')
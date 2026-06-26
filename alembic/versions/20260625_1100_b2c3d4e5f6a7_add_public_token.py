"""add email_address.public_token

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-25 11:00:00+00:00

"""
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) 先以 nullable 形式新增列，便于回填已有行
    with op.batch_alter_table('email_address', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('public_token', sa.String(length=32), nullable=True)
        )

    # 2) 回填：为每行生成唯一的无符号 uuid（uuid4().hex）
    conn = op.get_bind()
    rows = conn.execute(sa.text('SELECT id FROM email_address')).fetchall()
    for (row_id,) in rows:
        conn.execute(
            sa.text('UPDATE email_address SET public_token = :tok WHERE id = :id'),
            {'tok': uuid.uuid4().hex, 'id': row_id},
        )

    # 3) 创建唯一索引
    op.create_index(
        'ix_email_address_public_token',
        'email_address',
        ['public_token'],
        unique=True,
    )

    # 4) 列设为 NOT NULL
    with op.batch_alter_table('email_address', schema=None) as batch_op:
        batch_op.alter_column('public_token', nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('email_address', schema=None) as batch_op:
        batch_op.drop_column('public_token')

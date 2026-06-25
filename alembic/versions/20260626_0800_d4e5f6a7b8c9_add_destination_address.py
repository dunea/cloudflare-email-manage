"""add destination_address table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-26 08:00:00+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: str | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新建 destination_address 表，缓存 CF Email Routing 转发目标地址与验证状态。"""
    op.create_table(
        'destination_address',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('cf_account_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('cf_address_id', sa.String(length=64), nullable=False),
        sa.Column('verified', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['cf_account_id'], ['cf_account.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_destination_address_cf_account_id',
        'destination_address',
        ['cf_account_id'],
        unique=False,
    )
    op.create_index(
        'ix_destination_address_user_id',
        'destination_address',
        ['user_id'],
        unique=False,
    )
    op.create_index(
        'ix_destination_address_email',
        'destination_address',
        ['email'],
        unique=False,
    )
    op.create_index(
        'ix_destination_address_cf_address_id',
        'destination_address',
        ['cf_address_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_destination_address_cf_address_id', table_name='destination_address')
    op.drop_index('ix_destination_address_email', table_name='destination_address')
    op.drop_index('ix_destination_address_user_id', table_name='destination_address')
    op.drop_index('ix_destination_address_cf_account_id', table_name='destination_address')
    op.drop_table('destination_address')

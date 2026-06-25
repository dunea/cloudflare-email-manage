"""remove permission_type, allowed_zone_ids, owner_type

Revision ID: a1b2c3d4e5f6
Revises: 690a516ed7db
Create Date: 2026-06-25 10:00:00+00:00

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = '690a516ed7db'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # cf_account: 移除 permission_type 和 allowed_zone_ids
    with op.batch_alter_table('cf_account', schema=None) as batch_op:
        batch_op.drop_column('permission_type')
        batch_op.drop_column('allowed_zone_ids')

    # domain: 移除 owner_type
    with op.batch_alter_table('domain', schema=None) as batch_op:
        batch_op.drop_column('owner_type')


def downgrade() -> None:
    # cf_account: 恢复 permission_type 和 allowed_zone_ids
    with op.batch_alter_table('cf_account', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('allowed_zone_ids', sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                'permission_type',
                sa.String(length=16),
                nullable=False,
                server_default='all',
            )
        )

    # domain: 恢复 owner_type
    with op.batch_alter_table('domain', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'owner_type',
                sa.String(length=16),
                nullable=False,
                server_default='user',
            )
        )

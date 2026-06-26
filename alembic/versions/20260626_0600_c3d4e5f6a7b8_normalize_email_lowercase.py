"""normalize email addresses to lowercase

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-26 06:00:00+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: str | None = 'b2c3d4e5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """将存量邮箱地址与收件邮件地址统一规范化为小写。

    执行前先检测大小写冲突（如 Hello@x.com 与 hello@x.com 并存），
    有冲突则报错提示手动解决，避免 lower() 后违反唯一约束。
    """
    bind = op.get_bind()
    collisions = bind.execute(
        sa.text(
            "SELECT lower(full_address) "
            "FROM email_address "
            "GROUP BY lower(full_address) "
            "HAVING count(*) > 1"
        )
    ).fetchall()
    if collisions:
        raise RuntimeError(
            "存在大小写冲突的 email_address.full_address 记录，"
            "请手动合并后再执行迁移: "
            + ", ".join(row[0] for row in collisions)
        )

    # 将存量邮箱地址统一转为小写，与代码层规范化保持一致
    op.execute(
        sa.text(
            "UPDATE email_address "
            "SET full_address = lower(full_address), "
            "    local_part = lower(local_part)"
        )
    )
    # 将存量收件邮件的收发件地址统一转为小写
    op.execute(
        sa.text(
            "UPDATE inbound_email "
            "SET to_address = lower(to_address), "
            "    from_address = lower(from_address)"
        )
    )


def downgrade() -> None:
    """大小写规范化不可逆，保留空实现。"""
    pass

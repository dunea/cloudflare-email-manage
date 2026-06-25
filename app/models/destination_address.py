"""DestinationAddress 模型：CF Email Routing 转发目标地址（含验证状态缓存）。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.cf_account import CFAccount
    from app.models.user import User


class DestinationAddress(Base):
    """转发目标地址，对应 CF Email Routing destination address。

    目标地址为 account 级资源，可被同账号下任意域名的转发规则复用。
    验证状态由 CF 侧控制：添加后 CF 会向该邮箱发送验证邮件，邮箱所有者
    在浏览器点击验证链接并完成人机验证后，CF 标记 verified。
    本地缓存 verified 字段由同步操作从 CF 刷新。
    """

    __tablename__ = "destination_address"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cf_account_id: Mapped[int] = mapped_column(
        ForeignKey("cf_account.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    # 目标邮箱地址（统一小写存储）
    email: Mapped[str] = mapped_column(String(320), index=True)
    # CF 侧目标地址标识
    cf_address_id: Mapped[str] = mapped_column(String(64), index=True)
    # 是否已在 CF 完成验证
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    cf_account: Mapped[CFAccount] = relationship()
    user: Mapped[User] = relationship()

"""CFAccount 模型：用户绑定的 Cloudflare 账号。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.domain import Domain
    from app.models.user import User


class CFAccount(Base):
    """用户绑定的 Cloudflare 账号，API Token 加密存储。"""

    __tablename__ = "cf_account"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # Fernet 加密后的 API Token
    encrypted_api_token: Mapped[str] = mapped_column(Text)
    # CF 账号 ID（account_id）
    account_id: Mapped[str] = mapped_column(String(64))
    # 权限类型：all（全部域名）/ specific（仅指定域名）
    permission_type: Mapped[str] = mapped_column(String(16), default="all")
    # 当 permission_type=specific 时，存放允许的 zone_id（逗号分隔）
    allowed_zone_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    user: Mapped[User] = relationship(back_populates="cf_accounts")
    domains: Mapped[list[Domain]] = relationship(
        back_populates="cf_account", cascade="all, delete-orphan"
    )

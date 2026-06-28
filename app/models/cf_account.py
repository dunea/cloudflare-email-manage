"""CFAccount 模型：用户绑定的 Cloudflare 账号。"""

from __future__ import annotations

import json
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
    # CF 账号 ID（account_id，绑定时自动获取）
    account_id: Mapped[str] = mapped_column(String(64))
    # 最近一次 Cloudflare Token 权限预检报告（JSON 文本，不含 Token）
    capability_report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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

    @property
    def capability_report(self) -> dict[str, object] | None:
        """返回最近一次权限预检报告，用于 Pydantic 响应序列化。"""
        if not self.capability_report_json:
            return None
        try:
            parsed = json.loads(self.capability_report_json)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

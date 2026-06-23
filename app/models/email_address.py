"""EmailAddress 模型：邮箱地址。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.domain import Domain
    from app.models.forwarding_rule import ForwardingRule
    from app.models.user import User


class EmailAddress(Base):
    """邮箱地址，例如 hello@example.com。"""

    __tablename__ = "email_address"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domain.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    # 本地部分，例如 hello
    local_part: Mapped[str] = mapped_column(String(128))
    # 完整地址，例如 hello@example.com
    full_address: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    domain: Mapped[Domain] = relationship(back_populates="email_addresses")
    user: Mapped[User] = relationship(back_populates="email_addresses")
    forwarding_rules: Mapped[list[ForwardingRule]] = relationship(
        back_populates="email_address", cascade="all, delete-orphan"
    )

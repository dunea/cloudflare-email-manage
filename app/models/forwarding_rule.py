"""ForwardingRule 模型：邮箱转发规则。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.email_address import EmailAddress


class ForwardingRule(Base):
    """邮箱转发规则，对应 CF Email Routing rule。"""

    __tablename__ = "forwarding_rule"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email_address_id: Mapped[int] = mapped_column(
        ForeignKey("email_address.id"), index=True
    )
    # 转发目标地址
    destination_email: Mapped[str] = mapped_column(String(320))
    # CF 侧规则 ID
    cf_rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    email_address: Mapped[EmailAddress] = relationship(
        back_populates="forwarding_rules"
    )

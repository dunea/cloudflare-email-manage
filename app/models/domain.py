"""Domain 模型：Cloudflare 域名（zone）。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.cf_account import CFAccount
    from app.models.domain_assignment import DomainAssignment
    from app.models.email_address import EmailAddress


class Domain(Base):
    """Cloudflare 域名（zone），归属用户或平台。"""

    __tablename__ = "domain"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cf_account_id: Mapped[int] = mapped_column(ForeignKey("cf_account.id"), index=True)
    zone_id: Mapped[str] = mapped_column(String(64), index=True)
    domain_name: Mapped[str] = mapped_column(String(255), index=True)
    # 状态：active / pending / moved 等
    status: Mapped[str] = mapped_column(String(32), default="active")
    # 该域名的 Webhook 签名密钥（per-domain，注入到账号级 Worker 的 WEBHOOK_SECRETS）
    # 为空时收件校验回退到全局 CF_WEBHOOK_SECRET（兼容旧部署）
    webhook_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 是否将该域名作为收件邮箱域名纳入 Worker 部署与 Email Routing 配置
    inbound_routing_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    cf_account: Mapped[CFAccount] = relationship(back_populates="domains")
    email_addresses: Mapped[list[EmailAddress]] = relationship(
        back_populates="domain", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[DomainAssignment]] = relationship(
        back_populates="domain", cascade="all, delete-orphan"
    )

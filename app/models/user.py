"""User 模型：平台用户账号。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.api_key import APIKey
    from app.models.cf_account import CFAccount
    from app.models.domain_assignment import DomainAssignment
    from app.models.email_address import EmailAddress
    from app.models.outbound_email import OutboundEmail


class User(Base):
    """平台用户账号。"""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    # 角色：admin / user
    role: Mapped[str] = mapped_column(String(16), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    cf_accounts: Mapped[list[CFAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list[APIKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    email_addresses: Mapped[list[EmailAddress]] = relationship(back_populates="user")
    outbound_emails: Mapped[list[OutboundEmail]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    domain_assignments: Mapped[list[DomainAssignment]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

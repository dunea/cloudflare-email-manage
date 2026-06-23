"""DomainAssignment 模型：平台域名分配给普通用户。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.domain import Domain
    from app.models.user import User


class DomainAssignment(Base):
    """平台域名分配给普通用户的记录。"""

    __tablename__ = "domain_assignment"
    __table_args__ = (
        UniqueConstraint("domain_id", "user_id", name="uq_domain_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domain.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    domain: Mapped[Domain] = relationship(back_populates="assignments")
    user: Mapped[User] = relationship(back_populates="domain_assignments")

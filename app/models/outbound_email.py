"""OutboundEmail 模型：平台发送并记录的邮件。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class OutboundEmail(Base):
    """通过 Cloudflare Email Sending 发出的邮件记录。"""

    __tablename__ = "outbound_email"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    from_address: Mapped[str] = mapped_column(String(320), index=True)
    to_addresses_json: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(String(998))
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="sending", index=True)
    provider_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="outbound_emails")

    @property
    def to_addresses(self) -> list[str]:
        """返回收件人列表，兼容异常 JSON 时返回空列表。"""
        try:
            parsed = json.loads(self.to_addresses_json)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed]

    @property
    def provider_response(self) -> dict[str, object] | None:
        """返回 Cloudflare 原始响应，解析失败时返回 None。"""
        if not self.provider_response_json:
            return None
        try:
            parsed = json.loads(self.provider_response_json)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

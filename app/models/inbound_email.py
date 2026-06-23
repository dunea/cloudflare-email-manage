"""InboundEmail 模型：通过 Webhook 收到的邮件。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class InboundEmail(Base):
    """通过 Webhook 接收并存储的邮件。"""

    __tablename__ = "inbound_email"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    to_address: Mapped[str] = mapped_column(String(320), index=True)
    from_address: Mapped[str] = mapped_column(String(320), index=True)
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

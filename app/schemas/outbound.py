"""发件 相关 Pydantic 请求/响应模型。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class SendEmailRequest(BaseModel):
    """发送邮件请求体（CF Email Sending Beta）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    from_address: EmailStr = Field(description="发件地址，必须是平台内已管理的邮箱地址")
    to: list[EmailStr] = Field(min_length=1, description="收件地址列表")
    subject: str = Field(min_length=1, max_length=998, description="邮件主题")
    text: str | None = Field(default=None, description="纯文本正文")
    html: str | None = Field(default=None, description="HTML 正文")

    @model_validator(mode="after")
    def _require_body(self) -> Self:
        """text 与 html 至少需要提供一个。"""
        if not self.text and not self.html:
            raise ValueError("text 与 html 至少需要提供一个")
        return self


class SendEmailResult(BaseModel):
    """发送邮件响应体。"""

    from_address: str
    to: list[str]
    subject: str
    status: Literal["sent", "failed"]
    outbound_email_id: int | None = None
    provider_response: dict[str, object] | None = None


class OutboundEmailRead(BaseModel):
    """发件箱邮件响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    from_address: str
    to_addresses: list[str]
    subject: str
    body_text: str | None = None
    body_html: str | None = None
    status: Literal["sending", "sent", "failed"]
    provider_response: dict[str, object] | None = None
    error_message: str | None = None
    sent_at: datetime | None = None
    created_at: datetime

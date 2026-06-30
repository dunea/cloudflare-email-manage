"""收件邮件 相关 Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class InboundEmailPayload(BaseModel):
    """Webhook 收件请求体。

    字段名兼容常见 Webhook 载荷（to/from/subject/text/html），
    同时允许使用模型字段名提交。
    """

    model_config = ConfigDict(populate_by_name=True)

    to_address: EmailStr = Field(alias="to", description="收件地址")
    from_address: EmailStr = Field(alias="from", description="发件地址")
    from_name: str | None = None
    envelope_from: str | None = None
    reply_to: str | None = None
    message_id: str | None = None
    subject: str | None = Field(default=None, max_length=998)
    body_text: str | None = Field(default=None, alias="text")
    body_html: str | None = Field(default=None, alias="html")


class InboundEmailRead(BaseModel):
    """收件邮件响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    to_address: str
    from_address: str
    from_name: str | None = None
    envelope_from: str | None = None
    reply_to: str | None = None
    message_id: str | None = None
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    received_at: datetime
    created_at: datetime

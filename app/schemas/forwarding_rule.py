"""转发规则 相关 Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ForwardingRuleCreate(BaseModel):
    """创建转发规则请求体。"""

    email_address_id: int = Field(gt=0, description="源邮箱地址 id")
    destination_email: EmailStr = Field(description="转发目标邮箱地址")


class ForwardingRuleUpdate(BaseModel):
    """更新转发规则请求体，字段均可选。"""

    is_active: bool | None = None


class ForwardingRuleRead(BaseModel):
    """转发规则响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email_address_id: int
    destination_email: str
    cf_rule_id: str | None = None
    is_active: bool
    created_at: datetime

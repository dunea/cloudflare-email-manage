"""转发目标地址 相关 Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class DestinationAddressCreate(BaseModel):
    """创建转发目标地址请求体。"""

    cf_account_id: int = Field(gt=0, description="所属 CF 账号 id")
    email: EmailStr = Field(description="目标邮箱地址")


class DestinationAddressRead(BaseModel):
    """转发目标地址响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    cf_account_id: int
    user_id: int
    email: str
    cf_address_id: str
    verified: bool
    verified_at: datetime | None = None
    created_at: datetime

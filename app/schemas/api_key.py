"""API Key 相关 Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class APIKeyCreate(BaseModel):
    """创建 API Key 请求体。"""

    name: str = Field(min_length=1, max_length=128, description="API Key 名称/备注")


class APIKeyUpdate(BaseModel):
    """更新 API Key 请求体，字段均可选。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None


class APIKeyRead(BaseModel):
    """API Key 响应体（不含原始 key 与哈希）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    last_used_at: datetime | None = None
    is_active: bool
    created_at: datetime


class APIKeyCreated(APIKeyRead):
    """创建 API Key 的响应体，附带仅返回一次的原始 key。"""

    key: str = Field(description="原始 API Key，仅在创建时返回一次，请妥善保存")

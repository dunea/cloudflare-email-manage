"""CF 账号 相关 Pydantic 请求/响应模型。

注意：响应模型绝不包含 API Token（明文或密文）。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CFAccountCreate(BaseModel):
    """绑定 CF 账号请求体。"""

    name: str = Field(min_length=1, max_length=128)
    api_token: str = Field(min_length=1, description="CF API Token，仅用于绑定，不回显")
    account_id: str = Field(min_length=1, max_length=64)
    permission_type: Literal["all", "specific"] = "all"
    # permission_type=specific 时必须提供允许的 zone_id 列表
    allowed_zone_ids: list[str] | None = None


class CFAccountUpdate(BaseModel):
    """更新 CF 账号请求体，字段均可选。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    api_token: str | None = Field(default=None, min_length=1)
    permission_type: Literal["all", "specific"] | None = None
    allowed_zone_ids: list[str] | None = None
    is_active: bool | None = None


class CFAccountRead(BaseModel):
    """CF 账号响应体（不含 Token）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    account_id: str
    permission_type: str
    allowed_zone_ids: list[str] | None = None
    is_active: bool
    created_at: datetime

    @field_validator("allowed_zone_ids", mode="before")
    @classmethod
    def _split_zone_ids(cls, value: object) -> list[str] | None:
        """ORM 中以逗号分隔字符串存储，读取时拆分为列表。"""
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return [part for part in value.split(",") if part]
        if isinstance(value, list):
            return value
        return None

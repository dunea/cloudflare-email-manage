"""CF 账号 相关 Pydantic 请求/响应模型。

注意：响应模型绝不包含 API Token（明文或密文）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CFAccountCreate(BaseModel):
    """绑定 CF 账号请求体。"""

    name: str = Field(min_length=1, max_length=128)
    api_token: str = Field(min_length=1, description="CF API Token，仅用于绑定，不回显")
    # 可选：留空时绑定后自动从 CF API 获取
    account_id: str | None = Field(default=None, max_length=64)


class CFAccountUpdate(BaseModel):
    """更新 CF 账号请求体，字段均可选。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    api_token: str | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class CFAccountRead(BaseModel):
    """CF 账号响应体（不含 Token）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    account_id: str
    is_active: bool
    created_at: datetime

"""邮箱地址 相关 Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EmailAddressCreate(BaseModel):
    """创建邮箱地址请求体。

    full_address 由后端根据 local_part 与所属域名拼接生成。
    """

    domain_id: int = Field(gt=0, description="所属域名 id")
    # 邮箱本地部分（@ 前），仅允许常见合法字符
    local_part: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9._%+\-]+$",
        description="邮箱本地部分，例如 hello",
    )


class EmailAddressUpdate(BaseModel):
    """更新邮箱地址请求体，字段均可选。"""

    is_active: bool | None = None


class EmailAddressRead(BaseModel):
    """邮箱地址响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    domain_id: int
    user_id: int
    local_part: str
    full_address: str
    is_active: bool
    created_at: datetime

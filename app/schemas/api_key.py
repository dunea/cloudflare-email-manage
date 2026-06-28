"""API Key 相关 Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

API_KEY_SCOPES = {"send", "read_inbound"}
DEFAULT_API_KEY_SCOPES = ["send", "read_inbound"]


def normalize_scopes(value: object) -> list[str]:
    """规范化 API Key scopes，保持稳定顺序并校验合法值。"""
    if value is None or value == "":
        scopes = list(DEFAULT_API_KEY_SCOPES)
    elif isinstance(value, str):
        scopes = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        scopes = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError("scopes 格式无效")

    unique = sorted(
        set(scopes),
        key=lambda item: (
            DEFAULT_API_KEY_SCOPES.index(item)
            if item in DEFAULT_API_KEY_SCOPES
            else 99
        ),
    )
    invalid = [item for item in unique if item not in API_KEY_SCOPES]
    if invalid:
        raise ValueError(f"不支持的 API Key 权限: {', '.join(invalid)}")
    if not unique:
        raise ValueError("API Key 至少需要一个权限")
    return unique


class APIKeyCreate(BaseModel):
    """创建 API Key 请求体。"""

    name: str = Field(min_length=1, max_length=128, description="API Key 名称/备注")
    scopes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_API_KEY_SCOPES),
        description="API Key 权限范围：send / read_inbound",
    )

    @field_validator("scopes", mode="before")
    @classmethod
    def _validate_scopes(cls, value: object) -> list[str]:
        """校验并规范化权限范围。"""
        return normalize_scopes(value)


class APIKeyUpdate(BaseModel):
    """更新 API Key 请求体，字段均可选。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None
    scopes: list[str] | None = Field(default=None, description="API Key 权限范围")

    @field_validator("scopes", mode="before")
    @classmethod
    def _validate_scopes(cls, value: object) -> list[str] | None:
        """校验并规范化权限范围。"""
        if value is None:
            return None
        return normalize_scopes(value)


class APIKeyRead(BaseModel):
    """API Key 响应体（不含原始 key 与哈希）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    scopes: list[str]
    last_used_at: datetime | None = None
    is_active: bool
    created_at: datetime

    @field_validator("scopes", mode="before")
    @classmethod
    def _read_scopes(cls, value: object) -> list[str]:
        """将数据库中逗号分隔的 scopes 转成列表。"""
        return normalize_scopes(value)


class APIKeyCreated(APIKeyRead):
    """创建 API Key 的响应体，附带仅返回一次的原始 key。"""

    key: str = Field(description="原始 API Key，仅在创建时返回一次，请妥善保存")

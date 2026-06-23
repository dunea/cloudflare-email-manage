"""通用 Pydantic 模型：统一响应格式与分页。"""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一 API 响应格式：{"code": 0, "data": ..., "message": "ok"}。"""

    model_config = ConfigDict(from_attributes=True)

    code: int = 0
    data: T | None = None
    message: str = "ok"


class PageParams(BaseModel):
    """分页请求参数。"""

    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class PageData(BaseModel, Generic[T]):
    """分页响应数据。"""

    model_config = ConfigDict(from_attributes=True)

    total: int = 0
    page: int = 1
    size: int = 20
    items: list[T] = Field(default_factory=list)

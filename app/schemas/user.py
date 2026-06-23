"""用户与认证相关的 Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    """注册请求体。"""

    username: str = Field(min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserLogin(BaseModel):
    """登录请求体：username 字段可填用户名或邮箱。"""

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class UserUpdate(BaseModel):
    """更新当前用户信息请求体，字段均可选。"""

    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserRead(BaseModel):
    """用户信息响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: datetime


class Token(BaseModel):
    """登录/刷新返回的令牌对。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    """刷新令牌请求体。"""

    refresh_token: str = Field(min_length=1)

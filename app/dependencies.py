"""FastAPI 依赖：数据库会话、当前用户、权限校验等。"""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.exceptions import AuthError, PermissionError
from app.models import User
from app.services.auth_service import ACCESS_TOKEN_TYPE, decode_token
from app.services.user_service import get_user_by_id

# 数据库会话依赖别名
SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Bearer 令牌提取（auto_error=False 以便统一抛出业务异常）
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT 访问令牌")


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> User:
    """从 Authorization Bearer 令牌解析并返回当前登录用户。"""
    if credentials is None:
        raise AuthError("缺少认证凭证")

    payload = decode_token(credentials.credentials)
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise AuthError("无效的访问令牌")

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.isdigit():
        raise AuthError("令牌主体无效")

    user = await get_user_by_id(session, int(sub))
    if user is None or not user.is_active:
        raise AuthError("用户不存在或已被禁用")
    return user


# 当前登录用户依赖别名
CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(current_user: CurrentUser) -> User:
    """要求当前用户为管理员，否则抛出 PermissionError。"""
    if current_user.role != "admin":
        raise PermissionError("需要管理员权限")
    return current_user


# 管理员用户依赖别名
AdminUser = Annotated[User, Depends(require_admin)]

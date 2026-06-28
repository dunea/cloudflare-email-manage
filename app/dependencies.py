"""FastAPI 依赖：数据库会话、当前用户、权限校验等。"""

import hashlib
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.exceptions import AuthError, PermissionError
from app.models import User
from app.services.api_key_service import authenticate_api_key
from app.services.auth_service import ACCESS_TOKEN_TYPE, decode_token
from app.services.rate_limit import hit
from app.services.user_service import get_user_by_id

# 数据库会话依赖别名
SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Bearer 令牌提取（auto_error=False 以便统一抛出业务异常）
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT 访问令牌")


async def _user_from_access_token(session: AsyncSession, token: str) -> User:
    """从访问令牌解析并返回用户，校验失败抛出 AuthError。"""
    payload = decode_token(token)
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise AuthError("无效的访问令牌")

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.isdigit():
        raise AuthError("令牌主体无效")

    user = await get_user_by_id(session, int(sub))
    if user is None or not user.is_active:
        raise AuthError("用户不存在或已被禁用")
    return user


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> User:
    """从 Authorization Bearer 令牌解析并返回当前登录用户。"""
    if credentials is None:
        raise AuthError("缺少认证凭证")
    return await _user_from_access_token(session, credentials.credentials)


# 当前登录用户依赖别名
CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_request_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    """支持 JWT Bearer 或 X-API-Key 两种认证方式（程序化调用），任一通过即可。"""
    return await _get_request_user_with_scope(
        request, session, credentials, x_api_key, None
    )


async def _get_request_user_with_scope(
    request: Request,
    session: AsyncSession,
    credentials: HTTPAuthorizationCredentials | None,
    x_api_key: str | None,
    required_scope: str | None,
) -> User:
    """按可选 scope 校验请求用户；JWT 不受 API Key scope 限制。"""
    if x_api_key:
        bucket_key = hashlib.sha256(x_api_key.encode("utf-8")).hexdigest()
        hit(
            "api_key",
            bucket_key,
            settings.API_KEY_RATE_LIMIT_ATTEMPTS,
            settings.API_KEY_RATE_LIMIT_WINDOW_SECONDS,
        )
        return await authenticate_api_key(session, x_api_key, required_scope)
    if credentials is not None:
        return await _user_from_access_token(session, credentials.credentials)
    raise AuthError("缺少认证凭证")


# 兼容 JWT 与 API Key 的用户依赖别名
RequestUser = Annotated[User, Depends(get_request_user)]


async def get_send_request_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    """支持 JWT 或具备 send scope 的 API Key。"""
    return await _get_request_user_with_scope(
        request, session, credentials, x_api_key, "send"
    )


async def get_read_inbound_request_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    """支持 JWT 或具备 read_inbound scope 的 API Key。"""
    return await _get_request_user_with_scope(
        request, session, credentials, x_api_key, "read_inbound"
    )


RequestUserSend = Annotated[User, Depends(get_send_request_user)]
RequestUserReadInbound = Annotated[User, Depends(get_read_inbound_request_user)]


async def require_admin(current_user: CurrentUser) -> User:
    """要求当前用户为管理员，否则抛出 PermissionError。"""
    if current_user.role != "admin":
        raise PermissionError("需要管理员权限")
    return current_user


# 管理员用户依赖别名
AdminUser = Annotated[User, Depends(require_admin)]

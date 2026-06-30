"""Web 表现层依赖：基于 HttpOnly Cookie 的会话认证与登录重定向。"""

from typing import Annotated

from fastapi import Cookie, Depends, Request, Response

from app.config import settings
from app.dependencies import SessionDep, _user_from_access_token
from app.exceptions import AppException
from app.models import User
from app.services import auth_service

# 认证 Cookie 名称
ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"


class WebRedirect(Exception):
    """Web 层重定向信号：由全局处理器转换为 303 跳转（如未登录跳登录页）。"""

    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    *,
    access_max_age: int | None = None,
    refresh_max_age: int | None = None,
) -> None:
    """在响应上写入认证 Cookie（HttpOnly，SameSite=Lax）。"""
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=(
            access_max_age
            if access_max_age is not None
            else settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        ),
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=(
            refresh_max_age
            if refresh_max_age is not None
            else settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
        ),
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    """清除认证 Cookie。"""
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")


async def get_optional_web_user(
    request: Request,
    session: SessionDep,
    access_token: Annotated[str | None, Cookie()] = None,
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> User | None:
    """从 Cookie 解析当前用户；access 失效时用 refresh 续签。

    续签得到的新令牌暂存于 ``request.state.web_new_token_session``，由 HTTP 中间件在
    响应阶段写回 Cookie（依赖内直接写 Cookie 在路由返回自定义 Response 时不会
    生效，故统一交给中间件处理）。未登录返回 None。
    """
    if access_token:
        try:
            return await _user_from_access_token(session, access_token)
        except AppException:
            pass  # access 过期/无效，尝试用 refresh 续签

    if refresh_token:
        try:
            token_session = await auth_service.refresh_web_tokens(session, refresh_token)
            user = await _user_from_access_token(session, token_session.access_token)
        except AppException:
            return None
        request.state.web_new_token_session = token_session
        return user

    return None


async def get_web_user(
    request: Request,
    user: Annotated[User | None, Depends(get_optional_web_user)],
) -> User:
    """要求已登录，否则抛出 WebRedirect 跳转登录页（携带 next 回跳路径）。"""
    if user is None:
        raise WebRedirect(f"/login?next={request.url.path}")
    return user


async def require_web_admin(
    user: Annotated[User, Depends(get_web_user)],
) -> User:
    """要求当前用户为管理员，否则跳转仪表盘。"""
    if user.role != "admin":
        raise WebRedirect("/dashboard")
    return user


# 依赖别名
OptionalWebUser = Annotated[User | None, Depends(get_optional_web_user)]
CurrentWebUser = Annotated[User, Depends(get_web_user)]
AdminWebUser = Annotated[User, Depends(require_web_admin)]

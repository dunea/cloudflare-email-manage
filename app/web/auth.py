"""认证页面路由：登录、注册、登出（服务端渲染 + Cookie 会话）。

复用 app/services/auth_service 的注册/登录/令牌逻辑，不重复实现业务逻辑。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.config import settings
from app.dependencies import SessionDep
from app.exceptions import AppException
from app.schemas.user import UserCreate
from app.services import auth_service
from app.services.rate_limit import client_ip, hit, reset
from app.web.deps import OptionalWebUser, clear_auth_cookies, set_auth_cookies
from app.web.templating import error_message, flash, render

router = APIRouter(tags=["前端-认证"])


def _safe_next(target: str) -> str:
    """仅允许跳转到站内路径，防止开放重定向。"""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return "/dashboard"


@router.get("/login")
async def login_page(
    request: Request,
    user: OptionalWebUser,
    next_url: Annotated[str, Query(alias="next")] = "/dashboard",
) -> Response:
    """登录页：已登录直接跳仪表盘。"""
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "auth/login.html", form={"next": _safe_next(next_url)})


@router.post("/login")
async def login_submit(
    request: Request,
    session: SessionDep,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next_url: Annotated[str, Form(alias="next")] = "/dashboard",
) -> Response:
    """处理登录表单：校验凭证后下发 Cookie 并跳转。"""
    bucket_key = f"{client_ip(request)}:{username.lower()}"
    try:
        user = await auth_service.authenticate_user(session, username, password)
    except AppException as exc:
        try:
            hit(
                "login",
                bucket_key,
                settings.LOGIN_RATE_LIMIT_ATTEMPTS,
                settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
            )
        except AppException as rate_exc:
            flash(request, rate_exc.message, "error")
            return render(
                request,
                "auth/login.html",
                status_code=rate_exc.http_status,
                form={"username": username, "next": _safe_next(next_url)},
            )
        flash(request, exc.message, "error")
        return render(
            request,
            "auth/login.html",
            status_code=400,
            form={"username": username, "next": _safe_next(next_url)},
        )

    reset("login", bucket_key)
    tokens = auth_service.issue_tokens(user)
    response = RedirectResponse(_safe_next(next_url), status_code=303)
    set_auth_cookies(response, tokens.access_token, tokens.refresh_token)
    return response


@router.get("/register")
async def register_page(request: Request, user: OptionalWebUser) -> Response:
    """注册页：已登录直接跳仪表盘。"""
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "auth/register.html", form={})


@router.post("/register")
async def register_submit(
    request: Request,
    session: SessionDep,
    username: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """处理注册表单：校验并创建用户后跳登录页。"""
    try:
        data = UserCreate(username=username, email=email, password=password)
        await auth_service.register_user(session, data)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return render(
            request,
            "auth/register.html",
            status_code=400,
            form={"username": username, "email": email},
        )

    flash(request, "注册成功，请使用新账号登录", "success")
    return RedirectResponse("/login", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> Response:
    """登出：清除认证 Cookie 并跳转登录页。"""
    flash(request, "已退出登录", "success")
    response = RedirectResponse("/login", status_code=303)
    clear_auth_cookies(response)
    return response

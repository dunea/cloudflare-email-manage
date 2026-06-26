"""Web 表现层：Jinja2 模板实例、统一渲染与 flash 消息助手。"""

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import settings
from app.exceptions import AppException
from app.models import User
from app.web.csrf import get_csrf_token

# 模板目录：app/templates
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _format_dt(value: datetime | None) -> str:
    """模板过滤器 dt：将 datetime 格式化为 YYYY-MM-DD HH:MM。"""
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


templates.env.filters["dt"] = _format_dt

# 站点默认 SEO 描述（首页等未覆写 description block 时使用）
APP_DESCRIPTION = (
    "基于 Cloudflare 的邮箱与域名邮件管理平台："
    "绑定 CF 账号、管理邮箱地址与转发规则、通过 API 收发邮件，支持开源自部署。"
)

# flash 消息在会话中的存储键
_FLASH_KEY = "_flash"


def error_message(exc: Exception) -> str:
    """将校验错误 / 业务异常转换为可读的中文提示。"""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            return f"输入有误：{errors[0].get('msg', '请检查表单')}"
        return "输入有误，请检查表单"
    if isinstance(exc, AppException):
        return exc.message
    return "操作失败，请稍后重试"


def flash(request: Request, message: str, category: str = "info") -> None:
    """写入一条一次性提示消息（category：info/success/warning/error）。

    整体重新赋值（而非就地 append），以触发 Starlette Session 的 modified
    标记，确保会话 Cookie 被写回——就地 append 不会经过 __setitem__。
    """
    bucket: list[dict[str, str]] = list(request.session.get(_FLASH_KEY, []))
    bucket.append({"message": message, "category": category})
    request.session[_FLASH_KEY] = bucket


def pop_flashes(request: Request) -> list[dict[str, str]]:
    """取出并清空当前会话中的全部 flash 消息。"""
    return request.session.pop(_FLASH_KEY, [])


def render(
    request: Request,
    template: str,
    *,
    user: User | None = None,
    status_code: int = 200,
    active: str | None = None,
    # context 为模板变量集合，类型天然异构，此处使用 Any 合理
    **context: Any,
) -> HTMLResponse:
    """渲染模板并注入统一上下文（当前用户、应用信息、flash、当前导航）。"""
    base_context: dict[str, Any] = {
        "current_user": user,
        "app_name": settings.APP_NAME,
        "app_version": settings.APP_VERSION,
        "app_description": APP_DESCRIPTION,
        "csrf_token": get_csrf_token(request),
        "active": active,
        "layout": None,
        "flashes": pop_flashes(request),
    }
    base_context.update(context)
    return templates.TemplateResponse(
        request, template, base_context, status_code=status_code
    )


def render_error(
    request: Request,
    status_code: int,
    message: str,
    *,
    user: User | None = None,
) -> HTMLResponse:
    """渲染错误页（404 使用 errors/404.html，其余使用 errors/500.html）。"""
    template = "errors/404.html" if status_code == 404 else "errors/500.html"
    return render(
        request, template, user=user, status_code=status_code, error_message=message
    )

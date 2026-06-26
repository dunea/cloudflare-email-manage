"""Web 表单 CSRF 防护。"""

from secrets import token_urlsafe
from typing import Final

from fastapi import Request

from app.config import settings
from app.exceptions import AppException

_CSRF_SESSION_KEY: Final[str] = "_csrf_token"
_CSRF_HEADER: Final[str] = "X-CSRF-Token"
_SAFE_METHODS: Final[set[str]] = {"GET", "HEAD", "OPTIONS", "TRACE"}


def get_csrf_token(request: Request) -> str:
    """获取当前会话 CSRF token，不存在时生成。"""
    token = request.session.get(_CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = token_urlsafe(32)
        request.session[_CSRF_SESSION_KEY] = token
    return token


async def validate_csrf_token(request: Request) -> None:
    """校验生产环境 Web 表单的 CSRF token。"""
    if request.method.upper() in _SAFE_METHODS:
        return
    if not settings.CSRF_PROTECTION or not settings.is_production:
        return

    expected = request.session.get(_CSRF_SESSION_KEY)
    submitted = request.headers.get(_CSRF_HEADER)
    if submitted is None:
        form = await request.form()
        value = form.get("csrf_token")
        submitted = value if isinstance(value, str) else None

    if not isinstance(expected, str) or submitted != expected:
        raise AppException(
            "表单已过期，请刷新页面后重试",
            code=1403,
            http_status=403,
        )

"""公开邮件查询路由：无需登录，通过公开令牌查看邮箱最新邮件。

- GET /mail/{token}      → HTML 页面（供人工快速查看）
- GET /mail/{token}.txt  → 纯文本（便于程序化读取）

令牌为 EmailAddress.public_token（无符号 uuid）。停用或已删除的邮箱不可访问。
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.dependencies import SessionDep
from app.exceptions import NotFoundError
from app.schemas.inbound_email import InboundEmailRead
from app.services import email_service, inbound_service
from app.services.rate_limit import client_ip, hit
from app.web.templating import render, render_error

router = APIRouter(tags=["前端-公开邮件"])


async def _resolve_by_token(request: Request, session: SessionDep, token: str):
    """按令牌解析邮箱；无效则抛 404（不暴露存在性差异）。"""
    ip = client_ip(request)
    hit(
        "public_mail_ip",
        ip,
        settings.PUBLIC_MAIL_RATE_LIMIT_ATTEMPTS,
        settings.PUBLIC_MAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    hit(
        "public_mail",
        f"{ip}:{token}",
        settings.PUBLIC_MAIL_RATE_LIMIT_ATTEMPTS,
        settings.PUBLIC_MAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    address = await email_service.get_email_address_by_token(session, token)
    if address is None:
        raise NotFoundError("链接无效或邮箱不可用")
    email = await inbound_service.get_latest_inbound_by_address(
        session, address.full_address
    )
    return address, email


@router.get("/mail/{token}.txt", response_class=PlainTextResponse)
async def public_mail_text(
    request: Request, token: str, session: SessionDep
) -> PlainTextResponse:
    """纯文本格式返回邮箱最新一封邮件，便于程序化读取。"""
    try:
        address, email = await _resolve_by_token(request, session, token)
    except NotFoundError:
        return PlainTextResponse("链接无效或邮箱不可用", status_code=404)

    if email is None:
        return PlainTextResponse("暂无邮件", media_type="text/plain; charset=utf-8")

    lines = [
        f"发件人: {email.from_address}",
        f"收件人: {email.to_address}",
        f"时间: {email.received_at:%Y-%m-%d %H:%M}",
        f"主题: {email.subject or '(无主题)'}",
        "",
        email.body_text or "",
    ]
    return PlainTextResponse(
        "\n".join(lines), media_type="text/plain; charset=utf-8"
    )


@router.get("/mail/{token}")
async def public_mail_html(
    request: Request, token: str, session: SessionDep
) -> Response:
    """渲染简洁 HTML 页面，供人工在不登录情况下快速查看最新邮件。"""
    try:
        address, email = await _resolve_by_token(request, session, token)
    except NotFoundError:
        return render_error(request, 404, "链接无效或邮箱不可用")

    return render(
        request,
        "public/mail.html",
        user=None,
        layout="landing",
        active=None,
        address=address.full_address,
        email=InboundEmailRead.model_validate(email) if email else None,
    )

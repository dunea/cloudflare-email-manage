"""公开邮件查询路由：无需登录，通过公开令牌查看邮箱邮件并发件。

- GET /mail/{token}      → HTML 页面（供人工快速查看）
- GET /mail/{token}.txt  → 纯文本（便于程序化读取）
- POST /mail/{token}/send → 通过该邮箱公开令牌发件

令牌为 EmailAddress.public_token（无符号 uuid）。停用或已删除的邮箱不可访问。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from app.config import settings
from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.models import EmailAddress, InboundEmail
from app.schemas.inbound_email import InboundEmailRead
from app.schemas.outbound import OutboundEmailRead, SendEmailRequest
from app.services import email_service, inbound_service, outbound_service
from app.services.rate_limit import client_ip, hit
from app.web.templating import error_message, flash, render, render_error

router = APIRouter(tags=["前端-公开邮件"])

_PUBLIC_PREVIEW_LENGTH = 180


async def _resolve_by_token(
    request: Request, session: SessionDep, token: str
) -> EmailAddress:
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
    return address


def _parse_recipients(raw: str) -> list[str]:
    """将逗号/分号/换行分隔的收件人文本解析为列表。"""
    normalized = raw.replace("\n", ",").replace(";", ",")
    return [addr.strip() for addr in normalized.split(",") if addr.strip()]


def _hit_public_send_limit(request: Request, token: str) -> None:
    """公开邮件发件独立限流，降低 token 泄露后的滥用风险。"""
    ip = client_ip(request)
    hit(
        "public_mail_send_ip",
        ip,
        settings.PUBLIC_MAIL_SEND_RATE_LIMIT_ATTEMPTS,
        settings.PUBLIC_MAIL_SEND_RATE_LIMIT_WINDOW_SECONDS,
    )
    hit(
        "public_mail_send",
        f"{ip}:{token}",
        settings.PUBLIC_MAIL_SEND_RATE_LIMIT_ATTEMPTS,
        settings.PUBLIC_MAIL_SEND_RATE_LIMIT_WINDOW_SECONDS,
    )


def _inbound_preview(email: InboundEmail) -> str:
    """生成公开收件箱列表预览，避免列表渲染完整正文。"""
    if email.body_text:
        text = " ".join(email.body_text.split())
        if len(text) > _PUBLIC_PREVIEW_LENGTH:
            return f"{text[:_PUBLIC_PREVIEW_LENGTH].rstrip()}..."
        return text
    if email.body_html:
        return "HTML 正文，点击查看完整内容"
    return ""


def _inbound_list_item(email: InboundEmail) -> dict[str, object]:
    """公开收件箱列表项：完整正文只在详情页展示。"""
    return {
        "email": InboundEmailRead.model_validate(email),
        "preview": _inbound_preview(email),
    }


@router.get("/mail/{token}.txt", response_class=PlainTextResponse)
async def public_mail_text(
    request: Request, token: str, session: SessionDep
) -> PlainTextResponse:
    """纯文本格式返回邮箱最新一封邮件，便于程序化读取。"""
    try:
        address = await _resolve_by_token(request, session, token)
    except NotFoundError:
        return PlainTextResponse("链接无效或邮箱不可用", status_code=404)

    email = await inbound_service.get_latest_inbound_by_address(
        session, address.full_address
    )
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
    request: Request,
    token: str,
    session: SessionDep,
    tab: str = Query(default="inbound"),
    inbound_page: int = Query(default=1, ge=1),
    outbound_page: int = Query(default=1, ge=1),
) -> Response:
    """渲染公开邮箱工作台，供人工在不登录情况下查看和发件。"""
    try:
        address = await _resolve_by_token(request, session, token)
    except NotFoundError:
        return render_error(request, 404, "链接无效或邮箱不可用")

    inbound, inbound_total = await inbound_service.list_inbound_emails_by_address(
        session, address.full_address, inbound_page, 20
    )
    outbound, outbound_total = await outbound_service.list_outbound_emails_by_address(
        session, address.full_address, outbound_page, 20
    )
    return render(
        request,
        "public/mail.html",
        user=None,
        layout="landing",
        active=None,
        address=address.full_address,
        token=token,
        tab=tab if tab in {"inbound", "outbound", "compose"} else "inbound",
        inbound_emails=[_inbound_list_item(e) for e in inbound],
        inbound_page=inbound_page,
        inbound_total=inbound_total,
        outbound_emails=[OutboundEmailRead.model_validate(e) for e in outbound],
        outbound_page=outbound_page,
        outbound_total=outbound_total,
        form={},
    )


@router.get("/mail/{token}/inbound/{email_id:int}")
async def public_mail_inbound_detail(
    request: Request, token: str, email_id: int, session: SessionDep
) -> Response:
    """公开邮箱链接查看单封收件详情。"""
    try:
        address = await _resolve_by_token(request, session, token)
        email = await inbound_service.get_inbound_email_by_address_or_404(
            session, address.full_address, email_id
        )
    except NotFoundError:
        return render_error(request, 404, "邮件不存在或链接无效")

    return render(
        request,
        "public/mail_detail.html",
        user=None,
        layout="landing",
        active=None,
        address=address.full_address,
        token=token,
        email=InboundEmailRead.model_validate(email),
    )


@router.post("/mail/{token}/send")
async def public_mail_send(
    request: Request,
    token: str,
    session: SessionDep,
    to: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    text: Annotated[str, Form()] = "",
    html: Annotated[str, Form()] = "",
) -> Response:
    """公开邮箱链接发件；发件人固定为 token 对应邮箱地址。"""
    _hit_public_send_limit(request, token)
    try:
        address = await _resolve_by_token(request, session, token)
        data = SendEmailRequest(
            from_address=address.full_address,
            to=_parse_recipients(to),
            subject=subject,
            text=text or None,
            html=html or None,
        )
        await outbound_service.send_email_from_address(session, address, data)
    except NotFoundError:
        return render_error(request, 404, "链接无效或邮箱不可用")
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/mail/{token}?tab=compose", status_code=303)

    flash(request, "邮件已发送", "success")
    return RedirectResponse(f"/mail/{token}?tab=outbound", status_code=303)

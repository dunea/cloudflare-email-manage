"""发件箱页面路由：列表、详情、撰写并通过 CF Email Sending（Beta）发送。"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.schemas.email_address import EmailAddressRead
from app.schemas.outbound import OutboundEmailRead, SendEmailRequest
from app.services import email_service, outbound_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render, render_error

router = APIRouter(tags=["前端-发件"])


async def _sender_suggestions(
    session: SessionDep, user: CurrentWebUser
) -> list[EmailAddressRead]:
    """当前用户最近的启用邮箱地址建议；不是全集，允许用户手动输入。"""
    addresses, _ = await email_service.list_email_addresses(
        session, user, 1, 25, order="desc"
    )
    return [EmailAddressRead.model_validate(a) for a in addresses if a.is_active]


def _parse_recipients(raw: str) -> list[str]:
    """将逗号/分号/换行分隔的收件人文本解析为列表。"""
    normalized = raw.replace("\n", ",").replace(";", ",")
    return [addr.strip() for addr in normalized.split(",") if addr.strip()]


@router.get("/outbound")
async def list_outbound(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    from_address: str | None = Query(default=None),
) -> Response:
    """发件箱列表。"""
    emails, total = await outbound_service.list_outbound_emails(
        session, user, page, size, from_address
    )
    return render(
        request,
        "outbound/list.html",
        user=user,
        active="outbound",
        emails=[OutboundEmailRead.model_validate(e) for e in emails],
        page=page,
        size=size,
        total=total,
        from_address=from_address or "",
    )


@router.get("/outbound/compose")
async def compose(
    request: Request, user: CurrentWebUser, session: SessionDep
) -> Response:
    """发件撰写页。"""
    return render(
        request,
        "outbound/compose.html",
        user=user,
        active="outbound",
        senders=await _sender_suggestions(session, user),
        form={},
    )


async def _handle_send(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    from_address: Annotated[str, Form()],
    to: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    text: Annotated[str, Form()] = "",
    html: Annotated[str, Form()] = "",
) -> Response:
    """处理发件：校验发件地址归属后调用 CF Email Sending（Beta）。"""
    senders = await _sender_suggestions(session, user)
    try:
        data = SendEmailRequest(
            from_address=from_address,
            to=_parse_recipients(to),
            subject=subject,
            text=text or None,
            html=html or None,
        )
        await outbound_service.send_email(session, user, data)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return render(
            request,
            "outbound/compose.html",
            user=user,
            status_code=400,
            active="outbound",
            senders=senders,
            form={
                "from_address": from_address,
                "to": to,
                "subject": subject,
                "text": text,
                "html": html,
            },
        )
    flash(request, "邮件已发送", "success")
    return RedirectResponse("/outbound", status_code=303)


@router.post("/outbound/compose")
async def send(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    from_address: Annotated[str, Form()],
    to: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    text: Annotated[str, Form()] = "",
    html: Annotated[str, Form()] = "",
) -> Response:
    """处理新发件表单。"""
    return await _handle_send(
        request, user, session, from_address, to, subject, text, html
    )


@router.post("/outbound")
async def send_legacy(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    from_address: Annotated[str, Form()],
    to: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    text: Annotated[str, Form()] = "",
    html: Annotated[str, Form()] = "",
) -> Response:
    """兼容旧撰写页提交路径。"""
    return await _handle_send(
        request, user, session, from_address, to, subject, text, html
    )


@router.get("/outbound/{email_id:int}")
async def outbound_detail(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    email_id: int,
) -> Response:
    """发件详情。"""
    try:
        email = await outbound_service.get_outbound_email_or_404(
            session, email_id, user
        )
    except NotFoundError:
        return render_error(request, 404, "邮件不存在", user=user)
    return render(
        request,
        "outbound/detail.html",
        user=user,
        active="outbound",
        email=OutboundEmailRead.model_validate(email),
    )

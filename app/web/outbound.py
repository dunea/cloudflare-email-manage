"""发件页面路由：撰写并通过 CF Email Sending（Beta）发送。"""

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException
from app.schemas.email_address import EmailAddressRead
from app.schemas.outbound import SendEmailRequest
from app.services import email_service, outbound_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render

router = APIRouter(tags=["前端-发件"])


async def _active_senders(
    session: SessionDep, user: CurrentWebUser
) -> list[EmailAddressRead]:
    """当前用户启用的邮箱地址（可作为发件人）。"""
    addresses, _ = await email_service.list_email_addresses(session, user, 1, 200)
    return [EmailAddressRead.model_validate(a) for a in addresses if a.is_active]


def _parse_recipients(raw: str) -> list[str]:
    """将逗号/分号/换行分隔的收件人文本解析为列表。"""
    normalized = raw.replace("\n", ",").replace(";", ",")
    return [addr.strip() for addr in normalized.split(",") if addr.strip()]


@router.get("/outbound")
async def compose(
    request: Request, user: CurrentWebUser, session: SessionDep
) -> Response:
    """发件撰写页。"""
    return render(
        request,
        "outbound/compose.html",
        user=user,
        active="outbound",
        senders=await _active_senders(session, user),
        form={},
    )


@router.post("/outbound")
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
    """处理发件：校验发件地址归属后调用 CF Email Sending（Beta）。"""
    senders = await _active_senders(session, user)
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

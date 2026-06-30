"""收件箱页面路由：列表与详情（HTML 正文沙箱化展示）。"""

from fastapi import APIRouter, Query, Request, Response

from app.dependencies import SessionDep
from app.exceptions import NotFoundError
from app.schemas.inbound_email import InboundEmailRead
from app.services import inbound_service
from app.web.deps import CurrentWebUser
from app.web.templating import render, render_error

router = APIRouter(tags=["前端-收件箱"])


@router.get("/inbound")
async def list_inbound(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    to_address: str | None = Query(default=None),
) -> Response:
    """收件箱列表（可按收件地址过滤）。"""
    emails, total = await inbound_service.list_inbound_emails(
        session, user, page, size, to_address
    )
    return render(
        request,
        "inbound/list.html",
        user=user,
        active="inbound",
        emails=[InboundEmailRead.model_validate(e) for e in emails],
        page=page,
        size=size,
        total=total,
        to_address=to_address or "",
    )


@router.get("/inbound/{email_id:int}")
async def inbound_detail(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    email_id: int,
) -> Response:
    """收件详情：纯文本正文 + 沙箱化 HTML 预览。"""
    try:
        email = await inbound_service.get_inbound_email_or_404(
            session, email_id, user
        )
    except NotFoundError:
        return render_error(request, 404, "邮件不存在", user=user)
    return render(
        request,
        "inbound/detail.html",
        user=user,
        active="inbound",
        email=InboundEmailRead.model_validate(email),
    )

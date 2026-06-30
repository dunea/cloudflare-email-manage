"""发件 路由：通过 CF Email Sending（Beta）发送邮件。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
支持 JWT 或 X-API-Key 两种认证方式（程序化收发）。
"""

from fastapi import APIRouter, Query

from app.dependencies import CurrentUser, RequestUserSend, SessionDep
from app.schemas.common import ApiResponse, PageData
from app.schemas.outbound import OutboundEmailRead, SendEmailRequest, SendEmailResult
from app.services import outbound_service

router = APIRouter(prefix="/outbound", tags=["发件"])


@router.post(
    "/send",
    response_model=ApiResponse[SendEmailResult],
    summary="发送邮件（CF Email Sending Beta）",
)
async def send_email(
    data: SendEmailRequest, current_user: RequestUserSend, session: SessionDep
) -> ApiResponse[SendEmailResult]:
    """从平台内已管理的邮箱地址发送邮件。"""
    result = await outbound_service.send_email(session, current_user, data)
    return ApiResponse(data=result)


@router.get(
    "",
    response_model=ApiResponse[PageData[OutboundEmailRead]],
    summary="发件箱列表",
)
async def list_outbound_emails(
    current_user: CurrentUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    from_address: str | None = Query(default=None, description="按发件地址过滤"),
) -> ApiResponse[PageData[OutboundEmailRead]]:
    """分页查询当前用户发件箱，可按发件地址过滤。"""
    emails, total = await outbound_service.list_outbound_emails(
        session, current_user, page, size, from_address
    )
    page_data = PageData[OutboundEmailRead](
        total=total,
        page=page,
        size=size,
        items=[OutboundEmailRead.model_validate(e) for e in emails],
    )
    return ApiResponse(data=page_data)


@router.get(
    "/{email_id}",
    response_model=ApiResponse[OutboundEmailRead],
    summary="获取发件邮件",
)
async def get_outbound_email(
    email_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[OutboundEmailRead]:
    """获取指定发件邮件详情。"""
    email = await outbound_service.get_outbound_email_or_404(
        session, email_id, current_user
    )
    return ApiResponse(data=OutboundEmailRead.model_validate(email))

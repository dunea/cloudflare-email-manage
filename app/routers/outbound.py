"""发件 路由：通过 CF Email Sending（Beta）发送邮件。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
支持 JWT 或 X-API-Key 两种认证方式（程序化收发）。
"""

from fastapi import APIRouter

from app.dependencies import RequestUser, SessionDep
from app.schemas.common import ApiResponse
from app.schemas.outbound import SendEmailRequest, SendEmailResult
from app.services import outbound_service

router = APIRouter(prefix="/outbound", tags=["发件"])


@router.post(
    "/send",
    response_model=ApiResponse[SendEmailResult],
    summary="发送邮件（CF Email Sending Beta）",
)
async def send_email(
    data: SendEmailRequest, current_user: RequestUser, session: SessionDep
) -> ApiResponse[SendEmailResult]:
    """从平台内已管理的邮箱地址发送邮件。"""
    result = await outbound_service.send_email(session, current_user, data)
    return ApiResponse(data=result)

"""收件 路由：Webhook 收件端点与已收邮件查询。

Webhook 端点不走常规鉴权，改为校验 X-Webhook-Signature 签名；
查询端点需 JWT 认证并按归属隔离。
"""

from fastapi import APIRouter, Query, Request, status

from app.config import settings
from app.dependencies import RequestUserReadInbound, SessionDep
from app.exceptions import AppException
from app.schemas.common import ApiResponse, PageData
from app.schemas.inbound_email import InboundEmailRead
from app.services import inbound_service

router = APIRouter(prefix="/inbound", tags=["收件"])


@router.post(
    "/webhook",
    response_model=ApiResponse[InboundEmailRead],
    summary="Webhook 收件端点（校验签名）",
)
async def receive_webhook(
    request: Request, session: SessionDep
) -> ApiResponse[InboundEmailRead]:
    """接收 CF Worker 转发的邮件：校验签名后入库。"""
    raw_body = await request.body()
    if len(raw_body) > settings.WEBHOOK_MAX_BYTES:
        raise AppException(
            "Webhook 载荷过大",
            code=1413,
            http_status=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    signature = request.headers.get(inbound_service.WEBHOOK_SIGNATURE_HEADER)
    email = await inbound_service.process_webhook(session, raw_body, signature)
    return ApiResponse(data=InboundEmailRead.model_validate(email))


@router.get(
    "",
    response_model=ApiResponse[PageData[InboundEmailRead]],
    summary="收件列表",
)
async def list_inbound_emails(
    current_user: RequestUserReadInbound,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    to_address: str | None = Query(default=None, description="按收件地址过滤"),
) -> ApiResponse[PageData[InboundEmailRead]]:
    """分页查询当前用户收到的邮件，可按收件地址过滤。"""
    emails, total = await inbound_service.list_inbound_emails(
        session, current_user, page, size, to_address
    )
    page_data = PageData[InboundEmailRead](
        total=total,
        page=page,
        size=size,
        items=[InboundEmailRead.model_validate(e) for e in emails],
    )
    return ApiResponse(data=page_data)


@router.get(
    "/{email_id}",
    response_model=ApiResponse[InboundEmailRead],
    summary="获取收件邮件",
)
async def get_inbound_email(
    email_id: int, current_user: RequestUserReadInbound, session: SessionDep
) -> ApiResponse[InboundEmailRead]:
    """获取指定收件邮件详情。"""
    email = await inbound_service.get_inbound_email_or_404(
        session, email_id, current_user
    )
    return ApiResponse(data=InboundEmailRead.model_validate(email))

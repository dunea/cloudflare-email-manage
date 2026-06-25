"""邮箱地址 路由：邮箱地址 CRUD。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
"""

from fastapi import APIRouter, Query, status

from app.dependencies import CurrentUser, SessionDep
from app.schemas.common import ApiResponse, PageData
from app.schemas.email_address import (
    EmailAddressCreate,
    EmailAddressRead,
    EmailAddressUpdate,
)
from app.services import email_service

router = APIRouter(prefix="/email-addresses", tags=["邮箱地址"])


@router.post(
    "",
    response_model=ApiResponse[EmailAddressRead],
    status_code=status.HTTP_201_CREATED,
    summary="创建邮箱地址",
)
async def create_email_address(
    data: EmailAddressCreate, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[EmailAddressRead]:
    """在指定域名下创建邮箱地址。"""
    email_address = await email_service.create_email_address(
        session, current_user, data
    )
    return ApiResponse(data=EmailAddressRead.model_validate(email_address))


@router.get(
    "",
    response_model=ApiResponse[PageData[EmailAddressRead]],
    summary="邮箱地址列表",
)
async def list_email_addresses(
    current_user: CurrentUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    domain_id: int | None = Query(default=None, ge=1),
) -> ApiResponse[PageData[EmailAddressRead]]:
    """分页查询当前用户的邮箱地址，可按域名过滤。"""
    addresses, total = await email_service.list_email_addresses(
        session, current_user, page, size, domain_id
    )
    page_data = PageData[EmailAddressRead](
        total=total,
        page=page,
        size=size,
        items=[EmailAddressRead.model_validate(a) for a in addresses],
    )
    return ApiResponse(data=page_data)


@router.get(
    "/{email_address_id}",
    response_model=ApiResponse[EmailAddressRead],
    summary="获取邮箱地址",
)
async def get_email_address(
    email_address_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[EmailAddressRead]:
    """获取指定邮箱地址详情。"""
    email_address = await email_service.get_email_address_or_404(
        session, email_address_id, current_user
    )
    return ApiResponse(data=EmailAddressRead.model_validate(email_address))


@router.patch(
    "/{email_address_id}",
    response_model=ApiResponse[EmailAddressRead],
    summary="更新邮箱地址",
)
async def update_email_address(
    email_address_id: int,
    data: EmailAddressUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> ApiResponse[EmailAddressRead]:
    """更新邮箱地址（启用/停用）。"""
    email_address = await email_service.get_email_address_or_404(
        session, email_address_id, current_user
    )
    updated = await email_service.update_email_address(session, email_address, data)
    return ApiResponse(data=EmailAddressRead.model_validate(updated))


@router.delete(
    "/{email_address_id}",
    response_model=ApiResponse[None],
    summary="删除邮箱地址",
)
async def delete_email_address(
    email_address_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[None]:
    """软删除邮箱地址。"""
    email_address = await email_service.get_email_address_or_404(
        session, email_address_id, current_user
    )
    await email_service.delete_email_address(session, email_address)
    return ApiResponse(message="已删除")


@router.post(
    "/{email_address_id}/reset-token",
    response_model=ApiResponse[EmailAddressRead],
    summary="重置公开查询令牌",
)
async def reset_public_token(
    email_address_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[EmailAddressRead]:
    """重置邮箱地址的公开查询令牌，旧 /mail/{token} 链接立即失效。"""
    email_address = await email_service.get_email_address_or_404(
        session, email_address_id, current_user
    )
    updated = await email_service.regenerate_public_token(session, email_address)
    return ApiResponse(data=EmailAddressRead.model_validate(updated), message="已重置查询链接")

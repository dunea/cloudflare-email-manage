"""转发目标地址 路由：目标地址管理（调用 CF Email Routing）。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
"""

from fastapi import APIRouter, Query, status

from app.dependencies import CurrentUser, SessionDep
from app.schemas.common import ApiResponse, PageData
from app.schemas.destination_address import (
    DestinationAddressCreate,
    DestinationAddressRead,
)
from app.services import destination_service

router = APIRouter(prefix="/destination-addresses", tags=["转发目标地址"])


@router.post(
    "",
    response_model=ApiResponse[DestinationAddressRead],
    status_code=status.HTTP_201_CREATED,
    summary="添加转发目标地址",
)
async def create_destination_address(
    data: DestinationAddressCreate, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[DestinationAddressRead]:
    """添加转发目标地址，Cloudflare 会向该邮箱发送验证邮件。"""
    address = await destination_service.add_destination_address(
        session, current_user, data
    )
    return ApiResponse(data=DestinationAddressRead.model_validate(address))


@router.get(
    "",
    response_model=ApiResponse[PageData[DestinationAddressRead]],
    summary="转发目标地址列表",
)
async def list_destination_addresses(
    current_user: CurrentUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    cf_account_id: int | None = Query(default=None, ge=1),
    verified: bool | None = Query(default=None),
) -> ApiResponse[PageData[DestinationAddressRead]]:
    """分页查询当前用户的目标地址，可按账号与验证状态过滤。"""
    addresses, total = await destination_service.list_destination_addresses(
        session, current_user, page, size, cf_account_id, verified
    )
    page_data = PageData[DestinationAddressRead](
        total=total,
        page=page,
        size=size,
        items=[DestinationAddressRead.model_validate(a) for a in addresses],
    )
    return ApiResponse(data=page_data)


@router.post(
    "/sync",
    response_model=ApiResponse[list[DestinationAddressRead]],
    summary="同步目标地址验证状态",
)
async def sync_destination_addresses(
    current_user: CurrentUser,
    session: SessionDep,
    cf_account_id: int = Query(..., ge=1, description="要同步的 CF 账号 id"),
) -> ApiResponse[list[DestinationAddressRead]]:
    """从 Cloudflare 同步目标地址的验证状态到本地。"""
    addresses = await destination_service.sync_destination_addresses(
        session, current_user, cf_account_id
    )
    return ApiResponse(
        data=[DestinationAddressRead.model_validate(a) for a in addresses]
    )


@router.delete(
    "/{address_id}",
    response_model=ApiResponse[None],
    summary="删除转发目标地址",
)
async def delete_destination_address(
    address_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[None]:
    """删除目标地址，同步在 Cloudflare 删除（会停用引用该地址的路由规则）。"""
    address = await destination_service.get_destination_address_or_404(
        session, address_id, current_user
    )
    await destination_service.delete_destination_address(session, address)
    return ApiResponse(message="已删除")

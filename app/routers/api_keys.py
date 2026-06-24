"""API Key 路由：用户程序化访问密钥的管理。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
原始 API Key 仅在创建时返回一次，库中仅存哈希。
"""

from fastapi import APIRouter, Query, status

from app.dependencies import CurrentUser, SessionDep
from app.schemas.api_key import (
    APIKeyCreate,
    APIKeyCreated,
    APIKeyRead,
    APIKeyUpdate,
)
from app.schemas.common import ApiResponse, PageData
from app.services import api_key_service

router = APIRouter(prefix="/api-keys", tags=["API Key"])


@router.post(
    "",
    response_model=ApiResponse[APIKeyCreated],
    status_code=status.HTTP_201_CREATED,
    summary="创建 API Key",
)
async def create_api_key(
    data: APIKeyCreate, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[APIKeyCreated]:
    """创建 API Key，返回包含原始 key 的响应（仅此一次可见）。"""
    api_key, raw_key = await api_key_service.create_api_key(
        session, current_user, data
    )
    created = APIKeyCreated(
        **APIKeyRead.model_validate(api_key).model_dump(),
        key=raw_key,
    )
    return ApiResponse(data=created)


@router.get(
    "",
    response_model=ApiResponse[PageData[APIKeyRead]],
    summary="API Key 列表",
)
async def list_api_keys(
    current_user: CurrentUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[PageData[APIKeyRead]]:
    """分页查询当前用户的 API Key（不含原始 key 与哈希）。"""
    keys, total = await api_key_service.list_api_keys(
        session, current_user, page, size
    )
    page_data = PageData[APIKeyRead](
        total=total,
        page=page,
        size=size,
        items=[APIKeyRead.model_validate(k) for k in keys],
    )
    return ApiResponse(data=page_data)


@router.get(
    "/{key_id}",
    response_model=ApiResponse[APIKeyRead],
    summary="获取 API Key",
)
async def get_api_key(
    key_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[APIKeyRead]:
    """获取指定 API Key 详情。"""
    api_key = await api_key_service.get_api_key_or_404(
        session, key_id, current_user
    )
    return ApiResponse(data=APIKeyRead.model_validate(api_key))


@router.patch(
    "/{key_id}",
    response_model=ApiResponse[APIKeyRead],
    summary="更新 API Key",
)
async def update_api_key(
    key_id: int,
    data: APIKeyUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> ApiResponse[APIKeyRead]:
    """更新 API Key（重命名 / 启用停用）。"""
    api_key = await api_key_service.get_api_key_or_404(
        session, key_id, current_user
    )
    updated = await api_key_service.update_api_key(session, api_key, data)
    return ApiResponse(data=APIKeyRead.model_validate(updated))


@router.delete(
    "/{key_id}",
    response_model=ApiResponse[None],
    summary="删除 API Key",
)
async def delete_api_key(
    key_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[None]:
    """删除（吊销）API Key。"""
    api_key = await api_key_service.get_api_key_or_404(
        session, key_id, current_user
    )
    await api_key_service.delete_api_key(session, api_key)
    return ApiResponse(message="已删除")

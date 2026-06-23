"""用户 路由：当前用户信息管理与（管理员）用户列表。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
"""

from fastapi import APIRouter, Query

from app.dependencies import AdminUser, CurrentUser, SessionDep
from app.schemas.common import ApiResponse, PageData
from app.schemas.user import UserRead, UserUpdate
from app.services import user_service

router = APIRouter(prefix="/users", tags=["用户"])


@router.get("/me", response_model=ApiResponse[UserRead], summary="获取当前用户信息")
async def read_me(current_user: CurrentUser) -> ApiResponse[UserRead]:
    """返回当前登录用户信息。"""
    return ApiResponse(data=UserRead.model_validate(current_user))


@router.patch("/me", response_model=ApiResponse[UserRead], summary="更新当前用户信息")
async def update_me(
    data: UserUpdate, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[UserRead]:
    """更新当前用户的邮箱或密码。"""
    user = await user_service.update_user(session, current_user, data)
    return ApiResponse(data=UserRead.model_validate(user))


@router.get(
    "",
    response_model=ApiResponse[PageData[UserRead]],
    summary="用户列表（管理员）",
)
async def list_all_users(
    _: AdminUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[PageData[UserRead]]:
    """分页查询所有用户，仅管理员可用。"""
    users, total = await user_service.list_users(session, page, size)
    page_data = PageData[UserRead](
        total=total,
        page=page,
        size=size,
        items=[UserRead.model_validate(u) for u in users],
    )
    return ApiResponse(data=page_data)


@router.get(
    "/{user_id}",
    response_model=ApiResponse[UserRead],
    summary="获取指定用户（管理员）",
)
async def get_user(
    user_id: int, _: AdminUser, session: SessionDep
) -> ApiResponse[UserRead]:
    """按 id 查询用户，仅管理员可用。"""
    user = await user_service.get_user_or_404(session, user_id)
    return ApiResponse(data=UserRead.model_validate(user))

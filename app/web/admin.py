"""管理后台页面路由：用户列表与详情（仅管理员）。"""

from fastapi import APIRouter, Query, Request, Response

from app.dependencies import SessionDep
from app.exceptions import NotFoundError
from app.schemas.user import UserRead
from app.services import user_service
from app.web.deps import AdminWebUser
from app.web.templating import render, render_error

router = APIRouter(tags=["前端-管理后台"])


@router.get("/admin/users")
async def list_users(
    request: Request,
    admin: AdminWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> Response:
    """用户列表（仅管理员）。"""
    users, total = await user_service.list_users(session, page, size)
    return render(
        request,
        "admin/users.html",
        user=admin,
        active="admin_users",
        users=[UserRead.model_validate(u) for u in users],
        page=page,
        size=size,
        total=total,
    )


@router.get("/admin/users/{user_id:int}")
async def user_detail(
    request: Request,
    admin: AdminWebUser,
    session: SessionDep,
    user_id: int,
) -> Response:
    """用户详情（仅管理员）。"""
    try:
        target = await user_service.get_user_or_404(session, user_id)
    except NotFoundError:
        return render_error(request, 404, "用户不存在", user=admin)
    return render(
        request,
        "admin/user_detail.html",
        user=admin,
        active="admin_users",
        target=UserRead.model_validate(target),
    )

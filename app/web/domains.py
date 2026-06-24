"""域名页面路由：列表、详情，以及（管理员）平台域名分配管理。

复用 app/services/domain_service、email_service、user_service。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.schemas.domain import DomainRead
from app.schemas.email_address import EmailAddressRead
from app.services import domain_service, email_service, user_service
from app.web.deps import AdminWebUser, CurrentWebUser
from app.web.templating import error_message, flash, render, render_error

router = APIRouter(tags=["前端-域名"])


@router.get("/domains")
async def list_domains(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> Response:
    """域名列表（管理员可见全部，普通用户见自有 + 被分配）。"""
    domains, total = await domain_service.list_domains_for_user(
        session, user, page, size
    )
    return render(
        request,
        "domains/list.html",
        user=user,
        active="domains",
        domains=[DomainRead.model_validate(d) for d in domains],
        page=page,
        size=size,
        total=total,
    )


@router.get("/domains/{domain_id:int}")
async def domain_detail(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    domain_id: int,
) -> Response:
    """域名详情：域名下邮箱地址 +（管理员对平台域名）分配管理。"""
    try:
        domain = await domain_service.get_domain_or_404(session, domain_id, user)
    except NotFoundError:
        return render_error(request, 404, "域名不存在", user=user)

    addresses, _ = await email_service.list_email_addresses(
        session, user, 1, 100, domain_id
    )

    assignments: list[dict[str, object]] = []
    assignable_users: list[object] = []
    can_assign = user.role == "admin" and domain.owner_type == "platform"
    if can_assign:
        rows = await domain_service.list_domain_assignments(session, domain_id)
        users, _ = await user_service.list_users(session, 1, 200)
        user_map = {u.id: u for u in users}
        assignments = [
            {
                "user_id": r.user_id,
                "username": (
                    user_map[r.user_id].username
                    if r.user_id in user_map
                    else f"#{r.user_id}"
                ),
            }
            for r in rows
        ]
        assigned_ids = {r.user_id for r in rows}
        assignable_users = [
            u for u in users if u.id != user.id and u.id not in assigned_ids
        ]

    return render(
        request,
        "domains/detail.html",
        user=user,
        active="domains",
        domain=DomainRead.model_validate(domain),
        addresses=[EmailAddressRead.model_validate(a) for a in addresses],
        can_assign=can_assign,
        assignments=assignments,
        assignable_users=assignable_users,
    )


@router.post("/domains/{domain_id:int}/assignments")
async def assign_domain(
    request: Request,
    _: AdminWebUser,
    session: SessionDep,
    domain_id: int,
    target_user_id: Annotated[int, Form(alias="user_id")],
) -> Response:
    """将平台域名分配给用户（仅管理员）。"""
    try:
        await domain_service.assign_domain(session, domain_id, target_user_id)
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/domains/{domain_id}", status_code=303)
    flash(request, "已分配域名给该用户", "success")
    return RedirectResponse(f"/domains/{domain_id}", status_code=303)


@router.post("/domains/{domain_id:int}/assignments/{target_user_id:int}/delete")
async def unassign_domain(
    request: Request,
    _: AdminWebUser,
    session: SessionDep,
    domain_id: int,
    target_user_id: int,
) -> Response:
    """取消域名分配（仅管理员）。"""
    try:
        await domain_service.unassign_domain(session, domain_id, target_user_id)
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/domains/{domain_id}", status_code=303)
    flash(request, "已取消分配", "success")
    return RedirectResponse(f"/domains/{domain_id}", status_code=303)

"""域名页面路由：列表、详情，以及域名所有者共享域名给其他用户。

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
from app.web.deps import CurrentWebUser
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
    """域名列表（管理员可见全部，普通用户见自有 + 被共享的）。"""
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
    """域名详情：域名下邮箱地址 +（域名所有者）共享管理。"""
    try:
        domain = await domain_service.get_domain_or_404(session, domain_id, user)
    except NotFoundError:
        return render_error(request, 404, "域名不存在", user=user)

    addresses, address_total = await email_service.list_email_addresses(
        session, user, 1, 20, domain_id
    )

    # 判断当前用户是否为域名所有者（可共享给他人）
    from app.services.domain_service import _is_domain_owner

    can_assign = user.role == "admin" or await _is_domain_owner(
        session, user, domain
    )

    assignments: list[dict[str, object]] = []
    if can_assign:
        rows = await domain_service.list_domain_assignments(session, domain_id)
        assigned_users = await user_service.get_users_by_ids(
            session, [r.user_id for r in rows]
        )
        user_map = {u.id: u for u in assigned_users}
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

    return render(
        request,
        "domains/detail.html",
        user=user,
        active="domains",
        domain=DomainRead.model_validate(domain),
        addresses=[EmailAddressRead.model_validate(a) for a in addresses],
        address_total=address_total,
        can_assign=can_assign,
        assignments=assignments,
    )


@router.post("/domains/{domain_id:int}/assignments")
async def assign_domain(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    domain_id: int,
    username: Annotated[str, Form()],
) -> Response:
    """将域名共享给用户（域名所有者可操作，按用户名精确查找目标用户）。"""
    try:
        await domain_service.assign_domain_by_username(
            session, domain_id, username.strip(), user
        )
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/domains/{domain_id}", status_code=303)
    flash(request, f"已共享域名给用户「{username.strip()}」", "success")
    return RedirectResponse(f"/domains/{domain_id}", status_code=303)


@router.post("/domains/{domain_id:int}/assignments/{target_user_id:int}/delete")
async def unassign_domain(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    domain_id: int,
    target_user_id: int,
) -> Response:
    """取消域名共享（域名所有者可操作）。"""
    try:
        await domain_service.unassign_domain(
            session, domain_id, target_user_id, user
        )
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/domains/{domain_id}", status_code=303)
    flash(request, "已取消共享", "success")
    return RedirectResponse(f"/domains/{domain_id}", status_code=303)

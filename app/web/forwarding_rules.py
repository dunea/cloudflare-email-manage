"""转发规则页面路由：列表、创建、启用/停用、删除。

复用 app/services/forwarding_service（创建/删除会同步到 CF Email Routing）。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.schemas.email_address import EmailAddressRead
from app.schemas.forwarding_rule import (
    ForwardingRuleCreate,
    ForwardingRuleRead,
    ForwardingRuleUpdate,
)
from app.services import email_service, forwarding_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render

router = APIRouter(tags=["前端-转发规则"])


async def _email_options(
    session: SessionDep, user: CurrentWebUser
) -> list[EmailAddressRead]:
    """当前用户的邮箱地址（作为转发源选项）。"""
    addresses, _ = await email_service.list_email_addresses(session, user, 1, 200)
    return [EmailAddressRead.model_validate(a) for a in addresses]


@router.get("/forwarding-rules")
async def list_forwarding_rules(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    email_address_id: int | None = Query(default=None, ge=1),
) -> Response:
    """转发规则列表（可按源邮箱过滤），内置创建表单。"""
    rules, total = await forwarding_service.list_forwarding_rules(
        session, user, page, size, email_address_id
    )
    options = await _email_options(session, user)
    return render(
        request,
        "forwarding_rules/list.html",
        user=user,
        active="forwarding_rules",
        rules=[ForwardingRuleRead.model_validate(r) for r in rules],
        email_map={o.id: o.full_address for o in options},
        options=options,
        page=page,
        size=size,
        total=total,
        email_address_id=email_address_id,
    )


@router.post("/forwarding-rules")
async def create_forwarding_rule(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    email_address_id: Annotated[int, Form()],
    destination_email: Annotated[str, Form()],
) -> Response:
    """创建转发规则（同步到 CF）。"""
    try:
        data = ForwardingRuleCreate(
            email_address_id=email_address_id, destination_email=destination_email
        )
        await forwarding_service.create_forwarding_rule(session, user, data)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse("/forwarding-rules", status_code=303)
    flash(request, "已创建转发规则", "success")
    return RedirectResponse("/forwarding-rules", status_code=303)


@router.post("/forwarding-rules/{rule_id:int}/toggle")
async def toggle_forwarding_rule(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    rule_id: int,
) -> Response:
    """启用/停用转发规则。"""
    try:
        rule = await forwarding_service.get_forwarding_rule_or_404(
            session, rule_id, user
        )
    except NotFoundError:
        flash(request, "转发规则不存在", "error")
        return RedirectResponse("/forwarding-rules", status_code=303)
    await forwarding_service.update_forwarding_rule(
        session, rule, ForwardingRuleUpdate(is_active=not rule.is_active)
    )
    flash(request, "已更新转发规则状态", "success")
    return RedirectResponse("/forwarding-rules", status_code=303)


@router.post("/forwarding-rules/{rule_id:int}/delete")
async def delete_forwarding_rule(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    rule_id: int,
) -> Response:
    """删除转发规则（同步删除 CF 规则）。"""
    try:
        rule = await forwarding_service.get_forwarding_rule_or_404(
            session, rule_id, user
        )
        await forwarding_service.delete_forwarding_rule(session, rule)
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse("/forwarding-rules", status_code=303)
    flash(request, "已删除转发规则", "success")
    return RedirectResponse("/forwarding-rules", status_code=303)

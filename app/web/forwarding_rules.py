"""转发规则页面路由：列表、创建、启用/停用、删除。

复用 app/services/forwarding_service（创建/删除会同步到 CF Email Routing）。
创建表单中：源邮箱仅显示未绑定转发规则的地址；目标邮箱从已验证的目标地址
中选取，并按源邮箱所属 CF 账号联动过滤。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import exists, func, select

from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.models import Domain, EmailAddress, ForwardingRule
from app.schemas.destination_address import DestinationAddressRead
from app.schemas.email_address import EmailAddressRead
from app.schemas.forwarding_rule import (
    ForwardingRuleCreate,
    ForwardingRuleRead,
    ForwardingRuleUpdate,
)
from app.services import destination_service, email_service, forwarding_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render

router = APIRouter(tags=["前端-转发规则"])


async def _email_options(
    session: SessionDep, user: CurrentWebUser
) -> tuple[list[EmailAddressRead], int, int]:
    """当前用户可用作转发源的邮箱地址建议（排除已有未删除转发规则的地址）。

    同一源邮箱不能同时绑定两个转发目标，故已绑定的地址不再出现在选项中。
    返回的列表只是最近建议，不是全集；用户可手动输入完整地址。
    """
    source_base = select(EmailAddress).where(
        EmailAddress.is_deleted.is_(False),
        EmailAddress.is_active.is_(True),
    )
    if user.role != "admin":
        source_base = source_base.where(EmailAddress.user_id == user.id)

    bound_exists = exists().where(
        ForwardingRule.email_address_id == EmailAddress.id,
        ForwardingRule.is_deleted.is_(False),
    )
    available_base = source_base.where(~bound_exists)
    source_total = (
        await session.execute(select(func.count()).select_from(source_base.subquery()))
    ).scalar_one()
    available_total = (
        await session.execute(
            select(func.count()).select_from(available_base.subquery())
        )
    ).scalar_one()
    result = await session.execute(
        available_base.order_by(EmailAddress.id.desc()).limit(25)
    )
    return [
        EmailAddressRead.model_validate(a) for a in result.scalars().all()
    ], source_total, available_total


async def _email_account_map(
    session: SessionDep, options: list[EmailAddressRead]
) -> dict[int, int]:
    """邮箱地址 id → 所属 CF 账号 id 映射，用于前端目标地址联动过滤。"""
    if not options:
        return {}
    rows = (
        await session.execute(
            select(Domain.id, Domain.cf_account_id).where(
                Domain.id.in_([o.domain_id for o in options])
            )
        )
    ).all()
    domain_account = {row[0]: row[1] for row in rows}
    return {o.id: domain_account.get(o.domain_id, 0) for o in options}


async def _verified_dest_options(
    session: SessionDep, user: CurrentWebUser
) -> list[DestinationAddressRead]:
    """当前用户已验证的目标地址建议。"""
    addresses, _ = await destination_service.list_destination_addresses(
        session, user, 1, 50, verified=True
    )
    return [DestinationAddressRead.model_validate(a) for a in addresses]


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
    options, source_total, available_source_total = await _email_options(session, user)
    dest_options = await _verified_dest_options(session, user)
    email_account = await _email_account_map(session, options)
    rule_email_ids = [rule.email_address_id for rule in rules]
    rows = (
        await session.execute(
            select(EmailAddress).where(EmailAddress.id.in_(rule_email_ids))
        )
        if rule_email_ids
        else None
    )
    all_email_map = {a.id: a.full_address for a in rows.scalars().all()} if rows else {}
    # 目标地址按 CF 账号分组，供前端联动过滤。
    # 键统一转为字符串以兼容 JSON（Jinja tojson 会将 int 键转为字符串），
    # 值转为 plain dict 列表以避免 Pydantic 模型序列化问题。
    dests_by_account: dict[str, list[dict[str, str]]] = {}
    for d in dest_options:
        dests_by_account.setdefault(str(d.cf_account_id), []).append(
            {"email": d.email}
        )
    email_account_json = {str(k): str(v) for k, v in email_account.items()}
    return render(
        request,
        "forwarding_rules/list.html",
        user=user,
        active="forwarding_rules",
        rules=[ForwardingRuleRead.model_validate(r) for r in rules],
        email_map=all_email_map,
        options=options,
        source_total=source_total,
        available_source_total=available_source_total,
        email_account=email_account_json,
        dest_options=dest_options,
        dests_by_account=dests_by_account,
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
    email_address_id: Annotated[int | None, Form()] = None,
    source_email: Annotated[str, Form()] = "",
    destination_email: Annotated[str, Form()] = "",
) -> Response:
    """创建转发规则（同步到 CF）。"""
    try:
        if email_address_id is None:
            email_address = await email_service.get_email_address_by_full_address_or_404(
                session, source_email.strip(), user
            )
            email_address_id = email_address.id
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

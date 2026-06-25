"""邮箱地址页面路由：列表、创建、启用/停用、删除。

复用 app/services/email_service 与 domain_service。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.schemas.domain import DomainRead
from app.schemas.email_address import (
    EmailAddressCreate,
    EmailAddressRead,
    EmailAddressUpdate,
)
from app.services import domain_service, email_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render

router = APIRouter(tags=["前端-邮箱地址"])


@router.get("/email-addresses")
async def list_email_addresses(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    domain_id: str | None = Query(default=None),
) -> Response:
    """邮箱地址列表（可按域名过滤），并内置创建表单。"""
    # 兼容前端"全部域名"提交的空字符串，空值视为不过滤；
    # 非空值必须为 >= 1 的整数，否则忽略过滤
    parsed_domain_id: int | None = None
    if domain_id:
        try:
            value = int(domain_id)
        except ValueError:
            value = None
        if value is not None and value >= 1:
            parsed_domain_id = value
    addresses, total = await email_service.list_email_addresses(
        session, user, page, size, parsed_domain_id
    )
    domains, _ = await domain_service.list_domains_for_user(session, user, 1, 100)
    return render(
        request,
        "email_addresses/list.html",
        user=user,
        active="email_addresses",
        addresses=[EmailAddressRead.model_validate(a) for a in addresses],
        domains=[DomainRead.model_validate(d) for d in domains],
        page=page,
        size=size,
        total=total,
        domain_id=parsed_domain_id,
    )


@router.post("/email-addresses")
async def create_email_address(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    domain_id: Annotated[int, Form()],
    local_part: Annotated[str, Form()],
) -> Response:
    """创建邮箱地址。"""
    try:
        data = EmailAddressCreate(domain_id=domain_id, local_part=local_part)
        await email_service.create_email_address(session, user, data)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse("/email-addresses", status_code=303)
    flash(request, "已创建邮箱地址", "success")
    return RedirectResponse("/email-addresses", status_code=303)


@router.post("/email-addresses/{email_id:int}/toggle")
async def toggle_email_address(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    email_id: int,
) -> Response:
    """启用/停用邮箱地址。"""
    try:
        email = await email_service.get_email_address_or_404(session, email_id, user)
    except NotFoundError:
        flash(request, "邮箱地址不存在", "error")
        return RedirectResponse("/email-addresses", status_code=303)
    await email_service.update_email_address(
        session, email, EmailAddressUpdate(is_active=not email.is_active)
    )
    flash(request, "已更新邮箱地址状态", "success")
    return RedirectResponse("/email-addresses", status_code=303)


@router.post("/email-addresses/{email_id:int}/delete")
async def delete_email_address(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    email_id: int,
) -> Response:
    """删除邮箱地址。"""
    try:
        email = await email_service.get_email_address_or_404(session, email_id, user)
    except NotFoundError:
        flash(request, "邮箱地址不存在", "error")
        return RedirectResponse("/email-addresses", status_code=303)
    await email_service.delete_email_address(session, email)
    flash(request, "已删除邮箱地址", "success")
    return RedirectResponse("/email-addresses", status_code=303)


@router.post("/email-addresses/{email_id:int}/reset-token")
async def reset_public_token(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    email_id: int,
) -> Response:
    """重置邮箱地址的公开查询令牌（旧链接立即失效）。"""
    try:
        email = await email_service.get_email_address_or_404(session, email_id, user)
    except NotFoundError:
        flash(request, "邮箱地址不存在", "error")
        return RedirectResponse("/email-addresses", status_code=303)
    await email_service.regenerate_public_token(session, email)
    flash(request, "已重置查询链接，旧链接已失效", "success")
    return RedirectResponse("/email-addresses", status_code=303)

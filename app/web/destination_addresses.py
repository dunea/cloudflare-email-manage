"""转发目标地址页面路由：列表、添加、同步验证状态、删除。

复用 app/services/destination_service（添加/同步会调用 CF Email Routing）。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.schemas.cf_account import CFAccountRead
from app.schemas.destination_address import (
    DestinationAddressCreate,
    DestinationAddressRead,
)
from app.services import cf_account_service, destination_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render

router = APIRouter(tags=["前端-目标地址"])


async def _cf_account_options(
    session: SessionDep, user: CurrentWebUser
) -> list[CFAccountRead]:
    """当前用户的 CF 账号（作为目标地址归属选项）。"""
    accounts, _ = await cf_account_service.list_cf_accounts(session, user, 1, 200)
    return [CFAccountRead.model_validate(a) for a in accounts]


@router.get("/destination-addresses")
async def list_destination_addresses(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    cf_account_id: int | None = Query(default=None, ge=1),
) -> Response:
    """目标地址列表（可按 CF 账号过滤），内置添加表单与同步入口。"""
    addresses, total = await destination_service.list_destination_addresses(
        session, user, page, size, cf_account_id
    )
    accounts = await _cf_account_options(session, user)
    account_map = {a.id: a.name for a in accounts}
    return render(
        request,
        "destination_addresses/list.html",
        user=user,
        active="destination_addresses",
        addresses=[DestinationAddressRead.model_validate(a) for a in addresses],
        accounts=accounts,
        account_map=account_map,
        page=page,
        size=size,
        total=total,
        cf_account_id=cf_account_id,
    )


@router.post("/destination-addresses")
async def create_destination_address(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    cf_account_id: Annotated[int, Form()],
    email: Annotated[str, Form()],
) -> Response:
    """添加转发目标地址（CF 会向该邮箱发送验证邮件）。"""
    try:
        data = DestinationAddressCreate(cf_account_id=cf_account_id, email=email)
        await destination_service.add_destination_address(session, user, data)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse("/destination-addresses", status_code=303)
    flash(
        request,
        "已添加目标地址，验证邮件已发送，请前往该邮箱完成验证",
        "success",
    )
    return RedirectResponse("/destination-addresses", status_code=303)


@router.post("/destination-addresses/sync")
async def sync_destination_addresses(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    cf_account_id: Annotated[int, Form()],
) -> Response:
    """从 Cloudflare 同步目标地址的验证状态。"""
    try:
        addresses = await destination_service.sync_destination_addresses(
            session, user, cf_account_id
        )
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse("/destination-addresses", status_code=303)
    flash(request, f"已同步 {len(addresses)} 个目标地址状态", "success")
    return RedirectResponse("/destination-addresses", status_code=303)


@router.post("/destination-addresses/{address_id:int}/delete")
async def delete_destination_address(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    address_id: int,
) -> Response:
    """删除目标地址（同步删除 CF 记录，会停用引用规则）。"""
    try:
        address = await destination_service.get_destination_address_or_404(
            session, address_id, user
        )
        await destination_service.delete_destination_address(session, address)
    except NotFoundError:
        flash(request, "目标地址不存在", "error")
        return RedirectResponse("/destination-addresses", status_code=303)
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse("/destination-addresses", status_code=303)
    flash(request, "已删除目标地址", "success")
    return RedirectResponse("/destination-addresses", status_code=303)

"""CF 账号页面路由：列表、绑定、详情、编辑、同步域名、解绑。

复用 app/services/cf_account_service 与 domain_service，不重复业务逻辑。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.schemas.cf_account import CFAccountCreate, CFAccountRead, CFAccountUpdate
from app.services import cf_account_service, domain_service, worker_deploy_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render, render_error

router = APIRouter(tags=["前端-CF账号"])

# 绑定 CF 账号所需的 API Token 权限（展示给用户参考）
TOKEN_PERMISSIONS = [
    ("Zone:Email Routing:Edit", "转发规则管理"),
    ("Account:Email Routing Addresses:Edit", "目标地址管理"),
    ("Account:Email Send:Edit", "发件 Beta 权限"),
    ("Zone:Zone:Read", "读取域名信息"),
    ("Account:Workers Scripts:Edit", "一键部署收件 Worker（可选）"),
]


@router.get("/cf-accounts")
async def list_cf_accounts(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> Response:
    """CF 账号列表。"""
    accounts, total = await cf_account_service.list_cf_accounts(
        session, user, page, size
    )
    return render(
        request,
        "cf_accounts/list.html",
        user=user,
        active="cf_accounts",
        accounts=[CFAccountRead.model_validate(a) for a in accounts],
        page=page,
        size=size,
        total=total,
    )


@router.get("/cf-accounts/new")
async def new_cf_account(request: Request, user: CurrentWebUser) -> Response:
    """绑定 CF 账号表单页。"""
    return render(
        request,
        "cf_accounts/new.html",
        user=user,
        active="cf_accounts",
        permissions=TOKEN_PERMISSIONS,
        form={},
    )


@router.post("/cf-accounts")
async def create_cf_account(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    name: Annotated[str, Form()],
    api_token: Annotated[str, Form()],
    account_id: Annotated[str, Form()] = "",
) -> Response:
    """处理绑定表单：校验 Token 后加密存储，自动获取 account_id。"""
    try:
        data = CFAccountCreate(
            name=name,
            api_token=api_token,
            account_id=account_id or None,
        )
        await cf_account_service.bind_cf_account(session, user, data)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return render(
            request,
            "cf_accounts/new.html",
            user=user,
            status_code=400,
            active="cf_accounts",
            permissions=TOKEN_PERMISSIONS,
            form={
                "name": name,
                "account_id": account_id,
            },
        )
    flash(request, "已成功绑定 CF 账号", "success")
    return RedirectResponse("/cf-accounts", status_code=303)


@router.get("/cf-accounts/{account_id:int}")
async def cf_account_detail(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    account_id: int,
) -> Response:
    """CF 账号详情 + 编辑表单。"""
    try:
        account = await cf_account_service.get_cf_account_or_404(
            session, account_id, user
        )
    except NotFoundError:
        return render_error(request, 404, "CF 账号不存在", user=user)
    return render(
        request,
        "cf_accounts/detail.html",
        user=user,
        active="cf_accounts",
        account=CFAccountRead.model_validate(account),
    )


@router.post("/cf-accounts/{account_id:int}/edit")
async def edit_cf_account(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    account_id: int,
    name: Annotated[str, Form()],
    api_token: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
) -> Response:
    """更新 CF 账号（名称 / Token / 启停）。"""
    try:
        account = await cf_account_service.get_cf_account_or_404(
            session, account_id, user
        )
    except NotFoundError:
        flash(request, "CF 账号不存在", "error")
        return RedirectResponse("/cf-accounts", status_code=303)

    try:
        update = CFAccountUpdate(
            name=name,
            api_token=api_token or None,
            is_active=is_active == "on",
        )
        await cf_account_service.update_cf_account(session, account, update)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)

    flash(request, "已更新 CF 账号", "success")
    return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)


@router.post("/cf-accounts/{account_id:int}/sync")
async def sync_cf_account(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    account_id: int,
) -> Response:
    """从 Cloudflare 同步该账号下的域名。"""
    try:
        account = await cf_account_service.get_cf_account_or_404(
            session, account_id, user
        )
        domains = await domain_service.sync_domains(session, account, user)
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)
    flash(request, f"已同步 {len(domains)} 个域名", "success")
    return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)


@router.post("/cf-accounts/{account_id:int}/deploy-worker")
async def deploy_worker(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    account_id: int,
) -> Response:
    """一键部署/更新账号级收件 Worker（含所有域名 catch-all 配置）。"""
    try:
        account = await cf_account_service.get_cf_account_or_404(
            session, account_id, user
        )
        result = await worker_deploy_service.deploy_worker_for_account(
            session, account
        )
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)
    domain_count = len(result.get("domains", []))
    flash(
        request,
        f"Worker 「{result['worker_name']}」已部署/更新（{domain_count} 个域名）",
        "success",
    )
    return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)


@router.post("/cf-accounts/{account_id:int}/delete")
async def delete_cf_account(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    account_id: int,
) -> Response:
    """解绑（软删除）CF 账号。"""
    try:
        account = await cf_account_service.get_cf_account_or_404(
            session, account_id, user
        )
    except NotFoundError:
        flash(request, "CF 账号不存在", "error")
        return RedirectResponse("/cf-accounts", status_code=303)
    await cf_account_service.delete_cf_account(session, account)
    flash(request, "已解绑该 CF 账号", "success")
    return RedirectResponse("/cf-accounts", status_code=303)

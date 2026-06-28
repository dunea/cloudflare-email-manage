"""CF 账号页面路由：列表、绑定、详情、编辑、同步域名、解绑。

复用 app/services/cf_account_service 与 domain_service，不重复业务逻辑。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException, CFPermissionPrecheckError, NotFoundError
from app.schemas.cf_account import (
    CFAccountCreate,
    CFAccountRead,
    CFAccountUpdate,
    CFPermissionReport,
)
from app.services import (
    cf_account_service,
    cf_permission_service,
    domain_service,
    worker_deploy_service,
)
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render, render_error

router = APIRouter(tags=["前端-CF账号"])

# 绑定 CF 账号所需的 API Token 权限（展示给用户参考）
TOKEN_PERMISSIONS = [
    (item.required_permission, item.label, item.fix_hint)
    for item in cf_permission_service.REQUIRED_TOKEN_PERMISSIONS
]

TOKEN_SETUP_NOTES = [
    (
        "支持 User API Token (My Profile → API Tokens) "
        "和 Account API Token (Manage Account → API Tokens)。"
    ),
    "Account API Token 必须同时填写所属 Cloudflare Account ID。",
    "Cloudflare 权限编辑器需要分别添加「整个账户」和「邮箱域名」两组策略。",
    "整个账户权限只需要 Workers Scripts、Email Routing Addresses、Email Sending 的 Edit。",
    "邮箱域名权限需要 Zone 的 Read，以及 Zone Settings、Email Routing Rules 的 Edit。",
    "Workers Routes、Cloud Email Security、Email Routing Suppressions 当前不是必需权限。",
    "Token 资源范围必须覆盖要接入的 Account 和至少一个 Zone。",
    "API Token 输入框只填写原始 Token，不要包含 Bearer 前缀。",
    "如果 Token 配置了来源 IP 限制，请放行本服务的公网出口 IP。",
]


def _capability_report_context(
    report: CFPermissionReport | dict[str, object] | None,
) -> dict[str, object] | None:
    """将权限报告转成模板安全的 dict。"""
    if report is None:
        return None
    if isinstance(report, dict):
        return report
    return report.model_dump(mode="json")


def _capability_report_from_exception(
    exc: CFPermissionPrecheckError,
) -> dict[str, object]:
    """从业务异常中提取权限检查报告。"""
    report = exc.report
    if isinstance(report, dict):
        return report
    model_dump = getattr(report, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
    else:
        dumped = jsonable_encoder(report)
    return dumped if isinstance(dumped, dict) else {}


def _compact_deploy_error_message(exc: AppException) -> str:
    """Web toast 使用短错误；详细 Cloudflare path/code 留在 API 响应和日志中。"""
    if isinstance(exc, CFPermissionPrecheckError):
        report = _capability_report_from_exception(exc)
        for item in report.get("items", []):
            if isinstance(item, dict) and item.get("key") == "email_routing_settings":
                item_text = f"{item.get('message', '')} {item.get('fix_hint', '')}"
                if "暂时" in item_text or "非 JSON" in item_text or "未兼容" in item_text:
                    return (
                        "部署 Worker 失败：暂时无法确认 Email Routing 设置权限。"
                        "请稍后重试，或查看权限预检结果。"
                    )
                return (
                    "部署 Worker 失败：Token 缺少 Zone Settings 权限。"
                    "请在 Cloudflare Token 的邮箱域名权限中添加 Zone Settings: Edit，"
                    "然后重新检查权限。"
                )
    message = error_message(exc)
    if not message.startswith("部署 Worker 失败"):
        return message
    marker = "查询/启用 Email Routing 失败："
    if marker in message:
        domain = message.split(marker, 1)[1].split("。", 1)[0].strip()
        if domain:
            return (
                f"部署 Worker 失败：{domain} 的 Email Routing 设置不可访问。"
                "请在 Cloudflare Token 的邮箱域名权限中添加 Zone Settings: Edit，"
                "然后重新检查权限。"
            )
    if "Cloudflare 摘要：" in message:
        return message.split("Cloudflare 摘要：", 1)[0].strip()
    return message if len(message) <= 180 else f"{message[:177]}..."


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
        setup_notes=TOKEN_SETUP_NOTES,
        form={},
        capability_report=None,
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
        account = await cf_account_service.bind_cf_account(session, user, data)
    except (ValidationError, CFPermissionPrecheckError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return render(
            request,
            "cf_accounts/new.html",
            user=user,
            status_code=400,
            active="cf_accounts",
            permissions=TOKEN_PERMISSIONS,
            setup_notes=TOKEN_SETUP_NOTES,
            form={
                "name": name,
                "account_id": account_id,
            },
            capability_report=(
                _capability_report_from_exception(exc)
                if isinstance(exc, CFPermissionPrecheckError)
                else None
            ),
        )
    flash(request, "已成功绑定 CF 账号，权限预检已通过", "success")
    return RedirectResponse(f"/cf-accounts/{account.id}", status_code=303)


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
    account_read = CFAccountRead.model_validate(account)
    return render(
        request,
        "cf_accounts/detail.html",
        user=user,
        active="cf_accounts",
        account=account_read,
        capability_report=_capability_report_context(account_read.capability_report),
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
        updated = await cf_account_service.update_cf_account(session, account, update)
    except (ValidationError, CFPermissionPrecheckError, AppException) as exc:
        flash(request, error_message(exc), "error")
        await session.rollback()
        await session.refresh(user)
        account = await cf_account_service.get_cf_account_or_404(
            session, account_id, user
        )
        account_read = CFAccountRead.model_validate(account)
        return render(
            request,
            "cf_accounts/detail.html",
            user=user,
            status_code=400,
            active="cf_accounts",
            account=account_read,
            capability_report=(
                _capability_report_from_exception(exc)
                if isinstance(exc, CFPermissionPrecheckError)
                else _capability_report_context(account_read.capability_report)
            ),
        )

    flash(request, "已更新 CF 账号", "success")
    return RedirectResponse(f"/cf-accounts/{updated.id}", status_code=303)


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
    """一键部署/更新账号级收件 Worker（含邮箱域名 catch-all 配置）。"""
    try:
        account = await cf_account_service.get_cf_account_or_404(
            session, account_id, user
        )
        result = await worker_deploy_service.deploy_worker_for_account(
            session, account
        )
    except AppException as exc:
        flash(request, _compact_deploy_error_message(exc), "error")
        return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)
    flash(
        request,
        f"Worker 「{result.worker_name}」已部署/更新（{len(result.domains)} 个邮箱域名）。"
        "以后启用新的邮箱域名后，需要再次一键部署 Worker。",
        "success",
    )
    return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)


@router.post("/cf-accounts/{account_id:int}/check-permissions")
async def check_permissions(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    account_id: int,
) -> Response:
    """重新检查已绑定 CF 账号 Token 权限。"""
    try:
        account = await cf_account_service.get_cf_account_or_404(
            session, account_id, user
        )
        report = await cf_permission_service.refresh_cf_account_permissions(
            session, account
        )
    except AppException as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/cf-accounts/{account_id}", status_code=303)

    if report.overall_status == "passed":
        flash(request, "权限预检已通过", "success")
    else:
        flash(request, "权限预检未通过，请按检查结果修复 Token 设置", "warning")
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

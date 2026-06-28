"""CF账号 路由：绑定、查询、更新、软删除、域名同步。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
"""

from fastapi import APIRouter, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.dependencies import CurrentUser, SessionDep
from app.exceptions import CFPermissionPrecheckError
from app.schemas.cf_account import (
    CFAccountCreate,
    CFAccountRead,
    CFAccountTokenCheckRequest,
    CFAccountUpdate,
    CFPermissionReport,
    WorkerDeployResult,
)
from app.schemas.common import ApiResponse, PageData
from app.schemas.domain import DomainRead, DomainSyncResult
from app.services import (
    cf_account_service,
    cf_permission_service,
    domain_service,
    worker_deploy_service,
)

router = APIRouter(prefix="/cf-accounts", tags=["CF账号"])


def _permission_precheck_response(exc: CFPermissionPrecheckError) -> JSONResponse:
    """构造 CF 账号权限预检失败响应，保留结构化检查报告。"""
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": exc.code,
            "data": jsonable_encoder(exc.report),
            "message": exc.message,
        },
    )


@router.post(
    "/check-token",
    response_model=ApiResponse[CFPermissionReport],
    summary="绑定前检查 CF Token 权限",
)
async def check_token_permissions(
    data: CFAccountTokenCheckRequest, current_user: CurrentUser
) -> ApiResponse[CFPermissionReport]:
    """检查未入库 Token 的核心权限，不保存 Token。"""
    _ = current_user
    result = await cf_permission_service.inspect_token_permissions(
        data.api_token, data.account_id
    )
    return ApiResponse(data=result.report)


@router.post(
    "",
    response_model=ApiResponse[CFAccountRead],
    status_code=status.HTTP_201_CREATED,
    summary="绑定 CF 账号",
)
async def bind_cf_account(
    data: CFAccountCreate, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[CFAccountRead] | JSONResponse:
    """校验 API Token 后加密绑定 CF 账号。"""
    try:
        cf_account = await cf_account_service.bind_cf_account(
            session, current_user, data
        )
    except CFPermissionPrecheckError as exc:
        return _permission_precheck_response(exc)
    return ApiResponse(data=CFAccountRead.model_validate(cf_account))


@router.get(
    "",
    response_model=ApiResponse[PageData[CFAccountRead]],
    summary="CF 账号列表",
)
async def list_cf_accounts(
    current_user: CurrentUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[PageData[CFAccountRead]]:
    """分页查询当前用户绑定的 CF 账号。"""
    accounts, total = await cf_account_service.list_cf_accounts(
        session, current_user, page, size
    )
    page_data = PageData[CFAccountRead](
        total=total,
        page=page,
        size=size,
        items=[CFAccountRead.model_validate(a) for a in accounts],
    )
    return ApiResponse(data=page_data)


@router.get(
    "/{account_id}",
    response_model=ApiResponse[CFAccountRead],
    summary="获取 CF 账号",
)
async def get_cf_account(
    account_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[CFAccountRead]:
    """获取指定 CF 账号详情。"""
    cf_account = await cf_account_service.get_cf_account_or_404(
        session, account_id, current_user
    )
    return ApiResponse(data=CFAccountRead.model_validate(cf_account))


@router.patch(
    "/{account_id}",
    response_model=ApiResponse[CFAccountRead],
    summary="更新 CF 账号",
)
async def update_cf_account(
    account_id: int,
    data: CFAccountUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> ApiResponse[CFAccountRead] | JSONResponse:
    """更新 CF 账号信息（可更换 Token）。"""
    cf_account = await cf_account_service.get_cf_account_or_404(
        session, account_id, current_user
    )
    try:
        updated = await cf_account_service.update_cf_account(session, cf_account, data)
    except CFPermissionPrecheckError as exc:
        return _permission_precheck_response(exc)
    return ApiResponse(data=CFAccountRead.model_validate(updated))


@router.post(
    "/{account_id}/check-permissions",
    response_model=ApiResponse[CFPermissionReport],
    summary="重新检查已绑定 CF 账号权限",
)
async def check_cf_account_permissions(
    account_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> ApiResponse[CFPermissionReport]:
    """重新检查已绑定账号当前 Token 的核心权限，并保存检查报告。"""
    cf_account = await cf_account_service.get_cf_account_or_404(
        session, account_id, current_user
    )
    report = await cf_permission_service.refresh_cf_account_permissions(
        session, cf_account
    )
    return ApiResponse(data=report)


@router.delete(
    "/{account_id}",
    response_model=ApiResponse[None],
    summary="解绑 CF 账号",
)
async def delete_cf_account(
    account_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[None]:
    """软删除（解绑）CF 账号。"""
    cf_account = await cf_account_service.get_cf_account_or_404(
        session, account_id, current_user
    )
    await cf_account_service.delete_cf_account(session, cf_account)
    return ApiResponse(message="已解绑")


@router.post(
    "/{account_id}/sync",
    response_model=ApiResponse[DomainSyncResult],
    summary="同步域名",
)
async def sync_domains(
    account_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[DomainSyncResult]:
    """从 Cloudflare 同步该账号下的域名到本地。"""
    cf_account = await cf_account_service.get_cf_account_or_404(
        session, account_id, current_user
    )
    domains = await domain_service.sync_domains(session, cf_account, current_user)
    result = DomainSyncResult(
        synced=len(domains),
        domains=[DomainRead.model_validate(d) for d in domains],
    )
    return ApiResponse(data=result)


@router.post(
    "/{account_id}/deploy-worker",
    response_model=ApiResponse[WorkerDeployResult],
    summary="一键部署收件 Worker",
)
async def deploy_worker(
    account_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[WorkerDeployResult] | JSONResponse:
    """为该 CF 账号部署/更新账号级收件 Worker，并配置所有域名的 catch-all。

    流程：启用 Email Routing → 上传 Worker 脚本 → 设置域名→密钥 secret →
    为每个域名配置 catch-all → Worker。CF API Token 需具备
    Account:Workers Scripts:Edit 权限。
    """
    cf_account = await cf_account_service.get_cf_account_or_404(
        session, account_id, current_user
    )
    try:
        result = await worker_deploy_service.deploy_worker_for_account(
            session, cf_account
        )
    except CFPermissionPrecheckError as exc:
        return _permission_precheck_response(exc)
    return ApiResponse(data=result)

"""Worker 一键部署服务。

一个 CF 账号部署一个 Worker（``cf-email-manager-webhook``），持有：
  - ``WEBHOOK_URL``：平台收件端点（plain_text）
  - ``WEBHOOK_SECRETS``：域名 -> ``{zone_id, secret}`` 的 JSON 映射（secret）

并为该账号下每个域名配置 Email Routing catch-all -> Worker，
使域名下任意地址的邮件都投递到该 Worker。

部署流程：
  1. 校验 APP_BASE_URL 非 localhost（生产防呆）
  2. 加载该账号下所有域名，为缺失 webhook_secret 的域名预生成密钥（仅内存）
  3. 启用每个域名的 Email Routing（若未启用）
  4. 上传 Worker bundle 脚本（含 WEBHOOK_URL binding）
  5. **commit 新生成的 webhook_secret 到 DB**（先持久化密钥）
  6. 设置 WEBHOOK_SECRETS secret（{zone_id, secret} 映射，Worker 签名密钥生效）
  7. 对每个域名配置 catch-all -> Worker（最后一步，邮件开始流入）

任一步骤失败抛 CloudflareError/Exception：此时 catch-all 未配置，邮件不会
进入 Worker；即使 DB 已 commit 了新 secret，Worker 端仍是旧/无 secret，
影响最小（仅产生孤儿 secret，后续可重置）。
"""

from __future__ import annotations

import json
import logging
import secrets
from ipaddress import ip_address
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlparse

from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.exceptions import AppException, CloudflareError
from app.models import CFAccount, Domain
from app.schemas.cf_account import DeployedDomain, WorkerDeployResult
from app.services import cf_permission_service
from app.services.cf_account_service import build_client
from app.services.cloudflare import CloudflareClient, WorkerBinding

logger = logging.getLogger(__name__)

# 账号级 Worker 名称（CF 账号内唯一；重复部署会覆盖）
WORKER_NAME = "cf-email-manager-webhook"

# Worker bundle 资源路径（esbuild 打包产物，含 postal-mime）
BUNDLE_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "email_worker.bundle.js"
)

# 兼容性日期与 flags（与 examples/worker/wrangler.toml 保持一致）
_COMPATIBILITY_DATE = "2025-01-01"
_COMPATIBILITY_FLAGS = ["nodejs_compat"]


def _is_auth_or_permission_error(exc: CloudflareError) -> bool:
    """判断 Cloudflare 错误是否属于认证/权限类。"""
    raw = str(exc).lower()
    return (
        exc.cf_status_code in {401, 403}
        or "10000" in raw
        or "authentication error" in raw
        or "permission" in raw
        or "unauthorized" in raw
        or "forbidden" in raw
    )


def _raise_deploy_cloudflare_error(
    stage: str,
    exc: CloudflareError,
    *,
    account_id: str,
    zone_id: str | None = None,
    domain_name: str | None = None,
) -> NoReturn:
    """记录脱敏部署上下文，并转换成用户可行动的业务错误。"""
    log_parts = [f"stage={stage}", f"account_id={account_id}"]
    if zone_id:
        log_parts.append(f"zone_id={zone_id}")
    if domain_name:
        log_parts.append(f"domain_name={domain_name}")
    logger.exception(
        "一键部署 Worker 调用 Cloudflare 失败: %s; method=%s; path=%s; "
        "status=%s; errors=%s",
        ", ".join(log_parts),
        exc.cf_method,
        exc.cf_path,
        exc.cf_status_code,
        exc.cf_errors,
    )
    user_hint = cf_permission_service.describe_cloudflare_error(exc)
    error_is_permission = _is_auth_or_permission_error(exc)
    permission_hint = ""
    if error_is_permission and ("Worker 脚本" in stage or "Worker Secret" in stage):
        permission_hint = (
            " 请确认 Token 具备 Account:Workers Scripts:Edit / "
            "Workers Scripts Write，并重新检查权限。"
        )
    raise AppException(
        f"部署 Worker 失败：{stage}。{user_hint}{permission_hint} "
        f"Cloudflare 摘要：{exc}",
        code=1403 if error_is_permission else 1502,
        http_status=(
            http_status.HTTP_403_FORBIDDEN
            if error_is_permission
            else http_status.HTTP_502_BAD_GATEWAY
        ),
    ) from exc


def _zone_account_id(zone: dict[str, object]) -> str | None:
    """从 Cloudflare Zone 响应中提取 account.id。"""
    account = zone.get("account")
    if not isinstance(account, dict):
        return None
    account_id = account.get("id")
    return str(account_id) if account_id else None


def _assert_cloudflare_zones_match_account(
    zones: list[dict[str, object]], expected_account_id: str
) -> None:
    """确认部署前拉到的 Zone 均属于当前绑定账号。"""
    for zone in zones:
        account_id = _zone_account_id(zone)
        if account_id is None or account_id == expected_account_id:
            continue
        domain_name = str(zone.get("name") or zone.get("id") or "未知域名")
        raise AppException(
            "Cloudflare 返回的域名与当前绑定 Account 不匹配："
            f"{domain_name} 属于 Account {account_id}，"
            f"不是当前绑定的 {expected_account_id}。"
            "请重新同步域名；如果这些域名属于另一个 Cloudflare Account，"
            "请新增绑定账号。",
            code=1403,
            http_status=http_status.HTTP_403_FORBIDDEN,
        )


def _assert_local_domains_match_cloudflare_zones(
    domains: list[Domain], current_zone_ids: set[str]
) -> None:
    """部署前确认本地域名仍属于当前绑定账号。"""
    mismatched = [domain for domain in domains if domain.zone_id not in current_zone_ids]
    if not mismatched:
        return
    sample = "、".join(
        f"{domain.domain_name} (zone_id={domain.zone_id})"
        for domain in mismatched[:5]
    )
    extra = "" if len(mismatched) <= 5 else f" 等 {len(mismatched)} 个域名"
    raise AppException(
        "本地域名与当前 Cloudflare Account 不一致，已拒绝部署。"
        f"异常域名：{sample}{extra}。"
        "请先重新同步域名；如果这些域名属于另一个 Cloudflare Account，"
        "请新增绑定账号。",
        code=1403,
        http_status=http_status.HTTP_403_FORBIDDEN,
    )


async def _assert_deploy_domain_scope(
    client: CloudflareClient, cf_account: CFAccount, domains: list[Domain]
) -> None:
    """部署前重新拉取当前账号 Zone，并校验本地域名归属。"""
    zones = await client.list_zones(cf_account.account_id)
    _assert_cloudflare_zones_match_account(zones, cf_account.account_id)
    current_zone_ids = {str(zone.get("id")) for zone in zones if zone.get("id")}
    _assert_local_domains_match_cloudflare_zones(domains, current_zone_ids)


def _platform_webhook_url() -> str:
    """构造平台收件 Webhook 完整 URL（去掉末尾斜杠）。"""
    base = settings.APP_BASE_URL.rstrip("/")
    return f"{base}/api/v1/inbound/webhook"


def _validate_public_base_url() -> None:
    """部署时校验 APP_BASE_URL 是公网可达的 HTTPS URL，避免静默破坏 Worker 回传。

    拒绝：localhost/回环/私有/链路本地 IP、http（非 https）、缺 hostname。
    本地开发（CF_FAKE_MODE=True）放行。
    """
    if settings.CF_FAKE_MODE:
        return
    parsed = urlparse(settings.APP_BASE_URL.strip())
    host = parsed.hostname
    is_bad = host is None or parsed.scheme != "https" or host.lower() == "localhost"
    if not is_bad and host is not None:
        try:
            ip = ip_address(host)
            # 拒绝所有非 global IP（含 CGNAT 100.64.0.0/10、reserved 等）
            # 额外显式拒绝 multicast（ipaddress.is_global 对 224/4 视为 global）
            is_bad = not ip.is_global or ip.is_multicast or ip.is_unspecified
        except ValueError:
            # 非 IP 字面量（域名），已通过上面的检查
            is_bad = False
    if is_bad:
        raise AppException(
            "APP_BASE_URL 不可用作 Worker 回传地址（当前: "
            f"{settings.APP_BASE_URL!r}）。"
            "请在 .env 中配置公网可达的 HTTPS URL（如 https://your-domain.com）后重试。",
            code=1400,
        )


def _prepare_domain_secrets(domains: list[Domain]) -> list[str]:
    """为缺失 webhook_secret 的域名在内存中预生成密钥，返回有变更的 domain.id 列表。

    不在此处 commit，调用方需在所有 CF 部署成功后统一 commit。
    """
    changed_ids: list[str] = []
    for domain in domains:
        if not domain.webhook_secret:
            domain.webhook_secret = secrets.token_urlsafe(32)
            changed_ids.append(str(domain.id))
    return changed_ids


def _read_bundle() -> bytes:
    """读取 bundle 产物；缺失时给出明确指引。"""
    if not BUNDLE_PATH.exists():
        raise AppException(
            "Worker bundle 产物不存在: app/assets/email_worker.bundle.js。"
            "请先在 examples/worker 目录运行: "
            "npx esbuild src/index.js --bundle --format=esm "
            "--platform=browser --outfile=../../app/assets/email_worker.bundle.js",
            code=1500,
            http_status=500,
        )
    return BUNDLE_PATH.read_bytes()


async def deploy_worker_for_account(
    session: AsyncSession, cf_account: CFAccount
) -> WorkerDeployResult:
    """为指定 CF 账号一键部署收件 Worker，返回部署结果。

    调用顺序: 校验 APP_BASE_URL -> 预生成缺失密钥（仅内存）-> 启用 Email Routing
    -> 上传脚本 -> **commit 新密钥** -> 设置 secret -> 配置每个域名的 catch-all。
    commit 在 set_worker_secret 之前：避免 catch-all 已配置但 DB 未持久化导致
    邮件进入 Worker 后验签失败。
    """
    _validate_public_base_url()

    # 1. 加载该账号下所有域名
    domains = list(
        (
            await session.execute(
                select(Domain).where(Domain.cf_account_id == cf_account.id)
            )
        ).scalars()
    )
    if not domains:
        raise AppException(
            "该 CF 账号下尚无域名，请先同步域名后再部署 Worker",
            code=1400,
        )

    # 部署前重新检查 Token 核心能力，避免旧 Token 权限被外部收回后进入半部署状态。
    try:
        report = await cf_permission_service.refresh_cf_account_permissions(
            session, cf_account
        )
    except CloudflareError as exc:
        _raise_deploy_cloudflare_error(
            "权限复检失败",
            exc,
            account_id=cf_account.account_id,
        )
    cf_permission_service.ensure_report_passed(report)

    client = build_client(cf_account)
    try:
        await _assert_deploy_domain_scope(client, cf_account, domains)
    except CloudflareError as exc:
        _raise_deploy_cloudflare_error(
            "校验域名归属失败",
            exc,
            account_id=cf_account.account_id,
        )

    # 2. 内存中预生成缺失的 webhook_secret（延后 commit）
    _prepare_domain_secrets(domains)

    webhook_url = _platform_webhook_url()
    bundle_bytes = _read_bundle()

    # 3. 启用每个域名的 Email Routing（幂等: 已启用则无害）
    deployed: list[DeployedDomain] = []
    for domain in domains:
        try:
            routing_status = await client.get_email_routing_status(domain.zone_id)
            if not routing_status.get("enabled", False):
                await client.enable_email_routing(domain.zone_id)
        except CloudflareError as exc:
            _raise_deploy_cloudflare_error(
                f"查询/启用 Email Routing 失败：{domain.domain_name}",
                exc,
                account_id=cf_account.account_id,
                zone_id=domain.zone_id,
                domain_name=domain.domain_name,
            )
        deployed.append(
            DeployedDomain(
                domain_id=domain.id,
                domain_name=domain.domain_name,
                zone_id=domain.zone_id,
            )
        )

    # 4. 上传 Worker 脚本（含 WEBHOOK_URL plain_text binding）
    bindings: list[WorkerBinding] = [
        WorkerBinding(type="plain_text", name="WEBHOOK_URL", text=webhook_url),
    ]
    try:
        await client.upload_worker_script(
            account_id=cf_account.account_id,
            script_name=WORKER_NAME,
            main_module_name="index.js",
            script_content=bundle_bytes,
            compatibility_date=_COMPATIBILITY_DATE,
            compatibility_flags=_COMPATIBILITY_FLAGS,
            bindings=bindings,
        )
    except CloudflareError as exc:
        _raise_deploy_cloudflare_error(
            "上传 Worker 脚本失败",
            exc,
            account_id=cf_account.account_id,
        )

    # 5. 先 commit 新生成的 webhook_secret（DB 优先成为真相源）
    await session.commit()

    # 6. 设置 WEBHOOK_SECRETS secret（域名 -> {zone_id, secret} JSON 映射）
    secrets_map = {
        d.domain_name.lower(): {"zone_id": d.zone_id, "secret": d.webhook_secret}
        for d in domains
    }
    secrets_json = json.dumps(secrets_map, ensure_ascii=False, separators=(",", ":"))
    try:
        await client.set_worker_secret(
            account_id=cf_account.account_id,
            script_name=WORKER_NAME,
            secret_name="WEBHOOK_SECRETS",
            secret_value=secrets_json,
        )
    except CloudflareError as exc:
        _raise_deploy_cloudflare_error(
            "设置 Worker Secret 失败",
            exc,
            account_id=cf_account.account_id,
        )

    # 7. 对每个域名配置 catch-all -> Worker（最后一步，邮件开始流入）
    for domain in domains:
        try:
            await client.update_catch_all_to_worker(domain.zone_id, WORKER_NAME)
        except CloudflareError as exc:
            _raise_deploy_cloudflare_error(
                f"配置 {domain.domain_name} catch-all 失败 (zone_id={domain.zone_id})",
                exc,
                account_id=cf_account.account_id,
                zone_id=domain.zone_id,
                domain_name=domain.domain_name,
            )

    return WorkerDeployResult(
        worker_name=WORKER_NAME,
        webhook_url=webhook_url,
        domains=deployed,
    )

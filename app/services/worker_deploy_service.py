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
  5. 设置 WEBHOOK_SECRETS secret（{zone_id, secret} 映射）
  6. 对每个域名配置 catch-all -> Worker
  7. 所有 CF 部署成功后，再 commit 新生成的 webhook_secret（防半成品状态）

任何 CF 步骤失败抛出 CloudflareError（已映射 502），新生成的 webhook_secret
不会被持久化，平台仍可用旧密钥或全局 CF_WEBHOOK_SECRET 验签。
"""

from __future__ import annotations

import json
import logging
import secrets
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.exceptions import AppException, CloudflareError
from app.models import CFAccount, Domain
from app.schemas.cf_account import DeployedDomain, WorkerDeployResult
from app.services.cf_account_service import build_client
from app.services.cloudflare import WorkerBinding

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
    -> 上传脚本 -> 设置 secret -> 配置每个域名的 catch-all -> commit 新密钥。
    任何 CF 步骤失败抛出 CloudflareError（已映射 502），新生成的 webhook_secret
    不会被 commit。
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

    # 2. 内存中预生成缺失的 webhook_secret（延后 commit）
    _prepare_domain_secrets(domains)

    client = build_client(cf_account)
    webhook_url = _platform_webhook_url()
    bundle_bytes = _read_bundle()

    # 3. 启用每个域名的 Email Routing（幂等: 已启用则无害）
    deployed: list[DeployedDomain] = []
    for domain in domains:
        try:
            status = await client.get_email_routing_status(domain.zone_id)
            if not status.get("enabled", False):
                await client.enable_email_routing(domain.zone_id)
        except CloudflareError:
            logger.exception("启用 Email Routing 失败: %s", domain.domain_name)
            raise
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
        # 权限不足时给出可读提示
        if "10000" in str(exc) or "403" in str(exc) or "permission" in str(exc).lower():
            raise AppException(
                "部署 Worker 失败: CF API Token 缺少 Account:Workers Scripts:Edit 权限。"
                "请在 Cloudflare Dashboard 创建具备该权限的 Token 后重新绑定。",
                code=1403,
                http_status=403,
            ) from exc
        raise

    # 5. 设置 WEBHOOK_SECRETS secret（域名 -> {zone_id, secret} JSON 映射）
    secrets_map = {
        d.domain_name.lower(): {"zone_id": d.zone_id, "secret": d.webhook_secret}
        for d in domains
    }
    secrets_json = json.dumps(secrets_map, ensure_ascii=False, separators=(",", ":"))
    await client.set_worker_secret(
        account_id=cf_account.account_id,
        script_name=WORKER_NAME,
        secret_name="WEBHOOK_SECRETS",
        secret_value=secrets_json,
    )

    # 6. 对每个域名配置 catch-all -> Worker
    for domain in domains:
        await client.update_catch_all_to_worker(domain.zone_id, WORKER_NAME)

    # 7. 所有 CF 步骤成功后，commit 新生成的 webhook_secret
    await session.commit()

    return WorkerDeployResult(
        worker_name=WORKER_NAME,
        webhook_url=webhook_url,
        domains=deployed,
    )
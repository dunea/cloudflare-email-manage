"""Worker 一键部署服务。

一个 CF 账号部署一个 Worker（``cf-email-manager-webhook``），持有：
  - ``WEBHOOK_URL``：平台收件端点（plain_text）
  - ``WEBHOOK_SECRETS``：域名→签名密钥 的 JSON 映射（secret）

并为该账号下每个域名配置 Email Routing catch-all → Worker，
使域名下任意地址的邮件都投递到该 Worker。

部署流程：
  1. 确保账号下每个域名都有 webhook_secret（缺失则生成并提交）
  2. 启用每个域名的 Email Routing（若未启用）
  3. 上传 Worker bundle 脚本（含 WEBHOOK_URL binding）
  4. 设置 WEBHOOK_SECRETS secret（JSON 映射）
  5. 对每个域名配置 catch-all → Worker
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.exceptions import AppException, CloudflareError
from app.models import CFAccount, Domain
from app.services.cf_account_service import build_client

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


async def _ensure_domain_secrets(session: AsyncSession, domains: list[Domain]) -> None:
    """为缺失 webhook_secret 的域名生成密钥并提交。"""
    need_commit = False
    for domain in domains:
        if not domain.webhook_secret:
            domain.webhook_secret = secrets.token_urlsafe(32)
            need_commit = True
    if need_commit:
        await session.commit()


def _read_bundle() -> bytes:
    """读取 bundle 产物；缺失时给出明确指引。"""
    if not BUNDLE_PATH.exists():
        raise AppException(
            "Worker bundle 产物不存在：app/assets/email_worker.bundle.js。"
            "请先在 examples/worker 目录运行："
            "npx esbuild src/index.js --bundle --format=esm "
            "--platform=browser --outfile=../../app/assets/email_worker.bundle.js",
            code=1500,
            http_status=500,
        )
    return BUNDLE_PATH.read_bytes()


async def deploy_worker_for_account(
    session: AsyncSession, cf_account: CFAccount
) -> dict[str, Any]:
    """为指定 CF 账号一键部署收件 Worker。

    调用顺序：启用 Email Routing → 上传脚本 → 设置 secret → 配置每个域名的 catch-all。
    任何步骤失败抛出 CloudflareError（已映射 502）。
    """
    # 1. 加载该账号下所有域名，并确保都有 webhook_secret
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

    await _ensure_domain_secrets(session, domains)

    client = build_client(cf_account)
    webhook_url = _platform_webhook_url()
    bundle_bytes = _read_bundle()

    # 2. 启用每个域名的 Email Routing（幂等：已启用则无害）
    deployed_domains: list[dict[str, Any]] = []
    for domain in domains:
        try:
            status = await client.get_email_routing_status(domain.zone_id)
            if not status.get("enabled", False):
                await client.enable_email_routing(domain.zone_id)
        except CloudflareError:
            # 重新抛出，附加域名信息便于排错
            logger.exception("启用 Email Routing 失败: %s", domain.domain_name)
            raise
        deployed_domains.append(
            {
                "domain_id": domain.id,
                "domain_name": domain.domain_name,
                "zone_id": domain.zone_id,
            }
        )

    # 3. 上传 Worker 脚本（含 WEBHOOK_URL plain_text binding）
    bindings = [
        {
            "type": "plain_text",
            "name": "WEBHOOK_URL",
            "text": webhook_url,
        }
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
                "部署 Worker 失败：CF API Token 缺少 Account:Workers Scripts:Edit 权限。"
                "请在 Cloudflare Dashboard 创建具备该权限的 Token 后重新绑定。",
                code=1403,
                http_status=403,
            ) from exc
        raise

    # 4. 设置 WEBHOOK_SECRETS secret（域名→密钥 JSON 映射）
    secrets_map = {d.domain_name.lower(): d.webhook_secret for d in domains}
    secrets_json = json.dumps(secrets_map, ensure_ascii=False, separators=(",", ":"))
    await client.set_worker_secret(
        account_id=cf_account.account_id,
        script_name=WORKER_NAME,
        secret_name="WEBHOOK_SECRETS",
        secret_value=secrets_json,
    )

    # 5. 对每个域名配置 catch-all → Worker
    for domain in domains:
        await client.update_catch_all_to_worker(domain.zone_id, WORKER_NAME)

    return {
        "worker_name": WORKER_NAME,
        "webhook_url": webhook_url,
        "domains": deployed_domains,
    }
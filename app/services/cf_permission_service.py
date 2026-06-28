"""Cloudflare API Token 权限预检服务。

绑定 CF 账号前统一检查平台核心能力：域名读取、Email Routing、目标地址、
Email Sending 与 Workers Scripts。写权限检查使用无效 payload 探测，
不创建路由、不创建目标地址、不发送邮件、不创建 Worker、不写 secret。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, CFPermissionPrecheckError, CloudflareError
from app.models import CFAccount
from app.schemas.cf_account import CFPermissionCheckItem, CFPermissionReport
from app.services.cloudflare import CloudflareClient
from app.services.crypto import decrypt_token


@dataclass(frozen=True)
class PermissionRequirement:
    """单个核心能力的展示与修复信息。"""

    key: str
    label: str
    required_permission: str
    fix_hint: str


@dataclass(frozen=True)
class TokenPermissionCheckResult:
    """Token 权限预检结果，包含规范化后的 Token。"""

    api_token: str
    account_id: str | None
    report: CFPermissionReport


TOKEN_REQUIREMENT = PermissionRequirement(
    key="token_auth",
    label="API Token 有效性",
    required_permission="有效的 Cloudflare API Token",
    fix_hint="请重新复制 Cloudflare API Token 原始值；不要包含 Bearer 前缀。",
)

ZONE_REQUIREMENT = PermissionRequirement(
    key="zone_read",
    label="域名读取",
    required_permission="Zone:Zone:Read",
    fix_hint="创建 Token 时在 Zone 资源范围中选择需要接入的域名，并授予 Zone:Zone:Read。",
)

EMAIL_ROUTING_REQUIREMENT = PermissionRequirement(
    key="email_routing",
    label="Email Routing 规则",
    required_permission="Zone:Email Routing:Edit / Email Routing Rules Write",
    fix_hint="为所有接入域名授予 Email Routing 编辑权限，用于创建邮箱路由和 catch-all。",
)

DESTINATION_ADDRESS_REQUIREMENT = PermissionRequirement(
    key="routing_addresses",
    label="转发目标地址",
    required_permission="Account:Email Routing Addresses:Edit",
    fix_hint="在 Account 权限中添加 Email Routing Addresses 编辑权限。",
)

EMAIL_SENDING_REQUIREMENT = PermissionRequirement(
    key="email_sending",
    label="Email Sending 发件",
    required_permission="Account:Email Send:Edit / Email Sending Write",
    fix_hint="在 Account 权限中添加 Email Sending 发件权限，并确认域名已启用 Email Sending。",
)

WORKERS_REQUIREMENT = PermissionRequirement(
    key="workers_scripts",
    label="Workers 脚本",
    required_permission="Account:Workers Scripts:Edit / Workers Scripts Write",
    fix_hint="在 Account 权限中添加 Workers Scripts 编辑权限，否则无法一键部署收件 Worker。",
)

REQUIRED_TOKEN_PERMISSIONS: tuple[PermissionRequirement, ...] = (
    ZONE_REQUIREMENT,
    EMAIL_ROUTING_REQUIREMENT,
    DESTINATION_ADDRESS_REQUIREMENT,
    EMAIL_SENDING_REQUIREMENT,
    WORKERS_REQUIREMENT,
)


def _now() -> datetime:
    """返回 UTC 当前时间。"""
    return datetime.now(UTC)


def _make_item(
    requirement: PermissionRequirement,
    status_value: str,
    message: str,
) -> CFPermissionCheckItem:
    """构造单项检查结果。"""
    return CFPermissionCheckItem(
        key=requirement.key,
        label=requirement.label,
        status="passed" if status_value == "passed" else "failed",
        required_permission=requirement.required_permission,
        message=message,
        fix_hint=requirement.fix_hint,
    )


def _report(
    *,
    checked_at: datetime,
    account_id: str | None,
    zone_count: int,
    items: list[CFPermissionCheckItem],
) -> CFPermissionReport:
    """汇总检查项为报告。"""
    overall_status = "failed" if any(item.status == "failed" for item in items) else "passed"
    return CFPermissionReport(
        overall_status=overall_status,
        checked_at=checked_at,
        account_id=account_id,
        zone_count=zone_count,
        items=items,
    )


def _classify_cf_error(exc: CloudflareError) -> str:
    """将 Cloudflare 原始错误转成用户能行动的中文说明。"""
    raw = str(exc)
    lowered = raw.lower()
    if "暂时无法完成权限探测" in raw or "http 429" in lowered or "(http 5" in lowered:
        return "Cloudflare 暂时无法完成权限探测，请稍后重试。"
    if "返回非 json 响应" in lowered:
        return "Cloudflare 返回非 JSON 响应，暂时无法确认权限，请稍后重试。"
    if "无效探测 payload 返回了成功响应" in raw:
        return "Cloudflare 权限探测结果异常：无效探测请求返回成功，已拒绝绑定。"
    if "未识别的权限探测结果" in raw:
        return "Cloudflare 返回未识别的权限探测结果，无法确认写权限完整。"
    if "account id 不匹配" in lowered or "account id mismatch" in lowered:
        return (
            "新 Token 属于或覆盖的是另一个 Cloudflare Account；"
            "请为当前 Account 重新创建 Token，或新增绑定账号。"
        )
    if "10000" in raw or "authentication error" in lowered or "401" in raw:
        return (
            "Cloudflare 认证失败。常见原因是 Token 无效或过期、误填了 Bearer 前缀、"
            "Token 启用了来源 IP 限制但未放行本服务出口 IP，或 Account ID / 资源范围不匹配。"
        )
    if "403" in raw or "permission" in lowered or "unauthorized" in lowered:
        return "Cloudflare 拒绝访问该接口，通常是 Token 缺少对应权限或资源范围未覆盖。"
    return f"Cloudflare 返回错误：{raw}"


def describe_cloudflare_error(exc: CloudflareError) -> str:
    """公开的 Cloudflare 错误说明助手。"""
    return _classify_cf_error(exc)


def _normalize_token(api_token: str) -> str:
    """清理用户输入的 Token，拒绝 Bearer 前缀。"""
    token = api_token.strip()
    if token.lower().startswith("bearer "):
        raise AppException(
            "API Token 请填写原始 Token，不要包含 Bearer 前缀。",
            code=1400,
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    return token


async def _first_account_id(client: CloudflareClient) -> str | None:
    """从账号列表中提取第一个 account_id。"""
    accounts = await client.list_accounts()
    if not accounts:
        return None
    account_id = accounts[0].get("id")
    return str(account_id) if account_id else None


def _account_id_from_zones(zones: list[dict[str, object]]) -> str | None:
    """从 Zone 响应中提取 account.id。"""
    for zone in zones:
        account = zone.get("account")
        if isinstance(account, dict):
            account_id = account.get("id")
            if account_id:
                return str(account_id)
    return None


def _find_mismatched_zone_account_id(
    zones: list[dict[str, object]], expected_account_id: str
) -> str | None:
    """查找 Zone 响应中与期望账号不一致的 account.id。"""
    for zone in zones:
        account = zone.get("account")
        if not isinstance(account, dict):
            continue
        account_id = account.get("id")
        if account_id and str(account_id) != expected_account_id:
            return str(account_id)
    return None


async def _resolve_zones(
    client: CloudflareClient, explicit_account_id: str | None
) -> tuple[str | None, list[dict[str, object]]]:
    """解析账号 ID 并读取该账号下可访问 Zone。"""
    account_id = explicit_account_id.strip() if explicit_account_id else None
    if not account_id:
        account_id = None
    zones = await client.list_zones(account_id)
    if account_id is not None:
        mismatched = _find_mismatched_zone_account_id(zones, account_id)
        if mismatched is not None:
            raise CloudflareError(
                "Account ID 不匹配：Token 返回的域名属于 "
                f"{mismatched}，不是当前绑定的 {account_id}。"
            )
    if account_id is None:
        account_id = _account_id_from_zones(zones)
        if account_id is None:
            account_id = await _first_account_id(client)
    return account_id, zones


async def _add_cloudflare_check(
    items: list[CFPermissionCheckItem],
    requirement: PermissionRequirement,
    call: Callable[[], Awaitable[object]],
    success_message: str,
) -> None:
    """执行一个 Cloudflare 能力检查，并写入检查项。"""
    try:
        await call()
    except CloudflareError as exc:
        items.append(_make_item(requirement, "failed", _classify_cf_error(exc)))
        return
    items.append(_make_item(requirement, "passed", success_message))


async def _check_email_routing_rules_write(
    client: CloudflareClient, zone_id: str
) -> None:
    """检查 Email Routing 规则读可达与写权限。"""
    await client.list_routing_rules(zone_id)
    await client.probe_email_routing_rules_write(zone_id)


async def _check_destination_addresses_write(
    client: CloudflareClient, account_id: str
) -> None:
    """检查目标地址读可达与写权限。"""
    await client.list_destination_addresses(account_id)
    await client.probe_destination_addresses_write(account_id)


async def _check_email_routing_rules_write_for_zones(
    client: CloudflareClient, zones: list[dict[str, object]]
) -> None:
    """检查所有可访问 Zone 的 Email Routing 规则读可达与写权限。"""
    for zone in zones:
        zone_id = str(zone.get("id") or "")
        if not zone_id:
            raise CloudflareError(
                "Cloudflare Zone 响应缺少 zone_id，无法确认 Email Routing 权限。"
            )
        await _check_email_routing_rules_write(client, zone_id)


async def inspect_token_permissions(
    api_token: str, account_id: str | None = None
) -> TokenPermissionCheckResult:
    """检查一个未入库 Token 的核心权限，返回结构化报告。"""
    checked_at = _now()
    try:
        normalized_token = _normalize_token(api_token)
    except AppException as exc:
        item = _make_item(TOKEN_REQUIREMENT, "failed", exc.message)
        report = _report(
            checked_at=checked_at,
            account_id=account_id,
            zone_count=0,
            items=[item],
        )
        return TokenPermissionCheckResult(
            api_token=api_token.strip(),
            account_id=account_id,
            report=report,
        )

    client = CloudflareClient(normalized_token)
    items: list[CFPermissionCheckItem] = []

    try:
        await client.verify_token()
    except CloudflareError as exc:
        items.append(_make_item(TOKEN_REQUIREMENT, "failed", _classify_cf_error(exc)))
        report = _report(
            checked_at=checked_at,
            account_id=account_id,
            zone_count=0,
            items=items,
        )
        return TokenPermissionCheckResult(normalized_token, account_id, report)

    items.append(_make_item(TOKEN_REQUIREMENT, "passed", "Token 已通过 Cloudflare 校验。"))

    try:
        resolved_account_id, zones = await _resolve_zones(client, account_id)
    except CloudflareError as exc:
        items.append(_make_item(ZONE_REQUIREMENT, "failed", _classify_cf_error(exc)))
        report = _report(
            checked_at=checked_at,
            account_id=account_id,
            zone_count=0,
            items=items,
        )
        return TokenPermissionCheckResult(normalized_token, account_id, report)

    if not zones:
        message = (
            "未读取到该 Token 可访问的域名。请确认 Account ID 正确，"
            "并且 Token 的 Zone 资源范围覆盖至少一个需要接入的域名。"
        )
        items.append(_make_item(ZONE_REQUIREMENT, "failed", message))
        report = _report(
            checked_at=checked_at,
            account_id=resolved_account_id or account_id,
            zone_count=0,
            items=items,
        )
        return TokenPermissionCheckResult(
            normalized_token,
            resolved_account_id or account_id,
            report,
        )

    if resolved_account_id is None:
        items.append(
            _make_item(
                ZONE_REQUIREMENT,
                "failed",
                "已读取到域名，但 Cloudflare 响应中没有 account_id，无法绑定账号。",
            )
        )
        report = _report(
            checked_at=checked_at,
            account_id=None,
            zone_count=len(zones),
            items=items,
        )
        return TokenPermissionCheckResult(normalized_token, None, report)

    items.append(
        _make_item(
            ZONE_REQUIREMENT,
            "passed",
            f"已读取到 {len(zones)} 个可访问域名。",
        )
    )
    await _add_cloudflare_check(
        items,
        EMAIL_ROUTING_REQUIREMENT,
        lambda: _check_email_routing_rules_write_for_zones(client, zones),
        f"已通过 {len(zones)} 个域名的 Email Routing 规则读/写权限探测。",
    )
    await _add_cloudflare_check(
        items,
        DESTINATION_ADDRESS_REQUIREMENT,
        lambda: _check_destination_addresses_write(client, resolved_account_id),
        "已通过账号级转发目标地址读/写权限探测。",
    )
    await _add_cloudflare_check(
        items,
        EMAIL_SENDING_REQUIREMENT,
        lambda: client.probe_email_sending_write(resolved_account_id),
        "已通过 Email Sending 发件写权限探测；发件前仍需对应域名已启用 Email Sending。",
    )
    await _add_cloudflare_check(
        items,
        WORKERS_REQUIREMENT,
        lambda: client.probe_worker_scripts_write(resolved_account_id),
        "已通过 Workers Scripts 写权限探测；一键部署时将使用该能力上传 Worker。",
    )

    report = _report(
        checked_at=checked_at,
        account_id=resolved_account_id,
        zone_count=len(zones),
        items=items,
    )
    return TokenPermissionCheckResult(normalized_token, resolved_account_id, report)


def ensure_report_passed(report: CFPermissionReport) -> None:
    """权限报告未通过时抛出专用权限预检异常。"""
    if report.overall_status == "passed":
        return
    failed = [item for item in report.items if item.status == "failed"]
    summary = "；".join(f"{item.label}: {item.message}" for item in failed[:3])
    if len(failed) > 3:
        summary += f"；另有 {len(failed) - 3} 项未通过"
    raise CFPermissionPrecheckError(
        f"Cloudflare API Token 权限预检未通过：{summary}",
        report=report,
    )


def store_report(cf_account: CFAccount, report: CFPermissionReport) -> None:
    """将权限报告写入 CFAccount 对象。"""
    cf_account.capability_report_json = report.model_dump_json()
    cf_account.capability_checked_at = report.checked_at


async def inspect_bound_account(cf_account: CFAccount) -> CFPermissionReport:
    """检查已绑定账号当前 Token 的权限。"""
    token = decrypt_token(cf_account.encrypted_api_token)
    result = await inspect_token_permissions(token, cf_account.account_id)
    return result.report


async def refresh_cf_account_permissions(
    session: AsyncSession, cf_account: CFAccount
) -> CFPermissionReport:
    """重新检查已绑定账号权限，并保存最近一次报告。"""
    report = await inspect_bound_account(cf_account)
    store_report(cf_account, report)
    await session.commit()
    await session.refresh(cf_account)
    return report

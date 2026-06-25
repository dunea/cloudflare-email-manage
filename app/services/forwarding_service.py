"""转发规则逻辑。

转发规则对应 CF Email Routing rule：创建时调用 CF 创建规则并保存返回的
cf_rule_id，删除时调用 CF 删除规则。所属域名的 CF 账号（可能是平台账号）
用于解密 Token 构造客户端，用户无需感知底层 Token。
"""

from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.exceptions import AppException, NotFoundError
from app.models import CFAccount, Domain, EmailAddress, ForwardingRule, User
from app.schemas.forwarding_rule import ForwardingRuleCreate, ForwardingRuleUpdate
from app.services import destination_service, email_service
from app.services.cf_account_service import build_client
from app.services.cloudflare import CloudflareClient


async def _resolve_zone_and_client(
    session: AsyncSession, email_address: EmailAddress
) -> tuple[str, CloudflareClient]:
    """根据邮箱地址解析所属 zone_id 与对应的 CloudflareClient。"""
    domain = (
        await session.execute(
            select(Domain).where(Domain.id == email_address.domain_id)
        )
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")
    cf_account = (
        await session.execute(
            select(CFAccount).where(CFAccount.id == domain.cf_account_id)
        )
    ).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("CF 账号不存在")
    return domain.zone_id, build_client(cf_account)


def _build_rule_payload(full_address: str, destination_email: str) -> dict[str, Any]:
    """构造 CF Email Routing 转发规则请求体。"""
    return {
        "actions": [{"type": "forward", "value": [destination_email]}],
        "matchers": [{"type": "literal", "field": "to", "value": full_address}],
        "enabled": True,
        "name": f"forward {full_address} -> {destination_email}",
    }


def _accessible_rule_stmt(user: User) -> Select[tuple[ForwardingRule]]:
    """构造按所有权过滤的转发规则查询（关联 email_address 校验归属）。"""
    stmt = (
        select(ForwardingRule)
        .join(EmailAddress, ForwardingRule.email_address_id == EmailAddress.id)
        .where(ForwardingRule.is_deleted.is_(False))
    )
    if user.role != "admin":
        stmt = stmt.where(EmailAddress.user_id == user.id)
    return stmt


async def create_forwarding_rule(
    session: AsyncSession, user: User, data: ForwardingRuleCreate
) -> ForwardingRule:
    """创建转发规则：校验源邮箱未绑定、目标已验证后调用 CF 创建规则并入库。"""
    # 校验源邮箱地址存在且归属当前用户
    email_address = await email_service.get_email_address_or_404(
        session, data.email_address_id, user
    )

    # 同一源邮箱不能同时绑定两个转发目标：存在未删除规则即视为已绑定
    already_bound = (
        await session.execute(
            select(
                exists().where(
                    ForwardingRule.email_address_id == email_address.id,
                    ForwardingRule.is_deleted.is_(False),
                )
            )
        )
    ).scalar_one()
    if already_bound:
        raise AppException(
            "该源邮箱已绑定转发规则，请先删除现有规则后再绑定新目标",
            code=1409,
            http_status=409,
        )

    zone_id, client = await _resolve_zone_and_client(session, email_address)

    # 校验目标邮箱已在 CF 完成验证，未验证则引导用户先验证
    domain = (
        await session.execute(
            select(Domain).where(Domain.id == email_address.domain_id)
        )
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")
    cf_account = (
        await session.execute(
            select(CFAccount).where(CFAccount.id == domain.cf_account_id)
        )
    ).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("CF 账号不存在")
    destination_email = str(data.destination_email)
    await destination_service.ensure_verified(
        session, cf_account.id, destination_email
    )

    payload = _build_rule_payload(email_address.full_address, destination_email)
    result = await client.create_routing_rule(zone_id, payload)
    cf_rule_id = result.get("id") if isinstance(result, dict) else None

    rule = ForwardingRule(
        email_address_id=email_address.id,
        destination_email=destination_email,
        cf_rule_id=cf_rule_id,
        is_active=True,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


async def get_forwarding_rule_or_404(
    session: AsyncSession, rule_id: int, user: User
) -> ForwardingRule:
    """按 id 查询转发规则并校验归属。"""
    stmt = _accessible_rule_stmt(user).where(ForwardingRule.id == rule_id)
    rule = (await session.execute(stmt)).scalar_one_or_none()
    if rule is None:
        raise NotFoundError("转发规则不存在")
    return rule


async def list_forwarding_rules(
    session: AsyncSession,
    user: User,
    page: int,
    size: int,
    email_address_id: int | None = None,
) -> tuple[list[ForwardingRule], int]:
    """分页查询转发规则；可按源邮箱地址过滤。"""
    base = _accessible_rule_stmt(user)
    if email_address_id is not None:
        base = base.where(ForwardingRule.email_address_id == email_address_id)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    result = await session.execute(
        base.order_by(ForwardingRule.id).offset((page - 1) * size).limit(size)
    )
    return list(result.scalars().all()), total


async def update_forwarding_rule(
    session: AsyncSession, rule: ForwardingRule, data: ForwardingRuleUpdate
) -> ForwardingRule:
    """更新转发规则（目前支持启用/停用）。"""
    if data.is_active is not None:
        rule.is_active = data.is_active
    await session.commit()
    await session.refresh(rule)
    return rule


async def delete_forwarding_rule(
    session: AsyncSession, rule: ForwardingRule
) -> None:
    """删除转发规则：先调用 CF 删除规则，再软删除本地记录。"""
    if rule.cf_rule_id:
        email_address = (
            await session.execute(
                select(EmailAddress).where(
                    EmailAddress.id == rule.email_address_id
                )
            )
        ).scalar_one_or_none()
        if email_address is not None:
            zone_id, client = await _resolve_zone_and_client(session, email_address)
            await client.delete_routing_rule(zone_id, rule.cf_rule_id)

    rule.is_deleted = True
    rule.is_active = False
    await session.commit()

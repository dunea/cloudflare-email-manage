"""域名同步与共享逻辑。

同步：从 CF 拉取 Zone 列表，按 cf_account + zone_id 做 upsert。
共享：域名所有者可将域名共享给其他用户使用。
"""

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, NotFoundError, PermissionError
from app.models import CFAccount, Domain, DomainAssignment, User
from app.services.cf_account_service import build_client
from app.services.user_service import get_user_by_username


def _zone_account_id(zone: dict[str, object]) -> str | None:
    """从 Cloudflare Zone 响应中提取 account.id。"""
    account = zone.get("account")
    if not isinstance(account, dict):
        return None
    account_id = account.get("id")
    return str(account_id) if account_id else None


def _assert_zones_match_account(
    zones: list[dict[str, object]], expected_account_id: str
) -> None:
    """确认 Cloudflare 返回的 Zone 均属于当前绑定账号。"""
    for zone in zones:
        account_id = _zone_account_id(zone)
        if account_id is None or account_id == expected_account_id:
            continue
        domain_name = str(zone.get("name") or zone.get("id") or "未知域名")
        raise AppException(
            "Cloudflare 返回的域名与当前绑定 Account 不匹配："
            f"{domain_name} 属于 Account {account_id}，"
            f"不是当前绑定的 {expected_account_id}。"
            "请检查 Token 的 Account/Zone 资源范围，或为该 Account 新增绑定账号。",
            code=1403,
            http_status=status.HTTP_403_FORBIDDEN,
        )


async def sync_domains(
    session: AsyncSession, cf_account: CFAccount, owner: User
) -> list[Domain]:
    """从 CF 同步 cf_account 下的域名，返回同步后的域名列表。

    同步阶段不生成 webhook_secret，留待一键部署 Worker 时由
    worker_deploy_service._prepare_domain_secrets 统一生成，
    避免部署失败时出现"DB 已写但 Worker 未下发"的半成品状态。
    """
    client = build_client(cf_account)
    zones = await client.list_zones(cf_account.account_id)
    _assert_zones_match_account(zones, cf_account.account_id)

    synced: list[Domain] = []
    synced_zone_ids: set[str] = set()
    for zone in zones:
        zone_id = zone.get("id")
        domain_name = zone.get("name")
        if not zone_id or not domain_name:
            continue

        zone_id = str(zone_id)
        synced_zone_ids.add(zone_id)
        status = zone.get("status", "active")
        existing = (
            await session.execute(
                select(Domain).where(
                    Domain.cf_account_id == cf_account.id,
                    Domain.zone_id == zone_id,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            domain = Domain(
                cf_account_id=cf_account.id,
                zone_id=zone_id,
                domain_name=str(domain_name),
                status=str(status),
            )
            session.add(domain)
            synced.append(domain)
        else:
            existing.domain_name = str(domain_name)
            existing.status = str(status)
            synced.append(existing)

    existing_domains = (
        await session.execute(
            select(Domain).where(Domain.cf_account_id == cf_account.id)
        )
    ).scalars()
    for domain in existing_domains:
        if domain.zone_id not in synced_zone_ids:
            domain.status = "unavailable"

    await session.commit()
    for domain in synced:
        await session.refresh(domain)
    return synced


async def list_domains_for_user(
    session: AsyncSession, user: User, page: int, size: int
) -> tuple[list[Domain], int]:
    """分页查询用户可见域名：管理员见全部，普通用户见自有 + 被共享的。"""
    base = select(Domain).join(CFAccount).where(CFAccount.is_deleted.is_(False))
    if user.role != "admin":
        own = (
            select(Domain.id)
            .join(CFAccount)
            .where(
                CFAccount.user_id == user.id,
                CFAccount.is_deleted.is_(False),
            )
        )
        assigned = select(DomainAssignment.domain_id).where(
            DomainAssignment.user_id == user.id
        )
        base = base.where(Domain.id.in_(own) | Domain.id.in_(assigned))

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    result = await session.execute(
        base.order_by(Domain.id).offset((page - 1) * size).limit(size)
    )
    return list(result.scalars().all()), total


async def _user_can_access_domain(
    session: AsyncSession, user: User, domain: Domain
) -> bool:
    """判断普通用户是否可访问该域名（自有或被共享）。"""
    cf_account = (
        await session.execute(
            select(CFAccount).where(
                CFAccount.id == domain.cf_account_id,
                CFAccount.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if cf_account is not None and cf_account.user_id == user.id:
        return True

    assignment = (
        await session.execute(
            select(DomainAssignment).where(
                DomainAssignment.domain_id == domain.id,
                DomainAssignment.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    return assignment is not None


async def _is_domain_owner(
    session: AsyncSession, user: User, domain: Domain
) -> bool:
    """判断用户是否为域名的所有者（域名所属 CF 账号归属该用户）。"""
    cf_account = (
        await session.execute(
            select(CFAccount).where(
                CFAccount.id == domain.cf_account_id,
                CFAccount.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    return cf_account is not None and cf_account.user_id == user.id


async def get_domain_or_404(
    session: AsyncSession, domain_id: int, user: User
) -> Domain:
    """按 id 查询域名并校验访问权限。"""
    domain = (
        await session.execute(select(Domain).where(Domain.id == domain_id))
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")
    cf_account = (
        await session.execute(
            select(CFAccount).where(
                CFAccount.id == domain.cf_account_id,
                CFAccount.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("域名不存在")
    if user.role != "admin" and not await _user_can_access_domain(
        session, user, domain
    ):
        raise NotFoundError("域名不存在")
    return domain


async def assign_domain(
    session: AsyncSession,
    domain_id: int,
    target_user_id: int,
    owner: User,
) -> DomainAssignment:
    """将域名共享给指定用户（仅域名所有者可操作）。"""
    await _get_assignable_domain(session, domain_id, owner)
    target = (
        await session.execute(
            select(User).where(
                User.id == target_user_id, User.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError("目标用户不存在")

    # 不能共享给自己
    if target_user_id == owner.id:
        raise AppException("不能将域名共享给自己", code=1400)

    return await _create_assignment(session, domain_id, target_user_id)


async def assign_domain_by_username(
    session: AsyncSession,
    domain_id: int,
    username: str,
    owner: User,
) -> DomainAssignment:
    """按用户名共享域名（仅域名所有者可操作）。

    先校验操作者权限，再查找目标用户，避免通过错误消息枚举用户是否存在。
    """
    await _get_assignable_domain(session, domain_id, owner)
    target = await get_user_by_username(session, username)
    if target is None:
        raise NotFoundError("目标用户不存在")
    if target.id == owner.id:
        raise AppException("不能将域名共享给自己", code=1400)
    return await _create_assignment(session, domain_id, target.id)


async def _get_assignable_domain(
    session: AsyncSession, domain_id: int, owner: User
) -> Domain:
    """查询域名并校验操作者是否有权共享，无权时抛 NotFound/Permission。"""
    domain = (
        await session.execute(select(Domain).where(Domain.id == domain_id))
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")

    is_owner = await _is_domain_owner(session, owner, domain)
    if not is_owner and owner.role != "admin":
        raise PermissionError("仅域名所有者可共享域名")
    return domain


async def _create_assignment(
    session: AsyncSession, domain_id: int, target_user_id: int
) -> DomainAssignment:
    """创建共享记录，重复共享抛 409。"""
    assignment = DomainAssignment(domain_id=domain_id, user_id=target_user_id)
    session.add(assignment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AppException("该域名已共享给此用户", code=1409, http_status=409) from exc
    await session.refresh(assignment)
    return assignment


async def unassign_domain(
    session: AsyncSession,
    domain_id: int,
    target_user_id: int,
    owner: User,
) -> None:
    """取消某域名对某用户的共享（仅域名所有者可操作）。"""
    domain = (
        await session.execute(select(Domain).where(Domain.id == domain_id))
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")

    is_owner = await _is_domain_owner(session, owner, domain)
    if not is_owner and owner.role != "admin":
        raise PermissionError("仅域名所有者可取消共享")

    assignment = (
        await session.execute(
            select(DomainAssignment).where(
                DomainAssignment.domain_id == domain_id,
                DomainAssignment.user_id == target_user_id,
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError("共享记录不存在")
    await session.delete(assignment)
    await session.commit()


async def list_domain_assignments(
    session: AsyncSession, domain_id: int
) -> list[DomainAssignment]:
    """列出某域名的全部共享记录。"""
    result = await session.execute(
        select(DomainAssignment)
        .where(DomainAssignment.domain_id == domain_id)
        .order_by(DomainAssignment.id)
    )
    return list(result.scalars().all())

"""域名同步与分配逻辑。

同步：从 CF 拉取 Zone 列表，按 cf_account + zone_id 做 upsert。
分配：平台（管理员）域名分配给普通用户使用。
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, NotFoundError
from app.models import CFAccount, Domain, DomainAssignment, User
from app.services.cf_account_service import build_client


def _allowed_zone_set(cf_account: CFAccount) -> set[str] | None:
    """specific 权限下返回允许的 zone_id 集合，all 返回 None（不过滤）。"""
    if cf_account.permission_type != "specific":
        return None
    raw = cf_account.allowed_zone_ids or ""
    return {part for part in raw.split(",") if part}


async def sync_domains(
    session: AsyncSession, cf_account: CFAccount, owner: User
) -> list[Domain]:
    """从 CF 同步 cf_account 下的域名，返回同步后的域名列表。

    管理员账号的域名标记为 platform（可分配），普通用户为 user。
    """
    client = build_client(cf_account)
    zones = await client.list_zones(cf_account.account_id)
    allowed = _allowed_zone_set(cf_account)
    owner_type = "platform" if owner.role == "admin" else "user"

    synced: list[Domain] = []
    for zone in zones:
        zone_id = zone.get("id")
        domain_name = zone.get("name")
        if not zone_id or not domain_name:
            continue
        if allowed is not None and zone_id not in allowed:
            continue

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
                domain_name=domain_name,
                owner_type=owner_type,
                status=status,
            )
            session.add(domain)
            synced.append(domain)
        else:
            existing.domain_name = domain_name
            existing.status = status
            existing.owner_type = owner_type
            synced.append(existing)

    await session.commit()
    for domain in synced:
        await session.refresh(domain)
    return synced


async def list_domains_for_user(
    session: AsyncSession, user: User, page: int, size: int
) -> tuple[list[Domain], int]:
    """分页查询用户可见域名：管理员见全部，普通用户见自有 + 被分配的。"""
    base = select(Domain)
    if user.role != "admin":
        own = select(Domain.id).join(CFAccount).where(CFAccount.user_id == user.id)
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
    """判断普通用户是否可访问该域名（自有或被分配）。"""
    cf_account = (
        await session.execute(
            select(CFAccount).where(CFAccount.id == domain.cf_account_id)
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


async def get_domain_or_404(
    session: AsyncSession, domain_id: int, user: User
) -> Domain:
    """按 id 查询域名并校验访问权限。"""
    domain = (
        await session.execute(select(Domain).where(Domain.id == domain_id))
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")
    if user.role != "admin" and not await _user_can_access_domain(
        session, user, domain
    ):
        raise NotFoundError("域名不存在")
    return domain


async def assign_domain(
    session: AsyncSession, domain_id: int, target_user_id: int
) -> DomainAssignment:
    """将平台域名分配给指定用户（仅平台域名可分配）。"""
    domain = (
        await session.execute(select(Domain).where(Domain.id == domain_id))
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")
    if domain.owner_type != "platform":
        raise AppException("仅平台域名可分配给用户", code=1400)

    target = (
        await session.execute(
            select(User).where(
                User.id == target_user_id, User.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError("目标用户不存在")

    assignment = DomainAssignment(domain_id=domain_id, user_id=target_user_id)
    session.add(assignment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AppException("该域名已分配给此用户", code=1409, http_status=409) from exc
    await session.refresh(assignment)
    return assignment


async def list_domain_assignments(
    session: AsyncSession, domain_id: int
) -> list[DomainAssignment]:
    """列出某域名的全部分配记录。"""
    result = await session.execute(
        select(DomainAssignment)
        .where(DomainAssignment.domain_id == domain_id)
        .order_by(DomainAssignment.id)
    )
    return list(result.scalars().all())


async def unassign_domain(
    session: AsyncSession, domain_id: int, target_user_id: int
) -> None:
    """取消某域名对某用户的分配。"""
    assignment = (
        await session.execute(
            select(DomainAssignment).where(
                DomainAssignment.domain_id == domain_id,
                DomainAssignment.user_id == target_user_id,
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError("分配记录不存在")
    await session.delete(assignment)
    await session.commit()

"""转发目标地址管理逻辑。

目标地址为 CF Email Routing account 级资源，添加后 CF 会向该邮箱发送验证
邮件，邮箱所有者在浏览器完成验证后 CF 标记 verified。本地缓存 verified
状态由同步操作从 CF 刷新；创建转发规则前须调用 ensure_verified 实时校验。
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, NotFoundError
from app.models import CFAccount, DestinationAddress, User
from app.schemas.destination_address import DestinationAddressCreate
from app.services.cf_account_service import build_client


def _parse_verified(value: object) -> tuple[bool, datetime | None]:
    """将 CF 返回的 verified 字段（ISO 字符串或 None）转为本地状态。"""
    if not value:
        return False, None
    if isinstance(value, datetime):
        return True, value
    try:
        return True, datetime.fromisoformat(str(value))
    except ValueError:
        return True, None


async def _get_cf_account_or_404(
    session: AsyncSession, cf_account_id: int, user: User
) -> CFAccount:
    """按 id 查询 CF 账号并校验归属。"""
    cf_account = (
        await session.execute(
            select(CFAccount).where(
                CFAccount.id == cf_account_id, CFAccount.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("CF 账号不存在")
    if user.role != "admin" and cf_account.user_id != user.id:
        raise NotFoundError("CF 账号不存在")
    return cf_account


async def add_destination_address(
    session: AsyncSession, user: User, data: DestinationAddressCreate
) -> DestinationAddress:
    """添加转发目标地址：调用 CF 创建后入库（verified=False）。

    若同账号下已存在软删除的同邮箱记录则复活，否则新建。CF 创建时会向
    目标邮箱发送验证邮件，邮箱所有者需在浏览器完成验证。
    """
    cf_account = await _get_cf_account_or_404(session, data.cf_account_id, user)
    email = str(data.email).lower()

    existing = (
        await session.execute(
            select(DestinationAddress).where(
                DestinationAddress.cf_account_id == cf_account.id,
                func.lower(DestinationAddress.email) == email,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and not existing.is_deleted:
        raise AppException("该目标地址已存在", code=1409, http_status=409)

    client = build_client(cf_account)
    result = await client.create_destination_address(cf_account.account_id, email)
    cf_address_id = result.get("id") if isinstance(result, dict) else None
    if not cf_address_id:
        raise AppException("Cloudflare 未返回目标地址 ID", code=1502)
    verified, verified_at = _parse_verified(
        result.get("verified") if isinstance(result, dict) else None
    )

    if existing is not None:
        existing.is_deleted = False
        existing.user_id = user.id
        existing.email = email
        existing.cf_address_id = str(cf_address_id)
        existing.verified = verified
        existing.verified_at = verified_at
        await session.commit()
        await session.refresh(existing)
        return existing

    address = DestinationAddress(
        cf_account_id=cf_account.id,
        user_id=user.id,
        email=email,
        cf_address_id=str(cf_address_id),
        verified=verified,
        verified_at=verified_at,
    )
    session.add(address)
    await session.commit()
    await session.refresh(address)
    return address


async def sync_destination_addresses(
    session: AsyncSession, user: User, cf_account_id: int
) -> list[DestinationAddress]:
    """从 CF 同步指定账号下的目标地址验证状态，返回同步后列表。"""
    cf_account = await _get_cf_account_or_404(session, cf_account_id, user)
    client = build_client(cf_account)
    remote_items = await client.list_destination_addresses(cf_account.account_id)

    # 索引本地未删除记录，按 cf_address_id 匹配更新
    local_rows = (
        await session.execute(
            select(DestinationAddress).where(
                DestinationAddress.cf_account_id == cf_account.id,
                DestinationAddress.is_deleted.is_(False),
            )
        )
    ).scalars().all()
    local_by_cf_id = {r.cf_address_id: r for r in local_rows}

    seen_cf_ids: set[str] = set()
    for item in remote_items:
        if not isinstance(item, dict):
            continue
        cf_addr_id = item.get("id")
        if not cf_addr_id:
            continue
        seen_cf_ids.add(str(cf_addr_id))
        verified, verified_at = _parse_verified(item.get("verified"))
        local = local_by_cf_id.get(str(cf_addr_id))
        if local is not None:
            local.email = str(item.get("email", local.email)).lower()
            local.verified = verified
            local.verified_at = verified_at

    # CF 侧已不存在的本地记录标记为软删除
    for local in local_rows:
        if local.cf_address_id not in seen_cf_ids:
            local.is_deleted = True

    await session.commit()
    result = (
        await session.execute(
            select(DestinationAddress)
            .where(
                DestinationAddress.cf_account_id == cf_account.id,
                DestinationAddress.is_deleted.is_(False),
            )
            .order_by(DestinationAddress.id)
        )
    ).scalars().all()
    return list(result)


async def get_destination_address_or_404(
    session: AsyncSession, address_id: int, user: User
) -> DestinationAddress:
    """按 id 查询目标地址并校验归属。"""
    address = (
        await session.execute(
            select(DestinationAddress).where(
                DestinationAddress.id == address_id,
                DestinationAddress.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if address is None:
        raise NotFoundError("目标地址不存在")
    if user.role != "admin" and address.user_id != user.id:
        raise NotFoundError("目标地址不存在")
    return address


async def list_destination_addresses(
    session: AsyncSession,
    user: User,
    page: int,
    size: int,
    cf_account_id: int | None = None,
    verified: bool | None = None,
) -> tuple[list[DestinationAddress], int]:
    """分页查询目标地址；管理员查询全部，可按账号与验证状态过滤。"""
    base = select(DestinationAddress).where(DestinationAddress.is_deleted.is_(False))
    if user.role != "admin":
        base = base.where(DestinationAddress.user_id == user.id)
    if cf_account_id is not None:
        base = base.where(DestinationAddress.cf_account_id == cf_account_id)
    if verified is not None:
        base = base.where(DestinationAddress.verified.is_(verified))

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    result = await session.execute(
        base.order_by(DestinationAddress.id)
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(result.scalars().all()), total


async def delete_destination_address(
    session: AsyncSession, address: DestinationAddress
) -> None:
    """删除目标地址：先调用 CF 删除（会停用引用规则），再软删除本地记录。"""
    cf_account = (
        await session.execute(
            select(CFAccount).where(CFAccount.id == address.cf_account_id)
        )
    ).scalar_one_or_none()
    if cf_account is not None:
        client = build_client(cf_account)
        await client.delete_destination_address(
            cf_account.account_id, address.cf_address_id
        )
    address.is_deleted = True
    await session.commit()


async def ensure_verified(
    session: AsyncSession, cf_account_id: int, email: str
) -> DestinationAddress:
    """创建转发规则前实时校验目标邮箱已验证。

    从 CF 实时拉取目标地址列表确认 verified 非空，并刷新本地缓存。
    未验证则抛出业务异常，引导用户先完成验证。
    """
    cf_account = (
        await session.execute(
            select(CFAccount).where(
                CFAccount.id == cf_account_id, CFAccount.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("CF 账号不存在")

    client = build_client(cf_account)
    remote_items = await client.list_destination_addresses(cf_account.account_id)
    target_email = email.lower()
    matched: dict[str, object] | None = None
    for item in remote_items:
        if isinstance(item, dict) and str(item.get("email", "")).lower() == target_email:
            matched = item
            break

    if matched is None:
        raise AppException(
            "目标邮箱尚未添加为目标地址，请先在「目标地址」中添加并验证",
            code=1409,
            http_status=409,
        )
    verified, verified_at = _parse_verified(matched.get("verified"))
    if not verified:
        raise AppException(
            "目标邮箱尚未验证，请前往该邮箱收件箱点击 Cloudflare 发送的验证链接",
            code=1409,
            http_status=409,
        )

    # 刷新本地缓存
    local = (
        await session.execute(
            select(DestinationAddress).where(
                DestinationAddress.cf_account_id == cf_account.id,
                func.lower(DestinationAddress.email) == target_email,
                DestinationAddress.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if local is not None:
        local.verified = True
        local.verified_at = verified_at
        await session.commit()
        await session.refresh(local)
        return local

    # 本地无缓存则补建一条已验证记录
    address = DestinationAddress(
        cf_account_id=cf_account.id,
        user_id=cf_account.user_id,
        email=target_email,
        cf_address_id=str(matched.get("id") or ""),
        verified=True,
        verified_at=verified_at,
    )
    session.add(address)
    await session.commit()
    await session.refresh(address)
    return address

"""邮箱地址管理逻辑。

邮箱地址为平台内逻辑记录，full_address 全局唯一。创建时校验域名可访问，
软删除后可通过再次创建复活同名地址。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, NotFoundError
from app.models import CFAccount, EmailAddress, User
from app.schemas.email_address import EmailAddressCreate, EmailAddressUpdate
from app.services import domain_service
from app.services.cf_account_service import ensure_cf_account_usable


def _new_public_token() -> str:
    """生成无符号 uuid（uuid4().hex，32 位十六进制）作为公开查询令牌。"""
    return uuid.uuid4().hex


async def create_email_address(
    session: AsyncSession, user: User, data: EmailAddressCreate
) -> EmailAddress:
    """创建邮箱地址：校验域名可访问后拼接 full_address 并入库。"""
    # 校验域名存在且当前用户可访问（自有或被分配）
    domain = await domain_service.get_domain_or_404(session, data.domain_id, user)
    cf_account = (
        await session.execute(
            select(CFAccount).where(CFAccount.id == domain.cf_account_id)
        )
    ).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("CF 账号不存在")
    ensure_cf_account_usable(cf_account)
    # 邮箱地址统一小写存储（邮件协议域名不区分大小写，多数实现 local-part 也不区分）
    local_part = data.local_part.lower()
    full_address = f"{local_part}@{domain.domain_name.lower()}"

    existing = (
        await session.execute(
            select(EmailAddress).where(
                func.lower(EmailAddress.full_address) == full_address
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not existing.is_deleted:
            raise AppException("该邮箱地址已存在", code=1409, http_status=409)
        # 复活软删除的记录，归属当前用户
        existing.is_deleted = False
        existing.is_active = True
        existing.user_id = user.id
        existing.domain_id = domain.id
        existing.local_part = local_part
        # 复活时若缺少公开令牌则补一个
        if not existing.public_token:
            existing.public_token = _new_public_token()
        await session.commit()
        await session.refresh(existing)
        return existing

    email_address = EmailAddress(
        domain_id=domain.id,
        user_id=user.id,
        local_part=local_part,
        full_address=full_address,
        public_token=_new_public_token(),
    )
    session.add(email_address)
    await session.commit()
    await session.refresh(email_address)
    return email_address


async def get_email_address_or_404(
    session: AsyncSession, email_address_id: int, user: User
) -> EmailAddress:
    """按 id 查询邮箱地址；非管理员仅能访问自己的地址。"""
    stmt = select(EmailAddress).where(
        EmailAddress.id == email_address_id,
        EmailAddress.is_deleted.is_(False),
    )
    if user.role != "admin":
        stmt = stmt.where(EmailAddress.user_id == user.id)
    email_address = (await session.execute(stmt)).scalar_one_or_none()
    if email_address is None:
        raise NotFoundError("邮箱地址不存在")
    return email_address


async def list_email_addresses(
    session: AsyncSession,
    user: User,
    page: int,
    size: int,
    domain_id: int | None = None,
    order: str = "asc",
) -> tuple[list[EmailAddress], int]:
    """分页查询邮箱地址;管理员查询全部,可按域名过滤、按 id 排序。

    order: "asc"(默认,最旧在前)/ "desc"(最新在前,用于「近 N 条」批量复制/下载)。
    """
    base = select(EmailAddress).where(EmailAddress.is_deleted.is_(False))
    if user.role != "admin":
        base = base.where(EmailAddress.user_id == user.id)
    if domain_id is not None:
        base = base.where(EmailAddress.domain_id == domain_id)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    if order == "desc":
        ordered = base.order_by(EmailAddress.id.desc())
    else:
        ordered = base.order_by(EmailAddress.id)
    result = await session.execute(
        ordered.offset((page - 1) * size).limit(size)
    )
    return list(result.scalars().all()), total


async def update_email_address(
    session: AsyncSession, email_address: EmailAddress, data: EmailAddressUpdate
) -> EmailAddress:
    """更新邮箱地址（目前支持启用/停用）。"""
    if data.is_active is not None:
        email_address.is_active = data.is_active
    await session.commit()
    await session.refresh(email_address)
    return email_address


async def delete_email_address(
    session: AsyncSession, email_address: EmailAddress
) -> None:
    """软删除邮箱地址。"""
    email_address.is_deleted = True
    email_address.is_active = False
    await session.commit()


async def regenerate_public_token(
    session: AsyncSession, email_address: EmailAddress
) -> EmailAddress:
    """重置邮箱地址的公开查询令牌（旧令牌立即失效）。"""
    email_address.public_token = _new_public_token()
    await session.commit()
    await session.refresh(email_address)
    return email_address


async def get_email_address_by_token(
    session: AsyncSession, token: str
) -> EmailAddress | None:
    """按公开令牌查询邮箱地址；要求未删除且启用。"""
    stmt = select(EmailAddress).where(
        EmailAddress.public_token == token,
        EmailAddress.is_deleted.is_(False),
        EmailAddress.is_active.is_(True),
    )
    return (await session.execute(stmt)).scalar_one_or_none()

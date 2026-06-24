"""邮箱地址管理逻辑。

邮箱地址为平台内逻辑记录，full_address 全局唯一。创建时校验域名可访问，
软删除后可通过再次创建复活同名地址。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, NotFoundError
from app.models import EmailAddress, User
from app.schemas.email_address import EmailAddressCreate, EmailAddressUpdate
from app.services import domain_service


async def create_email_address(
    session: AsyncSession, user: User, data: EmailAddressCreate
) -> EmailAddress:
    """创建邮箱地址：校验域名可访问后拼接 full_address 并入库。"""
    # 校验域名存在且当前用户可访问（自有或被分配）
    domain = await domain_service.get_domain_or_404(session, data.domain_id, user)
    full_address = f"{data.local_part}@{domain.domain_name}"

    existing = (
        await session.execute(
            select(EmailAddress).where(EmailAddress.full_address == full_address)
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
        existing.local_part = data.local_part
        await session.commit()
        await session.refresh(existing)
        return existing

    email_address = EmailAddress(
        domain_id=domain.id,
        user_id=user.id,
        local_part=data.local_part,
        full_address=full_address,
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
) -> tuple[list[EmailAddress], int]:
    """分页查询邮箱地址；管理员查询全部，可按域名过滤。"""
    base = select(EmailAddress).where(EmailAddress.is_deleted.is_(False))
    if user.role != "admin":
        base = base.where(EmailAddress.user_id == user.id)
    if domain_id is not None:
        base = base.where(EmailAddress.domain_id == domain_id)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    result = await session.execute(
        base.order_by(EmailAddress.id).offset((page - 1) * size).limit(size)
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

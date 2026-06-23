"""CF 账号绑定逻辑：校验 Token、加密存储、查询与软删除。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, NotFoundError
from app.models import CFAccount, User
from app.schemas.cf_account import CFAccountCreate, CFAccountUpdate
from app.services.cloudflare import CloudflareClient
from app.services.crypto import decrypt_token, encrypt_token


def _join_zone_ids(zone_ids: list[str] | None) -> str | None:
    """将 zone_id 列表合并为逗号分隔字符串入库。"""
    if not zone_ids:
        return None
    return ",".join(zone_ids)


def build_client(cf_account: CFAccount) -> CloudflareClient:
    """根据 CF 账号解密 Token 并构造 CloudflareClient。"""
    token = decrypt_token(cf_account.encrypted_api_token)
    return CloudflareClient(token)


async def bind_cf_account(
    session: AsyncSession, user: User, data: CFAccountCreate
) -> CFAccount:
    """绑定 CF 账号：先校验 Token 有效性，再加密存储。"""
    if data.permission_type == "specific" and not data.allowed_zone_ids:
        raise AppException("权限类型为 specific 时必须提供 allowed_zone_ids", code=1400)

    # 调用 CF 校验 Token（无效会抛出 CloudflareError）
    client = CloudflareClient(data.api_token)
    await client.verify_token()

    cf_account = CFAccount(
        user_id=user.id,
        name=data.name,
        encrypted_api_token=encrypt_token(data.api_token),
        account_id=data.account_id,
        permission_type=data.permission_type,
        allowed_zone_ids=_join_zone_ids(data.allowed_zone_ids),
    )
    session.add(cf_account)
    await session.commit()
    await session.refresh(cf_account)
    return cf_account


async def get_cf_account_or_404(
    session: AsyncSession, account_id: int, user: User
) -> CFAccount:
    """按 id 查询 CF 账号；非管理员仅能访问自己的账号。"""
    stmt = select(CFAccount).where(
        CFAccount.id == account_id, CFAccount.is_deleted.is_(False)
    )
    if user.role != "admin":
        stmt = stmt.where(CFAccount.user_id == user.id)
    cf_account = (await session.execute(stmt)).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("CF 账号不存在")
    return cf_account


async def list_cf_accounts(
    session: AsyncSession, user: User, page: int, size: int
) -> tuple[list[CFAccount], int]:
    """分页查询当前用户的 CF 账号（管理员查询全部）。"""
    base = select(CFAccount).where(CFAccount.is_deleted.is_(False))
    if user.role != "admin":
        base = base.where(CFAccount.user_id == user.id)

    total = (
        await session.execute(
            select(func.count()).select_from(base.subquery())
        )
    ).scalar_one()

    result = await session.execute(
        base.order_by(CFAccount.id).offset((page - 1) * size).limit(size)
    )
    return list(result.scalars().all()), total


async def update_cf_account(
    session: AsyncSession, cf_account: CFAccount, data: CFAccountUpdate
) -> CFAccount:
    """更新 CF 账号；若提供新 Token 则重新校验并加密存储。"""
    if data.name is not None:
        cf_account.name = data.name
    if data.permission_type is not None:
        cf_account.permission_type = data.permission_type
    if data.allowed_zone_ids is not None:
        cf_account.allowed_zone_ids = _join_zone_ids(data.allowed_zone_ids)
    if data.is_active is not None:
        cf_account.is_active = data.is_active
    if data.api_token is not None:
        client = CloudflareClient(data.api_token)
        await client.verify_token()
        cf_account.encrypted_api_token = encrypt_token(data.api_token)

    if (
        cf_account.permission_type == "specific"
        and not cf_account.allowed_zone_ids
    ):
        raise AppException("权限类型为 specific 时必须提供 allowed_zone_ids", code=1400)

    await session.commit()
    await session.refresh(cf_account)
    return cf_account


async def delete_cf_account(session: AsyncSession, cf_account: CFAccount) -> None:
    """软删除 CF 账号。"""
    cf_account.is_deleted = True
    cf_account.is_active = False
    await session.commit()

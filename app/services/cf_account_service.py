"""CF 账号绑定逻辑：校验 Token、自动获取 account_id、加密存储、查询与软删除。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, NotFoundError
from app.models import CFAccount, User
from app.schemas.cf_account import CFAccountCreate, CFAccountUpdate
from app.services.cloudflare import CloudflareClient
from app.services.crypto import decrypt_token, encrypt_token


def build_client(cf_account: CFAccount) -> CloudflareClient:
    """根据 CF 账号解密 Token 并构造 CloudflareClient。"""
    token = decrypt_token(cf_account.encrypted_api_token)
    return CloudflareClient(token)


async def _resolve_account_id(client: CloudflareClient, explicit: str | None) -> str:
    """获取 CF account_id：优先使用用户传入值，否则自动从 Zone 列表提取。

    先调 GET /zones（不带 account.id，仅需 Zone:Zone:Read 权限），
    从第一个 Zone 的 account.id 字段提取 account_id。
    若 Zone 列表为空，再 fallback 到 GET /accounts（需要 Account 权限）。
    """
    if explicit:
        return explicit

    # 方案一：从 Zone 列表提取（只需 Zone:Zone:Read）
    zones = await client.list_zones()
    if zones:
        first_zone = zones[0]
        account = first_zone.get("account")
        if isinstance(account, dict):
            account_id = account.get("id")
            if account_id:
                return str(account_id)

    # 方案二：fallback 到 GET /accounts（需要 Account 权限）
    accounts = await client.list_accounts()
    if accounts:
        first = accounts[0]
        account_id = first.get("id")
        if account_id:
            return str(account_id)

    raise AppException(
        "无法自动获取 Account ID：Token 下没有可访问的域名，"
        "也无法读取账户列表。请手动填写 Account ID。",
        code=1400,
    )


async def bind_cf_account(
    session: AsyncSession, user: User, data: CFAccountCreate
) -> CFAccount:
    """绑定 CF 账号：校验 Token、自动获取 account_id、加密存储。"""
    # 调用 CF 校验 Token（无效会抛出 CloudflareError）
    client = CloudflareClient(data.api_token)
    await client.verify_token()

    # 自动获取 account_id（用户未传时从 CF API 拉取）
    account_id = await _resolve_account_id(client, data.account_id)

    cf_account = CFAccount(
        user_id=user.id,
        name=data.name,
        encrypted_api_token=encrypt_token(data.api_token),
        account_id=account_id,
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
    """更新 CF 账号；若提供新 Token 则重新校验、自动获取 account_id 并加密存储。"""
    if data.name is not None:
        cf_account.name = data.name
    if data.is_active is not None:
        cf_account.is_active = data.is_active
    if data.api_token is not None:
        client = CloudflareClient(data.api_token)
        await client.verify_token()
        cf_account.encrypted_api_token = encrypt_token(data.api_token)
        # 更新 Token 后自动刷新 account_id
        cf_account.account_id = await _resolve_account_id(client, None)

    await session.commit()
    await session.refresh(cf_account)
    return cf_account


async def delete_cf_account(session: AsyncSession, cf_account: CFAccount) -> None:
    """软删除 CF 账号。"""
    cf_account.is_deleted = True
    cf_account.is_active = False
    await session.commit()

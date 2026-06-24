"""API Key 管理逻辑：生成、哈希存储、认证与 CRUD。

API Key 为高熵随机串，仅存储其 SHA-256 哈希（确定性哈希以支持按哈希查找），
原始值只在创建时返回一次，禁止明文落库。
"""

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AuthError, NotFoundError
from app.models import APIKey, User
from app.schemas.api_key import APIKeyCreate, APIKeyUpdate

# API Key 前缀，便于识别与吊销
_KEY_PREFIX = "cfem_"


def generate_api_key() -> str:
    """生成一个高熵随机 API Key 原始串。"""
    return f"{_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    """对 API Key 做 SHA-256 哈希（确定性，便于按哈希查找比对）。"""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def create_api_key(
    session: AsyncSession, user: User, data: APIKeyCreate
) -> tuple[APIKey, str]:
    """创建 API Key，返回 (记录, 原始 key)；原始 key 仅此一次可见。"""
    raw_key = generate_api_key()
    api_key = APIKey(
        user_id=user.id,
        key_hash=hash_api_key(raw_key),
        name=data.name,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return api_key, raw_key


async def get_api_key_or_404(
    session: AsyncSession, key_id: int, user: User
) -> APIKey:
    """按 id 查询 API Key；非管理员仅能访问自己的。"""
    stmt = select(APIKey).where(
        APIKey.id == key_id, APIKey.is_deleted.is_(False)
    )
    if user.role != "admin":
        stmt = stmt.where(APIKey.user_id == user.id)
    api_key = (await session.execute(stmt)).scalar_one_or_none()
    if api_key is None:
        raise NotFoundError("API Key 不存在")
    return api_key


async def list_api_keys(
    session: AsyncSession, user: User, page: int, size: int
) -> tuple[list[APIKey], int]:
    """分页查询当前用户的 API Key（管理员查询全部）。"""
    base = select(APIKey).where(APIKey.is_deleted.is_(False))
    if user.role != "admin":
        base = base.where(APIKey.user_id == user.id)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    result = await session.execute(
        base.order_by(APIKey.id).offset((page - 1) * size).limit(size)
    )
    return list(result.scalars().all()), total


async def update_api_key(
    session: AsyncSession, api_key: APIKey, data: APIKeyUpdate
) -> APIKey:
    """更新 API Key（重命名 / 启用停用）。"""
    if data.name is not None:
        api_key.name = data.name
    if data.is_active is not None:
        api_key.is_active = data.is_active
    await session.commit()
    await session.refresh(api_key)
    return api_key


async def delete_api_key(session: AsyncSession, api_key: APIKey) -> None:
    """软删除 API Key。"""
    api_key.is_deleted = True
    api_key.is_active = False
    await session.commit()


async def authenticate_api_key(session: AsyncSession, raw_key: str) -> User:
    """校验 X-API-Key 原始串，返回所属用户并更新 last_used_at。"""
    key_hash = hash_api_key(raw_key)
    api_key = (
        await session.execute(
            select(APIKey).where(
                APIKey.key_hash == key_hash,
                APIKey.is_deleted.is_(False),
                APIKey.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if api_key is None:
        raise AuthError("无效的 API Key")

    user = (
        await session.execute(
            select(User).where(
                User.id == api_key.user_id, User.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("用户不存在或已被禁用")

    # 记录最近一次使用时间（tz-aware UTC）
    api_key.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    return user

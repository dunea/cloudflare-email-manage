"""用户管理逻辑：查询、列表与更新用户。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, NotFoundError
from app.models import User
from app.schemas.user import UserUpdate
from app.services.auth_service import hash_password


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    """按 id 查询未删除的用户。"""
    result = await session.execute(
        select(User).where(User.id == user_id, User.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


async def get_user_or_404(session: AsyncSession, user_id: int) -> User:
    """按 id 查询用户，不存在抛出 NotFoundError。"""
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("用户不存在")
    return user


async def get_user_by_username(
    session: AsyncSession, username: str
) -> User | None:
    """按用户名查询未删除的用户。"""
    result = await session.execute(
        select(User).where(
            User.username == username, User.is_deleted.is_(False)
        )
    )
    return result.scalar_one_or_none()


async def get_users_by_ids(
    session: AsyncSession, user_ids: list[int]
) -> list[User]:
    """按 id 批量查询用户，返回按 id 升序排列的列表。"""
    if not user_ids:
        return []
    result = await session.execute(
        select(User)
        .where(User.id.in_(user_ids), User.is_deleted.is_(False))
        .order_by(User.id)
    )
    return list(result.scalars().all())


async def list_users(
    session: AsyncSession, page: int, size: int
) -> tuple[list[User], int]:
    """分页查询用户列表，返回 (用户列表, 总数)。"""
    total_result = await session.execute(
        select(func.count()).select_from(User).where(User.is_deleted.is_(False))
    )
    total = total_result.scalar_one()

    result = await session.execute(
        select(User)
        .where(User.is_deleted.is_(False))
        .order_by(User.id)
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(result.scalars().all()), total


async def update_user(
    session: AsyncSession, user: User, data: UserUpdate
) -> User:
    """更新用户邮箱与密码（仅更新提供的字段）。"""
    if data.email is not None and data.email != user.email:
        exists = await session.execute(
            select(User).where(User.email == data.email, User.id != user.id)
        )
        if exists.scalars().first() is not None:
            raise AppException("邮箱已被占用", code=1409, http_status=409)
        user.email = data.email

    if data.password is not None:
        user.hashed_password = hash_password(data.password)

    await session.commit()
    await session.refresh(user)
    return user

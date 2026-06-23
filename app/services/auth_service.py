"""认证与安全：密码哈希、JWT 令牌、注册/登录/刷新逻辑。

说明：因 passlib 1.7.4 与 bcrypt 5.x / Python 3.13+ 不兼容，
此处直接使用 bcrypt 库完成密码哈希与校验。
"""

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.exceptions import AppException, AuthError
from app.models import User
from app.schemas.user import Token, UserCreate

# JWT 签名算法
ALGORITHM = "HS256"
# 令牌类型常量（写入 payload 的 type 字段，区分访问/刷新令牌）
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
# bcrypt 单次最多处理 72 字节，超出部分需手动截断
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    """使用 bcrypt 生成密码哈希。"""
    pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配。"""
    try:
        pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(pw, hashed.encode("utf-8"))
    except ValueError:
        # 哈希格式非法时视为校验失败
        return False


def _create_token(user_id: int, token_type: str, expires_delta: timedelta) -> str:
    """生成指定类型的 JWT 令牌。"""
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(user_id: int) -> str:
    """生成访问令牌。"""
    return _create_token(
        user_id,
        ACCESS_TOKEN_TYPE,
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def create_refresh_token(user_id: int) -> str:
    """生成刷新令牌。"""
    return _create_token(
        user_id,
        REFRESH_TOKEN_TYPE,
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )


def decode_token(token: str) -> dict[str, object]:
    """解码并校验 JWT 令牌，失败抛出 AuthError。"""
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise AuthError("令牌无效或已过期") from exc


def issue_tokens(user: User) -> Token:
    """为用户签发访问令牌与刷新令牌。"""
    return Token(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
        token_type="bearer",
    )


async def register_user(session: AsyncSession, data: UserCreate) -> User:
    """注册新用户：校验唯一性后写入数据库。"""
    result = await session.execute(
        select(User).where(
            (User.username == data.username) | (User.email == data.email)
        )
    )
    if result.scalars().first() is not None:
        raise AppException("用户名或邮箱已被注册", code=1409, http_status=409)

    user = User(
        username=data.username,
        email=data.email,
        hashed_password=hash_password(data.password),
        role="user",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate_user(
    session: AsyncSession, username: str, password: str
) -> User:
    """校验登录凭证，返回用户；失败抛出 AuthError。"""
    result = await session.execute(
        select(User).where(
            (User.username == username) | (User.email == username),
            User.is_deleted.is_(False),
        )
    )
    user = result.scalars().first()
    if user is None or not verify_password(password, user.hashed_password):
        raise AuthError("用户名或密码错误")
    if not user.is_active:
        raise AuthError("账号已被禁用")
    return user


async def refresh_tokens(session: AsyncSession, refresh_token: str) -> Token:
    """使用刷新令牌换取新的令牌对。"""
    payload = decode_token(refresh_token)
    if payload.get("type") != REFRESH_TOKEN_TYPE:
        raise AuthError("无效的刷新令牌")

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.isdigit():
        raise AuthError("令牌主体无效")

    result = await session.execute(
        select(User).where(User.id == int(sub), User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("用户不存在或已被禁用")
    return issue_tokens(user)


async def ensure_admin_user(session: AsyncSession) -> User | None:
    """首次启动时若不存在管理员账号则自动创建，返回新建用户或 None。"""
    result = await session.execute(select(User).where(User.role == "admin"))
    if result.scalars().first() is not None:
        return None

    admin = User(
        username="admin",
        email=settings.ADMIN_EMAIL,
        hashed_password=hash_password(settings.ADMIN_PASSWORD),
        role="admin",
    )
    session.add(admin)
    await session.commit()
    await session.refresh(admin)
    return admin

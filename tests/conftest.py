"""pytest 测试夹具：内存数据库与 AsyncClient。

测试隔离：每个测试用例使用独立的 SQLite 内存数据库，互不干扰。
"""

from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# 导入 models 确保所有表注册到 Base.metadata
import app.models  # noqa: F401
from app.database import Base, get_session
from app.main import app
from app.services.cloudflare import _reset_fake_destination_addresses
from app.services.rate_limit import reset_rate_limits


@pytest.fixture(autouse=True)
def reset_fake_cloudflare_state() -> Iterator[None]:
    """每个测试前后清理假 CF 内存状态。"""
    _reset_fake_destination_addresses()
    reset_rate_limits()
    yield
    _reset_fake_destination_addresses()
    reset_rate_limits()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """为单个测试创建独立的内存数据库会话。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 共享单连接，保证内存库在测试内持久
    )

    # 建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_maker() as session:
        yield session

    # 清理
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """提供已注入测试数据库会话的 HTTP 异步客户端。"""

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()

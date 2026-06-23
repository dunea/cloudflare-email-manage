"""冒烟测试：验证应用、数据库会话与测试夹具可正常工作。"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def test_health(client: AsyncClient) -> None:
    """健康检查端点返回统一成功响应。"""
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"


async def test_db_session_create_and_query(db_session: AsyncSession) -> None:
    """内存数据库可写入并查询数据。"""
    user = User(
        username="alice",
        email="alice@example.com",
        hashed_password="x",
        role="user",
    )
    db_session.add(user)
    await db_session.commit()

    result = await db_session.execute(select(User).where(User.username == "alice"))
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.email == "alice@example.com"
    assert fetched.is_active is True

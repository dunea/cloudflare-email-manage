"""前端 API Key / 个人资料 / 管理后台页面测试。"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import APIKey, User


async def _web_login(
    client: AsyncClient,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "password123",
) -> None:
    await client.post(
        "/register",
        data={"username": username, "email": email, "password": password},
    )
    await client.post("/login", data={"username": username, "password": password})


async def _get_user(db_session: AsyncSession, username: str = "alice") -> User:
    return (
        await db_session.execute(select(User).where(User.username == username))
    ).scalar_one()


# ---- API Key ----


async def test_api_keys_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api-keys", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_create_api_key_shows_once(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.post(
        "/api-keys", data={"name": "脚本"}, follow_redirects=False
    )
    assert resp.status_code == 303

    first = await client.get("/api-keys")
    assert "cfem_" in first.text
    assert "仅显示一次" in first.text

    second = await client.get("/api-keys")
    assert "cfem_" not in second.text


async def test_rename_api_key(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _web_login(client)
    await client.post("/api-keys", data={"name": "旧名"})
    key = (await db_session.execute(select(APIKey))).scalar_one()

    resp = await client.post(
        f"/api-keys/{key.id}/rename", data={"name": "新名"}, follow_redirects=False
    )
    assert resp.status_code == 303
    listing = await client.get("/api-keys")
    assert "新名" in listing.text


async def test_toggle_and_delete_api_key(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _web_login(client)
    await client.post("/api-keys", data={"name": "脚本"})
    key = (await db_session.execute(select(APIKey))).scalar_one()

    await client.post(f"/api-keys/{key.id}/toggle", follow_redirects=False)
    await db_session.refresh(key)
    assert key.is_active is False

    await client.post(f"/api-keys/{key.id}/delete", follow_redirects=False)
    await db_session.refresh(key)
    assert key.is_deleted is True


# ---- 个人资料 ----


async def test_profile_page_renders(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.get("/profile")
    assert resp.status_code == 200
    assert "alice" in resp.text


async def test_profile_update_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _web_login(client)
    resp = await client.post(
        "/profile",
        data={"email": "new@example.com", "password": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    user = await _get_user(db_session)
    assert user.email == "new@example.com"


async def test_profile_update_short_password(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.post(
        "/profile",
        data={"email": "alice@example.com", "password": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "输入有误" in resp.text


# ---- 管理后台 ----


async def test_admin_users_requires_admin(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.get("/admin/users", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


async def test_admin_users_list_and_detail(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _web_login(client)
    alice = await _get_user(db_session)
    alice.role = "admin"
    await db_session.commit()

    listing = await client.get("/admin/users")
    assert listing.status_code == 200
    assert "alice" in listing.text

    detail = await client.get(f"/admin/users/{alice.id}")
    assert detail.status_code == 200
    assert "alice@example.com" in detail.text

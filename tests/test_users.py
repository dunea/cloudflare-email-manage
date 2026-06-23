"""用户接口测试：当前用户信息、更新与管理员用户列表。"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.auth_service import ensure_admin_user


async def _register_and_login(
    client: AsyncClient,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "password123",
) -> str:
    """注册并登录，返回访问令牌。"""
    await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    return login.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    """构造 Bearer 认证头。"""
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(client: AsyncClient) -> str:
    """通过管理员账号登录获取令牌。"""
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": settings.ADMIN_EMAIL, "password": settings.ADMIN_PASSWORD},
    )
    return login.json()["data"]["access_token"]


async def test_me_requires_auth(client: AsyncClient) -> None:
    """未携带令牌访问 /me 返回 401。"""
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == 1401


async def test_me_invalid_token(client: AsyncClient) -> None:
    """非法令牌访问 /me 返回 401。"""
    resp = await client.get("/api/v1/users/me", headers=_auth("not-a-jwt"))
    assert resp.status_code == 401


async def test_me_success(client: AsyncClient) -> None:
    """携带有效令牌返回当前用户信息。"""
    token = await _register_and_login(client)
    resp = await client.get("/api/v1/users/me", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "alice"
    assert data["role"] == "user"


async def test_update_me_email(client: AsyncClient) -> None:
    """更新当前用户邮箱成功。"""
    token = await _register_and_login(client)
    resp = await client.patch(
        "/api/v1/users/me",
        headers=_auth(token),
        json={"email": "new@example.com"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["email"] == "new@example.com"


async def test_update_me_password(client: AsyncClient) -> None:
    """更新密码后旧密码失效、新密码可登录。"""
    token = await _register_and_login(client)
    resp = await client.patch(
        "/api/v1/users/me",
        headers=_auth(token),
        json={"password": "newpassword123"},
    )
    assert resp.status_code == 200

    old = await client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "password123"},
    )
    assert old.status_code == 401
    new = await client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "newpassword123"},
    )
    assert new.status_code == 200


async def test_update_me_duplicate_email(client: AsyncClient) -> None:
    """更新到已被占用的邮箱返回 409。"""
    await client.post(
        "/api/v1/auth/register",
        json={"username": "bob", "email": "bob@example.com", "password": "password123"},
    )
    token = await _register_and_login(client)
    resp = await client.patch(
        "/api/v1/users/me",
        headers=_auth(token),
        json={"email": "bob@example.com"},
    )
    assert resp.status_code == 409


async def test_list_users_requires_admin(client: AsyncClient) -> None:
    """普通用户访问用户列表返回 403。"""
    token = await _register_and_login(client)
    resp = await client.get("/api/v1/users", headers=_auth(token))
    assert resp.status_code == 403
    assert resp.json()["code"] == 1403


async def test_admin_can_list_users(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """管理员可分页查询用户列表。"""
    admin = await ensure_admin_user(db_session)
    assert admin is not None
    token = await _admin_token(client)

    await client.post(
        "/api/v1/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
    )
    resp = await client.get("/api/v1/users", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] >= 2
    usernames = [u["username"] for u in data["items"]]
    assert "admin" in usernames
    assert "alice" in usernames


async def test_admin_get_user_by_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """管理员可按 id 查询指定用户。"""
    await ensure_admin_user(db_session)
    token = await _admin_token(client)

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
    )
    user_id = reg.json()["data"]["id"]
    resp = await client.get(f"/api/v1/users/{user_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "alice"


async def test_admin_get_user_not_found(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """查询不存在用户返回 404。"""
    await ensure_admin_user(db_session)
    token = await _admin_token(client)
    resp = await client.get("/api/v1/users/99999", headers=_auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == 1404


async def test_ensure_admin_idempotent(db_session: AsyncSession) -> None:
    """首次创建管理员成功，再次调用不重复创建。"""
    first = await ensure_admin_user(db_session)
    assert first is not None
    assert first.role == "admin"
    assert first.email == settings.ADMIN_EMAIL
    second = await ensure_admin_user(db_session)
    assert second is None

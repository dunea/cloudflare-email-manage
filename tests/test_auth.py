"""认证接口测试：注册、登录、刷新令牌。

测试中 CF API 无涉及；数据库为每个用例独立的内存库。
"""

import pytest
from httpx import AsyncClient

from app.config import settings


async def _register(
    client: AsyncClient,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "password123",
) -> AsyncClient:
    """注册辅助函数，返回响应。"""
    return await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": email, "password": password},
    )


async def test_register_success(client: AsyncClient) -> None:
    """注册成功返回 201 与用户信息，且不泄露密码。"""
    resp = await _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["username"] == "alice"
    assert data["email"] == "alice@example.com"
    assert data["role"] == "user"
    assert data["is_active"] is True
    assert "hashed_password" not in data
    assert "password" not in data


async def test_register_duplicate_username(client: AsyncClient) -> None:
    """用户名重复返回 409。"""
    await _register(client)
    resp = await _register(client, email="other@example.com")
    assert resp.status_code == 409
    assert resp.json()["code"] == 1409


async def test_register_duplicate_email(client: AsyncClient) -> None:
    """邮箱重复返回 409。"""
    await _register(client)
    resp = await _register(client, username="bob")
    assert resp.status_code == 409


async def test_register_short_password(client: AsyncClient) -> None:
    """密码过短触发参数校验失败 422。"""
    resp = await _register(client, password="short")
    assert resp.status_code == 422
    assert resp.json()["code"] == 1422


async def test_register_invalid_email(client: AsyncClient) -> None:
    """非法邮箱触发参数校验失败 422。"""
    resp = await client.post(
        "/api/v1/auth/register",
        json={"username": "carol", "email": "not-an-email", "password": "password123"},
    )
    assert resp.status_code == 422


async def test_login_success(client: AsyncClient) -> None:
    """登录成功返回访问与刷新令牌。"""
    await _register(client)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "password123"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"


async def test_login_with_email(client: AsyncClient) -> None:
    """支持使用邮箱登录。"""
    await _register(client)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "alice@example.com", "password": "password123"},
    )
    assert resp.status_code == 200


async def test_login_wrong_password(client: AsyncClient) -> None:
    """密码错误返回 401。"""
    await _register(client)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "wrongpass1"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 1401


async def test_login_nonexistent_user(client: AsyncClient) -> None:
    """用户不存在返回 401。"""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "ghost", "password": "whatever1"},
    )
    assert resp.status_code == 401


async def test_api_login_rate_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 登录连续失败超过阈值后返回 429。"""
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60)

    first = await client.post(
        "/api/v1/auth/login",
        json={"username": "ghost", "password": "wrongpass1"},
    )
    assert first.status_code == 401
    second = await client.post(
        "/api/v1/auth/login",
        json={"username": "ghost", "password": "wrongpass1"},
    )
    assert second.status_code == 429


async def test_refresh_success(client: AsyncClient) -> None:
    """使用刷新令牌换取新令牌对。"""
    await _register(client)
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "password123"},
    )
    refresh_token = login.json()["data"]["refresh_token"]
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"


async def test_refresh_rejects_access_token(client: AsyncClient) -> None:
    """使用访问令牌作为刷新令牌应被拒绝。"""
    await _register(client)
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "password123"},
    )
    access_token = login.json()["data"]["access_token"]
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": access_token}
    )
    assert resp.status_code == 401


async def test_refresh_invalid_token(client: AsyncClient) -> None:
    """非法刷新令牌返回 401。"""
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": "garbage.token.value"}
    )
    assert resp.status_code == 401

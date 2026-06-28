"""前端认证页面冒烟测试：登录/注册/登出与 Cookie 会话。

复用 conftest 的 client（内存数据库 + ASGITransport）。
"""

import pytest
from httpx import AsyncClient

from app.config import settings


async def test_login_page_renders(client: AsyncClient) -> None:
    """登录页可正常渲染。"""
    resp = await client.get("/login")
    assert resp.status_code == 200
    assert "登录" in resp.text


async def test_register_page_renders(client: AsyncClient) -> None:
    """注册页可正常渲染。"""
    resp = await client.get("/register")
    assert resp.status_code == 200
    assert "注册" in resp.text


async def test_root_renders_landing_when_anonymous(client: AsyncClient) -> None:
    """未登录访问首页渲染营销落地页（可被搜索引擎抓取）。"""
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 200
    assert "免费注册" in resp.text
    # SEO 元信息存在
    assert 'name="description"' in resp.text
    assert 'property="og:title"' in resp.text


async def test_root_redirects_to_dashboard_when_authed(client: AsyncClient) -> None:
    """已登录访问首页重定向到仪表盘。"""
    await client.post(
        "/register",
        data={
            "username": "carol",
            "email": "carol@example.com",
            "password": "password123",
        },
    )
    await client.post("/login", data={"username": "carol", "password": "password123"})
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


async def test_dashboard_requires_auth(client: AsyncClient) -> None:
    """未登录访问仪表盘重定向到登录页。"""
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_register_then_login_flow(client: AsyncClient) -> None:
    """注册成功后跳登录页，登录后下发 Cookie 并可访问仪表盘。"""
    resp = await client.post(
        "/register",
        data={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    resp = await client.post(
        "/login",
        data={"username": "alice", "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert "access_token" in resp.cookies

    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "alice" in resp.text


async def test_login_invalid_credentials_rerenders(client: AsyncClient) -> None:
    """凭证错误时回填表单并显示错误提示。"""
    resp = await client.post(
        "/login",
        data={"username": "ghost", "password": "wrongpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "用户名或密码错误" in resp.text


async def test_login_rate_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续登录失败超过阈值后返回 429。"""
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60)

    first = await client.post(
        "/login",
        data={"username": "ghost", "password": "wrongpass"},
        follow_redirects=False,
    )
    assert first.status_code == 400

    second = await client.post(
        "/login",
        data={"username": "ghost", "password": "wrongpass"},
        follow_redirects=False,
    )
    assert second.status_code == 429
    assert "请求过于频繁" in second.text


async def test_register_short_password_rerenders(client: AsyncClient) -> None:
    """密码过短时不创建用户，回填表单并提示。"""
    resp = await client.post(
        "/register",
        data={"username": "shorty", "email": "s@example.com", "password": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "输入有误" in resp.text


async def test_logout_clears_session(client: AsyncClient) -> None:
    """登出后清除 Cookie，再访问受保护页面被拦截。"""
    await client.post(
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "password123"},
    )
    await client.post(
        "/login", data={"username": "bob", "password": "password123"}
    )
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200

    resp = await client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")

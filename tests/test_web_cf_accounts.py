"""前端 CF 账号页面测试：列表 / 绑定 / 同步 / 编辑 / 解绑。

所有 Cloudflare 调用通过 monkeypatch 替换 CloudflareClient 方法，不发真实请求。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CloudflareError
from app.models import CFAccount
from app.services.cloudflare import CloudflareClient
from app.services.crypto import decrypt_token


async def _web_login(
    client: AsyncClient,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "password123",
) -> None:
    """通过前端注册并登录，cookie 由 client 自动保存。"""
    await client.post(
        "/register",
        data={"username": username, "email": email, "password": password},
    )
    await client.post("/login", data={"username": username, "password": password})


def _patch_verify_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    monkeypatch.setattr(CloudflareClient, "verify_token", _verify)


def _patch_verify_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify(self: CloudflareClient) -> dict[str, str]:
        raise CloudflareError("Token 无效")

    monkeypatch.setattr(CloudflareClient, "verify_token", _verify)


def _patch_list_zones(
    monkeypatch: pytest.MonkeyPatch, zones: list[dict[str, str]]
) -> None:
    async def _list(self: CloudflareClient, account_id: str) -> list[dict[str, str]]:
        return zones

    monkeypatch.setattr(CloudflareClient, "list_zones", _list)


async def _bind(
    client: AsyncClient,
    *,
    name: str = "主账号",
    api_token: str = "tok",
    account_id: str = "acc-1",
    permission_type: str = "all",
    allowed_zone_ids: str = "",
) -> object:
    return await client.post(
        "/cf-accounts",
        data={
            "name": name,
            "api_token": api_token,
            "account_id": account_id,
            "permission_type": permission_type,
            "allowed_zone_ids": allowed_zone_ids,
        },
        follow_redirects=False,
    )


async def test_cf_accounts_requires_auth(client: AsyncClient) -> None:
    """未登录访问列表跳登录页。"""
    resp = await client.get("/cf-accounts", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_dashboard_renders_stats(client: AsyncClient) -> None:
    """登录后仪表盘渲染统计与最近收件区块。"""
    await _web_login(client)
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "最近收件" in resp.text


async def test_bind_cf_account_success(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定成功后跳列表，Token 加密入库，列表可见。"""
    _patch_verify_ok(monkeypatch)
    await _web_login(client)
    resp = await _bind(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cf-accounts"

    row = (await db_session.execute(select(CFAccount))).scalar_one()
    assert row.name == "主账号"
    assert decrypt_token(row.encrypted_api_token) == "tok"

    listing = await client.get("/cf-accounts")
    assert listing.status_code == 200
    assert "主账号" in listing.text


async def test_bind_cf_account_invalid_token(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token 校验失败回填表单并提示，不落库。"""
    _patch_verify_fail(monkeypatch)
    await _web_login(client)
    resp = await _bind(client)
    assert resp.status_code == 400
    assert "Token 无效" in resp.text
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


async def test_sync_domains_flashes_count(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步域名后在详情页提示同步数量。"""
    _patch_verify_ok(monkeypatch)
    _patch_list_zones(monkeypatch, [{"id": "z1", "name": "a.com", "status": "active"}])
    await _web_login(client)
    await _bind(client)
    account = (await db_session.execute(select(CFAccount))).scalar_one()

    resp = await client.post(
        f"/cf-accounts/{account.id}/sync", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/cf-accounts/{account.id}"

    detail = await client.get(f"/cf-accounts/{account.id}")
    assert detail.status_code == 200
    assert "已同步 1 个域名" in detail.text


async def test_edit_cf_account_renames(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编辑账号名称生效。"""
    _patch_verify_ok(monkeypatch)
    await _web_login(client)
    await _bind(client)
    account = (await db_session.execute(select(CFAccount))).scalar_one()

    resp = await client.post(
        f"/cf-accounts/{account.id}/edit",
        data={
            "name": "新名称",
            "permission_type": "all",
            "allowed_zone_ids": "",
            "api_token": "",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail = await client.get(f"/cf-accounts/{account.id}")
    assert "新名称" in detail.text


async def test_delete_cf_account(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解绑后列表不再可见。"""
    _patch_verify_ok(monkeypatch)
    await _web_login(client)
    await _bind(client)
    account = (await db_session.execute(select(CFAccount))).scalar_one()

    resp = await client.post(
        f"/cf-accounts/{account.id}/delete", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cf-accounts"

    listing = await client.get("/cf-accounts")
    assert "主账号" not in listing.text


async def test_cf_account_detail_not_found(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """访问不存在的账号详情返回 404 页面。"""
    await _web_login(client)
    resp = await client.get("/cf-accounts/99999")
    assert resp.status_code == 404
    assert "不存在" in resp.text
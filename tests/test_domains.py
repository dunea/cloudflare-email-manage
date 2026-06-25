"""域名同步与共享测试。

所有 Cloudflare 调用通过 monkeypatch 替换 CloudflareClient 方法 Mock，
不发出真实网络请求。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.auth_service import ensure_admin_user
from app.services.cloudflare import CloudflareClient

# 模拟 CF 返回的 Zone 列表
ZONES = [
    {"id": "zone1", "name": "example.com", "status": "active"},
    {"id": "zone2", "name": "example.org", "status": "active"},
]


# ---- 通用辅助 ----


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


async def _admin_token(client: AsyncClient) -> str:
    """登录管理员账号获取令牌。"""
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": settings.ADMIN_EMAIL, "password": settings.ADMIN_PASSWORD},
    )
    return login.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    """构造 Bearer 认证头。"""
    return {"Authorization": f"Bearer {token}"}


def _patch_cf(
    monkeypatch: pytest.MonkeyPatch, zones: list[dict[str, str]] | None = None
) -> None:
    """Mock CloudflareClient 的 verify_token、list_accounts 与 list_zones。"""

    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _fake_list_accounts(self: CloudflareClient) -> list[dict[str, str]]:
        return [{"id": "acc-123", "name": "test-account"}]

    async def _fake_list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, str]]:
        return ZONES if zones is None else zones

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)
    monkeypatch.setattr(CloudflareClient, "list_accounts", _fake_list_accounts)
    monkeypatch.setattr(CloudflareClient, "list_zones", _fake_list_zones)


async def _bind(
    client: AsyncClient,
    token: str,
    *,
    account_id: str | None = "acc-123",
) -> int:
    """绑定 CF 账号，返回账号 id。"""
    payload: dict[str, object] = {
        "name": "主账号",
        "api_token": "cf-token",
    }
    if account_id is not None:
        payload["account_id"] = account_id
    resp = await client.post(
        "/api/v1/cf-accounts", headers=_auth(token), json=payload
    )
    return resp.json()["data"]["id"]


# ---- 同步 ----


async def test_sync_domains(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步拉取 CF 域名并入库。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)

    resp = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["synced"] == 2
    names = {d["domain_name"] for d in data["domains"]}
    assert names == {"example.com", "example.org"}


async def test_sync_is_idempotent(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复同步不产生重复域名（按 zone_id upsert）。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)

    await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )
    await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )

    listing = await client.get("/api/v1/domains", headers=_auth(token))
    assert listing.json()["data"]["total"] == 2


async def test_sync_requires_auth(client: AsyncClient) -> None:
    """未认证同步返回 401。"""
    resp = await client.post("/api/v1/cf-accounts/1/sync")
    assert resp.status_code == 401


async def test_sync_unknown_account(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步不存在的账号返回 404。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    resp = await client.post(
        "/api/v1/cf-accounts/99999/sync", headers=_auth(token)
    )
    assert resp.status_code == 404


# ---- 列表 / 详情 / 访问隔离 ----


async def test_list_domains_empty(client: AsyncClient) -> None:
    """无域名时列表为空。"""
    token = await _register_and_login(client)
    resp = await client.get("/api/v1/domains", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 0


async def test_get_domain(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 id 获取域名详情。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)
    sync = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )
    domain_id = sync.json()["data"]["domains"][0]["id"]

    resp = await client.get(f"/api/v1/domains/{domain_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == domain_id


async def test_domain_access_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户无法访问他人的域名。"""
    _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    account_id = await _bind(client, token_a)
    sync = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token_a)
    )
    domain_id = sync.json()["data"]["domains"][0]["id"]

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.get(f"/api/v1/domains/{domain_id}", headers=_auth(token_b))
    assert resp.status_code == 404
    listing = await client.get("/api/v1/domains", headers=_auth(token_b))
    assert listing.json()["data"]["total"] == 0


# ---- 域名共享（所有者可操作）----


async def _setup_owner_domain(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, int]:
    """普通用户绑定并同步域名，返回 (用户令牌, 域名 id)。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)
    sync = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )
    domain_id = sync.json()["data"]["domains"][0]["id"]
    return token, domain_id


async def test_owner_share_domain_to_user(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """域名所有者共享域名后，目标用户可见可访问。"""
    owner_token, domain_id = await _setup_owner_domain(client, monkeypatch)

    # 创建普通用户
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password123",
        },
    )
    user_id = reg.json()["data"]["id"]
    user_token = (
        await client.post(
            "/api/v1/auth/login",
            json={"username": "bob", "password": "password123"},
        )
    ).json()["data"]["access_token"]

    # 共享前用户看不到
    before = await client.get("/api/v1/domains", headers=_auth(user_token))
    assert before.json()["data"]["total"] == 0

    # 所有者共享
    assign = await client.post(
        f"/api/v1/domains/{domain_id}/assignments",
        headers=_auth(owner_token),
        json={"user_id": user_id},
    )
    assert assign.status_code == 201

    # 共享后用户可见可访问
    after = await client.get("/api/v1/domains", headers=_auth(user_token))
    assert after.json()["data"]["total"] == 1
    detail = await client.get(
        f"/api/v1/domains/{domain_id}", headers=_auth(user_token)
    )
    assert detail.status_code == 200


async def test_share_duplicate_conflict(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复共享同一域名给同一用户返回 409。"""
    owner_token, domain_id = await _setup_owner_domain(client, monkeypatch)
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password123",
        },
    )
    user_id = reg.json()["data"]["id"]

    first = await client.post(
        f"/api/v1/domains/{domain_id}/assignments",
        headers=_auth(owner_token),
        json={"user_id": user_id},
    )
    assert first.status_code == 201
    second = await client.post(
        f"/api/v1/domains/{domain_id}/assignments",
        headers=_auth(owner_token),
        json={"user_id": user_id},
    )
    assert second.status_code == 409


async def test_non_owner_cannot_share(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非域名所有者无法共享域名（返回 403）。"""
    owner_token, domain_id = await _setup_owner_domain(client, monkeypatch)

    # 另一个用户尝试共享该域名
    other_token = await _register_and_login(
        client, username="carol", email="carol@example.com"
    )
    # 注册一个目标用户
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "dave",
            "email": "dave@example.com",
            "password": "password123",
        },
    )
    target_id = reg.json()["data"]["id"]

    resp = await client.post(
        f"/api/v1/domains/{domain_id}/assignments",
        headers=_auth(other_token),
        json={"user_id": target_id},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == 1403


async def test_cannot_share_to_self(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不能将域名共享给自己。"""
    owner_token, domain_id = await _setup_owner_domain(client, monkeypatch)

    # 获取自己的 user_id
    me = await client.get("/api/v1/users/me", headers=_auth(owner_token))
    my_id = me.json()["data"]["id"]

    resp = await client.post(
        f"/api/v1/domains/{domain_id}/assignments",
        headers=_auth(owner_token),
        json={"user_id": my_id},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 1400


async def test_unassign_domain(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消共享后用户不再可见域名。"""
    owner_token, domain_id = await _setup_owner_domain(client, monkeypatch)
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password123",
        },
    )
    user_id = reg.json()["data"]["id"]
    user_token = (
        await client.post(
            "/api/v1/auth/login",
            json={"username": "bob", "password": "password123"},
        )
    ).json()["data"]["access_token"]

    await client.post(
        f"/api/v1/domains/{domain_id}/assignments",
        headers=_auth(owner_token),
        json={"user_id": user_id},
    )
    # 列出共享记录
    listing = await client.get(
        f"/api/v1/domains/{domain_id}/assignments", headers=_auth(owner_token)
    )
    assert len(listing.json()["data"]) == 1

    # 取消共享
    unassign = await client.delete(
        f"/api/v1/domains/{domain_id}/assignments/{user_id}",
        headers=_auth(owner_token),
    )
    assert unassign.status_code == 200

    after = await client.get("/api/v1/domains", headers=_auth(user_token))
    assert after.json()["data"]["total"] == 0


async def test_admin_can_share_any_domain(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理员可以共享任何域名（包括普通用户的域名）。"""
    _patch_cf(monkeypatch)
    await ensure_admin_user(db_session)

    # 普通用户绑定并同步域名
    user_token = await _register_and_login(client)
    account_id = await _bind(client, user_token)
    sync = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(user_token)
    )
    domain_id = sync.json()["data"]["domains"][0]["id"]

    # 管理员登录
    admin_tok = await _admin_token(client)

    # 创建目标用户
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password123",
        },
    )
    target_id = reg.json()["data"]["id"]

    # 管理员共享普通用户的域名
    resp = await client.post(
        f"/api/v1/domains/{domain_id}/assignments",
        headers=_auth(admin_tok),
        json={"user_id": target_id},
    )
    assert resp.status_code == 201

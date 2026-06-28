"""邮箱地址 CRUD 测试。

所有 Cloudflare 调用通过 monkeypatch 替换 CloudflareClient 方法 Mock，
不发出真实网络请求。
"""

import pytest
from httpx import AsyncClient

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


def _auth(token: str) -> dict[str, str]:
    """构造 Bearer 认证头。"""
    return {"Authorization": f"Bearer {token}"}


def _patch_cf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock CloudflareClient 的绑定预检与域名同步调用。"""

    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _fake_list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, str]]:
        return ZONES

    async def _fake_list_routing_rules(
        self: CloudflareClient, zone_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _fake_list_destinations(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _fake_list_email_sending(
        self: CloudflareClient, zone_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _fake_probe_email_routing_rules_write(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, str]:
        return {"status": "ok"}

    async def _fake_probe_destination_addresses_write(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        return {"status": "ok"}

    async def _fake_probe_email_sending_write(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        return {"status": "ok"}

    async def _fake_probe_worker_scripts_write(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        return {"status": "ok"}

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)
    monkeypatch.setattr(CloudflareClient, "list_zones", _fake_list_zones)
    monkeypatch.setattr(CloudflareClient, "list_routing_rules", _fake_list_routing_rules)
    monkeypatch.setattr(
        CloudflareClient, "list_destination_addresses", _fake_list_destinations
    )
    monkeypatch.setattr(
        CloudflareClient, "list_email_sending_subdomains", _fake_list_email_sending
    )
    monkeypatch.setattr(
        CloudflareClient,
        "probe_email_routing_rules_write",
        _fake_probe_email_routing_rules_write,
    )
    monkeypatch.setattr(
        CloudflareClient,
        "probe_destination_addresses_write",
        _fake_probe_destination_addresses_write,
    )
    monkeypatch.setattr(
        CloudflareClient, "probe_email_sending_write", _fake_probe_email_sending_write
    )
    monkeypatch.setattr(
        CloudflareClient,
        "probe_worker_scripts_write",
        _fake_probe_worker_scripts_write,
    )


async def _bind(client: AsyncClient, token: str) -> int:
    """绑定 CF 账号，返回账号 id。"""
    resp = await client.post(
        "/api/v1/cf-accounts",
        headers=_auth(token),
        json={
            "name": "主账号",
            "api_token": "cf-token",
            "account_id": "acc-123",
        },
    )
    return resp.json()["data"]["id"]


async def _setup_domain(
    client: AsyncClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> int:
    """绑定并同步出域名，返回第一个域名 id（example.com）。"""
    _account_id, domain_id = await _setup_domain_with_account(
        client, token, monkeypatch
    )
    return domain_id


async def _setup_domain_with_account(
    client: AsyncClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int]:
    """绑定并同步出域名，返回账号 id 与第一个域名 id（example.com）。"""
    _patch_cf(monkeypatch)
    account_id = await _bind(client, token)
    sync = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )
    domains = sync.json()["data"]["domains"]
    domain_id = next(d["id"] for d in domains if d["domain_name"] == "example.com")
    return account_id, domain_id


# ---- 创建 ----


async def test_create_email_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建邮箱地址，full_address 由 local_part + 域名拼接。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)

    resp = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["full_address"] == "hello@example.com"
    assert data["local_part"] == "hello"
    assert data["is_active"] is True
    assert data["domain_id"] == domain_id
    # 公开查询令牌：32 位无符号 uuid（uuid4().hex）
    assert "public_token" in data
    assert len(data["public_token"]) == 32
    assert all(c in "0123456789abcdef" for c in data["public_token"])


async def test_create_requires_auth(client: AsyncClient) -> None:
    """未认证创建返回 401。"""
    resp = await client.post(
        "/api/v1/email-addresses",
        json={"domain_id": 1, "local_part": "hello"},
    )
    assert resp.status_code == 401


async def test_create_invalid_local_part(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法 local_part（含 @）返回 422。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)

    resp = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "bad@part"},
    )
    assert resp.status_code == 422


async def test_create_on_inaccessible_domain(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在不可访问的域名上创建邮箱地址返回 404。"""
    token_a = await _register_and_login(client)
    domain_id = await _setup_domain(client, token_a, monkeypatch)

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token_b),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    assert resp.status_code == 404


async def test_create_blocked_when_cf_account_inactive(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停用 CF 账号后，关联域名不能继续创建邮箱地址。"""
    token = await _register_and_login(client)
    account_id, domain_id = await _setup_domain_with_account(client, token, monkeypatch)
    await client.patch(
        f"/api/v1/cf-accounts/{account_id}",
        headers=_auth(token),
        json={"is_active": False},
    )

    resp = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    assert resp.status_code == 403
    assert "已停用" in resp.json()["message"]


async def test_create_blocked_when_cf_account_deleted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解绑 CF 账号后，关联域名按不可见处理。"""
    token = await _register_and_login(client)
    account_id, domain_id = await _setup_domain_with_account(client, token, monkeypatch)
    await client.delete(f"/api/v1/cf-accounts/{account_id}", headers=_auth(token))

    listing = await client.get("/api/v1/domains", headers=_auth(token))
    assert listing.json()["data"]["total"] == 0
    resp = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    assert resp.status_code == 404


async def test_create_duplicate_conflict(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同名邮箱地址重复创建返回 409。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)

    first = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    assert second.status_code == 409


# ---- 列表 / 详情 / 隔离 ----


async def test_list_email_addresses(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分页列出当前用户的邮箱地址。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    for local in ("a", "b", "c"):
        await client.post(
            "/api/v1/email-addresses",
            headers=_auth(token),
            json={"domain_id": domain_id, "local_part": local},
        )

    resp = await client.get("/api/v1/email-addresses", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 3


async def test_list_default_size_is_25(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 默认 size 为 25,超出的部分按分页截断。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    for i in range(30):
        await client.post(
            "/api/v1/email-addresses",
            headers=_auth(token),
            json={"domain_id": domain_id, "local_part": f"u{i:02d}"},
        )

    resp = await client.get("/api/v1/email-addresses", headers=_auth(token))
    data = resp.json()["data"]
    assert resp.status_code == 200
    assert data["size"] == 25
    assert data["page"] == 1
    assert data["total"] == 30
    assert len(data["items"]) == 25


async def test_list_order_desc(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """order=desc 返回 id 倒序，用于「近 N 条」批量复制/下载。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    for local in ("a", "b", "c"):
        await client.post(
            "/api/v1/email-addresses",
            headers=_auth(token),
            json={"domain_id": domain_id, "local_part": local},
        )

    resp = await client.get(
        "/api/v1/email-addresses",
        params={"order": "desc", "size": 10},
        headers=_auth(token),
    )
    items = resp.json()["data"]["items"]
    ids = [item["id"] for item in items]
    assert ids == sorted(ids, reverse=True)


async def test_list_size_max_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """size 上限提升到 500,超过应返回 422 校验错误。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "a"},
    )

    resp = await client.get(
        "/api/v1/email-addresses",
        params={"size": 501},
        headers=_auth(token),
    )
    assert resp.status_code == 422


async def test_list_invalid_order_rejected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """order 必须是 asc 或 desc,非法值返回 422。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "a"},
    )

    resp = await client.get(
        "/api/v1/email-addresses",
        params={"order": "random"},
        headers=_auth(token),
    )
    assert resp.status_code == 422


async def test_list_filter_by_domain(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 domain_id 过滤邮箱地址列表。"""
    token = await _register_and_login(client)
    _patch_cf(monkeypatch)
    account_id = await _bind(client, token)
    sync = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )
    domains = {d["domain_name"]: d["id"] for d in sync.json()["data"]["domains"]}

    await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domains["example.com"], "local_part": "a"},
    )
    await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domains["example.org"], "local_part": "b"},
    )

    resp = await client.get(
        "/api/v1/email-addresses",
        headers=_auth(token),
        params={"domain_id": domains["example.com"]},
    )
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["full_address"] == "a@example.com"


async def test_get_email_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 id 获取邮箱地址详情。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    created = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    ea_id = created.json()["data"]["id"]

    resp = await client.get(
        f"/api/v1/email-addresses/{ea_id}", headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == ea_id


async def test_get_unknown_returns_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """获取不存在的邮箱地址返回 404。"""
    token = await _register_and_login(client)
    resp = await client.get(
        "/api/v1/email-addresses/99999", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_access_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户无法访问他人的邮箱地址。"""
    token_a = await _register_and_login(client)
    domain_id = await _setup_domain(client, token_a, monkeypatch)
    created = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token_a),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    ea_id = created.json()["data"]["id"]

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.get(
        f"/api/v1/email-addresses/{ea_id}", headers=_auth(token_b)
    )
    assert resp.status_code == 404
    listing = await client.get("/api/v1/email-addresses", headers=_auth(token_b))
    assert listing.json()["data"]["total"] == 0


# ---- 更新 / 删除 ----


async def test_update_email_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更新邮箱地址的启用状态。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    created = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    ea_id = created.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/email-addresses/{ea_id}",
        headers=_auth(token),
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["is_active"] is False


async def test_delete_email_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """软删除邮箱地址后不再可见。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    created = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    ea_id = created.json()["data"]["id"]

    deleted = await client.delete(
        f"/api/v1/email-addresses/{ea_id}", headers=_auth(token)
    )
    assert deleted.status_code == 200

    after = await client.get(
        f"/api/v1/email-addresses/{ea_id}", headers=_auth(token)
    )
    assert after.status_code == 404


async def test_recreate_after_delete(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除后可再次创建同名地址（复活软删除记录）。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)
    created = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    ea_id = created.json()["data"]["id"]
    await client.delete(
        f"/api/v1/email-addresses/{ea_id}", headers=_auth(token)
    )

    recreated = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    assert recreated.status_code == 201
    assert recreated.json()["data"]["full_address"] == "hello@example.com"


# ---- 大小写规范化 ----


async def test_create_normalizes_uppercase_local_part(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建时 local_part 含大写字母，入库后统一为小写。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)

    resp = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "Hello"},
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["local_part"] == "hello"
    assert data["full_address"] == "hello@example.com"


async def test_create_duplicate_case_insensitive(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大写 local_part 与已存在的小写地址视为重复（409）。"""
    token = await _register_and_login(client)
    domain_id = await _setup_domain(client, token, monkeypatch)

    first = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "HELLO"},
    )
    assert second.status_code == 409

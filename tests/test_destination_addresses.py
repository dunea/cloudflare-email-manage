"""转发目标地址 测试。

所有 Cloudflare 调用通过 monkeypatch 替换 CloudflareClient 方法 Mock，
不发出真实网络请求。
"""

from typing import Any

import pytest
from httpx import AsyncClient

from app.services.cloudflare import CloudflareClient

# 模拟 CF 返回的 Zone 列表
ZONES = [
    {"id": "zone1", "name": "example.com", "status": "active"},
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


class _CFCalls:
    """记录目标地址与转发规则相关 CF 调用。"""

    def __init__(self) -> None:
        self.dest_items: list[dict[str, Any]] = []
        self.created_rules: list[tuple[str, dict[str, Any]]] = []
        self.deleted_rules: list[tuple[str, str]] = []


def _patch_cf(monkeypatch: pytest.MonkeyPatch) -> _CFCalls:
    """Mock CloudflareClient 的 CF 调用。"""
    calls = _CFCalls()

    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _fake_list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, str]]:
        return ZONES

    async def _fake_list_routing_rules(
        self: CloudflareClient, zone_id: str
    ) -> list[dict[str, Any]]:
        return []

    async def _fake_create_dest(
        self: CloudflareClient, account_id: str, email: str
    ) -> dict[str, Any]:
        return {"id": f"cf-dest-{email}", "email": email, "verified": None}

    async def _fake_list_dests(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, Any]]:
        return calls.dest_items

    async def _fake_delete_dest(
        self: CloudflareClient, account_id: str, address_id: str
    ) -> dict[str, Any]:
        return {"id": address_id}

    async def _fake_create_rule(
        self: CloudflareClient, zone_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        calls.created_rules.append((zone_id, payload))
        return {"id": f"cf-rule-{len(calls.created_rules)}", "enabled": True}

    async def _fake_delete_rule(
        self: CloudflareClient, zone_id: str, rule_id: str
    ) -> dict[str, Any]:
        calls.deleted_rules.append((zone_id, rule_id))
        return {"id": rule_id}

    async def _fake_list_email_sending(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, Any]]:
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
        CloudflareClient, "create_destination_address", _fake_create_dest
    )
    monkeypatch.setattr(
        CloudflareClient, "list_destination_addresses", _fake_list_dests
    )
    monkeypatch.setattr(
        CloudflareClient, "delete_destination_address", _fake_delete_dest
    )
    monkeypatch.setattr(CloudflareClient, "create_routing_rule", _fake_create_rule)
    monkeypatch.setattr(CloudflareClient, "delete_routing_rule", _fake_delete_rule)
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
    return calls


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


async def _sync_and_create_email(client: AsyncClient, token: str, account_id: int) -> int:
    """同步域名并创建一个邮箱地址，返回邮箱地址 id。"""
    sync = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )
    domain_id = sync.json()["data"]["domains"][0]["id"]
    created = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    return created.json()["data"]["id"]


# ---- 添加 ----


async def test_create_destination_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """添加目标地址调用 CF 创建并入库，初始未验证。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)

    resp = await client.post(
        "/api/v1/destination-addresses",
        headers=_auth(token),
        json={"cf_account_id": account_id, "email": "dest@gmail.com"},
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["email"] == "dest@gmail.com"
    assert data["verified"] is False
    assert data["cf_address_id"] == "cf-dest-dest@gmail.com"


async def test_create_requires_auth(client: AsyncClient) -> None:
    """未认证添加返回 401。"""
    resp = await client.post(
        "/api/v1/destination-addresses",
        json={"cf_account_id": 1, "email": "dest@gmail.com"},
    )
    assert resp.status_code == 401


async def test_create_duplicate(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同账号下重复添加同一目标地址返回 409。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)

    payload = {"cf_account_id": account_id, "email": "dest@gmail.com"}
    await client.post("/api/v1/destination-addresses", headers=_auth(token), json=payload)
    resp = await client.post(
        "/api/v1/destination-addresses", headers=_auth(token), json=payload
    )
    assert resp.status_code == 409


async def test_create_on_inaccessible_account(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """为他人的 CF 账号添加目标地址返回 404。"""
    _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    account_id = await _bind(client, token_a)

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.post(
        "/api/v1/destination-addresses",
        headers=_auth(token_b),
        json={"cf_account_id": account_id, "email": "dest@gmail.com"},
    )
    assert resp.status_code == 404


# ---- 列表 / 隔离 ----


async def test_list_destination_addresses(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列出当前用户的目标地址。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)
    for addr in ("a@x.com", "b@x.com"):
        await client.post(
            "/api/v1/destination-addresses",
            headers=_auth(token),
            json={"cf_account_id": account_id, "email": addr},
        )

    resp = await client.get("/api/v1/destination-addresses", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 2


async def test_list_access_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户无法看到他人的目标地址。"""
    _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    account_id = await _bind(client, token_a)
    await client.post(
        "/api/v1/destination-addresses",
        headers=_auth(token_a),
        json={"cf_account_id": account_id, "email": "a@x.com"},
    )

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.get("/api/v1/destination-addresses", headers=_auth(token_b))
    assert resp.json()["data"]["total"] == 0


# ---- 同步 ----


async def test_sync_updates_verified_status(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步从 CF 拉取目标地址并更新验证状态。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)
    await client.post(
        "/api/v1/destination-addresses",
        headers=_auth(token),
        json={"cf_account_id": account_id, "email": "dest@gmail.com"},
    )

    # 让 CF list 返回已验证状态
    async def _fake_list_dests(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "cf-dest-dest@gmail.com",
                "email": "dest@gmail.com",
                "verified": "2026-06-26T08:00:00Z",
            }
        ]

    monkeypatch.setattr(
        CloudflareClient, "list_destination_addresses", _fake_list_dests
    )

    resp = await client.post(
        "/api/v1/destination-addresses/sync",
        headers=_auth(token),
        params={"cf_account_id": account_id},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["verified"] is True
    assert data[0]["verified_at"] is not None


async def test_sync_removes_remotely_deleted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CF 侧已不存在的本地目标地址被标记为软删除。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)
    await client.post(
        "/api/v1/destination-addresses",
        headers=_auth(token),
        json={"cf_account_id": account_id, "email": "dest@gmail.com"},
    )

    # CF list 返回空（目标地址已在 CF 侧删除）
    async def _fake_list_dests(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(
        CloudflareClient, "list_destination_addresses", _fake_list_dests
    )

    resp = await client.post(
        "/api/v1/destination-addresses/sync",
        headers=_auth(token),
        params={"cf_account_id": account_id},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_sync_blocked_when_cf_account_inactive(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停用 CF 账号后不能同步目标地址。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)
    await client.patch(
        f"/api/v1/cf-accounts/{account_id}",
        headers=_auth(token),
        json={"is_active": False},
    )

    resp = await client.post(
        "/api/v1/destination-addresses/sync",
        headers=_auth(token),
        params={"cf_account_id": account_id},
    )
    assert resp.status_code == 403
    assert "已停用" in resp.json()["message"]


# ---- 删除 ----


async def test_delete_destination_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除目标地址调用 CF 删除并软删除本地记录。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)
    created = await client.post(
        "/api/v1/destination-addresses",
        headers=_auth(token),
        json={"cf_account_id": account_id, "email": "dest@gmail.com"},
    )
    address_id = created.json()["data"]["id"]

    resp = await client.delete(
        f"/api/v1/destination-addresses/{address_id}", headers=_auth(token)
    )
    assert resp.status_code == 200

    # 删除后不再可见
    listing = await client.get(
        "/api/v1/destination-addresses", headers=_auth(token)
    )
    assert listing.json()["data"]["total"] == 0


async def test_delete_destination_disables_impacted_forwarding_rules(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除目标地址前，会删除引用它的远端 rule 并停用本地转发规则。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id = await _bind(client, token)
    ea_id = await _sync_and_create_email(client, token, account_id)

    created_dest = await client.post(
        "/api/v1/destination-addresses",
        headers=_auth(token),
        json={"cf_account_id": account_id, "email": "dest@gmail.com"},
    )
    address_id = created_dest.json()["data"]["id"]
    calls.dest_items = [
        {
            "id": "cf-dest-dest@gmail.com",
            "email": "dest@gmail.com",
            "verified": "2026-06-26T08:00:00Z",
        }
    ]
    created_rule = await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token),
        json={"email_address_id": ea_id, "destination_email": "dest@gmail.com"},
    )
    assert created_rule.status_code == 201

    resp = await client.delete(
        f"/api/v1/destination-addresses/{address_id}", headers=_auth(token)
    )
    assert resp.status_code == 200
    assert calls.deleted_rules == [("zone1", "cf-rule-1")]

    listing = await client.get("/api/v1/forwarding-rules", headers=_auth(token))
    rule = listing.json()["data"]["items"][0]
    assert rule["is_active"] is False
    assert rule["cf_rule_id"] is None

"""转发规则 测试。

所有 Cloudflare 调用通过 monkeypatch 替换 CloudflareClient 方法 Mock，
不发出真实网络请求。create_routing_rule / delete_routing_rule 被记录调用次数
以验证 CF 集成。
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
    """记录 CF 转发规则相关调用，便于断言。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []


def _patch_cf(monkeypatch: pytest.MonkeyPatch) -> _CFCalls:
    """Mock CloudflareClient 的 CF 调用，返回调用记录对象。"""
    calls = _CFCalls()

    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _fake_list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, str]]:
        return ZONES

    async def _fake_create_rule(
        self: CloudflareClient, zone_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        calls.created.append((zone_id, payload))
        return {"id": f"cf-rule-{len(calls.created)}", "enabled": True}

    async def _fake_delete_rule(
        self: CloudflareClient, zone_id: str, rule_id: str
    ) -> dict[str, Any]:
        calls.deleted.append((zone_id, rule_id))
        return {"id": rule_id}

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)
    monkeypatch.setattr(CloudflareClient, "list_zones", _fake_list_zones)
    monkeypatch.setattr(CloudflareClient, "create_routing_rule", _fake_create_rule)
    monkeypatch.setattr(CloudflareClient, "delete_routing_rule", _fake_delete_rule)
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


async def _setup_email_address(client: AsyncClient, token: str) -> int:
    """绑定并同步域名、创建邮箱地址，返回邮箱地址 id。

    需在调用前完成 _patch_cf。
    """
    account_id = await _bind(client, token)
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


# ---- 创建 ----


async def test_create_forwarding_rule(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建转发规则并调用 CF 创建路由规则，保存返回的 cf_rule_id。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token)

    resp = await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token),
        json={"email_address_id": ea_id, "destination_email": "dest@other.com"},
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["destination_email"] == "dest@other.com"
    assert data["cf_rule_id"] == "cf-rule-1"
    assert data["is_active"] is True

    # 验证调用了 CF 创建规则，payload 含正确 matcher / action
    assert len(calls.created) == 1
    zone_id, payload = calls.created[0]
    assert zone_id == "zone1"
    assert payload["matchers"][0]["value"] == "hello@example.com"
    assert payload["actions"][0]["value"] == ["dest@other.com"]


async def test_create_requires_auth(client: AsyncClient) -> None:
    """未认证创建返回 401。"""
    resp = await client.post(
        "/api/v1/forwarding-rules",
        json={"email_address_id": 1, "destination_email": "dest@other.com"},
    )
    assert resp.status_code == 401


async def test_create_invalid_destination(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法目标邮箱返回 422。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token)

    resp = await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token),
        json={"email_address_id": ea_id, "destination_email": "not-an-email"},
    )
    assert resp.status_code == 422


async def test_create_on_inaccessible_email_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """为他人的邮箱地址创建转发规则返回 404。"""
    _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token_a)

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token_b),
        json={"email_address_id": ea_id, "destination_email": "dest@other.com"},
    )
    assert resp.status_code == 404


# ---- 列表 / 详情 / 隔离 ----


async def test_list_forwarding_rules(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分页列出当前用户的转发规则。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token)
    for dest in ("a@x.com", "b@x.com"):
        await client.post(
            "/api/v1/forwarding-rules",
            headers=_auth(token),
            json={"email_address_id": ea_id, "destination_email": dest},
        )

    resp = await client.get("/api/v1/forwarding-rules", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 2


async def test_list_filter_by_email_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 email_address_id 过滤转发规则。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token)
    await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token),
        json={"email_address_id": ea_id, "destination_email": "a@x.com"},
    )

    resp = await client.get(
        "/api/v1/forwarding-rules",
        headers=_auth(token),
        params={"email_address_id": ea_id},
    )
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["email_address_id"] == ea_id


async def test_get_forwarding_rule(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 id 获取转发规则详情。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token)
    created = await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token),
        json={"email_address_id": ea_id, "destination_email": "dest@other.com"},
    )
    rule_id = created.json()["data"]["id"]

    resp = await client.get(
        f"/api/v1/forwarding-rules/{rule_id}", headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == rule_id


async def test_get_unknown_returns_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """获取不存在的转发规则返回 404。"""
    token = await _register_and_login(client)
    resp = await client.get(
        "/api/v1/forwarding-rules/99999", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_access_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户无法访问他人的转发规则。"""
    calls = _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token_a)
    created = await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token_a),
        json={"email_address_id": ea_id, "destination_email": "dest@other.com"},
    )
    rule_id = created.json()["data"]["id"]
    assert len(calls.created) == 1

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.get(
        f"/api/v1/forwarding-rules/{rule_id}", headers=_auth(token_b)
    )
    assert resp.status_code == 404
    listing = await client.get(
        "/api/v1/forwarding-rules", headers=_auth(token_b)
    )
    assert listing.json()["data"]["total"] == 0


# ---- 更新 / 删除 ----


async def test_update_forwarding_rule(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更新转发规则的启用状态。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token)
    created = await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token),
        json={"email_address_id": ea_id, "destination_email": "dest@other.com"},
    )
    rule_id = created.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/forwarding-rules/{rule_id}",
        headers=_auth(token),
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["is_active"] is False


async def test_delete_forwarding_rule(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除转发规则会调用 CF 删除路由规则并软删除本地记录。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    ea_id = await _setup_email_address(client, token)
    created = await client.post(
        "/api/v1/forwarding-rules",
        headers=_auth(token),
        json={"email_address_id": ea_id, "destination_email": "dest@other.com"},
    )
    rule_id = created.json()["data"]["id"]

    deleted = await client.delete(
        f"/api/v1/forwarding-rules/{rule_id}", headers=_auth(token)
    )
    assert deleted.status_code == 200

    # 验证调用了 CF 删除规则
    assert len(calls.deleted) == 1
    zone_id, cf_rule_id = calls.deleted[0]
    assert zone_id == "zone1"
    assert cf_rule_id == "cf-rule-1"

    # 删除后不再可见
    after = await client.get(
        f"/api/v1/forwarding-rules/{rule_id}", headers=_auth(token)
    )
    assert after.status_code == 404

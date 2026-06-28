"""发件 API 测试。

所有 Cloudflare 调用通过 monkeypatch 替换 CloudflareClient 方法 Mock，
不发出真实网络请求。验证：发件地址归属校验、CF send_email 调用载荷、
正文必填、收件地址合法性，以及 X-API-Key 认证可发件。
"""

from typing import Any

import pytest
from httpx import AsyncClient

from app.config import settings
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
    """记录 CF 发件调用，便于断言。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []


def _patch_cf(monkeypatch: pytest.MonkeyPatch) -> _CFCalls:
    """Mock CloudflareClient 的 CF 调用，返回调用记录对象。"""
    calls = _CFCalls()

    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _fake_list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, str]]:
        return ZONES

    async def _fake_send(
        self: CloudflareClient, account_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        calls.sent.append((account_id, payload))
        return {"id": f"msg-{len(calls.sent)}"}

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)
    monkeypatch.setattr(CloudflareClient, "list_zones", _fake_list_zones)
    monkeypatch.setattr(CloudflareClient, "send_email", _fake_send)
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


async def _setup_email_address(client: AsyncClient, token: str) -> str:
    """绑定并同步域名、创建邮箱地址，返回 full_address。

    需在调用前完成 _patch_cf。
    """
    _account_id, full_address = await _setup_email_address_with_account(client, token)
    return full_address


async def _setup_email_address_with_account(
    client: AsyncClient, token: str
) -> tuple[int, str]:
    """绑定并同步域名、创建邮箱地址，返回账号 id 与 full_address。"""
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
    return account_id, created.json()["data"]["full_address"]


# ---- 发件 ----


async def test_send_email(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """从已管理地址发件，调用 CF send_email 并传递正确载荷。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)

    resp = await client.post(
        "/api/v1/outbound/send",
        headers=_auth(token),
        json={
            "from_address": "hello@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
            "text": "Hello world",
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["from_address"] == "hello@example.com"
    assert data["to"] == ["dest@other.com"]

    assert len(calls.sent) == 1
    account_id, payload = calls.sent[0]
    assert account_id == "acc-123"
    assert payload["from"] == "hello@example.com"
    assert payload["to"] == ["dest@other.com"]
    assert payload["subject"] == "Hi"
    assert payload["text"] == "Hello world"
    assert "html" not in payload


async def test_send_requires_auth(client: AsyncClient) -> None:
    """未认证发件返回 401。"""
    resp = await client.post(
        "/api/v1/outbound/send",
        json={
            "from_address": "hello@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
            "text": "x",
        },
    )
    assert resp.status_code == 401


async def test_send_from_unmanaged_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发件地址非本人管理时返回 404，不调用 CF。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)

    resp = await client.post(
        "/api/v1/outbound/send",
        headers=_auth(token),
        json={
            "from_address": "stranger@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
            "text": "x",
        },
    )
    assert resp.status_code == 404
    assert len(calls.sent) == 0


async def test_send_requires_body(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """text 与 html 均缺失返回 422。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)

    resp = await client.post(
        "/api/v1/outbound/send",
        headers=_auth(token),
        json={
            "from_address": "hello@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
        },
    )
    assert resp.status_code == 422


async def test_send_invalid_recipient(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法收件地址返回 422。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)

    resp = await client.post(
        "/api/v1/outbound/send",
        headers=_auth(token),
        json={
            "from_address": "hello@example.com",
            "to": ["not-an-email"],
            "subject": "Hi",
            "text": "x",
        },
    )
    assert resp.status_code == 422


async def test_send_via_api_key(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """使用 X-API-Key 程序化认证可发件。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)

    key_resp = await client.post(
        "/api/v1/api-keys", headers=_auth(token), json={"name": "prog"}
    )
    raw_key = key_resp.json()["data"]["key"]

    resp = await client.post(
        "/api/v1/outbound/send",
        headers={"X-API-Key": raw_key},
        json={
            "from_address": "hello@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
            "html": "<p>hi</p>",
        },
    )
    assert resp.status_code == 200
    assert len(calls.sent) == 1
    _, payload = calls.sent[0]
    assert payload["html"] == "<p>hi</p>"
    assert "text" not in payload


async def test_send_via_api_key_requires_send_scope(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API Key 缺少 send scope 时不能发件。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)

    key_resp = await client.post(
        "/api/v1/api-keys",
        headers=_auth(token),
        json={"name": "readonly", "scopes": ["read_inbound"]},
    )
    raw_key = key_resp.json()["data"]["key"]

    resp = await client.post(
        "/api/v1/outbound/send",
        headers={"X-API-Key": raw_key},
        json={
            "from_address": "hello@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
            "text": "x",
        },
    )
    assert resp.status_code == 403
    assert len(calls.sent) == 0


async def test_api_key_rate_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API Key 调用超过配置阈值后返回 429。"""
    _patch_cf(monkeypatch)
    monkeypatch.setattr(settings, "API_KEY_RATE_LIMIT_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "API_KEY_RATE_LIMIT_WINDOW_SECONDS", 60)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)

    key_resp = await client.post(
        "/api/v1/api-keys", headers=_auth(token), json={"name": "prog"}
    )
    raw_key = key_resp.json()["data"]["key"]
    payload = {
        "from_address": "hello@example.com",
        "to": ["dest@other.com"],
        "subject": "Hi",
        "text": "x",
    }

    first = await client.post(
        "/api/v1/outbound/send", headers={"X-API-Key": raw_key}, json=payload
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/v1/outbound/send", headers={"X-API-Key": raw_key}, json=payload
    )
    assert second.status_code == 429


async def test_send_with_invalid_api_key(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无效 X-API-Key 返回 401。"""
    _patch_cf(monkeypatch)
    resp = await client.post(
        "/api/v1/outbound/send",
        headers={"X-API-Key": "cfem_invalid"},
        json={
            "from_address": "hello@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
            "text": "x",
        },
    )
    assert resp.status_code == 401


async def test_send_case_insensitive_from_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发件地址大小写不敏感：Hello@example.com 匹配 hello@example.com。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)

    resp = await client.post(
        "/api/v1/outbound/send",
        headers=_auth(token),
        json={
            "from_address": "Hello@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
            "text": "x",
        },
    )
    assert resp.status_code == 200
    assert len(calls.sent) == 1


async def test_send_blocked_when_cf_account_inactive(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停用 CF 账号后，已有邮箱地址也不能继续发件。"""
    calls = _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    account_id, _full_address = await _setup_email_address_with_account(client, token)
    await client.patch(
        f"/api/v1/cf-accounts/{account_id}",
        headers=_auth(token),
        json={"is_active": False},
    )

    resp = await client.post(
        "/api/v1/outbound/send",
        headers=_auth(token),
        json={
            "from_address": "hello@example.com",
            "to": ["dest@other.com"],
            "subject": "Hi",
            "text": "x",
        },
    )
    assert resp.status_code == 403
    assert "已停用" in resp.json()["message"]
    assert len(calls.sent) == 0

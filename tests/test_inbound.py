"""Webhook 收件 测试。

验证：签名校验（缺失/错误/正确）、载荷入库、非法载荷 422，
以及按 to_address 归属隔离的查询。CF 调用经 monkeypatch Mock。
"""

import hashlib
import hmac
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import InboundEmail
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


def _sign(body: bytes) -> str:
    """对请求体计算 HMAC-SHA256 签名（与服务端一致）。"""
    return hmac.new(
        settings.CF_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


async def _post_webhook(
    client: AsyncClient,
    payload: dict[str, object],
    *,
    sign: bool = True,
    signature: str | None = None,
) -> object:
    """提交 Webhook 请求，可控制是否携带/伪造签名。"""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature
    elif sign:
        headers["X-Webhook-Signature"] = _sign(body)
    return await client.post(
        "/api/v1/inbound/webhook", content=body, headers=headers
    )


def _patch_cf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock CF 的 Token 校验与域名同步（用于创建邮箱地址）。"""

    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _fake_list_zones(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, str]]:
        return ZONES

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)
    monkeypatch.setattr(CloudflareClient, "list_zones", _fake_list_zones)


async def _setup_email_address(client: AsyncClient, token: str) -> str:
    """绑定并同步域名、创建邮箱地址，返回 full_address。"""
    bind = await client.post(
        "/api/v1/cf-accounts",
        headers=_auth(token),
        json={
            "name": "主账号",
            "api_token": "cf-token",
            "account_id": "acc-123",
            "permission_type": "all",
        },
    )
    account_id = bind.json()["data"]["id"]
    sync = await client.post(
        f"/api/v1/cf-accounts/{account_id}/sync", headers=_auth(token)
    )
    domain_id = sync.json()["data"]["domains"][0]["id"]
    created = await client.post(
        "/api/v1/email-addresses",
        headers=_auth(token),
        json={"domain_id": domain_id, "local_part": "hello"},
    )
    return created.json()["data"]["full_address"]


# ---- Webhook 签名与入库 ----


async def test_webhook_stores_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """签名正确时载荷被解析并入库。"""
    resp = await _post_webhook(
        client,
        {
            "to": "hello@example.com",
            "from": "sender@external.com",
            "subject": "Test",
            "text": "Body text",
            "html": "<p>Body</p>",
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["to_address"] == "hello@example.com"
    assert data["from_address"] == "sender@external.com"
    assert data["subject"] == "Test"
    assert data["body_text"] == "Body text"
    assert data["body_html"] == "<p>Body</p>"

    rows = (await db_session.execute(select(InboundEmail))).scalars().all()
    assert len(rows) == 1


async def test_webhook_missing_signature(client: AsyncClient) -> None:
    """缺少签名返回 401。"""
    resp = await _post_webhook(
        client,
        {"to": "hello@example.com", "from": "x@y.com", "text": "t"},
        sign=False,
    )
    assert resp.status_code == 401


async def test_webhook_invalid_signature(client: AsyncClient) -> None:
    """签名不匹配返回 401。"""
    resp = await _post_webhook(
        client,
        {"to": "hello@example.com", "from": "x@y.com", "text": "t"},
        signature="deadbeef",
    )
    assert resp.status_code == 401


async def test_webhook_invalid_payload(client: AsyncClient) -> None:
    """签名正确但载荷非法（收件地址错误）返回 422。"""
    resp = await _post_webhook(
        client,
        {"to": "not-an-email", "from": "x@y.com", "text": "t"},
    )
    assert resp.status_code == 422


# ---- 查询与隔离 ----


async def test_list_inbound_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户仅能看到发往自己邮箱地址的邮件。"""
    _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    await _setup_email_address(client, token_a)
    await _post_webhook(
        client,
        {"to": "hello@example.com", "from": "x@y.com", "subject": "s", "text": "t"},
    )

    list_a = await client.get("/api/v1/inbound", headers=_auth(token_a))
    assert list_a.status_code == 200
    assert list_a.json()["data"]["total"] == 1

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    list_b = await client.get("/api/v1/inbound", headers=_auth(token_b))
    assert list_b.json()["data"]["total"] == 0


async def test_get_inbound_email(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 id 获取归属于本人的收件邮件。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token)
    webhook = await _post_webhook(
        client,
        {"to": "hello@example.com", "from": "x@y.com", "subject": "s", "text": "t"},
    )
    email_id = webhook.json()["data"]["id"]

    resp = await client.get(f"/api/v1/inbound/{email_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == email_id


async def test_get_inbound_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """他人无法按 id 获取不属于自己的邮件。"""
    _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    await _setup_email_address(client, token_a)
    webhook = await _post_webhook(
        client,
        {"to": "hello@example.com", "from": "x@y.com", "subject": "s", "text": "t"},
    )
    email_id = webhook.json()["data"]["id"]

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.get(f"/api/v1/inbound/{email_id}", headers=_auth(token_b))
    assert resp.status_code == 404


async def test_list_requires_auth(client: AsyncClient) -> None:
    """未认证查询收件列表返回 401。"""
    resp = await client.get("/api/v1/inbound")
    assert resp.status_code == 401

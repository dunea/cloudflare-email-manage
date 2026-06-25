"""公开邮件查询端点 测试。

验证：/mail/{token}（HTML）与 /mail/{token}.txt（纯文本）无需登录即可访问，
返回最新邮件的发件人/收件人/时间/主题/正文；无效令牌 404；
停用邮箱不可访问；重置令牌后旧令牌失效。
"""

import hashlib
import hmac
import json

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.cloudflare import CloudflareClient

ZONES = [{"id": "zone1", "name": "example.com", "status": "active"}]


# ---- 通用辅助（与 test_inbound 类似） ----


async def _register_and_login(
    client: AsyncClient,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "password123",
) -> str:
    await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    login = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    return login.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sign(body: bytes) -> str:
    return hmac.new(
        settings.CF_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


async def _post_webhook(client: AsyncClient, payload: dict[str, object]) -> object:
    body = json.dumps(payload).encode("utf-8")
    return await client.post(
        "/api/v1/inbound/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": _sign(body),
        },
    )


def _patch_cf(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _fake_list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, str]]:
        return ZONES

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)
    monkeypatch.setattr(CloudflareClient, "list_zones", _fake_list_zones)


async def _setup(client: AsyncClient, token: str) -> str:
    """绑定并同步域名、创建邮箱地址，返回 public_token。"""
    bind = await client.post(
        "/api/v1/cf-accounts",
        headers=_auth(token),
        json={"name": "主账号", "api_token": "cf-token", "account_id": "acc-123"},
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
    return created.json()["data"]["public_token"]


# ---- 公开端点 ----


async def test_text_endpoint_returns_latest_mail(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯文本端点返回最新邮件的中文标签字段与正文。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token)
    await _post_webhook(
        client,
        {
            "to": "hello@example.com",
            "from": "sender@external.com",
            "subject": "测试主题",
            "text": "正文内容",
        },
    )
    resp = await client.get(f"/mail/{public_token}.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert "发件人: sender@external.com" in text
    assert "收件人: hello@example.com" in text
    assert "时间:" in text
    assert "主题: 测试主题" in text
    assert "正文内容" in text


async def test_html_endpoint_returns_page(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTML 端点返回包含邮件信息的 HTML 页面。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token)
    await _post_webhook(
        client,
        {
            "to": "hello@example.com",
            "from": "sender@external.com",
            "subject": "HTML测试",
            "text": "纯文本正文",
            "html": "<p>HTML正文</p>",
        },
    )
    resp = await client.get(f"/mail/{public_token}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "hello@example.com" in resp.text
    assert "sender@external.com" in resp.text
    assert "HTML测试" in resp.text


async def test_text_endpoint_empty_mailbox(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """邮箱暂无邮件时，文本端点返回 200 + 提示行。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token)
    resp = await client.get(f"/mail/{public_token}.txt")
    assert resp.status_code == 200
    assert "暂无邮件" in resp.text


async def test_html_endpoint_empty_mailbox(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """邮箱暂无邮件时，HTML 端点返回空状态。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token)
    resp = await client.get(f"/mail/{public_token}")
    assert resp.status_code == 200
    assert "暂无邮件" in resp.text


async def test_invalid_token_returns_404(client: AsyncClient) -> None:
    """无效令牌返回 404。"""
    resp = await client.get("/mail/nonexistenttoken1234567890abcdef12345.txt")
    assert resp.status_code == 404
    resp_html = await client.get("/mail/nonexistenttoken1234567890abcdef12345")
    assert resp_html.status_code == 404


async def test_disabled_address_inaccessible(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停用的邮箱地址公开端点不可访问。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token)
    # 获取邮箱地址 id 并停用
    listing = await client.get("/api/v1/email-addresses", headers=_auth(token))
    ea_id = listing.json()["data"]["items"][0]["id"]
    await client.patch(
        f"/api/v1/email-addresses/{ea_id}",
        headers=_auth(token),
        json={"is_active": False},
    )
    resp = await client.get(f"/mail/{public_token}.txt")
    assert resp.status_code == 404


async def test_reset_token_invalidates_old(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """重置令牌后旧令牌失效，新令牌可用。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token)
    listing = await client.get("/api/v1/email-addresses", headers=_auth(token))
    ea_id = listing.json()["data"]["items"][0]["id"]

    reset = await client.post(
        f"/api/v1/email-addresses/{ea_id}/reset-token", headers=_auth(token)
    )
    assert reset.status_code == 200
    new_token = reset.json()["data"]["public_token"]
    assert new_token != public_token

    # 旧令牌失效
    old = await client.get(f"/mail/{public_token}.txt")
    assert old.status_code == 404
    # 新令牌可用（无邮件，200 提示）
    new = await client.get(f"/mail/{new_token}.txt")
    assert new.status_code == 200

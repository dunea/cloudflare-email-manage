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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import InboundEmail, OutboundEmail
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

    async def _fake_list_routing_rules(
        self: CloudflareClient, zone_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _fake_list_destinations(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _fake_get_email_routing_status(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
        return {"enabled": True, "status": "ready"}

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
        CloudflareClient, "get_email_routing_status", _fake_get_email_routing_status
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


class _SendCalls:
    """记录公开发件调用。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []


def _patch_send(monkeypatch: pytest.MonkeyPatch) -> _SendCalls:
    """Mock Cloudflare 发件调用。"""
    calls = _SendCalls()

    async def _send(
        self: CloudflareClient, account_id: str, payload: dict[str, object]
    ) -> dict[str, object]:
        calls.sent.append((account_id, payload))
        return {"id": "public-msg-1"}

    monkeypatch.setattr(CloudflareClient, "send_email", _send)
    return calls


async def _setup(
    client: AsyncClient,
    token: str,
    db_session: AsyncSession | None = None,
) -> str:
    """绑定并同步域名、创建邮箱地址，返回 public_token。

    同步阶段不再生成 per-domain webhook_secret（由 worker_deploy_service
    统一管理），新建 Domain.webhook_secret 默认 NULL。

    若传入 ``db_session``，会先显式 seed 一个非空 secret 再清空，
    以覆盖 legacy Worker 依赖全局密钥 fallback 的兼容路径。
    """
    from sqlalchemy import select

    from app.models import CFAccount, Domain

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
    if db_session is not None:
        rows = (
            await db_session.execute(
                select(Domain).join(CFAccount).where(CFAccount.id == account_id)
            )
        ).scalars().all()
        assert rows, "test 期望同步至少产生一个域名"
        for d in rows:
            d.webhook_secret = "seeded-secret"
        await db_session.flush()
        for d in rows:
            d.webhook_secret = None
        await db_session.commit()
    return created.json()["data"]["public_token"]


# ---- 公开端点 ----


async def test_text_endpoint_returns_latest_mail(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯文本端点返回最新邮件的中文标签字段与正文。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token, db_session)
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
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTML 端点返回包含邮件信息的 HTML 页面。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token, db_session)
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
    assert "收件箱（1）" in resp.text
    assert "发件箱（0）" in resp.text
    assert "发件" in resp.text


async def test_public_mail_inbox_lists_preview_and_detail(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公开收件箱列表只展示预览，完整正文在详情页展示。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token, db_session)
    long_body = "VISIBLE_START " + ("正文" * 160) + " VISIBLE_END"
    await _post_webhook(
        client,
        {
            "to": "hello@example.com",
            "from": "sender@external.com",
            "subject": "长正文",
            "text": long_body,
        },
    )
    email = (
        await db_session.execute(
            select(InboundEmail).where(InboundEmail.subject == "长正文")
        )
    ).scalar_one()

    listing = await client.get(f"/mail/{public_token}")
    assert listing.status_code == 200
    assert "VISIBLE_START" in listing.text
    assert "VISIBLE_END" not in listing.text
    assert f"/mail/{public_token}/inbound/{email.id}" in listing.text
    assert "查看" in listing.text

    detail = await client.get(f"/mail/{public_token}/inbound/{email.id}")
    assert detail.status_code == 200
    assert "VISIBLE_END" in detail.text


async def test_public_mail_detail_rejects_other_address_email(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公开详情页只能查看 token 对应邮箱地址的邮件。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token, db_session)
    await _post_webhook(
        client,
        {
            "to": "other@example.com",
            "from": "sender@external.com",
            "subject": "Other mailbox",
            "text": "Should stay hidden",
        },
    )
    other_email = (
        await db_session.execute(
            select(InboundEmail).where(InboundEmail.subject == "Other mailbox")
        )
    ).scalar_one()

    resp = await client.get(f"/mail/{public_token}/inbound/{other_email.id}")
    assert resp.status_code == 404
    assert "Should stay hidden" not in resp.text


async def test_public_mail_send_uses_token_address(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公开链接发件时固定从 token 对应邮箱发送，并写入发件箱。"""
    _patch_cf(monkeypatch)
    calls = _patch_send(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token, db_session)

    resp = await client.post(
        f"/mail/{public_token}/send",
        data={
            "from_address": "attacker@example.com",
            "to": "dest@example.com",
            "subject": "公开发件",
            "text": "正文",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/mail/{public_token}?tab=outbound"
    assert len(calls.sent) == 1
    _account_id, payload = calls.sent[0]
    assert payload["from"] == "hello@example.com"
    assert payload["to"] == ["dest@example.com"]

    record = (await db_session.execute(select(OutboundEmail))).scalar_one()
    assert record.from_address == "hello@example.com"
    assert record.status == "sent"

    page = await client.get(f"/mail/{public_token}?tab=outbound")
    assert "发件箱（1）" in page.text
    assert "公开发件" in page.text


async def test_public_mail_send_rate_limit(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公开发件有独立限流。"""
    _patch_cf(monkeypatch)
    _patch_send(monkeypatch)
    monkeypatch.setattr(settings, "PUBLIC_MAIL_SEND_RATE_LIMIT_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "PUBLIC_MAIL_SEND_RATE_LIMIT_WINDOW_SECONDS", 60)
    token = await _register_and_login(client)
    public_token = await _setup(client, token, db_session)
    payload = {
        "to": "dest@example.com",
        "subject": "限流",
        "text": "正文",
    }

    first = await client.post(f"/mail/{public_token}/send", data=payload)
    assert first.status_code == 303
    second = await client.post(f"/mail/{public_token}/send", data=payload)
    assert second.status_code == 429


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


async def test_public_mail_rate_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """公开邮件链接连续访问超过配置阈值后返回 429。"""
    _patch_cf(monkeypatch)
    monkeypatch.setattr(settings, "PUBLIC_MAIL_RATE_LIMIT_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "PUBLIC_MAIL_RATE_LIMIT_WINDOW_SECONDS", 60)
    token = await _register_and_login(client)
    public_token = await _setup(client, token)

    first = await client.get(f"/mail/{public_token}.txt")
    assert first.status_code == 200
    second = await client.get(f"/mail/{public_token}.txt")
    assert second.status_code == 429


async def test_public_mail_ip_rate_limit_blocks_token_enumeration(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 IP 枚举不同 token 时也会被公开邮件全局限流拦截。"""
    monkeypatch.setattr(settings, "PUBLIC_MAIL_RATE_LIMIT_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "PUBLIC_MAIL_RATE_LIMIT_WINDOW_SECONDS", 60)

    first = await client.get("/mail/invalid-token-a.txt")
    assert first.status_code == 404
    second = await client.get("/mail/invalid-token-b.txt")
    assert second.status_code == 429


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


# ---- 大小写不敏感匹配 ----


async def test_public_mail_matches_case_insensitive(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook 传入大写 to 地址，公开查询仍能匹配到邮件。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    public_token = await _setup(client, token, db_session)
    # 创建的邮箱为 hello@example.com，Webhook 传入 HELLO@example.com
    await _post_webhook(
        client,
        {
            "to": "HELLO@example.com",
            "from": "sender@external.com",
            "subject": "大小写测试",
            "text": "正文内容",
        },
    )
    resp = await client.get(f"/mail/{public_token}.txt")
    assert resp.status_code == 200
    assert "主题: 大小写测试" in resp.text
    assert "正文内容" in resp.text

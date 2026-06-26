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
from app.models import CFAccount, Domain, InboundEmail, User
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
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, str]]:
        return ZONES

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)
    monkeypatch.setattr(CloudflareClient, "list_zones", _fake_list_zones)


async def _setup_email_address(
    client: AsyncClient,
    token: str,
    db_session: AsyncSession | None = None,
) -> str:
    """绑定并同步域名、创建邮箱地址，返回 full_address。

    同步会为域名生成 per-domain webhook_secret；若传入 ``db_session``，
    额外将其置空，以模拟「旧部署 Worker 用全局 CF_WEBHOOK_SECRET」的场景，
    使现有 webhook 测试仍走全局签名校验（inbound_service 在
    domain.webhook_secret 为空时回退到全局密钥）。
    """
    from sqlalchemy import select

    from app.models import CFAccount, Domain

    bind = await client.post(
        "/api/v1/cf-accounts",
        headers=_auth(token),
        json={
            "name": "主账号",
            "api_token": "cf-token",
            "account_id": "acc-123",
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
    if db_session is not None:
        rows = (
            await db_session.execute(
                select(Domain).join(CFAccount).where(CFAccount.id == account_id)
            )
        ).scalars().all()
        for d in rows:
            d.webhook_secret = None
        await db_session.commit()
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
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户仅能看到发往自己邮箱地址的邮件。"""
    _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    await _setup_email_address(client, token_a, db_session)
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
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按 id 获取归属于本人的收件邮件。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token, db_session)
    webhook = await _post_webhook(
        client,
        {"to": "hello@example.com", "from": "x@y.com", "subject": "s", "text": "t"},
    )
    email_id = webhook.json()["data"]["id"]

    resp = await client.get(f"/api/v1/inbound/{email_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == email_id


async def test_get_inbound_isolation(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """他人无法按 id 获取不属于自己的邮件。"""
    _patch_cf(monkeypatch)
    token_a = await _register_and_login(client)
    await _setup_email_address(client, token_a, db_session)
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


# ---- 大小写不敏感匹配 ----


async def test_webhook_normalizes_to_address_case(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook 传入混合大小写 to 地址，入库后统一为小写，归属匹配正常。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token, db_session)

    # 创建的邮箱为 hello@example.com，Webhook 传入 Hello@example.com
    resp = await _post_webhook(
        client,
        {
            "to": "Hello@example.com",
            "from": "Sender@External.com",
            "subject": "大小写测试",
            "text": "正文",
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 入库后地址应统一为小写
    assert data["to_address"] == "hello@example.com"
    assert data["from_address"] == "sender@external.com"

    # 归属查询应能匹配到这封邮件
    listing = await client.get("/api/v1/inbound", headers=_auth(token))
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1

    # 大小写不敏感的 to_address 过滤也应匹配
    filtered = await client.get(
        "/api/v1/inbound",
        headers=_auth(token),
        params={"to_address": "HELLO@example.com"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["data"]["total"] == 1


# ---- per-domain webhook_secret 签名校验 ----


async def _make_domain(
    db_session: AsyncSession,
    *,
    domain_name: str = "example.com",
    webhook_secret: str | None = "domain-secret-xyz",
) -> Domain:
    """直接构造 CFAccount + Domain（绕过 CF API），用于 webhook_secret 测试。"""
    from app.services.crypto import encrypt_token

    user = User(
        username=f"u_{domain_name.replace('.', '_')}",
        email=f"{domain_name}@test.local",
        hashed_password="x",
    )
    db_session.add(user)
    await db_session.flush()
    cf_account = CFAccount(
        user_id=user.id,
        name="t",
        encrypted_api_token=encrypt_token("tok"),
        account_id="acc-test",
    )
    db_session.add(cf_account)
    await db_session.flush()
    domain = Domain(
        cf_account_id=cf_account.id,
        zone_id=f"zone-{domain_name}",
        domain_name=domain_name,
        status="active",
        webhook_secret=webhook_secret,
    )
    db_session.add(domain)
    await db_session.commit()
    await db_session.refresh(domain)
    return domain


async def test_webhook_per_domain_secret_valid(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """域名有 webhook_secret 时，用该密钥签名通过；用全局密钥则失败。"""
    await _make_domain(db_session, webhook_secret="per-domain-key-001")

    body = json.dumps(
        {"to": "anyone@example.com", "from": "s@x.com", "subject": "t", "text": "x"}
    ).encode("utf-8")
    sig = hmac.new(b"per-domain-key-001", body, hashlib.sha256).hexdigest()
    resp = await client.post(
        "/api/v1/inbound/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == 0


async def test_webhook_per_domain_secret_wrong_global_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """域名有 webhook_secret 时，用全局密钥签名应被拒绝。"""
    await _make_domain(db_session, webhook_secret="per-domain-key-002")

    body = json.dumps(
        {"to": "a@example.com", "from": "s@x.com", "subject": "t", "text": "x"}
    ).encode("utf-8")
    # 用全局 CF_WEBHOOK_SECRET 签名（与域名密钥不一致）
    sig = _sign(body)
    resp = await client.post(
        "/api/v1/inbound/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
        },
    )
    assert resp.status_code == 401


async def test_webhook_fallback_global_when_domain_has_no_secret(
    client: AsyncClient,
) -> None:
    """域名 webhook_secret 为空（或域名不在 DB）时回退到全局密钥校验。"""
    body = json.dumps(
        {"to": "ghost@unknown.com", "from": "s@x.com", "subject": "t", "text": "x"}
    ).encode("utf-8")
    resp = await client.post(
        "/api/v1/inbound/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": _sign(body),
        },
    )
    assert resp.status_code == 200


async def test_webhook_domain_secret_case_insensitive(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """域名匹配大小写不敏感：to 用大写域名也能命中 DB 中的小写域名 secret。"""
    await _make_domain(db_session, webhook_secret="case-key", domain_name="Mixed.COM")

    body = json.dumps(
        {"to": "x@MIXED.com", "from": "s@x.com", "subject": "t", "text": "x"}
    ).encode("utf-8")
    sig = hmac.new(b"case-key", body, hashlib.sha256).hexdigest()
    resp = await client.post(
        "/api/v1/inbound/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
        },
    )
    assert resp.status_code == 200


# ---- _resolve_secret 针对性测试 ----


async def test_resolve_secret_skips_null_secret_row(
    db_session: AsyncSession,
) -> None:
    """_resolve_secret 跳过 webhook_secret 为 null 的行，fallback 全局密钥。"""
    from app.services import inbound_service
    from app.services.crypto import encrypt_token

    # 建两个同名域名：一个 secret=null（应跳过），另一个 secret="kept"
    user = User(
        username="resuser1",
        email="r1@test.local",
        hashed_password="x",
    )
    db_session.add(user)
    await db_session.flush()
    cf_account = CFAccount(
        user_id=user.id,
        name="t",
        encrypted_api_token=encrypt_token("tok"),
        account_id="acc-1",
    )
    db_session.add(cf_account)
    await db_session.flush()
    d_null = Domain(
        cf_account_id=cf_account.id,
        zone_id="z1",
        domain_name="dup.com",
        status="active",
        webhook_secret=None,
    )
    db_session.add(d_null)
    await db_session.flush()
    d_kept = Domain(
        cf_account_id=cf_account.id,
        zone_id="z2",
        domain_name="dup.com",
        status="active",
        webhook_secret="kept-secret",
    )
    db_session.add(d_kept)
    await db_session.commit()

    secret = await inbound_service._resolve_secret(db_session, "x@dup.com")
    assert secret == "kept-secret"


async def test_resolve_secret_falls_back_when_all_null(
    db_session: AsyncSession,
) -> None:
    """同名域名全部 secret=null 时，_resolve_secret fallback 到全局密钥。"""
    from app.services import inbound_service
    from app.services.crypto import encrypt_token

    user = User(
        username="resuser2",
        email="r2@test.local",
        hashed_password="x",
    )
    db_session.add(user)
    await db_session.flush()
    cf_account = CFAccount(
        user_id=user.id,
        name="t",
        encrypted_api_token=encrypt_token("tok"),
        account_id="acc-1",
    )
    db_session.add(cf_account)
    await db_session.flush()
    for i, zid in enumerate(["z1", "z2"], 1):
        d = Domain(
            cf_account_id=cf_account.id,
            zone_id=zid,
            domain_name="all-null.com",
            status="active",
            webhook_secret=None,
        )
        db_session.add(d)
    await db_session.commit()

    secret = await inbound_service._resolve_secret(db_session, "x@all-null.com")
    assert secret == settings.CF_WEBHOOK_SECRET


async def test_resolve_secret_deterministic_ordering(
    db_session: AsyncSession,
) -> None:
    """同名域名多 secret 时，按 id 升序选最早创建的行（确定性）。"""
    from app.services import inbound_service
    from app.services.crypto import encrypt_token

    user = User(
        username="resuser3",
        email="r3@test.local",
        hashed_password="x",
    )
    db_session.add(user)
    await db_session.flush()
    cf_account = CFAccount(
        user_id=user.id,
        name="t",
        encrypted_api_token=encrypt_token("tok"),
        account_id="acc-1",
    )
    db_session.add(cf_account)
    await db_session.flush()
    # 第一个 flush 拿到最小 id，第二个 flush 拿到更大 id
    d_first = Domain(
        cf_account_id=cf_account.id,
        zone_id="z-first",
        domain_name="order.com",
        status="active",
        webhook_secret="first-secret",
    )
    db_session.add(d_first)
    await db_session.flush()
    d_second = Domain(
        cf_account_id=cf_account.id,
        zone_id="z-second",
        domain_name="order.com",
        status="active",
        webhook_secret="second-secret",
    )
    db_session.add(d_second)
    await db_session.commit()

    secret = await inbound_service._resolve_secret(db_session, "x@order.com")
    # 按 id 升序，选最小 id 的行
    assert secret == "first-secret"

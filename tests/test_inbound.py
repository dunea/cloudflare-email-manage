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


async def _create_key(
    client: AsyncClient, token: str, scopes: list[str]
) -> str:
    """创建指定 scopes 的 API Key，返回原始 key。"""
    resp = await client.post(
        "/api/v1/api-keys",
        headers=_auth(token),
        json={"name": "prog", "scopes": scopes},
    )
    assert resp.status_code == 201
    return resp.json()["data"]["key"]


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

    同步阶段不再自动生成 webhook_secret（由 worker_deploy_service 统一管理），
    新建的 Domain.webhook_secret 默认 NULL，使本 helper 默认走全局签名校验。
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


async def test_webhook_rejects_oversized_payload(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook body 超过配置上限返回 413，且不入库。"""
    monkeypatch.setattr(settings, "WEBHOOK_MAX_BYTES", 16)
    body = b'{"to":"hello@example.com","text":"' + (b"x" * 32) + b'"}'

    resp = await client.post(
        "/api/v1/inbound/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": _sign(body),
        },
    )
    assert resp.status_code == 413
    rows = (await db_session.execute(select(InboundEmail))).scalars().all()
    assert rows == []


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


async def test_read_inbound_via_api_key_scope(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """具备 read_inbound scope 的 API Key 可读取收件列表和详情。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token, db_session)
    webhook = await _post_webhook(
        client,
        {"to": "hello@example.com", "from": "x@y.com", "subject": "s", "text": "t"},
    )
    raw_key = await _create_key(client, token, ["read_inbound"])
    headers = {"X-API-Key": raw_key}

    listing = await client.get("/api/v1/inbound", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1

    email_id = webhook.json()["data"]["id"]
    detail = await client.get(f"/api/v1/inbound/{email_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == email_id


async def test_read_inbound_via_api_key_requires_scope(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API Key 缺少 read_inbound scope 时不能读取收件。"""
    _patch_cf(monkeypatch)
    token = await _register_and_login(client)
    await _setup_email_address(client, token, db_session)
    webhook = await _post_webhook(
        client,
        {"to": "hello@example.com", "from": "x@y.com", "subject": "s", "text": "t"},
    )
    raw_key = await _create_key(client, token, ["send"])
    headers = {"X-API-Key": raw_key}

    listing = await client.get("/api/v1/inbound", headers=headers)
    assert listing.status_code == 403

    email_id = webhook.json()["data"]["id"]
    detail = await client.get(f"/api/v1/inbound/{email_id}", headers=headers)
    assert detail.status_code == 403


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
    """域名有 webhook_secret 且 payload 含 zone_id 时，用该密钥签名通过。"""
    domain = await _make_domain(db_session, webhook_secret="per-domain-key-001")

    body = json.dumps(
        {
            "to": "anyone@example.com",
            "from": "s@x.com",
            "zone_id": domain.zone_id,
            "subject": "t",
            "text": "x",
        }
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
    domain = await _make_domain(db_session, webhook_secret="per-domain-key-002")

    body = json.dumps(
        {
            "to": "a@example.com",
            "from": "s@x.com",
            "zone_id": domain.zone_id,
            "subject": "t",
            "text": "x",
        }
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
    domain = await _make_domain(
        db_session, webhook_secret="case-key", domain_name="Mixed.COM"
    )

    body = json.dumps(
        {
            "to": "x@MIXED.com",
            "from": "s@x.com",
            "zone_id": domain.zone_id,
            "subject": "t",
            "text": "x",
        }
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


async def _make_resolve_account(
    db_session: AsyncSession,
    *,
    username: str = "resuser",
) -> tuple[User, CFAccount]:
    """为 _resolve_secret 测试快速构造 User + CFAccount 并 flush。"""
    from app.services.crypto import encrypt_token

    user = User(
        username=username,
        email=f"{username}@test.local",
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
    return user, cf_account


async def test_resolve_secret_uses_zone_id(
    db_session: AsyncSession,
) -> None:
    """_resolve_secret 按 (zone_id, domain_name) 唯一定位，避免跨账号同名域名歧义。"""
    from app.services import inbound_service

    _user, cf_account = await _make_resolve_account(
        db_session, username="resuser1"
    )
    d_first = Domain(
        cf_account_id=cf_account.id,
        zone_id="z1",
        domain_name="dup.com",
        status="active",
        webhook_secret="first-secret",
    )
    db_session.add(d_first)
    await db_session.flush()
    d_second = Domain(
        cf_account_id=cf_account.id,
        zone_id="z2",
        domain_name="dup.com",
        status="active",
        webhook_secret="second-secret",
    )
    db_session.add(d_second)
    await db_session.commit()

    # 不同 zone_id 拿到不同 secret，不再歧义
    assert (
        await inbound_service._resolve_secret(db_session, "x@dup.com", "z1")
        == "first-secret"
    )
    assert (
        await inbound_service._resolve_secret(db_session, "x@dup.com", "z2")
        == "second-secret"
    )


async def test_resolve_secret_falls_back_when_null(
    db_session: AsyncSession,
) -> None:
    """zone_id 命中域名但 webhook_secret=null 时 fallback 全局密钥。"""
    from app.services import inbound_service

    _user, cf_account = await _make_resolve_account(
        db_session, username="resuser2"
    )
    d = Domain(
        cf_account_id=cf_account.id,
        zone_id="z1",
        domain_name="null.com",
        status="active",
        webhook_secret=None,
    )
    db_session.add(d)
    await db_session.commit()

    secret = await inbound_service._resolve_secret(db_session, "x@null.com", "z1")
    assert secret == settings.CF_WEBHOOK_SECRET


async def test_resolve_secret_fails_close_on_zone_mismatch(
    db_session: AsyncSession,
) -> None:
    """域名在 DB 中存在但 zone_id 不匹配 → fail-close（返回空串），不 fallback 全局密钥。"""
    from app.services import inbound_service

    _user, cf_account = await _make_resolve_account(
        db_session, username="resuser3"
    )
    d = Domain(
        cf_account_id=cf_account.id,
        zone_id="z-real",
        domain_name="real.com",
        status="active",
        webhook_secret="real-secret",
    )
    db_session.add(d)
    await db_session.commit()

    # zone_id 不匹配且域名已在 DB → fail-close，避免拿错/默认 secret
    secret = await inbound_service._resolve_secret(
        db_session, "x@real.com", "z-ghost"
    )
    assert secret == ""


async def test_resolve_secret_legacy_no_zone_id(
    db_session: AsyncSession,
) -> None:
    """旧 Worker 不传 zone_id 时，按域名降级匹配首条非空 secret（向后兼容）。"""
    from app.services import inbound_service

    _user, cf_account = await _make_resolve_account(
        db_session, username="resuser4"
    )
    d_null = Domain(
        cf_account_id=cf_account.id,
        zone_id="z1",
        domain_name="legacy.com",
        status="active",
        webhook_secret=None,
    )
    db_session.add(d_null)
    await db_session.flush()
    d_kept = Domain(
        cf_account_id=cf_account.id,
        zone_id="z2",
        domain_name="legacy.com",
        status="active",
        webhook_secret="legacy-secret",
    )
    db_session.add(d_kept)
    await db_session.commit()

    # 旧路径：zone_id=None → 按域名升序匹配，跳过 null
    secret = await inbound_service._resolve_secret(db_session, "x@legacy.com")
    assert secret == "legacy-secret"

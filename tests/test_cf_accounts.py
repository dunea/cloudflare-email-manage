"""CF 账号绑定测试。

所有对 Cloudflare 的调用均被 Mock：
- 业务流程通过 monkeypatch 替换 CloudflareClient 方法；
- CloudflareClient 自身通过 httpx.MockTransport 在 HTTP 层 Mock。
不会发出任何真实网络请求。
"""

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CloudflareError
from app.models import CFAccount
from app.services.cloudflare import CloudflareClient
from app.services.crypto import decrypt_token

PLAINTEXT_TOKEN = "cf-secret-token-value"


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


def _patch_verify_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """将 Token 校验替换为始终成功。"""

    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _fake_verify_account(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        """模拟 Account Token 校验成功。"""
        return {"status": "active"}

    async def _fake_list_accounts(self: CloudflareClient) -> list[dict[str, str]]:
        return [{"id": "acc-123", "name": "test-account"}]

    async def _fake_list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, object]]:
        return [
            {
                "id": "zone-e2e",
                "name": "e2e.example.com",
                "status": "active",
                "account": {"id": account_id or "acc-123", "name": "test-account"},
            }
        ]

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
    monkeypatch.setattr(CloudflareClient, "verify_account_token", _fake_verify_account)
    monkeypatch.setattr(CloudflareClient, "list_accounts", _fake_list_accounts)
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
        CloudflareClient, "probe_worker_scripts_write", _fake_probe_worker_scripts_write
    )


def _patch_verify_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """将 Token 校验替换为始终失败。"""

    async def _fake_verify(self: CloudflareClient) -> dict[str, str]:
        raise CloudflareError("Token 无效")

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)


async def _bind(
    client: AsyncClient,
    token: str,
    *,
    name: str = "主账号",
    api_token: str = PLAINTEXT_TOKEN,
    account_id: str | None = "acc-123",
) -> httpx.Response:
    """发起绑定 CF 账号请求。"""
    payload: dict[str, object] = {
        "name": name,
        "api_token": api_token,
    }
    if account_id is not None:
        payload["account_id"] = account_id
    return await client.post("/api/v1/cf-accounts", headers=_auth(token), json=payload)


# ---- 绑定 ----


async def test_bind_requires_auth(client: AsyncClient) -> None:
    """未认证绑定返回 401。"""
    resp = await client.post(
        "/api/v1/cf-accounts",
        json={"name": "x", "api_token": "t"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 1401


async def test_bind_success(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定成功返回 201，响应不含 Token。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["name"] == "主账号"
    assert data["account_id"] == "acc-123"
    assert data["is_active"] is True
    # 响应绝不暴露 Token
    assert "api_token" not in data
    assert "encrypted_api_token" not in data
    assert data["capability_report"]["overall_status"] == "passed"


async def test_bind_auto_account_id(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 account_id 时自动从 CF API 获取。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    resp = await _bind(client, token, account_id=None)
    assert resp.status_code == 201
    assert resp.json()["data"]["account_id"] == "acc-123"


async def test_bind_blank_account_id_auto_resolves(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """account_id 只含空白时按未填写处理，自动解析账号。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    resp = await _bind(client, token, account_id="   ")
    assert resp.status_code == 201
    assert resp.json()["data"]["account_id"] == "acc-123"


async def test_bind_encrypts_token_in_db(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入库 Token 为密文，且可解密还原。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    await _bind(client, token)

    row = (await db_session.execute(select(CFAccount))).scalar_one()
    assert row.encrypted_api_token != PLAINTEXT_TOKEN
    assert decrypt_token(row.encrypted_api_token) == PLAINTEXT_TOKEN


async def test_bind_invalid_token_rejected(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token 校验失败返回结构化权限报告，且不落库。"""
    _patch_verify_fail(monkeypatch)
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == 1403
    assert body["data"]["overall_status"] == "failed"

    count = (await db_session.execute(select(CFAccount))).scalars().all()
    assert count == []


async def test_bind_rejects_bearer_prefix(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """误粘 Bearer 前缀时拒绝绑定并提示填写原始 Token。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    resp = await _bind(client, token, api_token="Bearer cf-secret")
    assert resp.status_code == 403
    body = resp.json()
    assert "Bearer" in body["message"]
    assert body["data"]["items"][0]["key"] == "token_auth"
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


async def test_bind_account_token_requires_account_id(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account API Token 未填写 account_id 时返回明确提示。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    resp = await _bind(client, token, api_token="cfat_secret", account_id=None)
    assert resp.status_code == 403
    body = resp.json()
    assert "Account ID" in body["message"]
    item = body["data"]["items"][0]
    assert item["key"] == "token_auth"
    assert "Account ID" in item["message"]
    assert "Account ID" in item["fix_hint"]
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


async def test_bind_account_token_uses_account_verify(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cfat_ 前缀 Token 使用账号级 verify endpoint。"""
    _patch_verify_ok(monkeypatch)
    seen: list[str] = []

    async def _unexpected_user_verify(self: CloudflareClient) -> dict[str, str]:
        """确保账号令牌不会走用户级校验。"""
        raise AssertionError("Account API Token 不应调用 user token verify")

    async def _account_verify(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        """记录账号级校验使用的 Account ID。"""
        seen.append(account_id)
        return {"status": "active"}

    monkeypatch.setattr(CloudflareClient, "verify_token", _unexpected_user_verify)
    monkeypatch.setattr(CloudflareClient, "verify_account_token", _account_verify)

    token = await _register_and_login(client)
    resp = await _bind(client, token, api_token="cfat_secret", account_id="acc-123")
    assert resp.status_code == 201
    assert seen == ["acc-123"]
    data = resp.json()["data"]
    assert data["account_id"] == "acc-123"
    assert data["capability_report"]["items"][0]["message"].startswith(
        "Account API Token"
    )
    row = (await db_session.execute(select(CFAccount))).scalar_one()
    assert decrypt_token(row.encrypted_api_token) == "cfat_secret"


async def test_bind_unprefixed_token_falls_back_to_account_verify(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无前缀 Token 在 user verify 认证失败且提供 account_id 时回退账号校验。"""
    _patch_verify_ok(monkeypatch)
    seen: list[str] = []

    async def _user_verify_auth_failure(self: CloudflareClient) -> dict[str, str]:
        """模拟用户级 Token 校验返回认证失败。"""
        raise CloudflareError(
            "Cloudflare API 返回失败 (HTTP 403; GET /user/tokens/verify): "
            "[{'code': 10000, 'message': 'Authentication error'}]",
            cf_method="GET",
            cf_path="/user/tokens/verify",
            cf_status_code=403,
            cf_errors=[{"code": 10000, "message": "Authentication error"}],
        )

    async def _account_verify(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        """记录回退账号级校验使用的 Account ID。"""
        seen.append(account_id)
        return {"status": "active"}

    monkeypatch.setattr(CloudflareClient, "verify_token", _user_verify_auth_failure)
    monkeypatch.setattr(CloudflareClient, "verify_account_token", _account_verify)

    token = await _register_and_login(client)
    resp = await _bind(client, token, api_token="legacy-account-token")
    assert resp.status_code == 201
    assert seen == ["acc-123"]
    assert resp.json()["data"]["capability_report"]["items"][0]["message"].startswith(
        "Account API Token"
    )
    row = (await db_session.execute(select(CFAccount))).scalar_one()
    assert decrypt_token(row.encrypted_api_token) == "legacy-account-token"


async def test_bind_rejects_no_accessible_zone(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token 没有可访问 Zone 时拒绝绑定。"""
    _patch_verify_ok(monkeypatch)

    async def _empty_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(CloudflareClient, "list_zones", _empty_zones)
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 403
    body = resp.json()
    assert any(item["key"] == "zone_read" for item in body["data"]["items"])
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


async def test_bind_rejects_missing_workers_permission(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺少 Workers Scripts 能力时拒绝绑定。"""
    _patch_verify_ok(monkeypatch)

    async def _workers_forbidden(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        raise CloudflareError("HTTP 403: missing permission")

    monkeypatch.setattr(
        CloudflareClient, "probe_worker_scripts_write", _workers_forbidden
    )
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 403
    body = resp.json()
    workers = [
        item for item in body["data"]["items"] if item["key"] == "workers_scripts"
    ][0]
    assert workers["status"] == "failed"
    assert "Workers Scripts" in workers["required_permission"]
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


async def test_bind_defers_email_routing_write_probe(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定阶段不再探测所有域名的 Email Routing 写权限。"""
    _patch_verify_ok(monkeypatch)
    called = False

    async def _routing_write_forbidden(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, str]:
        nonlocal called
        called = True
        raise CloudflareError("HTTP 403: missing Email Routing Rules Write")

    monkeypatch.setattr(
        CloudflareClient,
        "probe_email_routing_rules_write",
        _routing_write_forbidden,
    )
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 201
    assert called is False
    item = [
        item
        for item in resp.json()["data"]["capability_report"]["items"]
        if item["key"] == "email_routing"
    ][0]
    assert item["status"] == "passed"
    assert "一键部署时" in item["message"]
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert len(rows) == 1


async def test_bind_defers_email_routing_settings_permission(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定阶段不再探测所有域名的 Email Routing 设置权限。"""
    _patch_verify_ok(monkeypatch)
    called = False

    async def _routing_settings_forbidden(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
        nonlocal called
        called = True
        raise CloudflareError(
            "Cloudflare API 返回失败 (HTTP 403; GET "
            f"/zones/{zone_id}/email/routing): "
            "[{'code': 10000, 'message': 'Authentication error'}]",
            cf_method="GET",
            cf_path=f"/zones/{zone_id}/email/routing",
            cf_status_code=403,
            cf_errors=[{"code": 10000, "message": "Authentication error"}],
        )

    monkeypatch.setattr(
        CloudflareClient, "get_email_routing_status", _routing_settings_forbidden
    )

    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 201
    assert called is False
    item = [
        item
        for item in resp.json()["data"]["capability_report"]["items"]
        if item["key"] == "email_routing_settings"
    ][0]
    assert item["status"] == "passed"
    assert "Zone Settings" in item["required_permission"]
    assert "一键部署时" in item["message"]
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert len(rows) == 1


async def test_bind_deferred_probe_does_not_call_unknown_routing_probe(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定阶段跳过 Email Routing 规则探测，因此不会暴露未兼容探测响应。"""
    _patch_verify_ok(monkeypatch)
    called = False

    async def _routing_unknown_response(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, str]:
        nonlocal called
        called = True
        raise CloudflareError(
            "Email Routing 规则写权限探测失败："
            "Cloudflare 返回了应用暂未兼容的探测响应，无法确认写权限；"
            "这不代表 Token 一定缺少权限。响应摘要：HTTP 418; "
            "Cloudflare errors: code=9999, message=unexpected"
        )

    monkeypatch.setattr(
        CloudflareClient,
        "probe_email_routing_rules_write",
        _routing_unknown_response,
    )
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 201
    assert called is False
    item = [
        item
        for item in resp.json()["data"]["capability_report"]["items"]
        if item["key"] == "email_routing"
    ][0]
    assert item["status"] == "passed"
    assert "一键部署时" in item["message"]
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert len(rows) == 1


async def test_bind_skips_email_routing_write_for_all_zones(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定多域名账号时不再逐个探测 Email Routing 写权限。"""
    _patch_verify_ok(monkeypatch)

    async def _two_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, object]]:
        return [
            {
                "id": "zone-a",
                "name": "a.example.com",
                "status": "active",
                "account": {"id": account_id or "acc-123", "name": "test-account"},
            },
            {
                "id": "zone-b",
                "name": "b.example.com",
                "status": "active",
                "account": {"id": account_id or "acc-123", "name": "test-account"},
            },
        ]

    probed_zone_ids: list[str] = []

    async def _probe_routing_write(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, str]:
        probed_zone_ids.append(zone_id)
        if zone_id == "zone-b":
            raise CloudflareError("HTTP 403: missing Email Routing Rules Write")
        return {"status": "ok"}

    monkeypatch.setattr(CloudflareClient, "list_zones", _two_zones)
    monkeypatch.setattr(
        CloudflareClient, "probe_email_routing_rules_write", _probe_routing_write
    )

    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 201
    assert probed_zone_ids == []
    item = [
        item
        for item in resp.json()["data"]["capability_report"]["items"]
        if item["key"] == "email_routing"
    ][0]
    assert item["status"] == "passed"
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert len(rows) == 1


async def test_bind_accepts_without_email_routing_write_for_all_zones(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多个可访问 Zone 绑定成功，但不做全域名 Email Routing 探测。"""
    _patch_verify_ok(monkeypatch)

    async def _two_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, object]]:
        return [
            {
                "id": "zone-a",
                "name": "a.example.com",
                "status": "active",
                "account": {"id": account_id or "acc-123", "name": "test-account"},
            },
            {
                "id": "zone-b",
                "name": "b.example.com",
                "status": "active",
                "account": {"id": account_id or "acc-123", "name": "test-account"},
            },
        ]

    probed_zone_ids: list[str] = []

    async def _probe_routing_write(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, str]:
        probed_zone_ids.append(zone_id)
        return {"status": "ok"}

    monkeypatch.setattr(CloudflareClient, "list_zones", _two_zones)
    monkeypatch.setattr(
        CloudflareClient, "probe_email_routing_rules_write", _probe_routing_write
    )

    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 201
    assert resp.json()["data"]["capability_report"]["zone_count"] == 2
    assert probed_zone_ids == []


async def test_bind_rejects_missing_destination_address_write(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标地址只能读不能写时拒绝绑定。"""
    _patch_verify_ok(monkeypatch)

    async def _destination_write_forbidden(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        raise CloudflareError("HTTP 403: missing Email Routing Addresses Write")

    monkeypatch.setattr(
        CloudflareClient,
        "probe_destination_addresses_write",
        _destination_write_forbidden,
    )
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 403
    item = [
        item
        for item in resp.json()["data"]["items"]
        if item["key"] == "routing_addresses"
    ][0]
    assert item["status"] == "failed"
    assert "Email Routing Addresses" in item["required_permission"]
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


async def test_bind_rejects_missing_email_sending_write(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺少 Email Sending 写权限时拒绝绑定。"""
    _patch_verify_ok(monkeypatch)

    async def _sending_write_forbidden(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        raise CloudflareError("HTTP 403: missing Email Send permission")

    monkeypatch.setattr(
        CloudflareClient,
        "probe_email_sending_write",
        _sending_write_forbidden,
    )
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 403
    item = [
        item for item in resp.json()["data"]["items"] if item["key"] == "email_sending"
    ][0]
    assert item["status"] == "failed"
    assert "Email Send" in item["required_permission"]
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


async def test_check_token_permissions_endpoint(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定前权限检查接口不落库，仅返回报告。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    resp = await client.post(
        "/api/v1/cf-accounts/check-token",
        headers=_auth(token),
        json={"api_token": PLAINTEXT_TOKEN, "account_id": "acc-123"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["overall_status"] == "passed"
    assert data["account_id"] == "acc-123"
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


# ---- 查询 / 更新 / 删除 ----


async def test_list_cf_accounts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列表返回当前用户绑定的全部账号。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    await _bind(client, token, name="账号A", account_id="a1")
    await _bind(client, token, name="账号B", account_id="a2")

    resp = await client.get("/api/v1/cf-accounts", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    names = {item["name"] for item in data["items"]}
    assert names == {"账号A", "账号B"}


async def test_get_cf_account(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 id 获取账号详情。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    account_id = (await _bind(client, token)).json()["data"]["id"]
    resp = await client.get(
        f"/api/v1/cf-accounts/{account_id}", headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == account_id


async def test_get_cf_account_not_found(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """获取不存在账号返回 404。"""
    token = await _register_and_login(client)
    resp = await client.get("/api/v1/cf-accounts/99999", headers=_auth(token))
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 1404
    assert body["data"] is None


async def test_cf_account_ownership_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户无法访问他人 CF 账号。"""
    _patch_verify_ok(monkeypatch)
    token_a = await _register_and_login(client)
    account_id = (await _bind(client, token_a)).json()["data"]["id"]

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.get(
        f"/api/v1/cf-accounts/{account_id}", headers=_auth(token_b)
    )
    assert resp.status_code == 404

    # B 的列表为空
    listing = await client.get("/api/v1/cf-accounts", headers=_auth(token_b))
    assert listing.json()["data"]["total"] == 0


async def test_update_cf_account(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更新账号名称成功。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    account_id = (await _bind(client, token)).json()["data"]["id"]
    resp = await client.patch(
        f"/api/v1/cf-accounts/{account_id}",
        headers=_auth(token),
        json={"name": "新名称"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "新名称"


async def test_update_cf_account_token_same_account(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 CF Account 下更换 Token 成功，account_id 不变。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    account_id = (await _bind(client, token)).json()["data"]["id"]
    resp = await client.patch(
        f"/api/v1/cf-accounts/{account_id}",
        headers=_auth(token),
        json={"api_token": "new-cf-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["account_id"] == "acc-123"

    row = (
        await db_session.execute(select(CFAccount).where(CFAccount.id == account_id))
    ).scalar_one()
    await db_session.refresh(row)
    assert row.account_id == "acc-123"
    assert decrypt_token(row.encrypted_api_token) == "new-cf-token"


async def test_update_cf_account_token_other_account_rejected(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更换为另一个 CF Account 的 Token 时拒绝，旧 Token 和 account_id 不变。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    account_id = (await _bind(client, token)).json()["data"]["id"]

    async def _other_account_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, object]]:
        return [
            {
                "id": "zone-other",
                "name": "other.example.com",
                "status": "active",
                "account": {"id": "acc-other", "name": "other"},
            }
        ]

    monkeypatch.setattr(CloudflareClient, "list_zones", _other_account_zones)
    resp = await client.patch(
        f"/api/v1/cf-accounts/{account_id}",
        headers=_auth(token),
        json={
            "name": "错误名称",
            "api_token": "other-account-token",
            "is_active": False,
        },
    )
    assert resp.status_code == 403
    assert "另一个 Cloudflare Account" in resp.json()["message"]

    row = (
        await db_session.execute(select(CFAccount).where(CFAccount.id == account_id))
    ).scalar_one()
    await db_session.refresh(row)
    assert row.name == "主账号"
    assert row.is_active is True
    assert row.account_id == "acc-123"
    assert decrypt_token(row.encrypted_api_token) == PLAINTEXT_TOKEN


async def test_delete_cf_account(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """软删除后不可再获取，且列表为空。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    account_id = (await _bind(client, token)).json()["data"]["id"]

    resp = await client.delete(
        f"/api/v1/cf-accounts/{account_id}", headers=_auth(token)
    )
    assert resp.status_code == 200

    gone = await client.get(
        f"/api/v1/cf-accounts/{account_id}", headers=_auth(token)
    )
    assert gone.status_code == 404
    listing = await client.get("/api/v1/cf-accounts", headers=_auth(token))
    assert listing.json()["data"]["total"] == 0


# ---- CloudflareClient HTTP 层 Mock（无真实网络）----


async def test_cloudflare_client_verify_token_success() -> None:
    """verify_token 解析标准信封返回 result。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/user/tokens/verify")
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(
            200, json={"success": True, "result": {"status": "active"}}
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.verify_token()
    assert result["status"] == "active"


async def test_cloudflare_client_verify_account_token_success() -> None:
    """verify_account_token 使用账号级校验接口。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """断言账号级 verify 请求路径并返回成功。"""
        assert request.url.path.endswith("/accounts/acc1/tokens/verify")
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(
            200, json={"success": True, "result": {"status": "active"}}
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.verify_account_token("acc1")
    assert result["status"] == "active"


async def test_cloudflare_client_failure_raises() -> None:
    """success=false 时抛出 CloudflareError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "errors": [{"message": "bad"}]}
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError) as ei:
        await cf.verify_token()
    assert "HTTP 200" in ei.value.message
    assert "GET /user/tokens/verify" in ei.value.message
    assert ei.value.cf_method == "GET"
    assert ei.value.cf_path == "/user/tokens/verify"
    assert ei.value.cf_status_code == 200


async def test_cloudflare_client_verify_account_token_failure_raises() -> None:
    """账号级 Token 校验失败时保留 Cloudflare 路径信息。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """返回账号级 verify 的 Cloudflare 认证失败响应。"""
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"code": 10000, "message": "Authentication error"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError) as ei:
        await cf.verify_account_token("acc1")
    assert "HTTP 403" in ei.value.message
    assert "GET /accounts/acc1/tokens/verify" in ei.value.message
    assert ei.value.cf_method == "GET"
    assert ei.value.cf_path == "/accounts/acc1/tokens/verify"
    assert ei.value.cf_status_code == 403


async def test_cloudflare_client_list_zones() -> None:
    """list_zones 传 account_id 时带 account.id 参数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/zones")
        assert request.url.params["account.id"] == "acc1"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {"id": "z1", "name": "a.com", "status": "active"},
                ],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    zones = await cf.list_zones("acc1")
    assert zones[0]["id"] == "z1"


async def test_cloudflare_client_list_zones_no_account_id() -> None:
    """list_zones 不传 account_id 时不带 account.id 参数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/zones")
        assert "account.id" not in request.url.params
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {
                        "id": "z1",
                        "name": "a.com",
                        "status": "active",
                        "account": {"id": "acc1", "name": "my-account"},
                    },
                ],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    zones = await cf.list_zones()
    assert zones[0]["id"] == "z1"
    assert zones[0]["account"]["id"] == "acc1"


async def test_cloudflare_client_list_zones_pagination() -> None:
    """list_zones 自动分页拉取全部域名。"""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        page = int(request.url.params.get("page", "1"))
        # 第一页 50 条，第二页 10 条，第三页空
        if page == 1:
            result = [
                {"id": f"z{i}", "name": f"a{i}.com", "status": "active"}
                for i in range(50)
            ]
        elif page == 2:
            result = [
                {"id": f"z{i}", "name": f"a{i}.com", "status": "active"}
                for i in range(50, 60)
            ]
        else:
            result = []
        return httpx.Response(200, json={"success": True, "result": result})

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    zones = await cf.list_zones("acc1")
    assert len(zones) == 60
    assert call_count == 2  # 第二页返回 <50 条即停止


async def test_cloudflare_client_list_accounts() -> None:
    """list_accounts 返回账户列表。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/accounts")
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [{"id": "acc1", "name": "my-account"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    accounts = await cf.list_accounts()
    assert accounts[0]["id"] == "acc1"

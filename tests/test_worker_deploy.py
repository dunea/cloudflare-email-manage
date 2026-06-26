"""一键部署 Worker 测试。

覆盖：
  - worker_deploy_service 业务流程（CF 调用全 Mock）
  - API 端点 POST /api/v1/cf-accounts/{id}/deploy-worker
  - Web 端点 POST /cf-accounts/{id}/deploy-worker
"""

from __future__ import annotations

import json as _json

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401  确保模型注册
from app.config import settings
from app.exceptions import AppException
from app.models import CFAccount, Domain, User
from app.services.cloudflare import CloudflareClient
from app.services.crypto import encrypt_token
from app.services import worker_deploy_service


# ---- helpers ----


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


async def _make_user_with_account_and_domains(
    db_session: AsyncSession,
    *,
    username: str = "owner",
    domains: list[tuple[str, str]] | None = None,
    account_id: str = "acc-1",
) -> tuple[User, CFAccount, list[Domain]]:
    """直接在 DB 构造用户 + CF 账号 + 域名（用于 service 单测，跳过 HTTP 绑定）。"""
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
        account_id=account_id,
    )
    db_session.add(cf_account)
    await db_session.flush()
    out: list[Domain] = []
    for zone_id, name in domains or [("zone-a", "example.com")]:
        d = Domain(
            cf_account_id=cf_account.id,
            zone_id=zone_id,
            domain_name=name,
            status="active",
            webhook_secret="init-secret",
        )
        db_session.add(d)
        out.append(d)
    await db_session.commit()
    for d in out:
        await db_session.refresh(d)
    return user, cf_account, out


def _patch_deploy_ok(
    monkeypatch: pytest.MonkeyPatch,
    *,
    upload_should_fail: Exception | None = None,
) -> dict[str, object]:
    """patch 所有 deploy 链路上的 CF 调用；可选择让 upload 失败。"""
    captured: dict[str, object] = {
        "upload_calls": 0,
        "secret_calls": 0,
        "catch_all_calls": [],
        "enable_calls": [],
    }

    async def _status(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
        return {"enabled": True, "status": "ready"}

    async def _enable(self: CloudflareClient, zone_id: str) -> dict[str, object]:
        enabled = captured["enable_calls"]
        assert isinstance(enabled, list)
        enabled.append(zone_id)
        return {"enabled": True}

    async def _upload(
        self: CloudflareClient,
        account_id: str,
        script_name: str,
        main_module_name: str,
        script_content: bytes,
        **_: object,
    ) -> dict[str, object]:
        cnt = captured["upload_calls"]
        assert isinstance(cnt, int)
        captured["upload_calls"] = cnt + 1
        if upload_should_fail is not None:
            raise upload_should_fail
        return {"id": "w1", "script_name": script_name}

    async def _secret(
        self: CloudflareClient,
        account_id: str,
        script_name: str,
        secret_name: str,
        secret_value: str,
    ) -> dict[str, object]:
        cnt = captured["secret_calls"]
        assert isinstance(cnt, int)
        captured["secret_calls"] = cnt + 1
        captured["last_secret_json"] = secret_value
        return {"name": secret_name}

    async def _catch_all(
        self: CloudflareClient, zone_id: str, worker_name: str
    ) -> dict[str, object]:
        calls = captured["catch_all_calls"]
        assert isinstance(calls, list)
        calls.append((zone_id, worker_name))
        return {"enabled": True, "actions": [{"type": "worker", "value": [worker_name]}]}

    monkeypatch.setattr(CloudflareClient, "get_email_routing_status", _status)
    monkeypatch.setattr(CloudflareClient, "enable_email_routing", _enable)
    monkeypatch.setattr(CloudflareClient, "upload_worker_script", _upload)
    monkeypatch.setattr(CloudflareClient, "set_worker_secret", _secret)
    monkeypatch.setattr(CloudflareClient, "update_catch_all_to_worker", _catch_all)
    # 跳过生产防呆校验：测试环境 APP_BASE_URL 默认为 localhost
    monkeypatch.setattr(
        worker_deploy_service, "_validate_public_base_url", lambda: None
    )
    # bundle 文件 mock：避免测试依赖真实文件
    monkeypatch.setattr(
        worker_deploy_service, "_read_bundle", lambda: b"// fake bundle"
    )
    return captured


# ---- service 单测 ----


async def test_deploy_success(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功：上传 1 次、secret 1 次、catch-all 每个域名各 1 次。

    secret 的 JSON 结构为 ``{domain: {zone_id, secret}}``，
    Worker 据此向平台回传 zone_id 以避免跨账号同名域名歧义。
    """
    _user, cf_account, _domains = await _make_user_with_account_and_domains(
        db_session,
        domains=[("z1", "a.com"), ("z2", "b.com")],
    )
    cap = _patch_deploy_ok(monkeypatch)

    result = await worker_deploy_service.deploy_worker_for_account(
        db_session, cf_account
    )

    assert result.worker_name == "cf-email-manager-webhook"
    assert result.webhook_url.endswith("/api/v1/inbound/webhook")
    assert len(result.domains) == 2
    assert cap["upload_calls"] == 1
    assert cap["secret_calls"] == 1
    catch = cap["catch_all_calls"]
    assert isinstance(catch, list)
    assert {z for z, _ in catch} == {"z1", "z2"}

    # secret JSON 结构：{domain: {zone_id, secret}}
    last_secret_json = cap["last_secret_json"]
    assert isinstance(last_secret_json, str)
    parsed = _json.loads(last_secret_json)
    assert parsed["a.com"]["zone_id"] == "z1"
    assert parsed["a.com"]["secret"] == "init-secret"
    assert parsed["b.com"]["zone_id"] == "z2"
    assert parsed["b.com"]["secret"] == "init-secret"


async def test_deploy_generates_missing_webhook_secret(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺失 webhook_secret 的域名会被自动生成。"""
    _user, cf_account, domains = await _make_user_with_account_and_domains(
        db_session, domains=[("z1", "a.com")]
    )
    domains[0].webhook_secret = None
    await db_session.commit()
    _patch_deploy_ok(monkeypatch)

    await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)
    await db_session.refresh(domains[0])
    assert domains[0].webhook_secret


async def test_deploy_no_domain_raises(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """账号下无域名时抛 AppException。"""
    from app.exceptions import AppException

    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, domains=[]
    )
    with pytest.raises(AppException):
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)


async def test_deploy_upload_failure_wraps_permission_hint(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传失败且提示权限不足时，转换为带 Workers Scripts:Edit 提示的 AppException。"""
    from app.exceptions import AppException, CloudflareError

    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, domains=[("z1", "a.com")]
    )
    _patch_deploy_ok(
        monkeypatch,
        upload_should_fail=CloudflareError("HTTP 403: missing permission"),
    )
    with pytest.raises(AppException) as ei:
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)
    assert "Workers Scripts:Edit" in str(ei.value.message)


async def test_deploy_email_routing_enabled_when_disabled(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Email Routing 未启用时自动启用。"""
    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, domains=[("z1", "a.com")]
    )
    cap = _patch_deploy_ok(monkeypatch)

    async def _status_disabled(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
        return {"enabled": False, "status": "not ready"}

    monkeypatch.setattr(
        CloudflareClient, "get_email_routing_status", _status_disabled
    )

    await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)
    enabled = cap["enable_calls"]
    assert isinstance(enabled, list) and enabled == ["z1"]


# ---- API 端点 ----


async def test_api_deploy_worker_unauthorized(client: AsyncClient) -> None:
    """未登录 401。"""
    resp = await client.post("/api/v1/cf-accounts/1/deploy-worker")
    assert resp.status_code == 401


async def test_api_deploy_worker_success(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """授权用户对自己账号触发一键部署成功。"""
    cap = _patch_deploy_ok(monkeypatch)
    token = await _register_and_login(
        client, username="apiuser", email="a@x.com"
    )
    # 查询注册用户的 id，并为其建 cf_account + domain
    from sqlalchemy import select as _sel
    from app.models import User as _U

    user = (
        await db_session.execute(_sel(_U).where(_U.username == "apiuser"))
    ).scalar_one()
    cf_account = CFAccount(
        user_id=user.id,
        name="t",
        encrypted_api_token=encrypt_token("tok"),
        account_id="acc-1",
    )
    db_session.add(cf_account)
    await db_session.flush()
    db_session.add(
        Domain(
            cf_account_id=cf_account.id,
            zone_id="z1",
            domain_name="a.com",
            status="active",
            webhook_secret="s",
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/cf-accounts/{cf_account.id}/deploy-worker",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["worker_name"] == "cf-email-manager-webhook"
    assert len(body["data"]["domains"]) == 1
    assert cap["upload_calls"] == 1


async def test_api_deploy_worker_not_owner(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非账号所有者 404。"""
    _patch_deploy_ok(monkeypatch)
    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, username="owner", domains=[("z1", "a.com")]
    )
    token = await _register_and_login(
        client, username="other", email="other@x.com"
    )
    resp = await client.post(
        f"/api/v1/cf-accounts/{cf_account.id}/deploy-worker",
        headers=_auth(token),
    )
    assert resp.status_code == 404


# ---- Web 端点 ----


async def test_web_deploy_worker_form_success(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web 表单 POST 一键部署 → 303 重定向 + flash。"""
    _patch_deploy_ok(monkeypatch)
    # 直接在 DB 造一个账号（不通过 HTTP 绑定以避免与 _patch_deploy_ok 冲突）
    from app.services.crypto import encrypt_token as _enc
    from app.models import CFAccount as _CFA, Domain as _D

    user = User(
        username="webuser",
        email="w@x.com",
        hashed_password="x",
    )
    db_session.add(user)
    await db_session.flush()
    cf_account = _CFA(
        user_id=user.id,
        name="t",
        encrypted_api_token=_enc("tok"),
        account_id="acc-1",
    )
    db_session.add(cf_account)
    await db_session.flush()
    db_session.add(
        _D(
            cf_account_id=cf_account.id,
            zone_id="z1",
            domain_name="a.com",
            status="active",
            webhook_secret="s",
        )
    )
    await db_session.commit()

    # 登录
    await client.post(
        "/register", data={"username": "webuser", "email": "w@x.com", "password": "p123456"}
    )
    await client.post("/login", data={"username": "webuser", "password": "p123456"})

    resp = await client.post(
        f"/cf-accounts/{cf_account.id}/deploy-worker",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/cf-accounts/{cf_account.id}" in resp.headers["location"]


# ---- APP_BASE_URL 校验 ----


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://localhost:8000",
        "https://localhost",
        "https://127.0.0.1",
        "https://[::1]",
        "https://10.0.0.1",
        "https://192.168.1.10",
        "https://172.16.0.1",
        "http://example.com",
        "ftp://example.com",
    ],
)
def test_validate_public_base_url_rejects_bad_urls(
    monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """私有/回环 IP、http、缺 scheme/hostname 均被拒绝。"""
    monkeypatch.setattr(settings, "APP_BASE_URL", bad_url)
    monkeypatch.setattr(settings, "CF_FAKE_MODE", False)
    with pytest.raises(AppException):
        worker_deploy_service._validate_public_base_url()


@pytest.mark.parametrize(
    "good_url",
    [
        "https://your-domain.com",
        "https://api.example.com",
    ],
)
def test_validate_public_base_url_accepts_public_https(
    monkeypatch: pytest.MonkeyPatch, good_url: str
) -> None:
    """公网 https URL 通过校验。"""
    monkeypatch.setattr(settings, "APP_BASE_URL", good_url)
    monkeypatch.setattr(settings, "CF_FAKE_MODE", False)
    worker_deploy_service._validate_public_base_url()


def test_validate_public_base_url_bypassed_in_fake_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CF_FAKE_MODE=True 时即使 localhost 也放行。"""
    monkeypatch.setattr(settings, "APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr(settings, "CF_FAKE_MODE", True)
    worker_deploy_service._validate_public_base_url()


# ---- 非 global IP 校验（含 CGNAT/reserved/benchmarking）----


@pytest.mark.parametrize(
    "bad_url",
    [
        # CGNAT 100.64.0.0/10（之前漏判）
        "https://100.64.0.1",
        # benchmarking 198.18.0.0/15
        "https://198.18.0.1",
        # IETF protocol assignments 192.0.0.0/24
        "https://192.0.0.1",
        # reserved 240.0.0.0/4
        "https://240.0.0.1",
        # multicast 224.0.0.0/4（is_global=True 但需显式拒绝）
        "https://224.0.0.1",
        # unspecified 0.0.0.0/8
        "https://0.0.0.0",
        # IPv6 link-local
        "https://[fe80::1]",
    ],
)
def test_validate_public_base_url_rejects_non_global_ip(
    monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """非 global IP（含 CGNAT / reserved / benchmarking / multicast / IPv6 link-local）一律拒绝。"""
    monkeypatch.setattr(settings, "APP_BASE_URL", bad_url)
    monkeypatch.setattr(settings, "CF_FAKE_MODE", False)
    with pytest.raises(AppException):
        worker_deploy_service._validate_public_base_url()
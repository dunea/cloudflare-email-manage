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
from app.exceptions import AppException, CFPermissionPrecheckError, CloudflareError
from app.models import CFAccount, Domain, User
from app.services import worker_deploy_service
from app.services.cloudflare import CloudflareClient
from app.services.crypto import encrypt_token

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
    enabled_zone_ids: set[str] | None = None,
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
    domain_specs = [("zone-a", "example.com")] if domains is None else domains
    enabled = (
        {zone_id for zone_id, _name in domain_specs}
        if enabled_zone_ids is None
        else enabled_zone_ids
    )
    for zone_id, name in domain_specs:
        d = Domain(
            cf_account_id=cf_account.id,
            zone_id=zone_id,
            domain_name=name,
            status="active",
            webhook_secret="init-secret",
            inbound_routing_enabled=zone_id in enabled,
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
    secret_should_fail: Exception | None = None,
    catch_all_should_fail: Exception | None = None,
) -> dict[str, object]:
    """patch 所有 deploy 链路上的 CF 调用；可选择让 upload 失败。"""
    captured: dict[str, object] = {
        "upload_calls": 0,
        "secret_calls": 0,
        "catch_all_calls": [],
        "enable_calls": [],
        "list_zones_account_ids": [],
        "routing_rules_calls": [],
        "routing_probe_calls": [],
        "status_calls": [],
    }

    async def _verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, object]]:
        account_ids = captured["list_zones_account_ids"]
        assert isinstance(account_ids, list)
        account_ids.append(account_id)
        return [
            {
                "id": "z1",
                "name": "a.com",
                "status": "active",
                "account": {"id": account_id or "acc-1", "name": "test"},
            },
            {
                "id": "z2",
                "name": "b.com",
                "status": "active",
                "account": {"id": account_id or "acc-1", "name": "test"},
            },
            {
                "id": "zone-a",
                "name": "example.com",
                "status": "active",
                "account": {"id": account_id or "acc-1", "name": "test"},
            }
        ]

    async def _list_routing_rules(
        self: CloudflareClient, zone_id: str
    ) -> list[dict[str, object]]:
        calls = captured["routing_rules_calls"]
        assert isinstance(calls, list)
        calls.append(zone_id)
        return []

    async def _list_destinations(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _list_email_sending(
        self: CloudflareClient, zone_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _probe_email_routing_rules_write(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, str]:
        calls = captured["routing_probe_calls"]
        assert isinstance(calls, list)
        calls.append(zone_id)
        return {"status": "ok"}

    async def _probe_destination_addresses_write(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        return {"status": "ok"}

    async def _probe_email_sending_write(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        return {"status": "ok"}

    async def _probe_worker_scripts_write(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        return {"status": "ok"}

    async def _status(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
        calls = captured["status_calls"]
        assert isinstance(calls, list)
        calls.append(zone_id)
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
        if secret_should_fail is not None:
            raise secret_should_fail
        return {"name": secret_name}

    async def _catch_all(
        self: CloudflareClient, zone_id: str, worker_name: str
    ) -> dict[str, object]:
        calls = captured["catch_all_calls"]
        assert isinstance(calls, list)
        calls.append((zone_id, worker_name))
        if catch_all_should_fail is not None:
            raise catch_all_should_fail
        return {"enabled": True, "actions": [{"type": "worker", "value": [worker_name]}]}

    monkeypatch.setattr(CloudflareClient, "verify_token", _verify)
    monkeypatch.setattr(CloudflareClient, "list_zones", _list_zones)
    monkeypatch.setattr(CloudflareClient, "list_routing_rules", _list_routing_rules)
    monkeypatch.setattr(CloudflareClient, "list_destination_addresses", _list_destinations)
    monkeypatch.setattr(
        CloudflareClient, "list_email_sending_subdomains", _list_email_sending
    )
    monkeypatch.setattr(
        CloudflareClient,
        "probe_email_routing_rules_write",
        _probe_email_routing_rules_write,
    )
    monkeypatch.setattr(
        CloudflareClient,
        "probe_destination_addresses_write",
        _probe_destination_addresses_write,
    )
    monkeypatch.setattr(
        CloudflareClient, "probe_email_sending_write", _probe_email_sending_write
    )
    monkeypatch.setattr(
        CloudflareClient, "probe_worker_scripts_write", _probe_worker_scripts_write
    )
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


async def test_deploy_only_enabled_email_domains(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只部署已启用收件路由的邮箱域名，跳过账号下其他域名。"""
    _user, cf_account, _domains = await _make_user_with_account_and_domains(
        db_session,
        domains=[("z1", "mail.com"), ("z2", "kenginet.com")],
        enabled_zone_ids={"z1"},
    )
    cap = _patch_deploy_ok(monkeypatch)

    result = await worker_deploy_service.deploy_worker_for_account(
        db_session, cf_account
    )

    assert [d.domain_name for d in result.domains] == ["mail.com"]
    assert cap["routing_rules_calls"] == ["z1"]
    assert cap["routing_probe_calls"] == ["z1"]
    status_calls = cap["status_calls"]
    assert isinstance(status_calls, list)
    assert status_calls == ["z1", "z1"]
    catch = cap["catch_all_calls"]
    assert isinstance(catch, list)
    assert catch == [("z1", "cf-email-manager-webhook")]
    last_secret_json = cap["last_secret_json"]
    assert isinstance(last_secret_json, str)
    assert set(_json.loads(last_secret_json)) == {"mail.com"}


async def test_deploy_ignores_disabled_domain_routing_forbidden(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非邮箱域名即使 Email Routing 设置不可访问，也不影响部署。"""
    _user, cf_account, _domains = await _make_user_with_account_and_domains(
        db_session,
        domains=[("z1", "mail.com"), ("z2", "kenginet.com")],
        enabled_zone_ids={"z1"},
    )
    _patch_deploy_ok(monkeypatch)

    async def _status_forbidden_on_disabled(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
        if zone_id == "z2":
            raise CloudflareError("HTTP 403: zone settings forbidden")
        return {"enabled": True, "status": "ready"}

    monkeypatch.setattr(
        CloudflareClient, "get_email_routing_status", _status_forbidden_on_disabled
    )

    result = await worker_deploy_service.deploy_worker_for_account(
        db_session, cf_account
    )

    assert [d.zone_id for d in result.domains] == ["z1"]


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
    _patch_deploy_ok(monkeypatch)
    with pytest.raises(AppException):
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)


async def test_deploy_no_enabled_email_domain_raises_without_cf_write(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有启用收件路由的邮箱域名时，部署前失败且不写 CF。"""
    _user, cf_account, _domains = await _make_user_with_account_and_domains(
        db_session,
        domains=[("z1", "mail.com"), ("z2", "kenginet.com")],
        enabled_zone_ids=set(),
    )
    cap = _patch_deploy_ok(monkeypatch)

    with pytest.raises(AppException) as ei:
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)

    assert "尚无启用收件路由的邮箱域名" in ei.value.message
    assert cap["upload_calls"] == 0
    assert cap["secret_calls"] == 0
    assert cap["catch_all_calls"] == []
    assert cap["status_calls"] == []


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


async def test_deploy_permission_precheck_failure_does_not_upload(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部署前权限预检失败时，不上传 Worker、不写 secret、不改 catch-all。"""
    from app.exceptions import CloudflareError

    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, domains=[("z1", "a.com")]
    )
    cap = _patch_deploy_ok(monkeypatch)

    async def _workers_forbidden(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        raise CloudflareError("HTTP 403: missing permission")

    monkeypatch.setattr(
        CloudflareClient, "probe_worker_scripts_write", _workers_forbidden
    )

    with pytest.raises(AppException) as ei:
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)
    assert "Workers 脚本" in ei.value.message
    assert cap["upload_calls"] == 0
    assert cap["secret_calls"] == 0
    assert cap["catch_all_calls"] == []


async def test_deploy_email_routing_settings_precheck_failure_does_not_upload(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部署前缺少 Zone Settings 时，不上传 Worker、不写 secret、不改 catch-all。"""
    from app.exceptions import CloudflareError

    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, domains=[("z1", "a.com")]
    )
    cap = _patch_deploy_ok(monkeypatch)

    async def _routing_settings_forbidden(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
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

    with pytest.raises(AppException) as ei:
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)

    assert "Email Routing 设置" in ei.value.message
    assert isinstance(ei.value, CFPermissionPrecheckError)
    report = ei.value.report
    item = [
        item for item in report.items if item.key == "email_routing_settings"
    ][0]
    assert "Zone Settings" in item.fix_hint
    assert cap["upload_calls"] == 0
    assert cap["secret_calls"] == 0
    assert cap["catch_all_calls"] == []


async def test_deploy_rejects_local_domain_outside_bound_account(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地旧域名不属于当前 Account 时，部署前失败且不写 CF。"""
    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, domains=[("z1", "a.com"), ("z-stale", "old.com")]
    )
    cap = _patch_deploy_ok(monkeypatch)

    with pytest.raises(AppException) as ei:
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)

    assert "本地域名与当前 Cloudflare Account 不一致" in ei.value.message
    assert "old.com" in ei.value.message
    assert cap["upload_calls"] == 0
    assert cap["secret_calls"] == 0
    assert cap["catch_all_calls"] == []


async def test_deploy_secret_failure_wraps_stage_and_cf_summary(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设置 Worker Secret 失败时，用户提示和日志都包含定位上下文。"""
    from app.exceptions import CloudflareError

    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, domains=[("z1", "a.com")]
    )
    logged: dict[str, object] = {}

    def _capture_log(message: str, *args: object, **kwargs: object) -> None:
        logged["message"] = message
        logged["args"] = args
        logged["kwargs"] = kwargs

    monkeypatch.setattr(worker_deploy_service.logger, "exception", _capture_log)
    _patch_deploy_ok(
        monkeypatch,
        secret_should_fail=CloudflareError(
            "Cloudflare API 返回失败 (HTTP 403; PUT "
            "/accounts/acc-1/workers/scripts/cf-email-manager-webhook/secrets): "
            "[{'code': 10000, 'message': 'Authentication error'}]",
            cf_method="PUT",
            cf_path="/accounts/acc-1/workers/scripts/cf-email-manager-webhook/secrets",
            cf_status_code=403,
            cf_errors=[{"code": 10000, "message": "Authentication error"}],
        ),
    )

    with pytest.raises(AppException) as ei:
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)

    assert "设置 Worker Secret 失败" in ei.value.message
    assert "10000" in ei.value.message
    assert "Authentication error" in ei.value.message
    message = str(logged.get("message", ""))
    args = logged.get("args", ())
    assert isinstance(args, tuple)
    log_text = message % args
    assert "method=PUT" in log_text
    assert "PUT" in log_text
    assert "/workers/scripts/cf-email-manager-webhook/secrets" in log_text
    assert "account_id=acc-1" in log_text
    assert "WEBHOOK_SECRETS" not in log_text
    assert "init-secret" not in log_text


async def test_deploy_catch_all_failure_wraps_domain_and_zone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置 catch-all 失败时，提示包含具体域名与 zone_id。"""
    from app.exceptions import CloudflareError

    _user, cf_account, _ = await _make_user_with_account_and_domains(
        db_session, domains=[("z1", "a.com")]
    )
    _patch_deploy_ok(
        monkeypatch,
        catch_all_should_fail=CloudflareError(
            "Cloudflare API 返回失败 (HTTP 403; PUT "
            "/zones/z1/email/routing/rules/catch_all): "
            "[{'code': 10000, 'message': 'Authentication error'}]",
            cf_method="PUT",
            cf_path="/zones/z1/email/routing/rules/catch_all",
            cf_status_code=403,
            cf_errors=[{"code": 10000, "message": "Authentication error"}],
        ),
    )

    with pytest.raises(AppException) as ei:
        await worker_deploy_service.deploy_worker_for_account(db_session, cf_account)

    assert "配置 a.com catch-all 失败" in ei.value.message
    assert "zone_id=z1" in ei.value.message
    assert "10000" in ei.value.message


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
            inbound_routing_enabled=True,
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
    from app.models import CFAccount as _CFA
    from app.models import Domain as _D
    from app.services.crypto import encrypt_token as _enc

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
            inbound_routing_enabled=True,
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


async def test_web_deploy_worker_zone_settings_error_uses_short_flash(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web 一键部署遇到 Zone Settings 缺失时，toast 不展示冗长 CF path。"""
    from app.exceptions import CloudflareError

    cap = _patch_deploy_ok(monkeypatch)
    await client.post(
        "/register",
        data={"username": "webfail", "email": "fail@x.com", "password": "p123456"},
    )
    await client.post("/login", data={"username": "webfail", "password": "p123456"})

    from sqlalchemy import select as _sel

    user = (
        await db_session.execute(_sel(User).where(User.username == "webfail"))
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
            inbound_routing_enabled=True,
        )
    )
    await db_session.commit()

    async def _routing_settings_forbidden(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
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

    resp = await client.post(
        f"/cf-accounts/{cf_account.id}/deploy-worker",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "部署 Worker 失败：Token 缺少 Zone Settings 权限" in resp.text
    assert "Cloudflare API 返回失败 (HTTP 403; GET" not in resp.text
    assert cap["upload_calls"] == 0


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

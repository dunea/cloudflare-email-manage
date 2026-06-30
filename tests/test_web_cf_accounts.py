"""前端 CF 账号页面测试：列表 / 绑定 / 同步 / 编辑 / 解绑。

所有 Cloudflare 调用通过 monkeypatch 替换 CloudflareClient 方法，不发真实请求。
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CFPermissionPrecheckError, CloudflareError
from app.models import CFAccount, Domain, EmailAddress, InboundEmail, User
from app.services.cloudflare import CloudflareClient
from app.services.crypto import decrypt_token, encrypt_token
from app.web.cf_accounts import _capability_report_from_exception
from app.web.templating import _format_dt


async def _web_login(
    client: AsyncClient,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "password123",
) -> None:
    """通过前端注册并登录，cookie 由 client 自动保存。"""
    await client.post(
        "/register",
        data={"username": username, "email": email, "password": password},
    )
    await client.post("/login", data={"username": username, "password": password})


def _patch_verify_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify(self: CloudflareClient) -> dict[str, str]:
        return {"status": "active"}

    async def _list_accounts(self: CloudflareClient) -> list[dict[str, str]]:
        return [{"id": "acc-1", "name": "test-account"}]

    async def _list_zones(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, object]]:
        return [
            {
                "id": "zone-e2e",
                "name": "e2e.example.com",
                "status": "active",
                "account": {"id": "acc-1", "name": "test-account"},
            }
        ]

    async def _list_routing_rules(
        self: CloudflareClient, zone_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _list_destinations(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _get_email_routing_status(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, object]:
        return {"enabled": True, "status": "ready"}

    async def _list_email_sending(
        self: CloudflareClient, zone_id: str
    ) -> list[dict[str, object]]:
        return []

    async def _probe_email_routing_rules_write(
        self: CloudflareClient, zone_id: str
    ) -> dict[str, str]:
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

    monkeypatch.setattr(CloudflareClient, "verify_token", _verify)
    monkeypatch.setattr(CloudflareClient, "list_accounts", _list_accounts)
    monkeypatch.setattr(CloudflareClient, "list_zones", _list_zones)
    monkeypatch.setattr(CloudflareClient, "list_routing_rules", _list_routing_rules)
    monkeypatch.setattr(CloudflareClient, "list_destination_addresses", _list_destinations)
    monkeypatch.setattr(
        CloudflareClient, "get_email_routing_status", _get_email_routing_status
    )
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


def _patch_verify_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify(self: CloudflareClient) -> dict[str, str]:
        raise CloudflareError("Token 无效")

    monkeypatch.setattr(CloudflareClient, "verify_token", _verify)


def _patch_list_zones(
    monkeypatch: pytest.MonkeyPatch, zones: list[dict[str, str]]
) -> None:
    async def _list(
        self: CloudflareClient, account_id: str | None = None
    ) -> list[dict[str, str]]:
        return zones

    monkeypatch.setattr(CloudflareClient, "list_zones", _list)


async def _bind(
    client: AsyncClient,
    *,
    name: str = "主账号",
    api_token: str = "tok",
    account_id: str = "acc-1",
) -> object:
    return await client.post(
        "/cf-accounts",
        data={
            "name": name,
            "api_token": api_token,
            "account_id": account_id,
        },
        follow_redirects=False,
    )


async def test_cf_accounts_requires_auth(client: AsyncClient) -> None:
    """未登录访问列表跳登录页。"""
    resp = await client.get("/cf-accounts", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_dashboard_renders_stats(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """登录后仪表盘渲染统计与最近收件区块。"""
    await _web_login(client)
    user = (
        await db_session.execute(select(User).where(User.username == "alice"))
    ).scalar_one()
    cf = CFAccount(
        user_id=user.id,
        name="acc",
        encrypted_api_token=encrypt_token("tok"),
        account_id="acc-1",
    )
    db_session.add(cf)
    await db_session.commit()
    await db_session.refresh(cf)
    domain = Domain(
        cf_account_id=cf.id,
        zone_id="zone-1",
        domain_name="example.com",
        status="active",
    )
    db_session.add(domain)
    await db_session.commit()
    await db_session.refresh(domain)
    address = EmailAddress(
        domain_id=domain.id,
        user_id=user.id,
        local_part="hello",
        full_address="hello@example.com",
        public_token=uuid.uuid4().hex,
    )
    db_session.add(address)
    db_session.add(
        InboundEmail(
            to_address=address.full_address,
            from_address="very-long-sender-name@example-long-domain.test",
            subject="仪表盘布局测试",
            body_text="正文",
        )
    )
    await db_session.commit()

    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "最近收件" in resp.text
    assert "仪表盘布局测试" in resp.text
    assert "table-fixed" in resp.text
    assert "whitespace-nowrap" in resp.text
    assert "md:hidden" in resp.text


async def test_new_cf_account_shows_current_permission_guidance(
    client: AsyncClient,
) -> None:
    """绑定页按 Cloudflare 当前 UI 区分整个账户与邮箱域名权限。"""
    await _web_login(client)
    resp = await client.get("/cf-accounts/new")
    assert resp.status_code == 200
    assert "整个账户" in resp.text
    assert "邮箱域名" in resp.text
    assert "Zone Settings" in resp.text
    assert "Email Routing Rules" in resp.text
    assert "Workers Routes" in resp.text
    assert "当前不是必需权限" in resp.text


async def test_bind_cf_account_success(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定成功后跳详情，Token 加密入库，列表可见。"""
    _patch_verify_ok(monkeypatch)
    await _web_login(client)
    resp = await _bind(client)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/cf-accounts/")

    row = (await db_session.execute(select(CFAccount))).scalar_one()
    assert row.name == "主账号"
    assert decrypt_token(row.encrypted_api_token) == "tok"

    listing = await client.get("/cf-accounts")
    assert listing.status_code == 200
    assert "主账号" in listing.text


async def test_bind_cf_account_invalid_token(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token 校验失败回填表单并提示，不落库。"""
    _patch_verify_fail(monkeypatch)
    await _web_login(client)
    resp = await _bind(client)
    assert resp.status_code == 400
    assert "Cloudflare API Token 权限预检未通过" in resp.text
    rows = (await db_session.execute(select(CFAccount))).scalars().all()
    assert rows == []


async def test_sync_domains_flashes_count(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步域名后在详情页提示同步数量。"""
    _patch_verify_ok(monkeypatch)
    _patch_list_zones(monkeypatch, [{"id": "z1", "name": "a.com", "status": "active"}])
    await _web_login(client)
    await _bind(client)
    account = (await db_session.execute(select(CFAccount))).scalar_one()

    resp = await client.post(
        f"/cf-accounts/{account.id}/sync", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/cf-accounts/{account.id}"

    detail = await client.get(f"/cf-accounts/{account.id}")
    assert detail.status_code == 200
    assert "已同步 1 个域名" in detail.text


async def test_edit_cf_account_renames(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编辑账号名称生效。"""
    _patch_verify_ok(monkeypatch)
    await _web_login(client)
    await _bind(client)
    account = (await db_session.execute(select(CFAccount))).scalar_one()

    resp = await client.post(
        f"/cf-accounts/{account.id}/edit",
        data={
            "name": "新名称",
            "api_token": "",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail = await client.get(f"/cf-accounts/{account.id}")
    assert "新名称" in detail.text


async def test_edit_cf_account_token_failure_rerenders_saved_state(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编辑页 Token 预检失败时回显数据库旧状态，并展示权限报告。"""
    _patch_verify_ok(monkeypatch)
    await _web_login(client)
    await _bind(client)
    account = (await db_session.execute(select(CFAccount))).scalar_one()

    async def _workers_forbidden(
        self: CloudflareClient, account_id: str
    ) -> dict[str, str]:
        raise CloudflareError("HTTP 403: missing Workers Scripts Write")

    monkeypatch.setattr(
        CloudflareClient, "probe_worker_scripts_write", _workers_forbidden
    )
    resp = await client.post(
        f"/cf-accounts/{account.id}/edit",
        data={
            "name": "错误名称",
            "api_token": "bad-token",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "Cloudflare API Token 权限预检未通过" in resp.text
    assert "Workers 脚本" in resp.text
    assert "主账号" in resp.text
    assert "错误名称" not in resp.text

    await db_session.refresh(account)
    assert account.name == "主账号"
    assert account.is_active is True
    assert account.account_id == "acc-1"
    assert decrypt_token(account.encrypted_api_token) == "tok"


async def test_format_dt_accepts_iso_string() -> None:
    """模板 dt 过滤器可格式化从 JSON 报告中取出的 ISO 时间字符串。"""
    assert _format_dt("2026-06-28T13:56:01+00:00") == "2026-06-28 13:56"
    assert _format_dt("2026-06-28T13:56:01Z") == "2026-06-28 13:56"


def test_capability_report_from_exception_accepts_dict_report() -> None:
    """权限预检异常携带 dict 报告时，Web 层可直接回显。"""
    report: dict[str, object] = {"overall_status": "failed", "items": []}
    exc = CFPermissionPrecheckError("权限预检失败", report=report)

    assert _capability_report_from_exception(exc) == report


def test_capability_report_from_exception_rejects_non_dict_report() -> None:
    """异常报告不是 dict 或 Pydantic model 时，不应导致页面 500。"""
    exc = CFPermissionPrecheckError("权限预检失败", report=["not", "dict"])

    assert _capability_report_from_exception(exc) == {}


async def test_delete_cf_account(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解绑后列表不再可见。"""
    _patch_verify_ok(monkeypatch)
    await _web_login(client)
    await _bind(client)
    account = (await db_session.execute(select(CFAccount))).scalar_one()

    resp = await client.post(
        f"/cf-accounts/{account.id}/delete", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cf-accounts"

    listing = await client.get("/cf-accounts")
    assert "主账号" not in listing.text


async def test_cf_account_detail_not_found(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """访问不存在的账号详情返回 404 页面。"""
    await _web_login(client)
    resp = await client.get("/cf-accounts/99999")
    assert resp.status_code == 404
    assert "不存在" in resp.text

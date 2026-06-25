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
                "account": {"id": "acc-123", "name": "test-account"},
            }
        ]

    monkeypatch.setattr(CloudflareClient, "verify_token", _fake_verify)
    monkeypatch.setattr(CloudflareClient, "list_accounts", _fake_list_accounts)
    monkeypatch.setattr(CloudflareClient, "list_zones", _fake_list_zones)


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


async def test_bind_auto_account_id(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 account_id 时自动从 CF API 获取。"""
    _patch_verify_ok(monkeypatch)
    token = await _register_and_login(client)
    resp = await _bind(client, token, account_id=None)
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
    """Token 校验失败返回 502，且不落库。"""
    _patch_verify_fail(monkeypatch)
    token = await _register_and_login(client)
    resp = await _bind(client, token)
    assert resp.status_code == 502
    assert resp.json()["code"] == 1502

    count = (await db_session.execute(select(CFAccount))).scalars().all()
    assert count == []


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
    assert resp.json()["code"] == 1404


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


async def test_cloudflare_client_failure_raises() -> None:
    """success=false 时抛出 CloudflareError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "errors": [{"message": "bad"}]}
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError):
        await cf.verify_token()


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

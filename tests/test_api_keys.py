"""API Key 管理 测试。

验证：创建时返回原始 key（仅一次）、库内仅存哈希、列表/详情/更新/删除、
归属隔离，以及原始 key 可用于 X-API-Key 认证（在发件测试中覆盖）。
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import APIKey
from app.services.api_key_service import hash_api_key

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
    client: AsyncClient, token: str, name: str = "默认"
) -> dict[str, object]:
    """创建一个 API Key，返回响应 data。"""
    resp = await client.post(
        "/api/v1/api-keys", headers=_auth(token), json={"name": name}
    )
    assert resp.status_code == 201
    return resp.json()["data"]


# ---- 创建 ----


async def test_create_api_key_returns_raw_key_once(client: AsyncClient) -> None:
    """创建时返回原始 key，前缀为 cfem_，且不回显哈希。"""
    token = await _register_and_login(client)
    data = await _create_key(client, token, name="prog")

    assert data["name"] == "prog"
    assert data["is_active"] is True
    assert data["key"].startswith("cfem_")
    # 不应泄露哈希字段
    assert "key_hash" not in data


async def test_create_requires_auth(client: AsyncClient) -> None:
    """未认证创建返回 401。"""
    resp = await client.post("/api/v1/api-keys", json={"name": "x"})
    assert resp.status_code == 401


async def test_only_hash_is_stored(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """库中只存哈希，不存明文 key。"""
    token = await _register_and_login(client)
    data = await _create_key(client, token)
    raw_key = data["key"]

    rows = (await db_session.execute(select(APIKey))).scalars().all()
    assert len(rows) == 1
    stored = rows[0]
    assert stored.key_hash == hash_api_key(raw_key)
    assert stored.key_hash != raw_key
    assert raw_key not in stored.key_hash


# ---- 列表 / 详情 ----


async def test_list_api_keys_hides_raw_key(client: AsyncClient) -> None:
    """列表分页返回，且不含原始 key。"""
    token = await _register_and_login(client)
    await _create_key(client, token, name="a")
    await _create_key(client, token, name="b")

    resp = await client.get("/api/v1/api-keys", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    for item in data["items"]:
        assert "key" not in item
        assert "key_hash" not in item


async def test_get_api_key(client: AsyncClient) -> None:
    """按 id 获取 API Key 详情。"""
    token = await _register_and_login(client)
    created = await _create_key(client, token)
    key_id = created["id"]

    resp = await client.get(f"/api/v1/api-keys/{key_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == key_id


async def test_get_unknown_returns_404(client: AsyncClient) -> None:
    """获取不存在的 API Key 返回 404。"""
    token = await _register_and_login(client)
    resp = await client.get("/api/v1/api-keys/99999", headers=_auth(token))
    assert resp.status_code == 404


# ---- 更新 / 删除 ----


async def test_update_api_key(client: AsyncClient) -> None:
    """更新 API Key 名称与启用状态。"""
    token = await _register_and_login(client)
    created = await _create_key(client, token, name="old")
    key_id = created["id"]

    resp = await client.patch(
        f"/api/v1/api-keys/{key_id}",
        headers=_auth(token),
        json={"name": "new", "is_active": False},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "new"
    assert data["is_active"] is False


async def test_delete_api_key(client: AsyncClient) -> None:
    """删除 API Key 后不再可见。"""
    token = await _register_and_login(client)
    created = await _create_key(client, token)
    key_id = created["id"]

    deleted = await client.delete(
        f"/api/v1/api-keys/{key_id}", headers=_auth(token)
    )
    assert deleted.status_code == 200

    after = await client.get(f"/api/v1/api-keys/{key_id}", headers=_auth(token))
    assert after.status_code == 404


# ---- 隔离 ----


async def test_access_isolation(client: AsyncClient) -> None:
    """用户无法访问他人的 API Key。"""
    token_a = await _register_and_login(client)
    created = await _create_key(client, token_a)
    key_id = created["id"]

    token_b = await _register_and_login(
        client, username="bob", email="bob@example.com"
    )
    resp = await client.get(f"/api/v1/api-keys/{key_id}", headers=_auth(token_b))
    assert resp.status_code == 404

    listing = await client.get("/api/v1/api-keys", headers=_auth(token_b))
    assert listing.json()["data"]["total"] == 0

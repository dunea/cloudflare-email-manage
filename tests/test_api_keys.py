"""API Key 管理 测试。

验证：创建时返回原始 key（仅一次）、库内仅存哈希、列表/详情/更新/删除、
归属隔离，以及原始 key 可用于 X-API-Key 认证（在发件测试中覆盖）。
"""

import sqlite3
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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
    client: AsyncClient,
    token: str,
    name: str = "默认",
    scopes: list[str] | None = None,
) -> dict[str, object]:
    """创建一个 API Key，返回响应 data。"""
    payload: dict[str, object] = {"name": name}
    if scopes is not None:
        payload["scopes"] = scopes
    resp = await client.post(
        "/api/v1/api-keys", headers=_auth(token), json=payload
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
    assert data["scopes"] == ["send", "read_inbound"]
    # 不应泄露哈希字段
    assert "key_hash" not in data


async def test_create_api_key_with_custom_scopes(client: AsyncClient) -> None:
    """创建时可指定最小 scope，响应按稳定顺序返回。"""
    token = await _register_and_login(client)
    data = await _create_key(client, token, name="readonly", scopes=["read_inbound"])

    assert data["name"] == "readonly"
    assert data["scopes"] == ["read_inbound"]


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
    await _create_key(client, token, name="b", scopes=["send"])

    resp = await client.get("/api/v1/api-keys", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    for item in data["items"]:
        assert "key" not in item
        assert "key_hash" not in item
    scopes_by_name = {item["name"]: item["scopes"] for item in data["items"]}
    assert scopes_by_name == {
        "a": ["send", "read_inbound"],
        "b": ["send"],
    }


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


async def test_update_api_key_scopes(client: AsyncClient) -> None:
    """API Key 权限范围可更新。"""
    token = await _register_and_login(client)
    created = await _create_key(client, token)
    key_id = created["id"]

    resp = await client.patch(
        f"/api/v1/api-keys/{key_id}",
        headers=_auth(token),
        json={"scopes": ["read_inbound"]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["scopes"] == ["read_inbound"]


async def test_rejects_unknown_scope(client: AsyncClient) -> None:
    """未知 scope 返回 422，避免意外开放管理接口。"""
    token = await _register_and_login(client)

    resp = await client.post(
        "/api/v1/api-keys",
        headers=_auth(token),
        json={"name": "bad", "scopes": ["admin"]},
    )
    assert resp.status_code == 422


async def test_rejects_empty_scopes(client: AsyncClient) -> None:
    """显式传空 scopes 返回 422，不回退为默认全权限。"""
    token = await _register_and_login(client)

    resp = await client.post(
        "/api/v1/api-keys",
        headers=_auth(token),
        json={"name": "empty", "scopes": []},
    )
    assert resp.status_code == 422


def test_existing_api_keys_receive_default_scopes_after_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧版本 API Key 迁移到 head 后默认拥有 send/read_inbound。"""
    from alembic.config import Config

    from alembic import command

    db_path = tmp_path / "migration.db"
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
    )
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("path_separator", "os")
    command.upgrade(alembic_config, "e5f6a7b8c9d0")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO user (username, email, hashed_password, role, is_active, is_deleted)
            VALUES ('legacy', 'legacy@example.com', 'x', 'user', 1, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO api_key (user_id, key_hash, name, is_active, is_deleted)
            VALUES (1, 'legacy-hash', 'legacy-key', 1, 0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(alembic_config, "head")

    conn = sqlite3.connect(db_path)
    try:
        scopes = conn.execute(
            "SELECT scopes FROM api_key WHERE key_hash = 'legacy-hash'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert scopes == "send,read_inbound"


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

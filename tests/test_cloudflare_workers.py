"""CloudflareClient 新增方法 HTTP 层 Mock 测试。

涵盖：一键部署 Worker 链路所需的 Workers Scripts API（上传/secret）、
Email Routing 启用与状态、Catch-all 规则查询/更新。
不发出任何真实网络请求（httpx.MockTransport）。
"""

import json

import httpx
import pytest

from app.services.cloudflare import CloudflareClient


# ---- Workers Scripts：上传 ----


async def test_upload_worker_script_multipart() -> None:
    """upload_worker_script 发送 multipart，metadata part + index.js 文件 part。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("Content-Type", "")
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"id": "worker-1", "script_name": "sw"},
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    bundle = b"// bundle js content"
    bindings = [{"type": "plain_text", "name": "WEBHOOK_URL", "text": "http://x/w"}]
    result = await cf.upload_worker_script(
        account_id="acc1",
        script_name="sw",
        main_module_name="index.js",
        script_content=bundle,
        compatibility_date="2025-01-01",
        compatibility_flags=["nodejs_compat"],
        bindings=bindings,
    )

    # URL 正确
    assert isinstance(captured["url"], str)
    assert captured["url"].endswith("/accounts/acc1/workers/scripts/sw")
    # multipart
    ct = captured["content_type"]
    assert isinstance(ct, str) and ct.startswith("multipart/form-data")
    # body 含 metadata(JSON) 和 index.js 文件
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'name="metadata"' in body
    assert b'"main_module":"index.js"' in body
    assert b'"compatibility_flags":["nodejs_compat"]' in body
    assert b'"WEBHOOK_URL"' in body
    assert b'name="index.js"' in body
    assert bundle in body
    assert result["id"] == "worker-1"


async def test_upload_worker_script_400_raises() -> None:
    """HTTP 400 时抛出 CloudflareError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"success": False, "errors": [{"message": "bad script"}]}
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(Exception):
        await cf.upload_worker_script(
            "acc", "sw", "index.js", b"x", compatibility_date="2025-01-01"
        )


# ---- Workers Scripts：Secret ----


async def test_set_worker_secret_payload() -> None:
    """set_worker_secret PUT 标准 JSON，body 含 name/text/type=secret。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"success": True, "result": {"name": "WEBHOOK_SECRETS"}}
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.set_worker_secret(
        "acc1", "sw", "WEBHOOK_SECRETS", '{"a.com":"s1"}'
    )
    url = captured["url"]
    assert isinstance(url, str) and url.endswith(
        "/accounts/acc1/workers/scripts/sw/secrets"
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["name"] == "WEBHOOK_SECRETS"
    assert body["text"] == '{"a.com":"s1"}'
    assert body["type"] == "secret"
    assert result["name"] == "WEBHOOK_SECRETS"


# ---- Email Routing：状态 / 启用 ----


async def test_get_email_routing_status() -> None:
    """get_email_routing_status 返回 status 对象。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/zones/z1/email/routing")
        assert request.method == "GET"
        return httpx.Response(
            200, json={"success": True, "result": {"enabled": True, "status": "ready"}}
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    status = await cf.get_email_routing_status("z1")
    assert status["enabled"] is True


async def test_enable_email_routing() -> None:
    """enable_email_routing 调用 .../email/routing/enable。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/zones/z1/email/routing/enable")
        assert request.method == "POST"
        return httpx.Response(
            200, json={"success": True, "result": {"enabled": True}}
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.enable_email_routing("z1")
    assert result["enabled"] is True


# ---- Email Routing：Catch-all ----


async def test_get_catch_all_rule() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/zones/z1/email/routing/rules/catch_all")
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "c1",
                    "enabled": False,
                    "actions": [],
                    "matchers": [{"type": "all"}],
                },
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    rule = await cf.get_catch_all_rule("z1")
    assert rule["enabled"] is False


async def test_update_catch_all_to_worker() -> None:
    """update_catch_all_to_worker PUT，action type=worker, value=[worker_name]。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        assert request.method == "PUT"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "c1",
                    "enabled": True,
                    "actions": [{"type": "worker", "value": ["sw"]}],
                    "matchers": [{"type": "all"}],
                },
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.update_catch_all_to_worker("z1", "sw")
    url = captured["url"]
    assert isinstance(url, str) and url.endswith(
        "/zones/z1/email/routing/rules/catch_all"
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["enabled"] is True
    assert body["actions"] == [{"type": "worker", "value": ["sw"]}]
    assert body["matchers"] == [{"type": "all"}]
    assert result["enabled"] is True
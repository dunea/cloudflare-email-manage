"""CloudflareClient 新增方法 HTTP 层 Mock 测试。

涵盖：一键部署 Worker 链路所需的 Workers Scripts API（上传/secret）、
Email Routing 启用与状态、Catch-all 规则查询/更新。
不发出任何真实网络请求（httpx.MockTransport）。
"""

import json

import httpx
import pytest

from app.config import settings
from app.exceptions import CloudflareError
from app.services.cloudflare import CloudflareClient, WorkerBinding

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
    bindings: list[WorkerBinding] = [
        WorkerBinding(type="plain_text", name="WEBHOOK_URL", text="http://x/w"),
    ]
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
    with pytest.raises(CloudflareError):
        await cf.upload_worker_script(
            "acc", "sw", "index.js", b"x", compatibility_date="2025-01-01"
        )


# ---- Workers Scripts：列表 / Secret ----


async def test_list_worker_scripts_uses_account_endpoint() -> None:
    """list_worker_scripts 使用账号级 Workers Scripts 列表接口。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url).endswith("/accounts/acc1/workers/scripts")
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [{"id": "sw", "script_name": "sw"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    scripts = await cf.list_worker_scripts("acc1")
    assert scripts[0]["script_name"] == "sw"


async def test_probe_worker_scripts_write_accepts_validation_error() -> None:
    """写权限探测用合法 secret body；脚本不存在时视为已通过鉴权。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            400,
            json={
                "success": False,
                "errors": [{"code": 10021, "message": "script not found"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.probe_worker_scripts_write("acc1")
    assert result["status"] == "ok"
    assert captured["method"] == "PUT"
    url = captured["url"]
    assert isinstance(url, str)
    script_prefix = (
        "https://api.cloudflare.com/client/v4/accounts/acc1/workers/scripts/"
        "cf-email-manager-permission-probe-"
    )
    assert url.startswith(script_prefix)
    assert url.endswith("/secrets")
    script_name = url.rsplit("/workers/scripts/", 1)[1].removesuffix("/secrets")
    assert script_name != "cf-email-manager-permission-probe-never-create"
    hex_suffix = script_name.removeprefix("cf-email-manager-permission-probe-")
    assert len(hex_suffix) == 32
    int(hex_suffix, 16)
    assert captured["body"] == {
        "name": "CF_EMAIL_MANAGER_PERMISSION_PROBE",
        "text": "probe",
        "type": "secret_text",
    }


async def test_probe_worker_scripts_write_permission_error_raises() -> None:
    """写权限探测遇到认证或权限错误时抛出 CloudflareError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"code": 10000, "message": "Authentication error"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError):
        await cf.probe_worker_scripts_write("acc1")


async def test_probe_worker_scripts_write_accepts_script_not_found_404() -> None:
    """Workers 写探测遇到明确脚本不存在时视为已通过鉴权。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "success": False,
                "errors": [{"code": 10021, "message": "script not found"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.probe_worker_scripts_write("acc1")
    assert result["status"] == "ok"


async def test_probe_worker_scripts_write_accepts_not_found_variants() -> None:
    """Workers 写探测兼容 Cloudflare 脚本不存在响应变体。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "success": False,
                "errors": [
                    {"code": "script_not_found", "message": "Worker does not exist"}
                ],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.probe_worker_scripts_write("acc1")
    assert result["status"] == "ok"


@pytest.mark.parametrize(
    "error",
    [
        {"code": "not_found", "message": "resource not found"},
        {"code": 10021, "message": "resource does not exist"},
    ],
)
async def test_probe_worker_scripts_write_rejects_generic_not_found(
    error: dict[str, object],
) -> None:
    """泛化 not found 不能被误判为 Workers 脚本写权限已通过。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"success": False, "errors": [error]},
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError, match="暂未兼容"):
        await cf.probe_worker_scripts_write("acc1")


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_probe_worker_scripts_write_transient_errors_raise(
    status_code: int,
) -> None:
    """429/5xx 不能被误判为 Workers 写权限通过。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"success": False, "errors": [{"message": "temporary error"}]},
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError, match="暂时无法完成权限探测"):
        await cf.probe_worker_scripts_write("acc1")


async def test_probe_worker_scripts_write_non_json_raises() -> None:
    """非 JSON 响应不能被误判为 Workers 写权限通过。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad gateway")

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError, match="非 JSON"):
        await cf.probe_worker_scripts_write("acc1")


async def test_probe_worker_scripts_write_unexpected_success_raises() -> None:
    """无效探测 payload 返回 2xx 时按异常失败处理。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": {}})

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError, match="无效探测 payload"):
        await cf.probe_worker_scripts_write("acc1")


async def test_probe_email_routing_rules_write_uses_invalid_create_payload() -> None:
    """Email Routing 写探测使用创建规则接口和无副作用非法 action。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            400,
            json={
                "success": False,
                "errors": [
                    {
                        "message": "matchers is required",
                        "source": {"pointer": "/matchers"},
                    }
                ],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.probe_email_routing_rules_write("zone1")
    assert result["status"] == "ok"
    assert captured["method"] == "POST"
    url = captured["url"]
    assert isinstance(url, str)
    assert url.endswith("/zones/zone1/email/routing/rules")
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["actions"][0]["type"] == "cf-email-manager-invalid-action"
    assert body["matchers"][0]["value"].endswith("@example.invalid")


async def test_probe_email_routing_rules_write_accepts_422_validation() -> None:
    """Email Routing 写探测接受 422 validation/source.pointer 响应。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "success": False,
                "errors": [
                    {
                        "message": "actions.0.type is invalid",
                        "source": {"pointer": "/actions/0/type"},
                    }
                ],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.probe_email_routing_rules_write("zone1")
    assert result["status"] == "ok"


async def test_probe_email_routing_rules_write_permission_error_raises() -> None:
    """Email Routing 写探测遇到认证或权限错误时失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"code": 10000, "message": "Authentication error"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError, match="权限不足"):
        await cf.probe_email_routing_rules_write("zone1")


async def test_probe_destination_addresses_write_uses_invalid_create_payload() -> None:
    """目标地址写探测使用创建地址接口和无效 payload，不发送验证邮件。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            422,
            json={
                "success": False,
                "errors": [{"message": "email is invalid"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.probe_destination_addresses_write("acc1")
    assert result["status"] == "ok"
    assert captured["method"] == "POST"
    url = captured["url"]
    assert isinstance(url, str)
    assert url.endswith("/accounts/acc1/email/routing/addresses")
    assert captured["body"] == {"email": "not-an-email"}


async def test_probe_destination_addresses_write_permission_error_raises() -> None:
    """目标地址写探测遇到认证或权限错误时失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"code": 10000, "message": "Authentication error"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError, match="权限不足"):
        await cf.probe_destination_addresses_write("acc1")


async def test_probe_email_sending_write_uses_invalid_send_payload() -> None:
    """Email Sending 写探测使用发件接口和无效 payload，不发送邮件。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(
            400,
            json={
                "success": False,
                "errors": [{"message": "from is required"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.probe_email_sending_write("acc1")
    assert result["status"] == "ok"
    assert captured["method"] == "POST"
    url = captured["url"]
    assert isinstance(url, str)
    assert url.endswith("/accounts/acc1/email/sending/send")
    assert captured["body"] == b"{}"


async def test_set_worker_secret_payload() -> None:
    """set_worker_secret PUT 标准 JSON，body 含 name/text/type=secret_text。"""
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
    assert body["type"] == "secret_text"
    assert result["name"] == "WEBHOOK_SECRETS"


# ---- Email Routing：状态 / 启用 ----


async def test_send_email_uses_email_sending_rest_endpoint() -> None:
    """send_email 使用当前 Cloudflare Email Sending REST 路径。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "delivered": ["dest@example.com"],
                    "permanent_bounces": [],
                    "queued": [],
                },
            },
        )

    payload = {
        "from": "hello@example.com",
        "to": ["dest@example.com"],
        "subject": "Hi",
        "text": "Hello",
    }
    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    result = await cf.send_email("acc1", payload)

    url = captured["url"]
    assert isinstance(url, str)
    assert url.endswith("/accounts/acc1/email/sending/send")
    assert captured["body"] == payload
    assert result["success"] is True


async def test_list_email_sending_subdomains_uses_zone_endpoint() -> None:
    """list_email_sending_subdomains 使用 Zone 级 Email Sending 配置列表接口。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url).endswith("/zones/zone1/email/sending/subdomains")
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [{"name": "example.com", "status": "ready"}],
            },
        )

    cf = CloudflareClient("tok", transport=httpx.MockTransport(handler))
    subdomains = await cf.list_email_sending_subdomains("zone1")
    assert subdomains[0]["name"] == "example.com"


async def test_fake_destination_addresses_follow_create_delete_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """假 CF 目标地址列表只反映 create/delete 后的状态。"""
    monkeypatch.setattr(settings, "CF_FAKE_MODE", True)
    cf = CloudflareClient("tok")
    account_id = "acc-fake-destination-state"

    assert await cf.list_destination_addresses(account_id) == []

    created = await cf.create_destination_address(account_id, "Dest@Example.com")
    assert created["email"] == "dest@example.com"
    assert created["verified"] == "2026-06-26T08:00:00Z"

    listed = await cf.list_destination_addresses(account_id)
    assert listed == [created]

    await cf.delete_destination_address(account_id, created["id"])
    assert await cf.list_destination_addresses(account_id) == []


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
    """get_catch_all_rule 返回规则对象，含 enabled/actions/matchers。"""

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
    assert rule.get("enabled") is False


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
    assert result.get("enabled") is True

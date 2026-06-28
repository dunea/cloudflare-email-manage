"""Cloudflare API 封装（httpx AsyncClient）。

统一封装 Cloudflare client/v4 接口：Token 校验、Zone 查询、
Email Routing（转发规则 / 目标地址）、Email Sending（Beta）。

所有方法均为异步，使用 Bearer Token 认证。返回值为 CF 响应中的
`result` 字段（列表或对象）。CF 返回 success=false 或 HTTP 非 2xx
时抛出 CloudflareError。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict

import httpx

from app.config import settings
from app.exceptions import CloudflareError

_FAKE_DESTINATION_VERIFIED_AT = "2026-06-26T08:00:00Z"
_fake_destination_addresses: dict[str, dict[str, dict[str, str]]] = {}


def _reset_fake_destination_addresses() -> None:
    """清空假 CF 目标地址缓存 (仅供测试隔离使用)."""
    _fake_destination_addresses.clear()


def _fake_destination_id(email: str) -> str:
    """根据邮箱生成稳定的假 CF 目标地址 ID。"""
    return "fake-dest-" + email.lower().replace("@", "-at-").replace(".", "-")


# ---- Worker 部署相关响应 TypedDict ----


class EmailRoutingStatus(TypedDict):
    """Zone Email Routing 启用状态响应。"""

    enabled: bool
    status: NotRequired[str]


class EmailRoutingAction(TypedDict, total=False):
    """Email Routing 规则 action（CF 文档常见字段）。

    ``type`` 为 ``worker`` 时 ``value`` 为 ``list[str]``（Worker 名称）；
    ``type`` 为 ``forward`` / ``email`` 时 ``value`` 为目标邮箱地址（字符串）。
    """

    type: str
    value: list[str] | str


class EmailRoutingMatcher(TypedDict, total=False):
    """Email Routing 规则 matcher（CF 文档常见字段）。"""

    type: str
    field: NotRequired[str]
    value: NotRequired[str]


class CatchAllRule(TypedDict, total=False):
    """Zone catch-all 规则响应。

    使用 ``total=False`` 允许所有键缺省，方便按 CF 实际响应灵活构造。
    """

    id: str
    enabled: bool
    actions: list[EmailRoutingAction]
    matchers: list[EmailRoutingMatcher]


class WorkerBinding(TypedDict):
    """Worker 绑定项（上传脚本时作为 metadata 的一部分传入）。"""

    type: str
    name: str
    text: NotRequired[str]


class WorkerUploadResult(TypedDict, total=False):
    """Worker 脚本上传成功后的 CF 响应 result。

    CF 在不同场景下会返回 ``id``、``script_name`` 或其他元信息，
    使用 ``total=False`` 允许按响应灵活取值。
    """

    id: str
    script_name: str
    etag: NotRequired[str]
    modified_on: NotRequired[str]
    created_on: NotRequired[str]


class WorkerSecretResult(TypedDict, total=False):
    """Worker secret 写入成功后的 CF 响应 result。"""

    name: str

# CF API 默认超时（秒）
_DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True)
class _ProbeDecision:
    """写权限探测响应的归类结果。"""

    passed: bool
    reason: str


_AUTH_OR_PERMISSION_MARKERS = (
    "10000",
    "authentication error",
    "permission denied",
    "missing permission",
    "insufficient permission",
    "not have permission",
    "unauthorized",
    "not authorized",
    "forbidden",
    "access denied",
)

_VALIDATION_MARKERS = (
    "validation",
    "schema",
    "request body",
    "body",
    "field",
    "required",
    "missing",
    "must",
    "expected",
    "parameter",
    "payload",
    "json",
    "malformed",
    "invalid",
    "not valid",
    "unprocessable",
)

_WORKERS_SCRIPT_NOT_FOUND_MARKERS = (
    "script not found",
    "script_not_found",
    "no such script",
    "could not find script",
    "workers script not found",
    "worker script not found",
    "worker does not exist",
)


def _body_to_text(body: object) -> str:
    """将 CF 响应体转换为可检索文本。"""
    if isinstance(body, dict) or isinstance(body, list):
        return json.dumps(body, ensure_ascii=False)
    return str(body)


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    """判断文本是否包含任一标记。"""
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _request_target(
    method: str, path: str, params: dict[str, Any] | None = None
) -> str:
    """构造不含敏感信息的 CF 请求定位摘要。"""
    target = f"{method.upper()} {path}"
    if params:
        target += f" params={params}"
    return target


def _has_error_source_pointer(body: object) -> bool:
    """Cloudflare schema 错误通常会带 source.pointer。"""
    if not isinstance(body, dict):
        return False
    for key in ("errors", "messages"):
        items = body.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if isinstance(source, dict) and source.get("pointer"):
                return True
    return False


def _summarize_probe_body(status_code: int, body: object) -> str:
    """提取可展示的 Cloudflare 探测响应摘要。"""
    parts = [f"HTTP {status_code}"]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            summaries: list[str] = []
            for error in errors[:3]:
                if not isinstance(error, dict):
                    continue
                fields: list[str] = []
                code = error.get("code")
                message = error.get("message")
                if code is not None:
                    fields.append(f"code={code}")
                if message:
                    fields.append(f"message={message}")
                if fields:
                    summaries.append(", ".join(fields))
            if summaries:
                parts.append("Cloudflare errors: " + " | ".join(summaries))
        else:
            messages = body.get("messages")
            if isinstance(messages, list) and messages:
                parts.append(f"Cloudflare messages: {messages[:3]}")
    return "; ".join(parts)


def _classify_write_probe_response(
    status_code: int,
    body: object,
    *,
    validation_markers: tuple[str, ...] = (),
    not_found_markers: tuple[str, ...] = (),
) -> _ProbeDecision:
    """将无效写请求的响应归类为通过或失败。

    探测 payload 本身是无效的，因此只有明确进入业务层参数/资源校验时
    才算通过；认证、权限、限流、服务端错误和未知响应一律失败。
    """
    text = _body_to_text(body)
    if 200 <= status_code < 300:
        return _ProbeDecision(
            False,
            "无效探测 payload 返回了成功响应，结果异常，已拒绝绑定。",
        )
    if status_code in {401, 403} or _has_marker(text, _AUTH_OR_PERMISSION_MARKERS):
        return _ProbeDecision(
            False,
            "Token 无效、权限不足或资源范围未覆盖该 Account / Zone。",
        )
    if status_code == 429 or status_code >= 500:
        return _ProbeDecision(
            False,
            "Cloudflare 暂时无法完成权限探测，请稍后重试。",
        )
    if 400 <= status_code < 500 and not_found_markers and _has_marker(
        text, not_found_markers
    ):
        return _ProbeDecision(True, "已进入写接口资源校验阶段。")
    all_validation_markers = _VALIDATION_MARKERS + validation_markers
    if 400 <= status_code < 500 and (
        _has_error_source_pointer(body) or _has_marker(text, all_validation_markers)
    ):
        return _ProbeDecision(True, "已进入写接口参数校验阶段。")
    summary = _summarize_probe_body(status_code, body)
    return _ProbeDecision(
        False,
        "Cloudflare 返回了应用暂未兼容的探测响应，无法确认写权限；"
        f"这不代表 Token 一定缺少权限。响应摘要：{summary}",
    )


class CloudflareClient:
    """单个 CF API Token 对应的异步客户端。

    transport 仅用于测试时注入 httpx.MockTransport，生产环境保持 None。
    """

    def __init__(
        self,
        api_token: str,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_token = api_token
        self._base_url = base_url or settings.CF_API_BASE_URL
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        """构造带 Bearer 认证的请求头。"""
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """发起一次 HTTP 请求并返回原始响应，网络异常转为 CloudflareError。"""
        target = _request_target(method, path, params)
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_DEFAULT_TIMEOUT,
            transport=self._transport,
        ) as client:
            try:
                return await client.request(
                    method, path, params=params, json=json, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                raise CloudflareError(
                    f"调用 Cloudflare API 失败 ({target}): {exc}",
                    cf_method=method.upper(),
                    cf_path=path,
                ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """发起请求并解析标准 CF 响应信封，返回 result 字段。"""
        resp = await self._raw_request(method, path, params=params, json=json)
        try:
            body = resp.json()
        except ValueError as exc:
            raise CloudflareError(
                "Cloudflare API 返回非 JSON 响应 "
                f"(HTTP {resp.status_code}; {_request_target(method, path, params)})",
                cf_method=method.upper(),
                cf_path=path,
                cf_status_code=resp.status_code,
            ) from exc

        if not isinstance(body, dict) or not body.get("success", False):
            errors = body.get("errors") if isinstance(body, dict) else None
            raise CloudflareError(
                "Cloudflare API 返回失败 "
                f"(HTTP {resp.status_code}; {_request_target(method, path, params)}): "
                f"{errors or body}",
                cf_method=method.upper(),
                cf_path=path,
                cf_status_code=resp.status_code,
                cf_errors=errors or body,
            )
        return body.get("result")

    async def _run_invalid_write_probe(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any],
        capability_label: str,
        validation_markers: tuple[str, ...] = (),
        not_found_markers: tuple[str, ...] = (),
    ) -> dict[str, str]:
        """发送无效写请求，确认 Token 已进入写接口业务校验阶段。"""
        resp = await self._raw_request(method, path, json=payload)
        try:
            body: object = resp.json()
        except ValueError as exc:
            raise CloudflareError(
                f"{capability_label}权限探测失败：Cloudflare 返回非 JSON 响应 "
                f"(HTTP {resp.status_code}; {_request_target(method, path)})",
                cf_method=method.upper(),
                cf_path=path,
                cf_status_code=resp.status_code,
            ) from exc

        decision = _classify_write_probe_response(
            resp.status_code,
            body,
            validation_markers=validation_markers,
            not_found_markers=not_found_markers,
        )
        if not decision.passed:
            raise CloudflareError(
                f"{capability_label}权限探测失败：{decision.reason} "
                f"(HTTP {resp.status_code}; {_request_target(method, path)}): {body}",
                cf_method=method.upper(),
                cf_path=path,
                cf_status_code=resp.status_code,
                cf_errors=body,
            )
        return {"status": "ok"}

    # ---- Token 校验 ----

    async def verify_token(self) -> dict[str, Any]:
        """校验 API Token 有效性（GET /user/tokens/verify）。"""
        if settings.CF_FAKE_MODE:
            return {"status": "active"}
        result = await self._request("GET", "/user/tokens/verify")
        return result if isinstance(result, dict) else {}

    # ---- Account（账户）----

    async def list_accounts(self) -> list[dict[str, Any]]:
        """列出 Token 可访问的 CF 账户（GET /accounts）。"""
        if settings.CF_FAKE_MODE:
            return [{"id": "acc-e2e", "name": "e2e-account"}]
        result = await self._request("GET", "/accounts")
        return result if isinstance(result, list) else []

    # ---- Zone（域名）----

    async def list_zones(self, account_id: str | None = None) -> list[dict[str, Any]]:
        """列出 Zone（域名），自动分页拉取全部。

        account_id 为 None 时不按账户过滤，返回 Token 可访问的所有 Zone，
        每个 Zone 自带 ``account: {id, name}`` 字段，仅需 Zone:Zone:Read 权限。
        """
        if settings.CF_FAKE_MODE:
            return [
                {
                    "id": "zone-e2e",
                    "name": "e2e.example.com",
                    "status": "active",
                    "account": {"id": "acc-e2e", "name": "e2e-account"},
                }
            ]
        all_zones: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, Any] = {"per_page": 50, "page": page}
            if account_id is not None:
                params["account.id"] = account_id
            result = await self._request("GET", "/zones", params=params)
            if not isinstance(result, list) or len(result) == 0:
                break
            all_zones.extend(result)
            if len(result) < 50:
                break
            page += 1
        return all_zones

    async def get_zone(self, zone_id: str) -> dict[str, Any]:
        """获取单个 Zone 详情。"""
        result = await self._request("GET", f"/zones/{zone_id}")
        return result if isinstance(result, dict) else {}

    # ---- Email Routing：转发规则 ----

    async def list_routing_rules(self, zone_id: str) -> list[dict[str, Any]]:
        """列出 Zone 的邮件转发规则。"""
        result = await self._request(
            "GET", f"/zones/{zone_id}/email/routing/rules"
        )
        return result if isinstance(result, list) else []

    async def probe_email_routing_rules_write(
        self, zone_id: str
    ) -> dict[str, str]:
        """无副作用探测 Email Routing 规则写权限。"""
        if settings.CF_FAKE_MODE:
            return {"status": "ok"}
        return await self._run_invalid_write_probe(
            "POST",
            f"/zones/{zone_id}/email/routing/rules",
            payload={
                "enabled": True,
                "name": "cf-email-manager-permission-probe-invalid",
                "matchers": [
                    {
                        "type": "literal",
                        "field": "to",
                        "value": "cf-email-manager-probe@example.invalid",
                    }
                ],
                "actions": [
                    {
                        "type": "cf-email-manager-invalid-action",
                        "value": ["probe@example.invalid"],
                    }
                ],
            },
            capability_label="Email Routing 规则写",
            validation_markers=(
                "action",
                "actions",
                "matcher",
                "matchers",
                "routing rule",
                "rule",
            ),
        )

    async def create_routing_rule(
        self, zone_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """创建一条邮件转发规则。"""
        if settings.CF_FAKE_MODE:
            return {"id": "rule-e2e"}
        result = await self._request(
            "POST", f"/zones/{zone_id}/email/routing/rules", json=payload
        )
        return result if isinstance(result, dict) else {}

    async def delete_routing_rule(
        self, zone_id: str, rule_id: str
    ) -> dict[str, Any]:
        """删除一条邮件转发规则。"""
        if settings.CF_FAKE_MODE:
            return {"id": rule_id}
        result = await self._request(
            "DELETE", f"/zones/{zone_id}/email/routing/rules/{rule_id}"
        )
        return result if isinstance(result, dict) else {}

    # ---- Email Routing：启用/状态 ----

    async def get_email_routing_status(self, zone_id: str) -> EmailRoutingStatus:
        """查询 Zone 的 Email Routing 状态（GET /zones/{zid}/email/routing）。"""
        if settings.CF_FAKE_MODE:
            return {"enabled": True, "status": "ready"}
        result = await self._request("GET", f"/zones/{zone_id}/email/routing")
        if not isinstance(result, dict):
            return {"enabled": False}
        return EmailRoutingStatus(
            enabled=bool(result.get("enabled", False)),
            status=str(result.get("status", "")),
        )

    async def enable_email_routing(self, zone_id: str) -> EmailRoutingStatus:
        """启用 Zone 的 Email Routing DNS 设置（POST .../email/routing/dns）。"""
        if settings.CF_FAKE_MODE:
            return {"enabled": True, "status": "ready"}
        result = await self._request(
            "POST", f"/zones/{zone_id}/email/routing/dns"
        )
        if not isinstance(result, dict):
            return {"enabled": True}
        return EmailRoutingStatus(
            enabled=bool(result.get("enabled", True)),
            status=str(result.get("status", "")),
        )

    # ---- Email Routing：catch-all 规则 ----

    async def get_catch_all_rule(self, zone_id: str) -> CatchAllRule:
        """获取 Zone 的 catch-all 规则（GET .../email/routing/rules/catch_all）。"""
        if settings.CF_FAKE_MODE:
            return CatchAllRule(
                id="catch-all-fake",
                enabled=False,
                actions=[],
                matchers=[{"type": "all"}],
            )
        result = await self._request(
            "GET", f"/zones/{zone_id}/email/routing/rules/catch_all"
        )
        if not isinstance(result, dict):
            return CatchAllRule(enabled=False, actions=[], matchers=[{"type": "all"}])
        out: CatchAllRule = CatchAllRule(
            enabled=bool(result.get("enabled", False)),
            actions=list(result.get("actions", [])),
            matchers=list(result.get("matchers", [{"type": "all"}])),
        )
        if result.get("id"):
            out["id"] = str(result["id"])
        return out

    async def update_catch_all_to_worker(
        self, zone_id: str, worker_name: str
    ) -> CatchAllRule:
        """将 Zone 的 catch-all 规则设为投递到指定 Worker（PUT .../rules/catch_all）。

        action: ``{"type": "worker", "value": [worker_name], "enabled": True}``。
        """
        if settings.CF_FAKE_MODE:
            return CatchAllRule(
                id="catch-all-fake",
                enabled=True,
                actions=[{"type": "worker", "value": [worker_name]}],
                matchers=[{"type": "all"}],
            )
        payload = {
            "enabled": True,
            "actions": [{"type": "worker", "value": [worker_name]}],
            "matchers": [{"type": "all"}],
        }
        result = await self._request(
            "PUT", f"/zones/{zone_id}/email/routing/rules/catch_all", json=payload
        )
        if not isinstance(result, dict):
            return CatchAllRule(
                enabled=True,
                actions=[{"type": "worker", "value": [worker_name]}],
                matchers=[{"type": "all"}],
            )
        out: CatchAllRule = CatchAllRule(
            enabled=bool(result.get("enabled", True)),
            actions=list(result.get("actions", [{"type": "worker", "value": [worker_name]}])),
            matchers=list(result.get("matchers", [{"type": "all"}])),
        )
        if result.get("id"):
            out["id"] = str(result["id"])
        return out

    # ---- Email Routing：目标地址 ----

    async def list_destination_addresses(
        self, account_id: str
    ) -> list[dict[str, Any]]:
        """列出账号下的转发目标地址。

        返回项含 ``id`` / ``email`` / ``verified``（verified 为 ISO 时间字符串或 None）。
        """
        if settings.CF_FAKE_MODE:
            return [
                dict(item)
                for item in _fake_destination_addresses.get(account_id, {}).values()
            ]
        result = await self._request(
            "GET", f"/accounts/{account_id}/email/routing/addresses"
        )
        return result if isinstance(result, list) else []

    async def probe_destination_addresses_write(
        self, account_id: str
    ) -> dict[str, str]:
        """无副作用探测转发目标地址写权限，不发送验证邮件。"""
        if settings.CF_FAKE_MODE:
            return {"status": "ok"}
        return await self._run_invalid_write_probe(
            "POST",
            f"/accounts/{account_id}/email/routing/addresses",
            payload={"email": "not-an-email"},
            capability_label="转发目标地址写",
            validation_markers=("email", "address", "destination", "mailbox"),
        )

    async def get_destination_address(
        self, account_id: str, address_id: str
    ) -> dict[str, Any]:
        """获取单个转发目标地址（含 verified 字段）。"""
        result = await self._request(
            "GET",
            f"/accounts/{account_id}/email/routing/addresses/{address_id}",
        )
        return result if isinstance(result, dict) else {}

    async def create_destination_address(
        self, account_id: str, email: str
    ) -> dict[str, Any]:
        """创建一个转发目标地址 (需被验证后才可用).

        CF 会向该邮箱发送验证邮件, 返回结果含 ``id`` / ``email`` / ``verified``.
        真实 CF 中 ``verified`` 通常为 None; 假 CF 模式返回固定时间戳.
        """
        if settings.CF_FAKE_MODE:
            normalized_email = email.lower()
            address_id = _fake_destination_id(normalized_email)
            item = {
                "id": address_id,
                "email": normalized_email,
                "verified": _FAKE_DESTINATION_VERIFIED_AT,
            }
            _fake_destination_addresses.setdefault(account_id, {})[address_id] = item
            return dict(item)
        result = await self._request(
            "POST",
            f"/accounts/{account_id}/email/routing/addresses",
            json={"email": email},
        )
        return result if isinstance(result, dict) else {}

    async def delete_destination_address(
        self, account_id: str, address_id: str
    ) -> dict[str, Any]:
        """删除一个转发目标地址。

        删除后 CF 会自动停用引用该地址的路由规则。
        """
        if settings.CF_FAKE_MODE:
            _fake_destination_addresses.get(account_id, {}).pop(address_id, None)
            return {"id": address_id}
        result = await self._request(
            "DELETE",
            f"/accounts/{account_id}/email/routing/addresses/{address_id}",
        )
        return result if isinstance(result, dict) else {}

    # ---- Email Sending（Beta）----

    async def send_email(
        self, account_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """通过 CF Email Sending（Beta）发送邮件。

        发送接口在成功时可能返回 202 且响应体非标准信封，故单独处理。
        """
        if settings.CF_FAKE_MODE:
            return {"id": "msg-e2e"}
        resp = await self._raw_request(
            "POST", f"/accounts/{account_id}/email/sending/send", json=payload
        )
        if resp.status_code >= 400:
            raise CloudflareError(
                f"Cloudflare 发件失败 (HTTP {resp.status_code}): {resp.text}"
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

    async def probe_email_sending_write(self, account_id: str) -> dict[str, str]:
        """无副作用探测 Email Sending 写权限，不发送真实邮件。"""
        if settings.CF_FAKE_MODE:
            return {"status": "ok"}
        return await self._run_invalid_write_probe(
            "POST",
            f"/accounts/{account_id}/email/sending/send",
            payload={},
            capability_label="Email Sending 发件写",
            validation_markers=("from", "to", "subject", "email", "send"),
        )

    async def list_email_sending_subdomains(self, zone_id: str) -> list[dict[str, Any]]:
        """列出 Zone 下 Email Sending subdomains，用于无副作用权限探测。"""
        if settings.CF_FAKE_MODE:
            return [{"name": "e2e.example.com", "status": "ready"}]
        result = await self._request(
            "GET", f"/zones/{zone_id}/email/sending/subdomains"
        )
        return result if isinstance(result, list) else []

    # ---- Workers Scripts：部署与 Secret ----

    async def list_worker_scripts(self, account_id: str) -> list[dict[str, Any]]:
        """列出账号下 Worker 脚本，用于无副作用权限探测。"""
        if settings.CF_FAKE_MODE:
            return [{"id": "cf-email-manager-webhook", "script_name": "cf-email-manager-webhook"}]
        result = await self._request("GET", f"/accounts/{account_id}/workers/scripts")
        return result if isinstance(result, list) else []

    async def probe_worker_scripts_write(self, account_id: str) -> dict[str, Any]:
        """探测 Workers Scripts 写权限，不创建或覆盖 Worker。

        使用不存在的脚本名和合法 secret payload 调用写接口。具备写权限时，
        Cloudflare 会进入资源/参数校验并返回 4xx 校验类错误；缺少权限或
        Token 无效时返回认证/权限错误。未知、限流和服务端错误不会放行。
        """
        if settings.CF_FAKE_MODE:
            return {"status": "ok"}

        probe_script = (
            "cf-email-manager-permission-probe-" f"{secrets.token_hex(16)}"
        )
        return await self._run_invalid_write_probe(
            "PUT",
            f"/accounts/{account_id}/workers/scripts/{probe_script}/secrets",
            payload={
                "name": "CF_EMAIL_MANAGER_PERMISSION_PROBE",
                "text": "probe",
                "type": "secret_text",
            },
            capability_label="Cloudflare Workers Scripts 写",
            validation_markers=("secret", "name", "text", "type"),
            not_found_markers=_WORKERS_SCRIPT_NOT_FOUND_MARKERS,
        )

    async def upload_worker_script(
        self,
        account_id: str,
        script_name: str,
        main_module_name: str,
        script_content: bytes,
        *,
        compatibility_date: str = "2025-01-01",
        compatibility_flags: list[str] | None = None,
        bindings: list[WorkerBinding] | None = None,
    ) -> WorkerUploadResult:
        """上传并部署 Worker 脚本（PUT /accounts/{aid}/workers/scripts/{name}）。

        使用 multipart/form-data：metadata part（JSON）+ 主模块文件 part。
        主模块必须作为文件（带 filename）上传，否则 CF 返回 10021。
        """
        if settings.CF_FAKE_MODE:
            return WorkerUploadResult(id="worker-fake", script_name=script_name)

        metadata: dict[str, object] = {
            "main_module": main_module_name,
            "compatibility_date": compatibility_date,
        }
        if compatibility_flags:
            metadata["compatibility_flags"] = compatibility_flags
        if bindings:
            metadata["bindings"] = bindings

        files = {
            "metadata": (
                None,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                "application/json",
            ),
            main_module_name: (
                main_module_name,
                script_content,
                "application/javascript+module",
            ),
        }

        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_DEFAULT_TIMEOUT,
            transport=self._transport,
        ) as client:
            path = f"/accounts/{account_id}/workers/scripts/{script_name}"
            try:
                resp = await client.put(
                    path,
                    files=files,
                    headers={
                        "Authorization": f"Bearer {self._api_token}",
                    },
                )
            except httpx.HTTPError as exc:
                raise CloudflareError(
                    f"调用 Cloudflare API 失败 ({_request_target('PUT', path)}): {exc}",
                    cf_method="PUT",
                    cf_path=path,
                ) from exc

        if resp.status_code >= 400:
            raise CloudflareError(
                "Cloudflare Worker 部署失败 "
                f"(HTTP {resp.status_code}; {_request_target('PUT', path)}): {resp.text}",
                cf_method="PUT",
                cf_path=path,
                cf_status_code=resp.status_code,
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("success") is False:
            errors = body.get("errors")
            raise CloudflareError(
                f"Cloudflare Worker 部署失败 "
                f"(HTTP {resp.status_code}; {_request_target('PUT', path)}): "
                f"{errors or body}",
                cf_method="PUT",
                cf_path=path,
                cf_status_code=resp.status_code,
                cf_errors=errors or body,
            )
        # 标准 CF 信封返回 result 字段；非标准则原样转换为 WorkerUploadResult
        if isinstance(body, dict) and "result" in body:
            raw = body["result"] if isinstance(body["result"], dict) else {}
        elif isinstance(body, dict):
            raw = body
        else:
            raw = {}
        out: WorkerUploadResult = WorkerUploadResult(
            id=str(raw.get("id", "")),
            script_name=str(raw.get("script_name", script_name)),
        )
        for key in ("etag", "modified_on", "created_on"):
            value = raw.get(key)
            if value is not None:
                out[key] = str(value)  # type: ignore[literal-required]  # noqa: PERF102
        return out

    async def set_worker_secret(
        self,
        account_id: str,
        script_name: str,
        secret_name: str,
        secret_value: str,
    ) -> WorkerSecretResult:
        """为 Worker 设置/更新 secret（PUT .../workers/scripts/{name}/secrets）。

        body: ``{"name": secret_name, "text": secret_value, "type": "secret_text"}``。
        secret 在 CF 端加密存储，dashboard 不可见。
        """
        if settings.CF_FAKE_MODE:
            return WorkerSecretResult(name=secret_name)
        payload = {
            "name": secret_name,
            "text": secret_value,
            "type": "secret_text",
        }
        result = await self._request(
            "PUT",
            f"/accounts/{account_id}/workers/scripts/{script_name}/secrets",
            json=payload,
        )
        if not isinstance(result, dict):
            return WorkerSecretResult(name=secret_name)
        return WorkerSecretResult(
            name=str(result.get("name", secret_name)),
        )

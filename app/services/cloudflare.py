"""Cloudflare API 封装（httpx AsyncClient）。

统一封装 Cloudflare client/v4 接口：Token 校验、Zone 查询、
Email Routing（转发规则 / 目标地址）、Email Sending（Beta）。

所有方法均为异步，使用 Bearer Token 认证。返回值为 CF 响应中的
`result` 字段（列表或对象）。CF 返回 success=false 或 HTTP 非 2xx
时抛出 CloudflareError。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.exceptions import CloudflareError

# CF API 默认超时（秒）
_DEFAULT_TIMEOUT = 15.0


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
                raise CloudflareError(f"调用 Cloudflare API 失败: {exc}") from exc

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
                f"Cloudflare API 返回非 JSON 响应 (HTTP {resp.status_code})"
            ) from exc

        if not isinstance(body, dict) or not body.get("success", False):
            errors = body.get("errors") if isinstance(body, dict) else None
            raise CloudflareError(f"Cloudflare API 返回失败: {errors or body}")
        return body.get("result")

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

    # ---- Email Routing：目标地址 ----

    async def list_destination_addresses(
        self, account_id: str
    ) -> list[dict[str, Any]]:
        """列出账号下的转发目标地址。"""
        result = await self._request(
            "GET", f"/accounts/{account_id}/email/routing/addresses"
        )
        return result if isinstance(result, list) else []

    async def create_destination_address(
        self, account_id: str, email: str
    ) -> dict[str, Any]:
        """创建一个转发目标地址（需被验证后才可用）。"""
        result = await self._request(
            "POST",
            f"/accounts/{account_id}/email/routing/addresses",
            json={"email": email},
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
            "POST", f"/accounts/{account_id}/email/send", json=payload
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

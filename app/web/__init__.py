"""Web 表现层：聚合所有页面路由为 web_router（不纳入 OpenAPI）。

该层与 ``/api/v1`` JSON API 平行，直接复用 ``app/services/*`` 业务逻辑，
通过服务端渲染（Jinja2）+ 表单 POST + Cookie 会话向浏览器用户提供界面。
"""

from fastapi import APIRouter

from app.web import (
    admin,
    api_keys,
    auth,
    cf_accounts,
    dashboard,
    destination_addresses,
    domains,
    email_addresses,
    forwarding_rules,
    inbound,
    outbound,
    profile,
    public_mail,
    seo,
)

# 聚合路由，由 main.py 挂载到根路径 "/"
web_router = APIRouter(include_in_schema=False)
web_router.include_router(dashboard.router)
web_router.include_router(auth.router)
web_router.include_router(cf_accounts.router)
web_router.include_router(domains.router)
web_router.include_router(email_addresses.router)
web_router.include_router(destination_addresses.router)
web_router.include_router(forwarding_rules.router)
web_router.include_router(inbound.router)
web_router.include_router(outbound.router)
web_router.include_router(api_keys.router)
web_router.include_router(profile.router)
web_router.include_router(admin.router)
web_router.include_router(public_mail.router)
web_router.include_router(seo.router)

__all__ = ["web_router"]

"""路由包：聚合所有子路由为统一的 api_router。"""

from fastapi import APIRouter

from app.routers import (
    admin,
    api_keys,
    auth,
    cf_accounts,
    destination_addresses,
    domains,
    email_addresses,
    forwarding_rules,
    inbound,
    outbound,
    users,
)

# 统一聚合路由，由 main.py 挂载到 /api/v1 前缀下
api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(cf_accounts.router)
api_router.include_router(domains.router)
api_router.include_router(email_addresses.router)
api_router.include_router(destination_addresses.router)
api_router.include_router(forwarding_rules.router)
api_router.include_router(inbound.router)
api_router.include_router(outbound.router)
api_router.include_router(api_keys.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]

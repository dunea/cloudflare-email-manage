"""首页与仪表盘路由：资源统计 + 最近收件。"""

from fastapi import APIRouter, Request, Response

from app.dependencies import SessionDep
from app.schemas.inbound_email import InboundEmailRead
from app.services import (
    cf_account_service,
    domain_service,
    email_service,
    forwarding_service,
    inbound_service,
)
from app.web.deps import CurrentWebUser, OptionalWebUser
from app.web.templating import render

router = APIRouter(tags=["前端-仪表盘"])


@router.get("/")
async def index(request: Request, user: OptionalWebUser) -> Response:
    """站点首页：展示营销落地页，登录态仅切换入口按钮。"""
    return render(request, "index.html", user=user, layout="landing")


@router.get("/dashboard")
async def dashboard(request: Request, user: CurrentWebUser, session: SessionDep) -> Response:
    """仪表盘：复用各 service 的列表统计（size=1 取 total）+ 最近 5 封收件。"""
    _, cf_total = await cf_account_service.list_cf_accounts(session, user, 1, 1)
    _, domain_total = await domain_service.list_domains_for_user(session, user, 1, 1)
    _, email_total = await email_service.list_email_addresses(session, user, 1, 1)
    _, rule_total = await forwarding_service.list_forwarding_rules(session, user, 1, 1)
    recent, inbound_total = await inbound_service.list_inbound_emails(session, user, 1, 5)

    stats = [
        {"label": "CF 账号", "value": cf_total, "href": "/cf-accounts", "icon": "☁️"},
        {"label": "域名", "value": domain_total, "href": "/domains", "icon": "🌐"},
        {
            "label": "邮箱地址",
            "value": email_total,
            "href": "/email-addresses",
            "icon": "📧",
        },
        {
            "label": "转发规则",
            "value": rule_total,
            "href": "/forwarding-rules",
            "icon": "↪️",
        },
    ]
    return render(
        request,
        "dashboard.html",
        user=user,
        active="dashboard",
        stats=stats,
        recent_inbound=[InboundEmailRead.model_validate(e) for e in recent],
        inbound_total=inbound_total,
    )

"""转发规则 路由：转发规则管理（调用 CF Email Routing）。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
"""

from fastapi import APIRouter, Query, status

from app.dependencies import CurrentUser, SessionDep
from app.schemas.common import ApiResponse, PageData
from app.schemas.forwarding_rule import (
    ForwardingRuleCreate,
    ForwardingRuleRead,
    ForwardingRuleUpdate,
)
from app.services import forwarding_service

router = APIRouter(prefix="/forwarding-rules", tags=["转发规则"])


@router.post(
    "",
    response_model=ApiResponse[ForwardingRuleRead],
    status_code=status.HTTP_201_CREATED,
    summary="创建转发规则",
)
async def create_forwarding_rule(
    data: ForwardingRuleCreate, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[ForwardingRuleRead]:
    """为邮箱地址创建转发规则，同步在 Cloudflare 创建路由规则。"""
    rule = await forwarding_service.create_forwarding_rule(
        session, current_user, data
    )
    return ApiResponse(data=ForwardingRuleRead.model_validate(rule))


@router.get(
    "",
    response_model=ApiResponse[PageData[ForwardingRuleRead]],
    summary="转发规则列表",
)
async def list_forwarding_rules(
    current_user: CurrentUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    email_address_id: int | None = Query(default=None, ge=1),
) -> ApiResponse[PageData[ForwardingRuleRead]]:
    """分页查询当前用户的转发规则，可按源邮箱地址过滤。"""
    rules, total = await forwarding_service.list_forwarding_rules(
        session, current_user, page, size, email_address_id
    )
    page_data = PageData[ForwardingRuleRead](
        total=total,
        page=page,
        size=size,
        items=[ForwardingRuleRead.model_validate(r) for r in rules],
    )
    return ApiResponse(data=page_data)


@router.get(
    "/{rule_id}",
    response_model=ApiResponse[ForwardingRuleRead],
    summary="获取转发规则",
)
async def get_forwarding_rule(
    rule_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[ForwardingRuleRead]:
    """获取指定转发规则详情。"""
    rule = await forwarding_service.get_forwarding_rule_or_404(
        session, rule_id, current_user
    )
    return ApiResponse(data=ForwardingRuleRead.model_validate(rule))


@router.patch(
    "/{rule_id}",
    response_model=ApiResponse[ForwardingRuleRead],
    summary="更新转发规则",
)
async def update_forwarding_rule(
    rule_id: int,
    data: ForwardingRuleUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> ApiResponse[ForwardingRuleRead]:
    """更新转发规则（启用/停用）。"""
    rule = await forwarding_service.get_forwarding_rule_or_404(
        session, rule_id, current_user
    )
    updated = await forwarding_service.update_forwarding_rule(session, rule, data)
    return ApiResponse(data=ForwardingRuleRead.model_validate(updated))


@router.delete(
    "/{rule_id}",
    response_model=ApiResponse[None],
    summary="删除转发规则",
)
async def delete_forwarding_rule(
    rule_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[None]:
    """删除转发规则，同步在 Cloudflare 删除路由规则。"""
    rule = await forwarding_service.get_forwarding_rule_or_404(
        session, rule_id, current_user
    )
    await forwarding_service.delete_forwarding_rule(session, rule)
    return ApiResponse(message="已删除")

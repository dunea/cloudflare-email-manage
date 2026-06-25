"""域名 路由：查询用户可见域名，域名所有者共享域名给他人。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
"""

from fastapi import APIRouter, Query, status

from app.dependencies import CurrentUser, SessionDep
from app.schemas.common import ApiResponse, PageData
from app.schemas.domain import (
    DomainAssignmentCreate,
    DomainAssignmentRead,
    DomainRead,
)
from app.services import domain_service

router = APIRouter(prefix="/domains", tags=["域名"])


@router.get(
    "",
    response_model=ApiResponse[PageData[DomainRead]],
    summary="域名列表",
)
async def list_domains(
    current_user: CurrentUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[PageData[DomainRead]]:
    """分页查询当前用户可见的域名。"""
    domains, total = await domain_service.list_domains_for_user(
        session, current_user, page, size
    )
    page_data = PageData[DomainRead](
        total=total,
        page=page,
        size=size,
        items=[DomainRead.model_validate(d) for d in domains],
    )
    return ApiResponse(data=page_data)


@router.get(
    "/{domain_id}",
    response_model=ApiResponse[DomainRead],
    summary="获取域名",
)
async def get_domain(
    domain_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[DomainRead]:
    """获取指定域名详情。"""
    domain = await domain_service.get_domain_or_404(session, domain_id, current_user)
    return ApiResponse(data=DomainRead.model_validate(domain))


@router.post(
    "/{domain_id}/assignments",
    response_model=ApiResponse[DomainAssignmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="共享域名（域名所有者）",
)
async def assign_domain(
    domain_id: int,
    data: DomainAssignmentCreate,
    current_user: CurrentUser,
    session: SessionDep,
) -> ApiResponse[DomainAssignmentRead]:
    """将域名共享给指定用户，仅域名所有者可操作。"""
    assignment = await domain_service.assign_domain(
        session, domain_id, data.user_id, current_user
    )
    return ApiResponse(data=DomainAssignmentRead.model_validate(assignment))


@router.get(
    "/{domain_id}/assignments",
    response_model=ApiResponse[list[DomainAssignmentRead]],
    summary="域名共享记录",
)
async def list_assignments(
    domain_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[list[DomainAssignmentRead]]:
    """列出某域名的全部共享记录。"""
    await domain_service.get_domain_or_404(session, domain_id, current_user)
    assignments = await domain_service.list_domain_assignments(session, domain_id)
    return ApiResponse(
        data=[DomainAssignmentRead.model_validate(a) for a in assignments]
    )


@router.delete(
    "/{domain_id}/assignments/{user_id}",
    response_model=ApiResponse[None],
    summary="取消域名共享",
)
async def unassign_domain(
    domain_id: int, user_id: int, current_user: CurrentUser, session: SessionDep
) -> ApiResponse[None]:
    """取消某域名对某用户的共享，仅域名所有者可操作。"""
    await domain_service.unassign_domain(session, domain_id, user_id, current_user)
    return ApiResponse(message="已取消共享")

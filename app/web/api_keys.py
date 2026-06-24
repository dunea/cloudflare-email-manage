"""API Key 页面路由：列表、创建（一次性展示原始 key）、改名、启停、撤销。

复用 app/services/api_key_service。原始 key 仅在创建后通过会话一次性回显。
"""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.schemas.api_key import APIKeyCreate, APIKeyRead, APIKeyUpdate
from app.services import api_key_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render

router = APIRouter(tags=["前端-APIKey"])

# 新建 API Key 原始串在会话中的一次性存储键
_NEW_KEY_SESSION = "_new_api_key"


@router.get("/api-keys")
async def list_api_keys(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> Response:
    """API Key 列表；若刚创建则一次性展示原始 key。"""
    keys, total = await api_key_service.list_api_keys(session, user, page, size)
    new_key = request.session.pop(_NEW_KEY_SESSION, None)
    return render(
        request,
        "api_keys/list.html",
        user=user,
        active="api_keys",
        keys=[APIKeyRead.model_validate(k) for k in keys],
        new_key=new_key,
        page=page,
        size=size,
        total=total,
    )


@router.post("/api-keys")
async def create_api_key(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    name: Annotated[str, Form()],
) -> Response:
    """创建 API Key，原始 key 暂存会话供下次列表页一次性展示。"""
    try:
        data = APIKeyCreate(name=name)
        _, raw_key = await api_key_service.create_api_key(session, user, data)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse("/api-keys", status_code=303)
    request.session[_NEW_KEY_SESSION] = raw_key
    flash(request, "已创建 API Key，请立即复制保存（仅显示一次）", "success")
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/{key_id:int}/rename")
async def rename_api_key(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    key_id: int,
    name: Annotated[str, Form()],
) -> Response:
    """重命名 API Key。"""
    try:
        key = await api_key_service.get_api_key_or_404(session, key_id, user)
        await api_key_service.update_api_key(session, key, APIKeyUpdate(name=name))
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse("/api-keys", status_code=303)
    flash(request, "已重命名 API Key", "success")
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/{key_id:int}/toggle")
async def toggle_api_key(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    key_id: int,
) -> Response:
    """启用/停用 API Key。"""
    try:
        key = await api_key_service.get_api_key_or_404(session, key_id, user)
    except NotFoundError:
        flash(request, "API Key 不存在", "error")
        return RedirectResponse("/api-keys", status_code=303)
    await api_key_service.update_api_key(
        session, key, APIKeyUpdate(is_active=not key.is_active)
    )
    flash(request, "已更新 API Key 状态", "success")
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/{key_id:int}/delete")
async def delete_api_key(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    key_id: int,
) -> Response:
    """撤销（软删除）API Key。"""
    try:
        key = await api_key_service.get_api_key_or_404(session, key_id, user)
    except NotFoundError:
        flash(request, "API Key 不存在", "error")
        return RedirectResponse("/api-keys", status_code=303)
    await api_key_service.delete_api_key(session, key)
    flash(request, "已撤销 API Key", "success")
    return RedirectResponse("/api-keys", status_code=303)

"""个人资料页面路由：查看与修改邮箱、密码。"""

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.dependencies import SessionDep
from app.exceptions import AppException
from app.schemas.user import UserUpdate
from app.services import user_service
from app.web.deps import CurrentWebUser
from app.web.templating import error_message, flash, render

router = APIRouter(tags=["前端-个人资料"])


@router.get("/profile")
async def profile_page(request: Request, user: CurrentWebUser) -> Response:
    """个人资料页。"""
    return render(
        request, "profile/index.html", user=user, form={"email": user.email}
    )


@router.post("/profile")
async def update_profile(
    request: Request,
    user: CurrentWebUser,
    session: SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
) -> Response:
    """更新邮箱与密码（密码留空则不变）。"""
    try:
        data = UserUpdate(email=email, password=password or None)
        await user_service.update_user(session, user, data)
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return render(
            request,
            "profile/index.html",
            user=user,
            status_code=400,
            form={"email": email},
        )
    flash(request, "资料已更新", "success")
    return RedirectResponse("/profile", status_code=303)

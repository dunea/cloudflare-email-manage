"""认证 路由：注册、登录、刷新令牌。

路由层只做参数接收、调用 service 并返回统一响应。
"""

from fastapi import APIRouter, status

from app.dependencies import SessionDep
from app.schemas.common import ApiResponse
from app.schemas.user import Token, TokenRefresh, UserCreate, UserLogin, UserRead
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post(
    "/register",
    response_model=ApiResponse[UserRead],
    status_code=status.HTTP_201_CREATED,
    summary="用户注册",
)
async def register(data: UserCreate, session: SessionDep) -> ApiResponse[UserRead]:
    """注册新用户。"""
    user = await auth_service.register_user(session, data)
    return ApiResponse(data=UserRead.model_validate(user))


@router.post("/login", response_model=ApiResponse[Token], summary="用户登录")
async def login(data: UserLogin, session: SessionDep) -> ApiResponse[Token]:
    """校验账号密码并返回访问/刷新令牌。"""
    user = await auth_service.authenticate_user(session, data.username, data.password)
    return ApiResponse(data=auth_service.issue_tokens(user))


@router.post("/refresh", response_model=ApiResponse[Token], summary="刷新令牌")
async def refresh(data: TokenRefresh, session: SessionDep) -> ApiResponse[Token]:
    """使用刷新令牌换取新的令牌对。"""
    tokens = await auth_service.refresh_tokens(session, data.refresh_token)
    return ApiResponse(data=tokens)

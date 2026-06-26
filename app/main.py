"""FastAPI 应用入口：创建 app、注册路由与异常处理器。"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# 导入 models 确保所有表注册到 Base.metadata
import app.models  # noqa: F401
from app.config import settings
from app.database import async_session_maker
from app.exceptions import register_exception_handlers
from app.routers import api_router
from app.services.auth_service import ensure_admin_user
from app.web import web_router
from app.web.deps import WebRedirect, set_auth_cookies

# API 统一前缀
API_PREFIX = "/api/v1"

# 静态资源目录：app/static
_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期：确保管理员账号存在。

    建表由 Alembic 迁移负责（启动前先执行 ``alembic upgrade head``）。
    此处不再调用 ``create_all``，避免与迁移产生“表已存在”冲突。
    """
    settings.validate_for_startup()
    async with async_session_maker() as session:
        await ensure_admin_user(session)
    yield


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
        lifespan=lifespan,
    )

    # 会话中间件（flash 消息），复用 SECRET_KEY 签名
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SECRET_KEY,
        same_site="lax",
        https_only=settings.COOKIE_SECURE,
    )

    # 注册全局异常处理器（JSON API）
    register_exception_handlers(app)

    # Web 层重定向异常 → 302/303 跳转（如未登录跳登录页）
    @app.exception_handler(WebRedirect)
    async def _handle_web_redirect(
        _: Request, exc: WebRedirect
    ) -> RedirectResponse:
        return RedirectResponse(exc.location, status_code=303)

    # access 令牌续签后，将新 Cookie 写回响应（依赖内暂存于 request.state）
    @app.middleware("http")
    async def _persist_refreshed_tokens(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        new_tokens = getattr(request.state, "web_new_tokens", None)
        if new_tokens is not None:
            set_auth_cookies(response, new_tokens[0], new_tokens[1])
        return response

    # 挂载静态资源与前端页面路由
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(web_router)

    # 挂载聚合路由（JSON API）
    app.include_router(api_router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/health", tags=["健康检查"])
    async def health() -> dict[str, object]:
        """健康检查端点。"""
        return {"code": 0, "data": {"status": "ok"}, "message": "ok"}

    return app


app = create_app()

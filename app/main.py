"""FastAPI 应用入口：创建 app、注册路由与异常处理器。"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

# 导入 models 确保所有表注册到 Base.metadata
import app.models  # noqa: F401
from app.config import settings
from app.database import async_session_maker
from app.exceptions import register_exception_handlers
from app.routers import api_router
from app.services.auth_service import ensure_admin_user

# API 统一前缀
API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期：确保管理员账号存在。

    建表由 Alembic 迁移负责（启动前先执行 ``alembic upgrade head``）。
    此处不再调用 ``create_all``，避免与迁移产生“表已存在”冲突。
    """
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

    # 注册全局异常处理器
    register_exception_handlers(app)

    # 挂载聚合路由
    app.include_router(api_router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/health", tags=["健康检查"])
    async def health() -> dict[str, object]:
        """健康检查端点。"""
        return {"code": 0, "data": {"status": "ok"}, "message": "ok"}

    return app


app = create_app()

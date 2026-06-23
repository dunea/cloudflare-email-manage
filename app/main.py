"""FastAPI 应用入口：创建 app、注册路由与异常处理器。"""

from fastapi import FastAPI

from app.config import settings
from app.exceptions import register_exception_handlers
from app.routers import api_router

# API 统一前缀
API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
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

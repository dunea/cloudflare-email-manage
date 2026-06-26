"""自定义异常与全局异常处理器。"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppException(Exception):
    """应用级业务异常基类。

    code 为业务错误码（非 0），http_status 为返回的 HTTP 状态码。
    """

    def __init__(
        self,
        message: str,
        code: int = 1,
        http_status: int = status.HTTP_400_BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


class NotFoundError(AppException):
    """资源不存在。"""

    def __init__(self, message: str = "资源不存在", code: int = 1404) -> None:
        super().__init__(message, code=code, http_status=status.HTTP_404_NOT_FOUND)


class AuthError(AppException):
    """认证失败。"""

    def __init__(self, message: str = "认证失败", code: int = 1401) -> None:
        super().__init__(message, code=code, http_status=status.HTTP_401_UNAUTHORIZED)


class PermissionError(AppException):
    """权限不足。"""

    def __init__(self, message: str = "权限不足", code: int = 1403) -> None:
        super().__init__(message, code=code, http_status=status.HTTP_403_FORBIDDEN)


class CloudflareError(AppException):
    """调用 Cloudflare API 失败。"""

    def __init__(self, message: str = "Cloudflare API 调用失败", code: int = 1502) -> None:
        super().__init__(message, code=code, http_status=status.HTTP_502_BAD_GATEWAY)


def _error_response(code: int, message: str, http_status: int) -> JSONResponse:
    """构造统一错误响应体。"""
    return JSONResponse(
        status_code=http_status,
        content={"code": code, "data": None, "message": message},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """在 FastAPI 应用上注册全局异常处理器。"""

    @app.exception_handler(AppException)
    async def _handle_app_exception(_: Request, exc: AppException) -> JSONResponse:
        return _error_response(exc.code, exc.message, exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            code=1422,
            message=f"请求参数校验失败: {exc.errors()}",
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "未处理的服务器异常",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return _error_response(
            code=1500,
            message="服务器内部错误",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

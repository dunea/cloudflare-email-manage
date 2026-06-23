"""API Key 路由。

路由层只做参数接收、权限校验、调用 service 并返回统一响应。
具体接口将在后续阶段实现。
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api-keys", tags=["API Key"])

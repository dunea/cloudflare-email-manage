"""FastAPI 依赖：数据库会话、当前用户、权限校验等。

说明：认证逻辑将在后续阶段补全，此处先提供数据库会话依赖与类型别名，
作为依赖注入的统一入口。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session

# 数据库会话依赖别名
SessionDep = Annotated[AsyncSession, Depends(get_session)]

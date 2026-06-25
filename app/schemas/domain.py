"""域名 相关 Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DomainRead(BaseModel):
    """域名响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    cf_account_id: int
    zone_id: str
    domain_name: str
    status: str
    created_at: datetime


class DomainSyncResult(BaseModel):
    """域名同步结果。"""

    synced: int = 0
    domains: list[DomainRead] = Field(default_factory=list)


class DomainAssignmentCreate(BaseModel):
    """域名共享请求体。"""

    user_id: int = Field(gt=0)


class DomainAssignmentRead(BaseModel):
    """域名分配记录响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    domain_id: int
    user_id: int
    created_at: datetime

"""CF 账号 相关 Pydantic 请求/响应模型。

注意：响应模型绝不包含 API Token（明文或密文）。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CFPermissionCheckItem(BaseModel):
    """单项 Cloudflare Token 能力检查结果。"""

    key: str
    label: str
    status: Literal["passed", "failed"]
    required_permission: str
    message: str
    fix_hint: str


class CFPermissionReport(BaseModel):
    """Cloudflare Token 权限预检报告。"""

    overall_status: Literal["passed", "failed"]
    checked_at: datetime
    account_id: str | None = None
    zone_count: int = 0
    items: list[CFPermissionCheckItem] = Field(default_factory=list)


class CFAccountTokenCheckRequest(BaseModel):
    """绑定前 Token 权限预检请求体，不落库。"""

    api_token: str = Field(min_length=1, description="CF API Token，仅用于检查，不回显")
    account_id: str | None = Field(default=None, max_length=64)


class CFAccountCreate(BaseModel):
    """绑定 CF 账号请求体。"""

    name: str = Field(min_length=1, max_length=128)
    api_token: str = Field(min_length=1, description="CF API Token，仅用于绑定，不回显")
    # 可选：留空时绑定后自动从 CF API 获取
    account_id: str | None = Field(default=None, max_length=64)


class CFAccountUpdate(BaseModel):
    """更新 CF 账号请求体，字段均可选。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    api_token: str | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class CFAccountRead(BaseModel):
    """CF 账号响应体（不含 Token）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    account_id: str
    is_active: bool
    created_at: datetime
    capability_report: CFPermissionReport | None = None
    capability_checked_at: datetime | None = None


class DeployedDomain(BaseModel):
    """一键部署返回：已部署的域名信息。"""

    model_config = ConfigDict(from_attributes=True)

    domain_id: int
    domain_name: str
    zone_id: str


class WorkerDeployResult(BaseModel):
    """一键部署 Worker 结果。"""

    model_config = ConfigDict(from_attributes=True)

    worker_name: str
    webhook_url: str
    domains: list[DeployedDomain] = Field(default_factory=list)

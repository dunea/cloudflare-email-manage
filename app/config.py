"""应用配置：使用 pydantic-settings 从 .env 读取环境变量。"""

from functools import lru_cache
from ipaddress import ip_address
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置项，对应 .env.example 中的变量。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # 应用安全
    SECRET_KEY: str = "your-secret-key-at-least-32-chars"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    ENVIRONMENT: str = "development"

    # 平台自身对外可访问的基础 URL（用于 Worker 回传 webhook）
    # 本地开发：http://localhost:8000
    # 生产：https://your-domain.com
    APP_BASE_URL: str = "http://localhost:8000"

    # 数据库
    DATABASE_URL: str = "sqlite+aiosqlite:///./cf_email.db"

    # Cloudflare
    CF_API_BASE_URL: str = "https://api.cloudflare.com/client/v4"
    CF_WEBHOOK_SECRET: str = "your-webhook-secret-here"

    # 平台管理员（首次启动自动创建）
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "change-me-on-first-run"

    # 应用元信息
    APP_NAME: str = "CF Email Manager"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # 前端会话 Cookie 是否仅限 HTTPS（生产环境置 true）
    COOKIE_SECURE: bool = False
    # Web 表单 CSRF 防护；生产环境必须开启
    CSRF_PROTECTION: bool = True

    # 仅供 e2e 测试：置 true 时 CloudflareClient 返回内置假数据，不发真实请求
    CF_FAKE_MODE: bool = False

    @property
    def is_production(self) -> bool:
        """是否生产环境。"""
        return self.ENVIRONMENT.lower() in {"prod", "production"}

    def validate_for_startup(self) -> None:
        """生产启动前校验高风险配置，失败则拒绝启动。"""
        if not self.is_production:
            return

        errors: list[str] = []
        if self.SECRET_KEY == "your-secret-key-at-least-32-chars":
            errors.append("SECRET_KEY 仍为默认值")
        if len(self.SECRET_KEY) < 32:
            errors.append("SECRET_KEY 长度必须至少 32 个字符")
        if self.CF_WEBHOOK_SECRET == "your-webhook-secret-here":
            errors.append("CF_WEBHOOK_SECRET 仍为默认值")
        if self.ADMIN_PASSWORD == "change-me-on-first-run":
            errors.append("ADMIN_PASSWORD 仍为默认值")
        if self.DEBUG:
            errors.append("生产环境必须关闭 DEBUG")
        if self.CF_FAKE_MODE:
            errors.append("生产环境不得启用 CF_FAKE_MODE")
        if not self.COOKIE_SECURE:
            errors.append("生产环境必须启用 COOKIE_SECURE")
        if not self.CSRF_PROTECTION:
            errors.append("生产环境必须启用 CSRF_PROTECTION")
        if not _is_public_https_url(self.APP_BASE_URL):
            errors.append("APP_BASE_URL 必须是公网可达的 HTTPS URL")

        if errors:
            raise RuntimeError("生产配置校验失败: " + "；".join(errors))


def _is_public_https_url(value: str) -> bool:
    """校验 URL 是否为公网 HTTPS 地址。"""
    parsed = urlparse(value.strip())
    host = parsed.hostname
    if host is None or parsed.scheme != "https" or host.lower() == "localhost":
        return False
    try:
        ip = ip_address(host)
    except ValueError:
        return True
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的配置实例（带缓存）。"""
    return Settings()


settings: Settings = get_settings()

"""应用配置：使用 pydantic-settings 从 .env 读取环境变量。"""

from functools import lru_cache

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

    # 仅供 e2e 测试：置 true 时 CloudflareClient 返回内置假数据，不发真实请求
    CF_FAKE_MODE: bool = False


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的配置实例（带缓存）。"""
    return Settings()


settings: Settings = get_settings()

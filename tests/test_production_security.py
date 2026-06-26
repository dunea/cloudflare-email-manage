"""生产安全配置与 Web CSRF 防护测试。"""

import pytest
from httpx import AsyncClient

from app.config import Settings, settings


def test_production_defaults_are_rejected() -> None:
    """生产环境拒绝默认密钥、默认密码和不安全 URL。"""
    cfg = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="your-secret-key-at-least-32-chars",
        CF_WEBHOOK_SECRET="your-webhook-secret-here",
        ADMIN_PASSWORD="change-me-on-first-run",
        APP_BASE_URL="http://localhost:8000",
        COOKIE_SECURE=False,
    )
    with pytest.raises(RuntimeError) as exc_info:
        cfg.validate_for_startup()

    message = str(exc_info.value)
    assert "SECRET_KEY" in message
    assert "CF_WEBHOOK_SECRET" in message
    assert "APP_BASE_URL" in message


def test_valid_production_config_passes() -> None:
    """生产环境安全配置完整时允许启动。"""
    cfg = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="s" * 32,
        CF_WEBHOOK_SECRET="w" * 32,
        ADMIN_PASSWORD="change-me-securely",
        APP_BASE_URL="https://example.com",
        COOKIE_SECURE=True,
        CSRF_PROTECTION=True,
        DEBUG=False,
        CF_FAKE_MODE=False,
    )

    cfg.validate_for_startup()


async def test_production_web_post_requires_csrf(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生产环境 Web 表单 POST 缺少 CSRF token 时返回 403。"""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "CSRF_PROTECTION", True)

    resp = await client.post(
        "/login",
        data={"username": "alice", "password": "password123"},
        follow_redirects=False,
    )

    assert resp.status_code == 403
    assert resp.json()["message"] == "表单已过期，请刷新页面后重试"

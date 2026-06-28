"""限流辅助函数测试。"""

from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from app.config import settings
from app.services.rate_limit import client_ip


class _Request:
    """用于测试 client_ip 的最小 Request 替身。"""

    def __init__(self, host: str, headers: dict[str, str] | None = None) -> None:
        self.client = SimpleNamespace(host=host)
        self.headers = Headers(headers or {})


def test_client_ip_ignores_forwarded_for_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认不信任客户端伪造的 X-Forwarded-For。"""
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "")
    request = _Request("198.51.100.10", {"X-Forwarded-For": "203.0.113.8"})

    assert client_ip(request) == "198.51.100.10"


def test_client_ip_uses_forwarded_for_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可信代理转发的 X-Forwarded-For 会解析第一个合法 IP。"""
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.0/8")
    request = _Request(
        "10.1.2.3",
        {"X-Forwarded-For": "not-an-ip, 203.0.113.8, 198.51.100.9"},
    )

    assert client_ip(request) == "203.0.113.8"


def test_client_ip_ignores_proxy_headers_from_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开启代理头后，非可信直连地址仍不能伪造客户端 IP。"""
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.0/8")
    request = _Request("198.51.100.10", {"X-Forwarded-For": "203.0.113.8"})

    assert client_ip(request) == "198.51.100.10"


def test_client_ip_supports_trusted_proxy_cidr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可信代理配置支持 CIDR。"""
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "192.0.2.0/24")
    request = _Request("192.0.2.42", {"X-Real-IP": "203.0.113.8"})

    assert client_ip(request) == "203.0.113.8"

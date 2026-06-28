"""轻量内存限流工具。

当前项目默认面向单实例自托管，使用进程内固定窗口计数即可覆盖登录爆破、
公开链接扫描和 API Key 滥用的基础防护。多实例部署时可替换为 Redis。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from ipaddress import ip_address, ip_network

from fastapi import Request, status

from app.config import settings
from app.exceptions import AppException


@dataclass
class _Bucket:
    """单个限流桶状态。"""

    count: int
    reset_at: float


_buckets: dict[tuple[str, str], _Bucket] = {}


def _now() -> float:
    """返回当前单调时钟秒数。"""
    return time.monotonic()


def reset_rate_limits() -> None:
    """清空限流状态（测试隔离使用）。"""
    _buckets.clear()


def client_ip(request: Request) -> str:
    """提取客户端 IP；默认不信任客户端可伪造的代理头。"""
    if request.client is None:
        return "unknown"
    direct_ip = request.client.host
    if not _should_trust_proxy_headers(direct_ip):
        return direct_ip

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parsed = _first_valid_ip(forwarded.split(","))
        if parsed is not None:
            return parsed

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        parsed = _first_valid_ip([real_ip])
        if parsed is not None:
            return parsed

    return direct_ip


def _should_trust_proxy_headers(direct_ip: str) -> bool:
    """仅在显式开启且直连地址命中可信代理时信任代理头。"""
    if not settings.TRUST_PROXY_HEADERS:
        return False
    try:
        client_addr = ip_address(direct_ip)
    except ValueError:
        return False

    for item in settings.TRUSTED_PROXY_IPS.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            if client_addr in ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def _first_valid_ip(values: list[str]) -> str | None:
    """返回列表中第一个合法 IP 字符串。"""
    for value in values:
        try:
            return str(ip_address(value.strip()))
        except ValueError:
            continue
    return None


def hit(bucket: str, key: str, limit: int, window_seconds: int) -> None:
    """记录一次访问，超过限制时抛出 429。"""
    if limit <= 0 or window_seconds <= 0:
        return

    now = _now()
    storage_key = (bucket, key)
    state = _buckets.get(storage_key)
    if state is None or now >= state.reset_at:
        _buckets[storage_key] = _Bucket(count=1, reset_at=now + window_seconds)
        return

    state.count += 1
    if state.count > limit:
        raise AppException(
            "请求过于频繁，请稍后重试",
            code=1429,
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
        )


def reset(bucket: str, key: str) -> None:
    """清除指定限流桶。"""
    _buckets.pop((bucket, key), None)

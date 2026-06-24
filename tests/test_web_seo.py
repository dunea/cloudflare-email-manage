"""SEO 路由测试：robots.txt 与 sitemap.xml。

复用 conftest 的 client（内存数据库 + ASGITransport）。
"""

from httpx import AsyncClient


async def test_robots_txt(client: AsyncClient) -> None:
    """robots.txt 允许公开页、屏蔽应用路径并指向 sitemap。"""
    resp = await client.get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "User-agent: *" in body
    assert "Disallow: /dashboard" in body
    assert "Disallow: /api-keys" in body
    assert "sitemap.xml" in body


async def test_sitemap_xml(client: AsyncClient) -> None:
    """sitemap.xml 列出公开可索引页面。"""
    resp = await client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"]
    body = resp.text
    assert "<urlset" in body
    assert "<loc>" in body
    assert "/login" in body
